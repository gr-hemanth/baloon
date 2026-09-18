"""Tests for FreeLLM (OpenAI-compatible) Answer Engine.

Verifies:
1. Real HTTP request dispatch to FreeLLM /chat/completions endpoint (mocked transport).
2. Structured JSON response parsing and answer mapping.
3. Multi-question worksheet answer generation across all question types (MCQ, ONE_WORD, SHORT, LONG).
4. Single question answer generation via answer_question.
5. Strict failure on API error (no silent fallback to RuleBasedAnswerEngine).
6. Authentication failure handling (HTTP 401/403).
7. Rate limit / quota error handling (HTTP 429).
8. Request timeout handling.
9. Network / connection failure handling.
10. Malformed JSON / schema mismatch error handling.
11. Missing answers handling.
12. Sensitive API key redaction in repr, str, and error messages.
13. Configurable base URL, model, and parameters.
14. Full WorksheetPipeline integration with FreeLLM.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from packages.shared.config import settings
from packages.worksheets.answer_engine import (
    AnswerEngineFactory,
    LLMAnswerEngine,
    RuleBasedAnswerEngine,
)
from packages.worksheets.answer_models import AnswerStatus
from packages.worksheets.exceptions import (
    LLMAuthenticationError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    MissingAnswerError,
)
from packages.worksheets.models import (
    ParsedQuestion,
    ParsedWorksheet,
    QuestionOption,
    QuestionType,
)
from packages.worksheets.pipeline import WorksheetPipeline
from tests.test_worksheet_parser import _create_synthetic_mcq_docx


def _create_sample_parsed_worksheet() -> ParsedWorksheet:
    """Create a sample in-memory ParsedWorksheet with multiple question types."""
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


def _create_freellm_success_response(answers_payload: list) -> httpx.Response:
    """Helper creating a simulated OpenAI-compatible 200 OK JSON response."""
    response_json = {
        "id": "chatcmpl-freellm-test-12345",
        "object": "chat.completion",
        "created": 1715000000,
        "model": "default",
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
            "prompt_tokens": 150,
            "completion_tokens": 300,
            "total_tokens": 450,
        },
    }
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = response_json
    mock_resp.text = json.dumps(response_json)
    return mock_resp


@pytest.mark.asyncio
async def test_freellm_successful_answer_generation():
    """Verify FreeLLM constructs OpenAI-compatible request to {FREELLM_BASE_URL}/chat/completions."""
    sample_answers = [
        {
            "question_id": "q_1",
            "question_number": "1",
            "answer_text": "A. Central Processing Unit",
            "selected_option": "A",
            "confidence": 0.98,
            "explanation": "CPU acronym.",
        },
        {
            "question_id": "q_2",
            "question_number": "2",
            "answer_text": "Stack",
            "selected_option": None,
            "confidence": 0.96,
            "explanation": "LIFO = Stack.",
        },
        {
            "question_id": "q_3",
            "question_number": "3",
            "answer_text": "A process has its own address space, whereas threads share memory.",
            "selected_option": None,
            "confidence": 0.92,
            "explanation": "Process vs thread.",
        },
        {
            "question_id": "q_4",
            "question_number": "4",
            "answer_text": "Raft achieves consensus via leader election, log replication, and safety guarantees.",
            "selected_option": None,
            "confidence": 0.90,
            "explanation": "Raft protocol overview.",
        },
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_freellm_success_response(sample_answers)

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="mock-freellm-api-key-xyz",
        base_url="http://127.0.0.1:31415/v1",
        model="default",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()
    result = await engine.generate_answers(ws)

    # 1. Verification of real API call construction
    mock_client.post.assert_called_once()
    call_args, call_kwargs = mock_client.post.call_args
    assert call_args[0] == "http://127.0.0.1:31415/v1/chat/completions"
    assert call_kwargs["headers"]["Authorization"] == "Bearer mock-freellm-api-key-xyz"
    assert call_kwargs["json"]["model"] in ("default", "auto")
    assert call_kwargs["json"]["response_format"] == {"type": "json_object"}
    assert len(call_kwargs["json"]["messages"]) == 2

    # 2. Verification of returned answers
    assert result.provider == "freellm"
    assert result.total_count == 4
    assert result.success_count == 4
    assert result.average_confidence >= 0.90

    ans1 = result.get_answer("q_1")
    assert ans1 is not None
    assert ans1.selected_option == "A"
    assert ans1.status == AnswerStatus.SUCCESS

    ans2 = result.get_answer("q_2")
    assert ans2 is not None
    assert ans2.answer_text == "Stack"


@pytest.mark.asyncio
async def test_freellm_single_question_generation():
    """Verify FreeLLM answer_question resolves an individual question."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_freellm_success_response([
        {
            "question_id": "q_single",
            "answer_text": "Paging divides physical memory into fixed-size frames.",
            "confidence": 0.93,
        }
    ])

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="mock-key",
        base_url="http://127.0.0.1:31415/v1",
        http_client=mock_client,
    )

    q = ParsedQuestion(
        question_id="q_single",
        question_text="What is paging in memory management?",
        question_type=QuestionType.SHORT_ANSWER,
    )
    ans = await engine.answer_question(q)

    assert ans.question_id == "q_single"
    assert "Paging divides physical memory" in ans.answer_text
    assert ans.confidence == 0.93
    assert ans.status == AnswerStatus.SUCCESS


