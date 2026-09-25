"""Comprehensive test suite for NVIDIA Answer Engine as Primary Provider with FreeLLM Fallback.

Verifies:
1. NVIDIA success -> FreeLLM not called.
2. NVIDIA timeout -> FreeLLM fallback.
3. NVIDIA connection failure -> FreeLLM fallback.
4. Both providers fail -> clean AnswerEngineError.
5. Zero secret leakage in exceptions, logs, str, and repr.
6. Support for all question types: MCQ, ONE_WORD, SHORT_ANSWER, LONG_ANSWER, TABLE_CELL (targets), CODE, PSEUDOCODE, OUTPUT_TRACING.
7. Configurable provider order via settings (defaulting to NVIDIA -> FreeLLM).
8. Provider status endpoint /api/v1/provider/status.
9. Controlled real NVIDIA connectivity and generation test (when credentials present).
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from packages.shared.config import settings
from packages.worksheets.answer_engine import (
    AnswerEngineFactory,
    BaseAnswerEngine,
    LLMAnswerEngine,
    RuleBasedAnswerEngine,
    redact_api_keys,
)
from packages.worksheets.answer_models import (
    AnswerStatus,
    GeneratedAnswer,
    WorksheetAnswers,
)
from packages.worksheets.exceptions import (
    AnswerEngineError,
    LLMAuthenticationError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    MissingAnswerError,
)
from packages.worksheets.models import (
    AnswerTargetSpec,
    ParsedQuestion,
    ParsedWorksheet,
    QuestionOption,
    QuestionType,
)


def _create_sample_parsed_worksheet() -> ParsedWorksheet:
    """Create a sample in-memory ParsedWorksheet covering multiple question types."""
    q1 = ParsedQuestion(
        question_id="q_1",
        question_number="1",
        question_type=QuestionType.MCQ,
        question_text="What does CPU stand for?",
        options=[
            QuestionOption(key="A", text="Central Processing Unit"),
            QuestionOption(key="B", text="Computer Personal Unit"),
            QuestionOption(key="C", text="Core Processing Unit"),
        ],
        marks=1,
    )
    q2 = ParsedQuestion(
        question_id="q_2",
        question_number="2",
        question_type=QuestionType.ONE_WORD,
        question_text="Which data structure operates on LIFO principle?",
        marks=1,
    )
    q3 = ParsedQuestion(
        question_id="q_3",
        question_number="3",
        question_type=QuestionType.SHORT_ANSWER,
        question_text="Differentiate between Process and Thread in operating systems.",
        marks=2,
    )
    q4 = ParsedQuestion(
        question_id="q_4",
        question_number="4",
        question_type=QuestionType.LONG_ANSWER,
        question_text="Explain the Raft consensus algorithm and its state machine replication.",
        marks=5,
    )
    return ParsedWorksheet(
        filename="test_worksheet.docx",
        file_format="docx",
        questions=[q1, q2, q3, q4],
        course_code="21CSC303J",
        title="Operating Systems and Architecture",
        session=101,
        slo=1,
    )


def _create_comprehensive_worksheet() -> ParsedWorksheet:
    """Create worksheet covering MCQ, tables, code, pseudocode, and output/trace questions."""
    q_mcq = ParsedQuestion(
        question_id="q_mcq",
        question_number="1",
        question_type=QuestionType.MCQ,
        question_text="Which layer in OSI model manages routing?",
        options=[
            QuestionOption(key="A", text="Physical"),
            QuestionOption(key="B", text="Data Link"),
            QuestionOption(key="C", text="Network"),
            QuestionOption(key="D", text="Transport"),
        ],
        marks=1,
    )
    q_table = ParsedQuestion(
        question_id="q_table",
        question_number="2",
        question_type=QuestionType.TABLE_CELL,
        question_text="Complete the comparison table between TCP and UDP.",
        targets=[
            AnswerTargetSpec(target_id="t_1", column_header="TCP", row_label="Reliability"),
            AnswerTargetSpec(target_id="t_2", column_header="UDP", row_label="Reliability"),
        ],
        marks=2,
    )
    q_code = ParsedQuestion(
        question_id="q_code",
        question_number="3",
        question_type=QuestionType.CODE,
        question_text="Write a Python function to compute factorial recursively.",
        marks=3,
    )
    q_pseudo = ParsedQuestion(
        question_id="q_pseudo",
        question_number="4",
        question_type=QuestionType.PSEUDOCODE,
        question_text="Write pseudocode for binary search algorithm.",
        marks=3,
    )
    q_trace = ParsedQuestion(
        question_id="q_trace",
        question_number="5",
        question_type=QuestionType.OUTPUT_TRACING,
        question_text="Trace the output of loop: for i in range(3): print(i * 2)",
        marks=2,
    )
    return ParsedWorksheet(
        filename="comprehensive_test.docx",
        file_format="docx",
        questions=[q_mcq, q_table, q_code, q_pseudo, q_trace],
        course_code="21CSC201J",
        title="Data Structures and Algorithms",
        session=1,
        slo=1,
    )


def _create_nvidia_success_response(answers_payload: list) -> httpx.Response:
    """Helper creating a simulated OpenAI-compatible 200 OK JSON response from NVIDIA."""
    response_json = {
        "id": "chatcmpl-nv-test-12345",
        "object": "chat.completion",
        "created": 1715000000,
        "model": "nvidia/nemotron-3-super-120b-a12b",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"answers": answers_payload}),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 400,
            "total_tokens": 600,
        },
    }
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = response_json
    mock_resp.text = json.dumps(response_json)
    return mock_resp


# ==============================================================================
# TEST 1: NVIDIA SUCCESS -> FreeLLM NOT CALLED
# ==============================================================================

@pytest.mark.asyncio
async def test_nvidia_success_freellm_not_called():
    """Verify that when NVIDIA succeeds, fallback FreeLLM provider is never invoked."""
    sample_answers = [
        {"question_id": "q_1", "question_number": "1", "answer_text": "Central Processing Unit", "selected_option": "A", "confidence": 0.98},
        {"question_id": "q_2", "question_number": "2", "answer_text": "Stack", "confidence": 0.96},
        {"question_id": "q_3", "question_number": "3", "answer_text": "A process has isolated address space whereas threads share memory.", "confidence": 0.95},
        {"question_id": "q_4", "question_number": "4", "answer_text": "Raft achieves consensus via leader election and log replication.", "confidence": 0.92},
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_nvidia_success_response(sample_answers)

    mock_freellm = AsyncMock(spec=BaseAnswerEngine)
    mock_freellm.provider = "freellm"

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="nvapi-mock-test-key",
        base_url="https://integrate.api.nvidia.com/v1",
        model="nvidia/nemotron-3-super-120b-a12b",
        fallback_engine=mock_freellm,
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()
    result = await engine.generate_answers(ws)

    # 1. Verification of real request construction to NVIDIA
    mock_client.post.assert_called_once()
    call_args, call_kwargs = mock_client.post.call_args
    assert call_args[0] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert call_kwargs["headers"]["Authorization"] == "Bearer nvapi-mock-test-key"
    assert call_kwargs["json"]["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert call_kwargs["json"]["response_format"] == {"type": "json_object"}

    # 2. FreeLLM must NOT be called
    mock_freellm.generate_answers.assert_not_called()

    # 3. Verification of returned answers
    assert result.provider == "nvidia"
    assert result.total_count == 4
    assert result.success_count == 4
    assert result.average_confidence >= 0.90
    assert result.get_answer("q_1").selected_option == "A"


# ==============================================================================
# TEST 2: NVIDIA TIMEOUT -> FreeLLM FALLBACK
# ==============================================================================

@pytest.mark.asyncio
async def test_nvidia_timeout_freellm_fallback():
    """Verify that when NVIDIA times out across all retries, system automatically falls back to FreeLLM."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.side_effect = httpx.TimeoutException("NVIDIA API request timed out after 60.0s")

    ws = _create_sample_parsed_worksheet()
    fallback_answers = [
        GeneratedAnswer(
            question_id=q.question_id,
            question_number=q.question_number,
            question_type=q.question_type,
            answer_text=f"FreeLLM fallback answer for {q.question_id}",
            confidence=0.94,
            status=AnswerStatus.SUCCESS,
        )
        for q in ws.questions
    ]
    mock_freellm = AsyncMock(spec=BaseAnswerEngine)
    mock_freellm.provider = "freellm"
    mock_freellm.generate_answers.return_value = WorksheetAnswers(
        worksheet_filename=ws.filename,
        answers=fallback_answers,
        provider="freellm",
        metadata={"model": "default"},
    )

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="nvapi-mock-key",
        max_retries=1,
        retry_backoff=0.001,
        fallback_engine=mock_freellm,
        http_client=mock_client,
    )

    result = await engine.generate_answers(ws)

    # NVIDIA exhausted its initial attempt + 1 retry
    assert mock_client.post.call_count == 2
    # Fallback to FreeLLM was executed
    mock_freellm.generate_answers.assert_called_once()
    assert result.provider == "freellm"
    assert len(result.answers) == 4
    assert result.answers[0].answer_text == "FreeLLM fallback answer for q_1"


