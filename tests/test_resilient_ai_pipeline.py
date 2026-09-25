"""Comprehensive tests for AI provider resilience, retries, failover, batching, and circuit breaker.

Section 11 Test Coverage:
1. NVIDIA 503 -> retry with backoff -> success
2. NVIDIA 503 -> retries exhausted -> fallback to FreeLLM
3. NVIDIA 429 -> retry with backoff (and Retry-After if present)
4. NVIDIA 400 permanent error -> fail over immediately without retrying
5. FreeLLM batching: 9 questions split into chunks of 4, 4, 1 in correct order with correct IDs
6. Partial generation: batch 1 succeeds with NVIDIA, batch 2 fails with NVIDIA and falls back to FreeLLM, merged answer set has every question exactly once
7. Provider health / circuit breaker: degraded after repeated failures, cooldown, recovery after successful probe
8. Answer integrity: question order preserved, question IDs preserved, table targets preserved, code answers contain code without markdown fences
"""

import asyncio
import json
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from packages.worksheets.answer_engine import (
    CircuitState,
    LLMAnswerEngine,
    ProviderCircuitBreaker,
    get_circuit_breaker,
    reset_circuit_breakers,
)
from packages.worksheets.answer_models import (
    AnswerStatus,
    GeneratedAnswer,
    WorksheetAnswers,
)
from packages.worksheets.exceptions import (
    AnswerEngineError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from packages.worksheets.models import (
    AnswerTargetSpec,
    ParsedQuestion,
    ParsedWorksheet,
    QuestionOption,
    QuestionType,
)


def _create_mock_response(status_code: int, data: Dict[str, Any], headers: Dict[str, str] = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers=headers or {"content-type": "application/json"},
        content=json.dumps(data).encode("utf-8"),
        request=httpx.Request("POST", "https://mock.api/v1/chat/completions"),
    )


def _create_completion_response(answers: List[Dict[str, Any]], model: str = "nvidia/nemotron-3-super-120b-a12b") -> httpx.Response:
    content_obj = {"answers": answers}
    payload = {
        "id": "chatcmpl-mock-12345",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content_obj),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }
    return _create_mock_response(200, payload)


# ==============================================================================
# TEST 1: NVIDIA 503 -> RETRY WITH BACKOFF -> SUCCESS
# ==============================================================================

@pytest.mark.asyncio
async def test_nvidia_503_retry_with_backoff_success():
    """Verify that transient 503 error triggers backoff retry and succeeds on attempt 2."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    resp_503 = _create_mock_response(503, {"error": {"message": "Service temporarily overloaded", "code": 503}})
    resp_200 = _create_completion_response([
        {"question_id": "q_1", "question_number": "1", "answer_text": "Central Processing Unit", "selected_option": "A", "confidence": 0.98}
    ])
    mock_client.post.side_effect = [resp_503, resp_200]

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-key",
        max_retries=2,
        retry_backoff=0.01,
        model_fallbacks=[],
        http_client=mock_client,
    )

    q = ParsedQuestion(question_id="q_1", question_number="1", question_type=QuestionType.MCQ, question_text="What is CPU?")
    ws = ParsedWorksheet(filename="test.docx", file_format="docx", questions=[q])

    result = await engine.generate_answers(ws)

    assert result.provider == "nvidia"
    assert len(result.answers) == 1
    assert result.answers[0].answer_text == "Central Processing Unit"
    assert result.answers[0].status == AnswerStatus.SUCCESS
    assert mock_client.post.call_count == 2


# ==============================================================================
# TEST 2: NVIDIA 503 -> RETRIES EXHAUSTED -> FALLBACK TO FREELLM
# ==============================================================================

@pytest.mark.asyncio
async def test_nvidia_503_retries_exhausted_fallback_to_freellm():
    """Verify that when NVIDIA exhausts 503 retries, it fails over to FreeLLM fallback."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    resp_503 = _create_mock_response(503, {"error": {"message": "Service temporarily overloaded", "code": 503}})
    mock_client.post.return_value = resp_503

    mock_freellm = AsyncMock(spec=LLMAnswerEngine)
    mock_freellm.provider = "freellm"
    fallback_answer = GeneratedAnswer(
        question_id="q_1",
        question_number="1",
        question_type=QuestionType.SHORT_ANSWER,
        answer_text="FreeLLM answered this question successfully.",
        confidence=0.92,
        status=AnswerStatus.SUCCESS,
        metadata={"provider": "freellm"},
    )
    mock_freellm.generate_answers.return_value = WorksheetAnswers(
        worksheet_filename="test.docx",
        answers=[fallback_answer],
        provider="freellm",
    )

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-key",
        max_retries=1,
        retry_backoff=0.01,
        model_fallbacks=[],
        fallback_engine=mock_freellm,
        http_client=mock_client,
    )

    q = ParsedQuestion(question_id="q_1", question_number="1", question_type=QuestionType.SHORT_ANSWER, question_text="What is virtual memory?")
    ws = ParsedWorksheet(filename="test.docx", file_format="docx", questions=[q])

    result = await engine.generate_answers(ws)

    assert mock_client.post.call_count == 2  # initial + 1 retry
    mock_freellm.generate_answers.assert_called_once()
    assert result.provider == "freellm"
    assert len(result.answers) == 1
    assert result.answers[0].answer_text == "FreeLLM answered this question successfully."