@pytest.mark.asyncio
async def test_freellm_authentication_failure_raises_exception():
    """Verify HTTP 401 raises LLMAuthenticationError and does NOT silently fall back."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    error_resp = MagicMock(spec=httpx.Response)
    error_resp.status_code = 401
    error_resp.headers = {"content-type": "application/json"}
    error_resp.json.return_value = {"error": {"message": "Invalid or expired FreeLLM API key."}}
    mock_client.post.return_value = error_resp

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="bad-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMAuthenticationError) as exc_info:
        await engine.generate_answers(ws)

    assert "authentication failed (401)" in str(exc_info.value).lower()
    mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_freellm_rate_limit_error_handling():
    """Verify HTTP 429 raises LLMRateLimitError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    error_resp = MagicMock(spec=httpx.Response)
    error_resp.status_code = 429
    error_resp.headers = {"content-type": "application/json"}
    error_resp.json.return_value = {"error": {"message": "Rate limit exceeded on FreeLLM API."}}
    mock_client.post.return_value = error_resp

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMRateLimitError) as exc_info:
        await engine.generate_answers(ws)

    assert "rate limit exceeded" in str(exc_info.value).lower()
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_freellm_timeout_handling():
    """Verify timeout on FreeLLM raises LLMTimeoutError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.side_effect = httpx.TimeoutException("Read timed out after 60s")

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="valid-key",
        timeout=15.0,
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMTimeoutError) as exc_info:
        await engine.generate_answers(ws)

    assert "timed out after 15.0s" in str(exc_info.value)


@pytest.mark.asyncio
async def test_freellm_network_error_handling():
    """Verify connection failure to FreeLLM raises LLMNetworkError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.side_effect = httpx.ConnectError("Failed to connect to 127.0.0.1:31415")

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMNetworkError) as exc_info:
        await engine.generate_answers(ws)

    assert "network request failed" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_freellm_malformed_json_response():
    """Verify non-JSON response from FreeLLM raises LLMResponseError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    raw_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Here is the raw text without any JSON structure.",
                }
            }
        ]
    }
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = raw_response
    mock_client.post.return_value = mock_resp

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMResponseError) as exc_info:
        await engine.generate_answers(ws)

    assert "malformed json" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_freellm_partial_missing_answers_handling():
    """Verify omitted questions are assigned AnswerStatus.ERROR."""
    # FreeLLM returns answer only for q_1
    partial = [
        {
            "question_id": "q_1",
            "answer_text": "A. Central Processing Unit",
            "selected_option": "A",
            "confidence": 0.95,
        }
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_freellm_success_response(partial)

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()
    result = await engine.generate_answers(ws)

    assert result.total_count == 4
    assert result.success_count == 1

    ans1 = result.get_answer("q_1")
    assert ans1.status == AnswerStatus.SUCCESS

    ans2 = result.get_answer("q_2")
    assert ans2.status == AnswerStatus.ERROR
    assert "omitted" in ans2.error_message.lower()


@pytest.mark.asyncio
async def test_freellm_zero_answers_raises_error():
    """Verify empty answers array raises MissingAnswerError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_freellm_success_response([])

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(MissingAnswerError) as exc_info:
        await engine.generate_answers(ws)

    assert "zero answers" in str(exc_info.value).lower()


def test_freellm_sensitive_api_key_redaction():
    """Verify FreeLLM API key is never exposed in repr or str."""
    secret = "sk-freellm-super-secret-key-12345"
    engine = LLMAnswerEngine(provider="freellm", api_key=secret)

    repr_str = repr(engine)
    str_val = str(engine)

    assert secret not in repr_str
    assert secret not in str_val
    assert "has_api_key=True" in repr_str
    assert "provider='freellm'" in repr_str


@pytest.mark.asyncio
async def test_freellm_unconfigured_behavior():
    """Verify unconfigured key raises LLMAuthenticationError unless fallback enabled."""
    ws = _create_sample_parsed_worksheet()

    with patch.object(settings, "FREELLM_API_KEY", None), patch.dict("os.environ", {}, clear=True):
        # 1. Default (fallback disabled)
        engine_strict = LLMAnswerEngine(
            provider="freellm",
            api_key=None,
            allow_fallback_when_unconfigured=False,
        )
        with pytest.raises(LLMAuthenticationError) as exc_info:
            await engine_strict.generate_answers(ws)
        assert "FREELLM_API_KEY" in str(exc_info.value)

        # 2. Fallback allowed
        engine_fallback = LLMAnswerEngine(
            provider="freellm",
            api_key=None,
            allow_fallback_when_unconfigured=True,
        )
        res = await engine_fallback.generate_answers(ws)
        assert res.provider == "rule_based"
        assert res.total_count == 4