# ==============================================================================
# TEST 3: NVIDIA CONNECTION FAILURE -> FreeLLM FALLBACK
# ==============================================================================

@pytest.mark.asyncio
async def test_nvidia_connection_failure_freellm_fallback():
    """Verify that when NVIDIA connection fails (network error), system falls back to FreeLLM."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.side_effect = httpx.ConnectError("Failed to connect to integrate.api.nvidia.com")

    ws = _create_sample_parsed_worksheet()
    fallback_answers = [
        GeneratedAnswer(
            question_id=q.question_id,
            question_number=q.question_number,
            question_type=q.question_type,
            answer_text=f"FreeLLM network fallback for {q.question_id}",
            confidence=0.93,
            status=AnswerStatus.SUCCESS,
        )
        for q in ws.questions
    ]
    mock_freellm = AsyncMock(spec=BaseAnswerEngine)
    mock_freellm.provider = "freellm"
    mock_freellm.generate_answers.return_value = WorksheetAnswers(
        worksheet_filename=ws.filename,
        answers=fallback_answers,
        provider="freellm",
    )

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="nvapi-mock-key",
        max_retries=1,
        retry_backoff=0.001,
        fallback_engine=mock_freellm,
        http_client=mock_client,
    )

    result = await engine.generate_answers(ws)

    # FreeLLM was called on failover
    mock_freellm.generate_answers.assert_called_once()
    assert result.provider == "freellm"
    assert result.answers[0].answer_text == "FreeLLM network fallback for q_1"


# ==============================================================================
# TEST 4: BOTH PROVIDERS FAIL -> CLEAN ERROR
# ==============================================================================

@pytest.mark.asyncio
async def test_both_providers_fail_clean_error():
    """Verify that when both NVIDIA and FreeLLM fail, an AnswerEngineError is raised with descriptive details."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.side_effect = httpx.TimeoutException("NVIDIA API request timed out after 60.0s")

    mock_freellm = AsyncMock(spec=BaseAnswerEngine)
    mock_freellm.provider = "freellm"
    mock_freellm.generate_answers.side_effect = LLMNetworkError("FreeLLM local daemon is not running on 127.0.0.1:31415")

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="nvapi-mock-key",
        max_retries=0,
        fallback_engine=mock_freellm,
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(AnswerEngineError) as exc_info:
        await engine.generate_answers(ws)

    err_str = str(exc_info.value)
    assert "Both primary provider ('nvidia') and fallback provider ('freellm') failed" in err_str
    assert "Primary:" in err_str
    assert "Fallback:" in err_str


