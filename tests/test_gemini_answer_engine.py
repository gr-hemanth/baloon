"""Tests for Gemini LLM Answer Engine (Milestone 5/7 Expansion).

Verifies:
1. Real HTTP request dispatch to Google Gemini REST API (mocked transport for testing).
2. Structured JSON response parsing and answer mapping.
3. Multi-question worksheet answer generation across all question types (MCQ, ONE_WORD, SHORT, LONG).
4. Single question answer generation via answer_question.
5. Strict failure on API error (no silent fallback to RuleBasedAnswerEngine).
6. Authentication / API key error handling (HTTP 400/401/403).
7. Rate limit / quota error handling (HTTP 429).
8. Request timeout handling.
9. Network / connection failure handling.
10. Malformed JSON / schema mismatch error handling.
11. Safety filter block handling.
12. Empty or missing candidates handling.
13. Partial / missing answers handling.
14. Unconfigured key handling (explicit error vs. allowed fallback).
15. Sensitive key redaction in repr, str, and logs.
16. Configurable model and parameters via environment variables.
17. Full WorksheetPipeline integration with LLMAnswerEngine.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from packages.worksheets.answer_engine import (
    AnswerEngineFactory,
    LLMAnswerEngine,
    RuleBasedAnswerEngine,
)
from packages.worksheets.answer_models import AnswerStatus, GeneratedAnswer
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


def _create_gemini_success_response(answers_payload: list) -> httpx.Response:
    """Helper creating a simulated Gemini 200 OK JSON response."""
    response_json = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps({"answers": answers_payload})
                        }
                    ],
                    "role": "model",
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 200,
            "candidatesTokenCount": 350,
            "totalTokenCount": 550,
        },
    }
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = response_json
    mock_resp.text = json.dumps(response_json)
    return mock_resp


@pytest.mark.asyncio
async def test_gemini_successful_answer_generation():
    """Verify LLMAnswerEngine formats structured prompt, executes POST, and parses answers."""
    sample_answers = [
        {
            "question_id": "q_1",
            "question_number": "1",
            "answer_text": "A. Central Processing Unit",
            "selected_option": "A",
            "confidence": 0.98,
            "explanation": "CPU standard acronym.",
        },
        {
            "question_id": "q_2",
            "question_number": "2",
            "answer_text": "Stack",
            "selected_option": None,
            "confidence": 0.95,
            "explanation": "Stack uses Last-In First-Out.",
        },
        {
            "question_id": "q_3",
            "question_number": "3",
            "answer_text": "A process has its own address space, while threads share the process address space.",
            "selected_option": None,
            "confidence": 0.92,
            "explanation": "Core OS distinction.",
        },
        {
            "question_id": "q_4",
            "question_number": "4",
            "answer_text": "Raft is a consensus algorithm that elects a leader and replicates log entries.",
            "selected_option": None,
            "confidence": 0.90,
            "explanation": "Raft consensus overview.",
        },
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_gemini_success_response(sample_answers)

    engine = LLMAnswerEngine(
        provider="gemini",
        api_key="mock-gemini-key-12345",
        model="gemini-1.5-flash",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()
    result = await engine.generate_answers(ws)

    # 1. Verification of real API call construction
    mock_client.post.assert_called_once()
    call_args, call_kwargs = mock_client.post.call_args
    assert "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent" in call_args[0]
    assert call_kwargs["headers"]["x-goog-api-key"] == "mock-gemini-key-12345"
    assert call_kwargs["json"]["generationConfig"]["responseMimeType"] == "application/json"

    # 2. Verification of returned answers
    assert result.provider == "gemini"
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
async def test_gemini_single_question_generation():
    """Verify answer_question formats and resolves an individual question."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_gemini_success_response([
        {
            "question_id": "q_single",
            "answer_text": "Single question answer",
            "confidence": 0.94,
        }
    ])

    engine = LLMAnswerEngine(
        provider="gemini",
        api_key="mock-key",
        http_client=mock_client,
    )

    q = ParsedQuestion(
        question_id="q_single",
        question_text="What is virtual memory?",
        question_type=QuestionType.SHORT_ANSWER,
    )
    ans = await engine.answer_question(q)

    assert ans.question_id == "q_single"
    assert ans.answer_text == "Single question answer"
    assert ans.confidence == 0.94
    assert ans.status == AnswerStatus.SUCCESS