def test_factory_resolves_freellm():
    """Verify factory resolves 'freellm' provider to LLMAnswerEngine."""
    engine = AnswerEngineFactory.get_engine("freellm")
    assert isinstance(engine, LLMAnswerEngine)
    assert engine.provider == "freellm"
    assert engine.model == "default"
    assert "http://127.0.0.1:31415/v1" in engine.base_url


@pytest.mark.asyncio
async def test_freellm_automatic_model_routing_sends_default_model():
    """Verify FreeLLM defaults to model='default' for automatic routing without hardcoding."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_freellm_success_response([
        {
            "question_id": "q_1",
            "question_number": "1",
            "answer_text": "A. Central Processing Unit",
            "selected_option": "A",
            "confidence": 0.98,
        }
    ])

    # Instantiate with NO explicit model parameter to verify default routing
    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="mock-key",
        http_client=mock_client,
    )
    assert engine.model == "default"

    ws = ParsedWorksheet(
        filename="test.docx",
        file_format="docx",
        questions=[
            ParsedQuestion(
                question_id="q_1",
                question_number="1",
                question_type=QuestionType.MCQ,
                question_text="What is CPU?",
                marks=1,
            )
        ],
    )
    await engine.generate_answers(ws)

    mock_client.post.assert_called_once()
    _, call_kwargs = mock_client.post.call_args
    # Confirms model is routed automatically ('auto'/'default') to /chat/completions
    assert call_kwargs["json"]["model"] in ("default", "auto")



@pytest.mark.asyncio
async def test_worksheet_pipeline_with_freellm_engine(tmp_path: Path):
    """Verify WorksheetPipeline end-to-end processing with FreeLLM engine."""
    orig_docx = _create_synthetic_mcq_docx(tmp_path / "mcq_freellm.docx")

    sample_answers = [
        {"question_id": "q_1", "question_number": "1", "answer_text": "A. Central Processing Unit", "selected_option": "A", "confidence": 0.98},
        {"question_id": "q_2", "question_number": "2", "answer_text": "B. Stack", "selected_option": "B", "confidence": 0.95},
        {"question_id": "q_3", "question_number": "3", "answer_text": "B. HTTPS", "selected_option": "B", "confidence": 0.97},
        {"question_id": "q_4", "question_number": "4", "answer_text": "C. Network layer", "selected_option": "C", "confidence": 0.96},
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_freellm_success_response(sample_answers)

    freellm_engine = LLMAnswerEngine(
        provider="freellm",
        api_key="mock-freellm-pipeline-key",
        http_client=mock_client,
    )

    pipeline = WorksheetPipeline(answer_engine=freellm_engine)
    result = await pipeline.process(orig_docx, output_dir=tmp_path)

    assert result.success is True
    assert result.completed_file.exists()
    assert result.completed_file.name == "completed_mcq_freellm.docx"
    assert result.answers.provider == "freellm"
    assert result.answers.total_count >= 4


@pytest.mark.asyncio
async def test_freellm_humanized_student_prompt_and_prohibitions():
    """Verify FreeLLM system and user prompts enforce college-student persona and prohibit markdown artifacts."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_freellm_success_response([
        {
            "question_id": "q_1",
            "question_number": "1",
            "answer_text": "Central Processing Unit",
            "selected_option": "A",
            "confidence": 0.98,
        }
    ])

    engine = LLMAnswerEngine(
        provider="freellm",
        api_key="mock-key",
        http_client=mock_client,
    )

    ws = ParsedWorksheet(
        filename="test.docx",
        file_format="docx",
        questions=[
            ParsedQuestion(
                question_id="q_1",
                question_number="1",
                question_type=QuestionType.MCQ,
                question_text="What does CPU stand for?",
                marks=1,
            )
        ],
    )
    await engine.generate_answers(ws)

    mock_client.post.assert_called_once()
    _, call_kwargs = mock_client.post.call_args
    messages = call_kwargs["json"]["messages"]
    system_msg = messages[0]["content"]
    user_msg = messages[1]["content"]

    # 1. Verify student persona enforcement
    assert "college student" in system_msg.lower()
    assert "professor" not in system_msg.lower()

    # 2. Verify strict markdown prohibitions in prompt
    assert "NO MARKDOWN" in system_msg
    assert "###" in system_msg
    assert "**" in system_msg
    assert "*" in system_msg

    # 3. Verify user prompt prohibitions
    assert "NO markdown headers (###)" in user_msg
    assert "NO bold text (**)" in user_msg
    assert "NO bullet asterisks (*)" in user_msg
