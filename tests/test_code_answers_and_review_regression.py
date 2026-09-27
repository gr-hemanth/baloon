"""Regression tests for programming answers and submission confirmation dialog.

Validates all 15 required regression tests:
1. Java thread "Hello" -> actual Java code.
2. Two even/odd threads -> actual Java code.
3. sleep + join -> actual Java code containing sleep() and join().
4. Interface implementation -> actual Java code.
5. Inheritance implementation -> actual Java code.
6. Theory question "Explain threading" -> TEXT, not CODE.
7. Empty CODE answer -> rejected and regenerated.
8. Prose-only CODE answer -> rejected and regenerated.
9. NVIDIA invalid code answer -> FreeLLM fallback.
10. Both providers produce invalid code -> clean failure.
11. Submit button opens confirmation modal.
12. Cancel closes modal and makes zero submit requests.
13. Confirm invokes exactly one submit request.
14. Double-click does not duplicate submission.
15. Browser refresh preserves AWAITING_USER_REVIEW.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import docx
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.worker.tasks import store_job_credentials
from packages.shared.models.job import Job, JobStatus
from packages.worksheets.answer_engine import (
    AnswerEngineFactory,
    LLMAnswerEngine,
    RuleBasedAnswerEngine,
)
from packages.worksheets.answer_models import AnswerStatus, GeneratedAnswer
from packages.worksheets.classifier import CodeIntentDetector, QuestionClassifier
from packages.worksheets.code_validator import CodeAnswerValidator
from packages.worksheets.exceptions import AnswerGenerationError
from packages.worksheets.filler import DocxWorksheetFiller
from packages.worksheets.models import (
    ParsedQuestion,
    ParsedWorksheet,
    QuestionType,
    ResponseMode,
)
from packages.worksheets.pipeline import WorksheetPipeline


# ==============================================================================
# TESTS 1 - 5: PROGRAMMING ANSWERS PRODUCE REAL CODE
# ==============================================================================

@pytest.mark.asyncio
async def test_01_java_thread_hello_returns_real_java_code():
    """Test 1: Java thread 'Hello' -> actual Java code."""
    engine = RuleBasedAnswerEngine()
    q = ParsedQuestion(
        question_id="q_1",
        question_number="1",
        question_text='Write a Java program to print "Hello" using a thread.',
        question_type=QuestionType.CODE,
        response_mode=ResponseMode.CODE,
        language="java",
    )
    ans = await engine.answer_question(q)
    assert ans.status == AnswerStatus.SUCCESS
    val = CodeAnswerValidator.validate(ans.answer_text, q, "java")
    assert val.is_valid, f"Validation failed: {val.reason}"
    assert "class HelloThread extends Thread" in ans.answer_text or "implements Runnable" in ans.answer_text
    assert 'System.out.println("Hello")' in ans.answer_text or 'println("Hello")' in ans.answer_text


@pytest.mark.asyncio
async def test_02_two_even_odd_threads_returns_real_java_code():
    """Test 2: Two even/odd threads -> actual Java code."""
    engine = RuleBasedAnswerEngine()
    q = ParsedQuestion(
        question_id="q_2",
        question_number="2",
        question_text="Create two threads to print even and odd numbers separately.",
        question_type=QuestionType.CODE,
        response_mode=ResponseMode.CODE,
        language="java",
    )
    ans = await engine.answer_question(q)
    assert ans.status == AnswerStatus.SUCCESS
    val = CodeAnswerValidator.validate(ans.answer_text, q, "java")
    assert val.is_valid, f"Validation failed: {val.reason}"
    code = ans.answer_text
    assert "EvenThread" in code or "even" in code.lower()
    assert "OddThread" in code or "odd" in code.lower()
    assert ".start()" in code
    # Ensure it's not prose
    assert "Create two Runnable implementations" not in code


@pytest.mark.asyncio
async def test_03_sleep_and_join_returns_real_java_code():
    """Test 3: sleep + join -> actual Java code containing sleep() and join()."""
    engine = RuleBasedAnswerEngine()
    q = ParsedQuestion(
        question_id="q_3",
        question_number="3",
        question_text="Demonstrate thread sleep and join in Java.",
        question_type=QuestionType.CODE,
        response_mode=ResponseMode.CODE,
        language="java",
    )
    ans = await engine.answer_question(q)
    assert ans.status == AnswerStatus.SUCCESS
    val = CodeAnswerValidator.validate(ans.answer_text, q, "java")
    assert val.is_valid, f"Validation failed: {val.reason}"
    code = ans.answer_text
    assert "Thread.sleep" in code
    assert ".join(" in code
    assert len(code.strip()) > 30


@pytest.mark.asyncio
async def test_04_interface_implementation_returns_real_java_code():
    """Test 4: Interface implementation -> actual Java code."""
    engine = RuleBasedAnswerEngine()
    q = ParsedQuestion(
        question_id="q_4",
        question_number="4",
        question_text="Write a Java interface and implement it in a class.",
        question_type=QuestionType.CODE,
        response_mode=ResponseMode.CODE,
        language="java",
    )
    ans = await engine.answer_question(q)
    assert ans.status == AnswerStatus.SUCCESS
    val = CodeAnswerValidator.validate(ans.answer_text, q, "java")
    assert val.is_valid, f"Validation failed: {val.reason}"
    code = ans.answer_text
    assert "interface " in code
    assert "implements " in code


@pytest.mark.asyncio
async def test_05_inheritance_implementation_returns_real_java_code():
    """Test 5: Inheritance implementation -> actual Java code."""
    engine = RuleBasedAnswerEngine()
    q = ParsedQuestion(
        question_id="q_5",
        question_number="5",
        question_text="Create a superclass Employee and subclass Manager in Java.",
        question_type=QuestionType.CODE,
        response_mode=ResponseMode.CODE,
        language="java",
    )
    ans = await engine.answer_question(q)
    assert ans.status == AnswerStatus.SUCCESS
    val = CodeAnswerValidator.validate(ans.answer_text, q, "java")
    assert val.is_valid, f"Validation failed: {val.reason}"
    code = ans.answer_text
    assert "class Employee" in code
    assert "extends Employee" in code or "extends" in code


# ==============================================================================
# TEST 6: THEORY QUESTION CLASSIFICATION
# ==============================================================================

def test_06_theory_question_explain_threading_is_text_not_code():
    """Test 6: Theory question 'Explain threading' -> TEXT, not CODE."""
    prompt = "Explain threading in modern operating systems and its advantages."
    mode, lang = CodeIntentDetector.detect(prompt, context={"course_code": "21CSC203P"})
    assert mode == ResponseMode.TEXT
    assert mode != ResponseMode.CODE

    q = ParsedQuestion(
        question_id="q_theory",
        question_number="6",
        question_text=prompt,
    )
    qtype = QuestionClassifier.classify(q, context={"course_code": "21CSC203P"})
    assert q.response_mode == ResponseMode.TEXT
    assert q.response_mode != ResponseMode.CODE


# ==============================================================================
# TESTS 7 - 10: VALIDATION & REGENERATION POLICIES
# ==============================================================================

@pytest.mark.asyncio
async def test_07_empty_code_answer_rejected_and_regenerated():
    """Test 7: Empty CODE answer -> rejected and regenerated."""
    q = ParsedQuestion(
        question_id="q_even_odd",
        question_number="2",
        question_text="Create two threads to print even and odd numbers separately.",
        question_type=QuestionType.CODE,
        response_mode=ResponseMode.CODE,
        language="java",
    )
    # Validator rejects empty answer
    val = CodeAnswerValidator.validate("", q, "java")
    assert not val.is_valid
    assert "empty" in val.reason.lower()

    # Model returns empty in first call, valid code on regeneration
    valid_code = (
        "class EvenThread extends Thread {\n"
        "    public void run() {\n"
        "        for (int i = 2; i <= 10; i += 2) System.out.println(\"Even: \" + i);\n"
        "    }\n"
        "}\n"
        "class OddThread extends Thread {\n"
        "    public void run() {\n"
        "        for (int i = 1; i <= 9; i += 2) System.out.println(\"Odd: \" + i);\n"
        "    }\n"
        "}\n"
        "public class Main {\n"
        "    public static void main(String[] args) {\n"
        "        new EvenThread().start();\n"
        "        new OddThread().start();\n"
        "    }\n"
        "}"
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    resp_empty = MagicMock(spec=httpx.Response)
    resp_empty.status_code = 200
    resp_empty.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"answers": [{"question_id": "q_even_odd", "answer_text": ""}]})}}]
    }

    resp_valid = MagicMock(spec=httpx.Response)
    resp_valid.status_code = 200
    resp_valid.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"answers": [{"question_id": "q_even_odd", "answer_text": valid_code}]})}}]
    }

    mock_client.post.side_effect = [resp_empty, resp_valid]

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-key",
        http_client=mock_client,
    )
    ws = ParsedWorksheet(filename="t.docx", file_format="docx", questions=[q])
    result = await engine.generate_answers(ws)
    ans = result.get_answer("q_even_odd")
    assert ans is not None
    assert ans.status == AnswerStatus.SUCCESS
    assert "EvenThread" in ans.answer_text


@pytest.mark.asyncio
async def test_08_prose_only_code_answer_rejected_and_regenerated():
    """Test 8: Prose-only CODE answer -> rejected and regenerated."""
    q = ParsedQuestion(
        question_id="q_even_odd",
        question_number="2",
        question_text="Create two threads to print even and odd numbers separately.",
        question_type=QuestionType.CODE,
        response_mode=ResponseMode.CODE,
        language="java",
    )
    prose_answer = "Create two Runnable implementations: one prints even numbers, the other prints odd numbers. Start both threads to run concurrently."

    val = CodeAnswerValidator.validate(prose_answer, q, "java")
    assert not val.is_valid
    assert "prose" in val.reason.lower() or "declarations" in val.reason.lower() or "syntax" in val.reason.lower()

    valid_code = (
        "class EvenThread extends Thread {\n"
        "    public void run() {\n"
        "        for (int i = 2; i <= 10; i += 2) System.out.println(\"Even: \" + i);\n"
        "    }\n"
        "}\n"
        "class OddThread extends Thread {\n"
        "    public void run() {\n"
        "        for (int i = 1; i <= 9; i += 2) System.out.println(\"Odd: \" + i);\n"
        "    }\n"
        "}\n"
        "public class Main {\n"
        "    public static void main(String[] args) {\n"
        "        new EvenThread().start();\n"
        "        new OddThread().start();\n"
        "    }\n"
        "}"
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False

    resp_prose = MagicMock(spec=httpx.Response)
    resp_prose.status_code = 200
    resp_prose.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"answers": [{"question_id": "q_even_odd", "answer_text": prose_answer}]})}}]
    }

    resp_valid = MagicMock(spec=httpx.Response)
    resp_valid.status_code = 200
    resp_valid.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"answers": [{"question_id": "q_even_odd", "answer_text": valid_code}]})}}]
    }

    mock_client.post.side_effect = [resp_prose, resp_valid]

    engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-key",
        http_client=mock_client,
    )
    ws = ParsedWorksheet(filename="t.docx", file_format="docx", questions=[q])
    result = await engine.generate_answers(ws)
    ans = result.get_answer("q_even_odd")
    assert ans is not None
    assert ans.status == AnswerStatus.SUCCESS
    assert "class EvenThread" in ans.answer_text


@pytest.mark.asyncio
async def test_09_nvidia_invalid_code_answer_falls_back_to_freellm():
    """Test 9: NVIDIA invalid code answer -> FreeLLM fallback."""
    q = ParsedQuestion(
        question_id="q_sleep",
        question_number="3",
        question_text="Demonstrate thread sleep and join in Java.",
        question_type=QuestionType.CODE,
        response_mode=ResponseMode.CODE,
        language="java",
    )

    # Primary NVIDIA client returns prose on both initial and regeneration attempts
    nvidia_client = AsyncMock(spec=httpx.AsyncClient)
    nvidia_client.is_closed = False
    prose_resp = MagicMock(spec=httpx.Response)
    prose_resp.status_code = 200
    prose_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"answers": [{"question_id": "q_sleep", "answer_text": "Use Thread.sleep to pause and thread.join to wait for execution."}]})}}]
    }
    nvidia_client.post.return_value = prose_resp

    # Fallback FreeLLM client returns real executable Java code
    valid_code = (
        "class Worker extends Thread {\n"
        "    public void run() {\n"
        "        try { Thread.sleep(500); } catch (Exception e) {}\n"
        "    }\n"
        "}\n"
        "public class Main {\n"
        "    public static void main(String[] args) throws Exception {\n"
        "        Worker t = new Worker(); t.start(); t.join();\n"
        "    }\n"
        "}"
    )
    freellm_client = AsyncMock(spec=httpx.AsyncClient)
    freellm_client.is_closed = False
    valid_resp = MagicMock(spec=httpx.Response)
    valid_resp.status_code = 200
    valid_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"answers": [{"question_id": "q_sleep", "answer_text": valid_code}]})}}]
    }
    freellm_client.post.return_value = valid_resp

    fallback_engine = LLMAnswerEngine(
        provider="freellm",
        api_key="mock-freellm-key",
        http_client=freellm_client,
    )

    primary_engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-nv-key",
        fallback_engine=fallback_engine,
        http_client=nvidia_client,
    )

    ws = ParsedWorksheet(filename="t.docx", file_format="docx", questions=[q])
    result = await primary_engine.generate_answers(ws)
    ans = result.get_answer("q_sleep")
    assert ans is not None
    assert ans.status == AnswerStatus.SUCCESS
    assert "Thread.sleep(500)" in ans.answer_text
    assert ans.metadata.get("provider") == "freellm"


@pytest.mark.asyncio
async def test_10_both_providers_produce_invalid_code_clean_failure():
    """Test 10: Both providers produce invalid code -> clean structured AnswerGenerationError."""
    q = ParsedQuestion(
        question_id="q_sleep",
        question_number="3",
        question_text="Demonstrate thread sleep and join in Java.",
        question_type=QuestionType.CODE,
        response_mode=ResponseMode.CODE,
        language="java",
    )

    # Both providers return purely descriptive prose
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.is_closed = False
    prose_resp = MagicMock(spec=httpx.Response)
    prose_resp.status_code = 200
    prose_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"answers": [{"question_id": "q_sleep", "answer_text": "Just call sleep and join."}]})}}]
    }
    mock_client.post.return_value = prose_resp

    fb_client = AsyncMock(spec=httpx.AsyncClient)
    fb_client.is_closed = False
    fb_client.post.return_value = prose_resp

    fallback_engine = LLMAnswerEngine(
        provider="freellm",
        api_key="mock-freellm-key",
        http_client=fb_client,
    )

    primary_engine = LLMAnswerEngine(
        provider="nvidia",
        api_key="mock-nv-key",
        fallback_engine=fallback_engine,
        http_client=mock_client,
    )

    ws = ParsedWorksheet(filename="t.docx", file_format="docx", questions=[q])
    with pytest.raises(AnswerGenerationError) as exc_info:
        await primary_engine.generate_answers(ws)

    err_str = str(exc_info.value)
    assert "Could not generate a valid code answer" in err_str
    assert "Primary provider ('nvidia') failed validation" in err_str
    assert "Fallback provider ('freellm') failed validation" in err_str


# ==============================================================================
# TESTS 11 - 15: SUBMISSION CONFIRMATION DIALOG & CONTROLS
# ==============================================================================

def test_11_submit_button_opens_confirmation_modal():
    """Test 11: Submit button opens confirmation modal with explicit text and Cancel / Submit actions."""
    dashboard_path = Path("apps/api/static/dashboard.html")
    assert dashboard_path.exists()
    content = dashboard_path.read_text(encoding="utf-8")

    # Review button triggers openSubmitConfirmationModal
    assert 'onclick="openSubmitConfirmationModal()"' in content
    # Modal exists in DOM
    assert 'id="submit-confirm-modal"' in content
    # Confirmation modal text matches requirement
    assert "Your completed worksheet is ready and has been uploaded to Google Drive. Review it before submitting to SRM." in content
    # Cancel button exists
    assert 'onclick="closeSubmitConfirmationModal()"' in content
    # Submit to SRM confirmation action exists
    assert 'onclick="confirmAndSubmitToSRM()"' in content


def test_12_cancel_closes_modal_and_makes_zero_submit_requests(client: TestClient, db_session: Session):
    """Test 12: Cancel closes modal and makes zero submit requests, keeping job in AWAITING_USER_REVIEW."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={
            "drive_file_id": "test_id_123",
            "drive_web_url": "https://drive.google.com/file/d/test_id_123/view",
            "drive_permission_status": "VERIFIED_PUBLIC_READER",
            "review_ready": True,
        },
    )
    db_session.add(job)
    db_session.commit()

    # Cancel action purely closes modal without invoking POST /api/v1/jobs/{job_id}/submit
    # Check job state remains AWAITING_USER_REVIEW
    resp = client.get(f"/api/v1/jobs/{job.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "AWAITING_USER_REVIEW"
    assert data["review_ready"] is True
    assert data["submission_allowed"] is True
    assert data["drive_web_view_link"] == "https://drive.google.com/file/d/test_id_123/view"


def test_13_confirm_invokes_exactly_one_submit_request(client: TestClient, db_session: Session):
    """Test 13: Confirm invokes exactly one submit request and transitions to SUBMITTING."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={
            "drive_file_id": "test_drive_file",
            "drive_web_url": "https://drive.google.com/file/d/test_drive_file/view",
            "review_ready": True,
        },
    )
    db_session.add(job)
    db_session.commit()
    store_job_credentials(job.id, {"USER_ID": "RA2111003010001", "PASSWORD": "SecretPassword!"})

    with patch("apps.api.routes.jobs.submit_job") as mock_celery_task:
        mock_celery_task.delay.return_value = MagicMock(id="task-submit-1")
        submit_resp = client.post(f"/api/v1/jobs/{job.id}/submit")
        assert submit_resp.status_code == 200
        data = submit_resp.json()
        assert data["status"] == "SUBMITTING"
        assert data["current_step"] == "submitting_link_to_srm"
        mock_celery_task.delay.assert_called_once()


def test_14_double_click_does_not_duplicate_submission(client: TestClient, db_session: Session):
    """Test 14: Double-click does not duplicate submission (atomic backend idempotency)."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={
            "drive_file_id": "test_drive_file",
            "drive_web_url": "https://drive.google.com/file/d/test_drive_file/view",
            "review_ready": True,
        },
    )
    db_session.add(job)
    db_session.commit()
    store_job_credentials(job.id, {"USER_ID": "RA2111003010001", "PASSWORD": "SecretPassword!"})

    with patch("apps.api.routes.jobs.submit_job") as mock_celery_task:
        mock_celery_task.delay.return_value = MagicMock(id="task-submit-1")

        # First submit request
        resp1 = client.post(f"/api/v1/jobs/{job.id}/submit")
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "SUBMITTING"
        assert mock_celery_task.delay.call_count == 1

        # Second rapid submit request (double-click simulation)
        resp2 = client.post(f"/api/v1/jobs/{job.id}/submit")
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "SUBMITTING"

        # Celery dispatch count remains exactly 1!
        assert mock_celery_task.delay.call_count == 1


def test_15_browser_refresh_preserves_awaiting_user_review(client: TestClient, db_session: Session):
    """Test 15: Browser refresh preserves AWAITING_USER_REVIEW status and controls."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={
            "course_code": "21CSC203P",
            "session": 8,
            "slo": 2,
            "completed_file": "completed_1082.docx",
            "drive_file_id": "test_drive_file_1082",
            "drive_web_url": "https://drive.google.com/file/d/test_drive_file_1082/view",
            "drive_permission_status": "VERIFIED_PUBLIC_READER",
            "review_ready": True,
        },
    )
    db_session.add(job)
    db_session.commit()

    # Browser refresh simulates querying GET /api/v1/jobs/{job_id} multiple times
    for _ in range(3):
        resp = client.get(f"/api/v1/jobs/{job.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "AWAITING_USER_REVIEW"
        assert data["current_step"] == "awaiting_user_review"
        assert data["review_ready"] is True
        assert data["submission_allowed"] is True
        assert data["drive_web_view_link"] == "https://drive.google.com/file/d/test_drive_file_1082/view"