# ==============================================================================
# TEST 5: NO SECRET LEAKAGE
# ==============================================================================

@pytest.mark.asyncio
async def test_no_secret_leakage_in_exceptions_and_repr():
    """Verify that NVIDIA and FreeLLM API keys are strictly redacted from errors, repr, and str."""
    secret_nv_key = "nvapi-abcdef1234567890abcdef1234567890_topsecret"
    secret_fl_key = "freellmapi-99887766554433221100aabbccddeeff_confidential"

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.side_effect = httpx.TimeoutException(f"Connection timed out using credentials {secret_nv_key}")

    mock_freellm = AsyncMock(spec=BaseAnswerEngine)
    mock_freellm.provider = "freellm"
    mock_freellm.generate_answers.side_effect = LLMResponseError(f"FreeLLM failed with auth token {secret_fl_key}")

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key=secret_nv_key,
        max_retries=0,
        fallback_engine=mock_freellm,
        http_client=mock_client,
    )

    # 1. Repr and str redaction
    repr_str = repr(engine)
    str_val = str(engine)
    assert secret_nv_key not in repr_str
    assert secret_nv_key not in str_val
    assert "has_api_key=True" in repr_str

    # 2. Exception redaction
    ws = _create_sample_parsed_worksheet()
    with pytest.raises(AnswerEngineError) as exc_info:
        await engine.generate_answers(ws)

    err_str = str(exc_info.value)
    assert secret_nv_key not in err_str
    assert secret_fl_key not in err_str
    assert "[REDACTED_NVIDIA_KEY]" in err_str or "[REDACTED" in err_str
    assert "[REDACTED_FREELLM_KEY]" in err_str or "[REDACTED" in err_str