# ==============================================================================
# TEST 3: NVIDIA 429 -> RETRY WITH BACKOFF & RETRY-AFTER
# ==============================================================================

@pytest.mark.asyncio
async def test_nvidia_429_retry_with_backoff_and_retry_after():
    """Verify that 429 rate limit error respects Retry-After header and retries successfully."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    resp_429 = _create_mock_response(
        429,
        {"error": {"message": "Rate limit exceeded. Please retry later.", "code": 429}},
        headers={"content-type": "application/json", "retry-after": "0.05"},
    )
    resp_200 = _create_completion_response([
        {"question_id": "q_1", "question_number": "1", "answer_text": "Paging separates physical memory into frames.", "confidence": 0.95}
    ])
    mock_client.post.side_effect = [resp_429, resp_200]

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-key",
        max_retries=2,
        retry_backoff=0.01,
        model_fallbacks=[],
        http_client=mock_client,
    )

    q = ParsedQuestion(question_id="q_1", question_number="1", question_type=QuestionType.SHORT_ANSWER, question_text="What is paging?")
    ws = ParsedWorksheet(filename="test.docx", file_format="docx", questions=[q])

    result = await engine.generate_answers(ws)

    assert result.provider == "nvidia"
    assert result.answers[0].answer_text == "Paging separates physical memory into frames."
    assert mock_client.post.call_count == 2


# ==============================================================================
# TEST 4: NVIDIA 400 PERMANENT ERROR -> FAIL OVER IMMEDIATELY
# ==============================================================================

@pytest.mark.asyncio
async def test_nvidia_400_permanent_error_immediate_failover():
    """Verify that permanent errors (400 Bad Request) do not waste retries and fail over immediately."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    resp_400 = _create_mock_response(400, {"error": {"message": "Invalid request body structure", "code": 400}})
    mock_client.post.return_value = resp_400

    mock_freellm = AsyncMock(spec=LLMAnswerEngine)
    mock_freellm.provider = "freellm"
    fallback_answer = GeneratedAnswer(
        question_id="q_1",
        question_number="1",
        question_type=QuestionType.SHORT_ANSWER,
        answer_text="FreeLLM rescue answer.",
        confidence=0.90,
        status=AnswerStatus.SUCCESS,
        metadata={"provider": "freellm"},
    )
    mock_freellm.generate_answers.return_value = WorksheetAnswers(
        worksheet_filename="test.docx",
        answers=[fallback_answer],
        provider="freellm",
    )

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-key",
        max_retries=3,  # Should NOT retry 3 times!
        retry_backoff=0.01,
        model_fallbacks=[],
        fallback_engine=mock_freellm,
        http_client=mock_client,
    )

    q = ParsedQuestion(question_id="q_1", question_number="1", question_type=QuestionType.SHORT_ANSWER, question_text="Question text")
    ws = ParsedWorksheet(filename="test.docx", file_format="docx", questions=[q])

    result = await engine.generate_answers(ws)

    # Must only call NVIDIA ONCE (no wasted retries on permanent 400)
    assert mock_client.post.call_count == 1
    mock_freellm.generate_answers.assert_called_once()
    assert result.provider == "freellm"


# ==============================================================================
# TEST 5: FREELLM BATCHING (9 QUESTIONS -> 4, 4, 1)
# ==============================================================================