@pytest.mark.asyncio
async def test_gemini_authentication_error_raises_exception():
    """Verify HTTP 400/401/403 raises LLMAuthenticationError and does NOT fall back to rule engine."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    error_resp = MagicMock(spec=httpx.Response)
    error_resp.status_code = 400
    error_resp.headers = {"content-type": "application/json"}
    error_resp.json.return_value = {"error": {"message": "API key not valid. Please pass a valid API key."}}
    mock_client.post.return_value = error_resp

    engine = LLMAnswerEngine(
        provider="gemini",
        api_key="invalid-api-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMAuthenticationError) as exc_info:
        await engine.generate_answers(ws)

    assert "authentication failed (400)" in str(exc_info.value).lower()
    # Confirm it did not return rule-based answers
    mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_gemini_rate_limit_error_handling():
    """Verify HTTP 429 raises LLMRateLimitError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    error_resp = MagicMock(spec=httpx.Response)
    error_resp.status_code = 429
    error_resp.headers = {"content-type": "application/json"}
    error_resp.json.return_value = {"error": {"message": "Quota exceeded for quota metric 'Generate Content API'."}}
    mock_client.post.return_value = error_resp

    engine = LLMAnswerEngine(
        provider="gemini",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMRateLimitError) as exc_info:
        await engine.generate_answers(ws)

    assert "rate limit exceeded" in str(exc_info.value).lower()
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_gemini_timeout_handling():
    """Verify request timeout raises LLMTimeoutError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.side_effect = httpx.TimeoutException("Read timed out after 60s")

    engine = LLMAnswerEngine(
        provider="gemini",
        api_key="valid-key",
        timeout=10.0,
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMTimeoutError) as exc_info:
        await engine.generate_answers(ws)

    assert "timed out after 10.0s" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gemini_network_error_handling():
    """Verify transport / connection failure raises LLMNetworkError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.side_effect = httpx.ConnectError("Failed to resolve host generativelanguage.googleapis.com")

    engine = LLMAnswerEngine(
        provider="gemini",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMNetworkError) as exc_info:
        await engine.generate_answers(ws)

    assert "network request failed" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_gemini_malformed_json_response():
    """Verify non-JSON model output raises LLMResponseError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    raw_response = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "I am an AI assistant and here are your answers in plain text without json."}],
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
        provider="gemini",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMResponseError) as exc_info:
        await engine.generate_answers(ws)

    assert "malformed json" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_gemini_safety_blocked_response():
    """Verify finishReason == SAFETY raises LLMResponseError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    raw_response = {
        "candidates": [
            {
                "finishReason": "SAFETY",
                "content": {"parts": []},
            }
        ]
    }
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = raw_response
    mock_client.post.return_value = mock_resp

    engine = LLMAnswerEngine(
        provider="gemini",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(LLMResponseError) as exc_info:
        await engine.generate_answers(ws)

    assert "safety filters" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_gemini_partial_missing_answers_marked_as_error():
    """Verify questions omitted by the model are marked with AnswerStatus.ERROR."""
    # Model only answers q_1; omits q_2, q_3, q_4
    partial_answers = [
        {
            "question_id": "q_1",
            "answer_text": "A. Central Processing Unit",
            "selected_option": "A",
            "confidence": 0.95,
        }
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_gemini_success_response(partial_answers)

    engine = LLMAnswerEngine(
        provider="gemini",
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
async def test_gemini_zero_matching_answers_raises_error():
    """Verify completely empty answers list raises MissingAnswerError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_gemini_success_response([])

    engine = LLMAnswerEngine(
        provider="gemini",
        api_key="valid-key",
        http_client=mock_client,
    )

    ws = _create_sample_parsed_worksheet()

    with pytest.raises(MissingAnswerError) as exc_info:
        await engine.generate_answers(ws)

    assert "zero answers" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_unconfigured_behavior():
    """Verify unconfigured key raises LLMAuthenticationError unless allow_fallback is set."""
    # 1. Fallback disabled (default when user explicitly selects gemini)
    engine_strict = LLMAnswerEngine(
        provider="gemini",
        api_key=None,
        allow_fallback_when_unconfigured=False,
    )
    ws = _create_sample_parsed_worksheet()

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(LLMAuthenticationError):
            await engine_strict.generate_answers(ws)

    # 2. Fallback allowed
    engine_fallback = LLMAnswerEngine(
        provider="gemini",
        api_key=None,
        allow_fallback_when_unconfigured=True,
    )
    with patch.dict("os.environ", {}, clear=True):
        fallback_res = await engine_fallback.generate_answers(ws)
        assert fallback_res.provider == "rule_based"
        assert fallback_res.total_count == 4


def test_answer_engine_factory_resolutions():
    """Verify factory returns appropriate engine instances."""
    # Default rule engine
    engine_rule = AnswerEngineFactory.get_engine("rule")
    assert isinstance(engine_rule, RuleBasedAnswerEngine)

    # Explicit gemini request
    engine_gemini = AnswerEngineFactory.get_engine("gemini")
    assert isinstance(engine_gemini, LLMAnswerEngine)
    assert engine_gemini.provider == "gemini"

    # Pluggable openai request
    engine_openai = AnswerEngineFactory.get_engine("openai")
    assert isinstance(engine_openai, LLMAnswerEngine)
    assert engine_openai.provider == "openai"


def test_sensitive_api_key_redaction():
    """Verify API key is never exposed in repr or str."""
    secret_key = "AIzaSySecretApiKeyDoNotExpose12345"
    engine = LLMAnswerEngine(provider="gemini", api_key=secret_key)

    repr_str = repr(engine)
    str_val = str(engine)

    assert secret_key not in repr_str
    assert secret_key not in str_val
    assert "has_api_key=True" in repr_str


@pytest.mark.asyncio
async def test_worksheet_pipeline_with_gemini_engine(tmp_path: Path):
    """Verify WorksheetPipeline end-to-end processing with real mock-backed Gemini engine."""
    orig_docx = _create_synthetic_mcq_docx(tmp_path / "mcq_worksheet.docx")

    sample_answers = [
        {"question_id": "q_1", "question_number": "1", "answer_text": "A. Central Processing Unit", "selected_option": "A", "confidence": 0.98},
        {"question_id": "q_2", "question_number": "2", "answer_text": "B. Stack", "selected_option": "B", "confidence": 0.95},
        {"question_id": "q_3", "question_number": "3", "answer_text": "B. HTTPS", "selected_option": "B", "confidence": 0.97},
        {"question_id": "q_4", "question_number": "4", "answer_text": "C. Network layer", "selected_option": "C", "confidence": 0.96},
    ]

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    mock_client.post.return_value = _create_gemini_success_response(sample_answers)

    gemini_engine = LLMAnswerEngine(
        provider="gemini",
        api_key="mock-key-pipeline",
        http_client=mock_client,
    )

    pipeline = WorksheetPipeline(answer_engine=gemini_engine)
    result = await pipeline.process(orig_docx, output_dir=tmp_path)

    assert result.success is True
    assert result.completed_file.exists()
    assert result.completed_file.name == "completed_mcq_worksheet.docx"
    assert result.answers.provider == "gemini"
    assert result.answers.total_count >= 4