# ==============================================================================
# TEST 6: COMPREHENSIVE QUESTION TYPES (MCQ, Table, Code, Pseudocode, Output)
# ==============================================================================

@pytest.mark.asyncio
async def test_comprehensive_question_types_supported():
    """Verify structured response for MCQ, tables, code, pseudocode, and output/trace questions."""
    ws = _create_comprehensive_worksheet()

    sample_answers = [
        {
            "question_id": "q_mcq",
            "question_number": "1",
            "answer_text": "Network",
            "selected_option": "C",
            "confidence": 0.99,
            "explanation": "Network layer handles routing.",
        },
        {
            "question_id": "q_table",
            "question_number": "2",
            "answer_text": "Completed TCP vs UDP table.",
            "target_answers": {
                "t_1": "Connection-oriented with retransmission",
                "t_2": "Connectionless with no guarantee",
            },
            "confidence": 0.96,
        },
        {
            "question_id": "q_code",
            "question_number": "3",
            "answer_text": "def factorial(n):\n    return 1 if n <= 1 else n * factorial(n - 1)",
            "confidence": 0.98,
        },
        {
            "question_id": "q_pseudo",
            "question_number": "4",
            "answer_text": "function binarySearch(A, target):\n    low = 0, high = len(A)-1\n    while low <= high:\n        mid = (low + high) // 2\n        if A[mid] == target: return mid\n        elif A[mid] < target: low = mid + 1\n        else: high = mid - 1\n    return -1",
            "confidence": 0.95,
        },
        {
            "question_id": "q_trace",
            "question_number": "5",
            "answer_text": "Iteration 0: i=0, output=0\nIteration 1: i=1, output=2\nIteration 2: i=2, output=4\nFinal Output: 0, 2, 4",
            "confidence": 0.97,
        },
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_nvidia_success_response(sample_answers)

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="nvapi-test-key",
        http_client=mock_client,
    )

    result = await engine.generate_answers(ws)

    assert result.provider == "nvidia"
    assert result.total_count == 5
    assert result.success_count == 5

    # Check MCQ
    ans_mcq = result.get_answer("q_mcq")
    assert ans_mcq.selected_option == "C"

    # Check Table targets
    ans_table = result.get_answer("q_table")
    assert ans_table.target_answers.get("t_1") == "Connection-oriented with retransmission"
    assert ans_table.target_answers.get("t_2") == "Connectionless with no guarantee"

    # Check Code and Pseudocode
    ans_code = result.get_answer("q_code")
    assert "def factorial" in ans_code.answer_text
    assert "```" not in ans_code.answer_text

    ans_pseudo = result.get_answer("q_pseudo")
    assert "binarySearch" in ans_pseudo.answer_text

    # Check Output / Trace
    ans_trace = result.get_answer("q_trace")
    assert "output=" in ans_trace.answer_text


# ==============================================================================
# TEST 7: CONFIGURABLE PROVIDER ORDER VIA SETTINGS
# ==============================================================================