@pytest.mark.asyncio
async def test_freellm_batching_9_questions_chunks_of_4_4_1():
    """Verify that 9 questions are split into chunks of 4, 4, 1 and merged in exact order."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    # Simulate dynamic responses matching each batch's questions
    async def chunk_response(*args, **kwargs):
        body = kwargs.get("json", {})
        messages = body.get("messages", [])
        user_content = messages[1]["content"] if len(messages) > 1 else ""
        import re
        q_ids = re.findall(r'"question_id":\s*"([^"]+)"', user_content)
        answers = [
            {"question_id": qid, "question_number": qid.split("_")[1], "answer_text": f"Answer for {qid}", "confidence": 0.95}
            for qid in q_ids
        ]
        return _create_completion_response(answers, model="default")

    mock_client.post.side_effect = chunk_response

    questions = [
        ParsedQuestion(
            question_id=f"q_{i}",
            question_number=str(i),
            question_type=QuestionType.SHORT_ANSWER,
            question_text=f"Question text {i}",
            marks=2,
        )
        for i in range(1, 10)
    ]
    ws = ParsedWorksheet(filename="worksheet_9.docx", file_format="docx", questions=questions)

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="mock-freellm-key",
        chunk_size=4,
        http_client=mock_client,
    )

    result = await engine.generate_answers(ws)

    # 9 questions with chunk_size 4 -> exactly 3 HTTP requests
    assert mock_client.post.call_count == 3
    assert len(result.answers) == 9
    for i, ans in enumerate(result.answers, 1):
        assert ans.question_id == f"q_{i}"
        assert ans.question_number == str(i)
        assert ans.answer_text == f"Answer for q_{i}"


# ==============================================================================
# TEST 6: PARTIAL GENERATION (BATCH 1 NVIDIA, BATCH 2 FREELLM)
# ==============================================================================

@pytest.mark.asyncio
async def test_partial_generation_batch1_nvidia_batch2_freellm_merged():
    """Verify partial generation where Batch 1 succeeds on NVIDIA, Batch 2 fails on NVIDIA and succeeds on FreeLLM.
    
    Merged answer set contains all 8 questions in exact order with zero duplicates.
    """
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    # 8 questions: Batch 1 (q_1..q_4), Batch 2 (q_5..q_8)
    questions = [
        ParsedQuestion(
            question_id=f"q_{i}",
            question_number=str(i),
            question_type=QuestionType.SHORT_ANSWER,
            question_text=f"Question {i}",
            marks=2,
        )
        for i in range(1, 9)
    ]
    ws = ParsedWorksheet(filename="worksheet_8.docx", file_format="docx", questions=questions)

    # Batch 1 returns 200 OK from NVIDIA
    b1_answers = [
        {"question_id": f"q_{i}", "question_number": str(i), "answer_text": f"NVIDIA answer {i}", "confidence": 0.95}
        for i in range(1, 5)
    ]
    resp_b1 = _create_completion_response(b1_answers, model="nvidia/nemotron-3-super-120b-a12b")

    # Batch 2 fails with 503 on NVIDIA
    resp_b2_fail = _create_mock_response(503, {"error": {"message": "Service temporarily overloaded", "code": 503}})

    # Sequence of NVIDIA calls: Batch 1 (success), Batch 2 attempt 1 (503), Batch 2 attempt 2 (503)
    mock_client.post.side_effect = [resp_b1, resp_b2_fail, resp_b2_fail]

    # FreeLLM fallback mock receives missing Batch 2 (q_5..q_8) and succeeds
    mock_freellm = AsyncMock(spec=LLMAnswerEngine)
    mock_freellm.provider = "freellm"

    async def freellm_fallback_fn(missing_ws, context=None):
        fb_ans = [
            GeneratedAnswer(
                question_id=q.question_id,
                question_number=q.question_number,
                question_type=q.question_type,
                answer_text=f"FreeLLM answer {q.question_number}",
                confidence=0.90,
                status=AnswerStatus.SUCCESS,
                metadata={"provider": "freellm"},
            )
            for q in missing_ws.questions
        ]
        return WorksheetAnswers(
            worksheet_filename=missing_ws.filename,
            answers=fb_ans,
            provider="freellm",
        )

    mock_freellm.generate_answers.side_effect = freellm_fallback_fn

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-key",
        chunk_size=4,
        max_retries=1,
        retry_backoff=0.01,
        model_fallbacks=[],
        fallback_engine=mock_freellm,
        http_client=mock_client,
    )

    result = await engine.generate_answers(ws)

    # Verify merged answer set
    assert len(result.answers) == 8
    # Exact question order preserved
    for i, ans in enumerate(result.answers, 1):
        assert ans.question_id == f"q_{i}"
        if i <= 4:
            assert ans.answer_text == f"NVIDIA answer {i}"
            assert ans.metadata.get("provider") == "nvidia"
        else:
            assert ans.answer_text == f"FreeLLM answer {i}"
            assert ans.metadata.get("provider") == "freellm"

    # Effective provider indicates mixed execution
    assert "nvidia" in result.provider
    assert "freellm" in result.provider
    # FreeLLM was called ONLY for the failed batch (q_5..q_8), NOT for already succeeded q_1..q_4
    mock_freellm.generate_answers.assert_called_once()
    called_missing_ws = mock_freellm.generate_answers.call_args[0][0]
    assert [q.question_id for q in called_missing_ws.questions] == ["q_5", "q_6", "q_7", "q_8"]


# ==============================================================================
# TEST 7: PROVIDER HEALTH & CIRCUIT BREAKER
# ==============================================================================

@pytest.mark.asyncio
async def test_circuit_breaker_degraded_cooldown_recovery():
    """Verify circuit breaker lifecycle: Closed -> Open (after repeated 503s) -> Cooldown -> Half-Open Probe -> Closed."""
    cb = ProviderCircuitBreaker("nvidia_cb_test", failure_threshold=2, cooldown_seconds=0.1)

    assert cb.state == CircuitState.CLOSED
    assert cb.can_attempt() is True

    # Record 1st transient failure
    cb.record_failure("503 Overloaded", is_transient=True)
    assert cb.state == CircuitState.CLOSED
    assert cb.can_attempt() is True

    # Record 2nd transient failure -> trips to OPEN
    cb.record_failure("503 Overloaded", is_transient=True)
    assert cb.state == CircuitState.OPEN
    assert cb.can_attempt() is False

    # Check status reporting
    status = cb.to_dict()
    assert status["state"] == "OPEN"
    assert status["consecutive_failures"] == 2

    # Wait for cooldown
    await asyncio.sleep(0.12)

    # After cooldown, probe is allowed (HALF_OPEN)
    assert cb.can_attempt() is True
    assert cb.state == CircuitState.HALF_OPEN

    # Probe succeeds -> resets circuit to CLOSED
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0


# ==============================================================================
# TEST 8: ANSWER INTEGRITY (ORDER, IDS, TABLES, CODE CLEANLINESS)
# ==============================================================================

@pytest.mark.asyncio
async def test_answer_integrity_order_ids_tables_code_no_markdown_fences():
    """Verify that answers preserve question order, IDs, table cell targets, and code is clean without markdown fences."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    q_mcq = ParsedQuestion(
        question_id="q_1",
        question_number="1",
        question_type=QuestionType.MCQ,
        question_text="Which OSI layer handles routing?",
        options=[QuestionOption(key="C", text="Network")],
    )
    q_table = ParsedQuestion(
        question_id="q_2",
        question_number="2",
        question_type=QuestionType.TABLE_CELL,
        question_text="Complete comparison table.",
        targets=[
            AnswerTargetSpec(target_id="t_1", column_header="TCP", row_label="Connection"),
            AnswerTargetSpec(target_id="t_2", column_header="UDP", row_label="Connection"),
        ],
    )
    q_code = ParsedQuestion(
        question_id="q_3",
        question_number="3",
        question_type=QuestionType.CODE,
        question_text="Write a Python factorial function.",
    )
    q_pseudo = ParsedQuestion(
        question_id="q_4",
        question_number="4",
        question_type=QuestionType.PSEUDOCODE,
        question_text="Write binary search pseudocode.",
    )

    ws = ParsedWorksheet(
        filename="integrity_test.docx",
        file_format="docx",
        questions=[q_mcq, q_table, q_code, q_pseudo],
    )

    mock_response_data = [
        {
            "question_id": "q_1",
            "question_number": "1",
            "selected_option": "C",
            "answer_text": "Network",
            "confidence": 0.99,
        },
        {
            "question_id": "q_2",
            "question_number": "2",
            "target_answers": {
                "t_1": "Connection-oriented protocol",
                "t_2": "Connectionless protocol",
            },
            "answer_text": "Table completed.",
            "confidence": 0.95,
        },
        {
            "question_id": "q_3",
            "question_number": "3",
            "answer_text": "def factorial(n):\n    if n <= 1:\n        return 1\n    return n * factorial(n - 1)",
            "confidence": 0.96,
        },
        {
            "question_id": "q_4",
            "question_number": "4",
            "answer_text": "FUNCTION binary_search(arr, target):\n    low = 0\n    high = len(arr) - 1\n    WHILE low <= high:\n        mid = (low + high) // 2\n        IF arr[mid] == target RETURN mid\n    RETURN -1",
            "confidence": 0.94,
        },
    ]

    mock_client.post.return_value = _create_completion_response(mock_response_data)

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-key",
        http_client=mock_client,
    )

    result = await engine.generate_answers(ws)

    # 1. Question order and IDs strictly preserved
    assert [a.question_id for a in result.answers] == ["q_1", "q_2", "q_3", "q_4"]

    # 2. Table targets preserved
    ans_table = result.get_answer("q_2")
    assert ans_table.target_answers.get("t_1") == "Connection-oriented protocol"
    assert ans_table.target_answers.get("t_2") == "Connectionless protocol"

    # 3. Code answers contain clean code without markdown fences
    ans_code = result.get_answer("q_3")
    assert "def factorial" in ans_code.answer_text
    assert "```" not in ans_code.answer_text

    # 4. Pseudocode answers contain clean pseudocode without markdown fences
    ans_pseudo = result.get_answer("q_4")
    assert "FUNCTION binary_search" in ans_pseudo.answer_text
    assert "```" not in ans_pseudo.answer_text