def test_configurable_provider_order_defaults_to_nvidia():
    """Verify settings.get_provider_order() defaults to ['nvidia', 'freellm']."""
    order = settings.get_provider_order()
    assert order == ["nvidia", "freellm"]


def test_configurable_provider_order_customization():
    """Verify that settings can customize provider priority order."""
    with patch.object(settings, "AI_PROVIDER_ORDER", "freellm,nvidia"):
        order = settings.get_provider_order()
        assert order == ["freellm", "nvidia"]


def test_factory_creates_nvidia_primary_with_freellm_fallback():
    """Verify AnswerEngineFactory creates NVIDIA primary with FreeLLM fallback by default."""
    with patch.object(settings, "WORKSHEET_ANSWER_PROVIDER", "nvidia"), \
         patch.object(settings, "AI_FALLBACK_PROVIDER", "freellm"), \
         patch.object(settings, "AI_PROVIDER_ORDER", "nvidia,freellm"):
        engine = AnswerEngineFactory.get_engine()
        assert isinstance(engine, LLMAnswerEngine)
        assert engine.provider == "nvidia"
        assert engine.fallback_engine is not None
        assert engine.fallback_engine.provider == "freellm"


def test_factory_reversed_provider_order():
    """Verify AnswerEngineFactory creates FreeLLM primary with NVIDIA fallback when configured."""
    with patch.object(settings, "AI_PROVIDER_ORDER", "freellm,nvidia"), \
         patch.object(settings, "WORKSHEET_ANSWER_PROVIDER", "freellm"), \
         patch.object(settings, "AI_FALLBACK_PROVIDER", "nvidia"):
        engine = AnswerEngineFactory.get_engine("freellm")
        assert isinstance(engine, LLMAnswerEngine)
        assert engine.provider == "freellm"
        assert engine.fallback_engine is not None
        assert engine.fallback_engine.provider == "nvidia"


# ==============================================================================
# TEST 8: PROVIDER STATUS ENDPOINT
# ==============================================================================

def test_provider_status_endpoint_reports_nvidia_primary():
    """Verify GET /api/v1/provider/status clearly reports NVIDIA as active primary provider."""
    client = TestClient(app)
    response = client.get("/api/v1/provider/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["primary_provider"] == "nvidia"
    assert data["fallback_provider"] == "freellm"
    assert data["provider_order"] == ["nvidia", "freellm"]
    assert "nemotron" in data["model"]
    assert "integrate.api.nvidia.com" in data["base_url"]


# ==============================================================================
# TEST 9: CONTROLLED REAL NVIDIA ANSWER GENERATION TEST
# ==============================================================================

@pytest.mark.asyncio
async def test_controlled_real_nvidia_generation():
    """Controlled real answer generation test against NVIDIA API.
    
    Runs ONLY if NVIDIA_API_KEY is present in settings or environment.
    Does NOT upload to Google Drive or submit to SRM.
    """
    api_key = settings.NVIDIA_API_KEY or os.getenv("NVIDIA_API_KEY")
    if not api_key:
        pytest.skip("NVIDIA_API_KEY not configured; skipping controlled live test.")

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key=api_key,
        base_url=settings.NVIDIA_BASE_URL,
        model=settings.NVIDIA_MODEL,
        timeout=30.0,
    )

    q = ParsedQuestion(
        question_id="real_q1",
        question_number="1",
        question_type=QuestionType.SHORT_ANSWER,
        question_text="What is virtual memory in operating systems?",
        marks=2,
    )
    ws = ParsedWorksheet(
        filename="controlled_real_test.docx",
        file_format="docx",
        questions=[q],
        course_code="21CSC303J",
        title="Operating Systems",
        session=1,
        slo=1,
    )

    result = await engine.generate_answers(ws)

    assert result.provider == "nvidia"
    assert len(result.answers) == 1
    ans = result.answers[0]
    assert ans.status == AnswerStatus.SUCCESS
    assert ans.confidence >= 0.70
    assert len(ans.answer_text) > 10
    # Authenticity: No markdown fences
    assert "```" not in ans.answer_text
    # Persona: authentic student tone, no conversational filler
    assert "certainly" not in ans.answer_text.lower()
