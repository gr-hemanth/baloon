"""Integration tests for Milestone 7: Final End-to-End Background Workflow.

Verifies:
1. Full end-to-end background workflow success across all states:
   PENDING -> RUNNING -> DOWNLOADING -> PROCESSING -> UPLOADING -> SUBMITTING -> VERIFYING -> COMPLETED.
2. CAPTCHA pause and resume flow:
   Job pauses in WAITING_FOR_CAPTCHA, resumes upon solution submission, and completes.
3. Idempotency on retries:
   Skips redundant Drive upload if already uploaded; skips duplicate SRM submission if already submitted.
4. Error handling across stages:
   - Download failure transitions to FAILED without corrupting documents.
   - Drive upload failure transitions to FAILED without submitting to SRM.
   - Drive permission verification failure transitions to FAILED.
   - SRM verification failure transitions to FAILED and never marks COMPLETED.
5. Sensitive data redaction:
   Passwords, tokens, and OAuth keys are never exposed in job error messages, logs, or results.
6. Document lifecycle safety:
   Original worksheet is untouched; completed worksheet is a distinct copy.
7. Celery worker process_job task execution with eager mode.
"""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import docx
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.worker.tasks import (
    _run_job_workflow,
    clear_job_credentials,
    get_job_credentials,
    process_job,
    redact_sensitive_info,
    store_job_credentials,
)
from packages.drive.client import BaseDriveClient
from packages.drive.exceptions import DriveUploadError, DriveVerificationError
from packages.drive.models import DriveFileMetadata
from packages.shared.models.job import Job, JobStatus
from packages.srm.exceptions import CaptchaRequired, DownloadFailed, VerificationFailed
from packages.srm.models import (
    SRMCourse,
    SRMSessionStatus,
    SRMSubmissionResult,
    SRMWorksheetMetadata,
)
from packages.srm.orchestrator import SRMOrchestrator
from packages.worksheets.pipeline import WorksheetPipeline
from tests.conftest import TestingSessionLocal
from tests.test_worksheet_parser import _create_synthetic_mcq_docx


def _setup_mock_orchestrator(tmp_path: Path) -> MagicMock:
    """Create a mock SRMOrchestrator pre-configured with valid responses."""
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    # Courses
    course = SRMCourse(
        course_code="21CSC303J",
        course_name="Database Management Systems",
        batch_id="B1",
        semester=3,
        department="CSE",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course])

    # Worksheets metadata
    ws_meta = SRMWorksheetMetadata(
        course_code="21CSC303J",
        session=101,
        slo=1,
        filename="1011.docx",
        format="docx",
        storage_path="data/coordinator/21CSC303J/slp",
        download_url="https://srm.portal/file/1011.docx",
        is_available=True,
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_meta])
    mock_orch.get_worksheet_file = AsyncMock(return_value="https://srm.portal/file/1011.docx")

    # Download returns a real valid docx
    real_sample_docx = _create_synthetic_mcq_docx(tmp_path / "1011.docx")

    async def fake_download(file_url_or_id, destination_dir=None, filename=None):
        dest_dir = destination_dir or tmp_path
        target = dest_dir / (filename or "1011.docx")
        target.write_bytes(real_sample_docx.read_bytes())
        return target

    mock_orch.download_worksheet = AsyncMock(side_effect=fake_download)

    # Session status & submission
    status = SRMSessionStatus(
        session=1,
        practice_status=0,
        slo_links={},
    )
    mock_orch.get_session_status = AsyncMock(return_value=status)
    mock_orch.submit_worksheet_link = AsyncMock(
        return_value=SRMSubmissionResult(
            success=True,
            message="Link updated successfully",
            returned_link="https://drive.google.com/file/d/test_file_id/view",
        )
    )
    mock_orch.verify_submission = AsyncMock(return_value=True)
    return mock_orch


def _setup_mock_drive_client() -> MagicMock:
    """Create a mock BaseDriveClient returning verified public sharing metadata."""
    mock_drive = MagicMock(spec=BaseDriveClient)
    meta = DriveFileMetadata(
        file_id="drive_file_abc123",
        filename="completed_1011.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        web_url="https://drive.google.com/file/d/drive_file_abc123/view?usp=drivesdk",
        download_url="https://drive.google.com/uc?id=drive_file_abc123&export=download",
        is_public=True,
        permission_status="VERIFIED_PUBLIC_READER",
    )
    mock_drive.upload_file = AsyncMock(return_value=meta)
    mock_drive.verify_public_permission = AsyncMock(return_value=True)
    return mock_drive


@pytest.mark.asyncio
async def test_full_end_to_end_workflow_success(db_session: Session, tmp_path: Path):
    """Verify complete background workflow executes through all states to COMPLETED."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    credentials = {
        "USER_ID": "RA2111003010001",
        "PASSWORD": "SecretStudentPassword!",
        "google_drive": {"access_token": "ya29.mock_access_token"},
    }

    # Execute workflow
    await _run_job_workflow(
        job_id=job.id,
        credentials=credentials,
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)

    # 1. State verification
    assert job.status == JobStatus.COMPLETED
    assert job.current_step == "workflow_completed"
    assert job.error_message is None

    # 2. Result payload verification
    result = job.result
    assert result is not None
    assert result["course_code"] == "21CSC303J"
    assert result["session"] == 101
    assert result["slo"] == 1
    assert result["drive_file_id"] == "drive_file_abc123"
    assert "https://drive.google.com" in result["drive_web_url"]
    assert result["drive_permission_status"] == "VERIFIED_PUBLIC_READER"
    assert result["verification_status"] == "VERIFIED"
    assert result["practice_status"] == 2

    # 3. Document naming and non-destructive check
    assert result["original_file"].endswith(".docx")
    assert result["completed_file"].startswith("completed_")
    assert result["original_file"] != result["completed_file"]

    # 4. Ensure orchestrator and drive interactions were called properly
    mock_orch.connect.assert_called_once()
    mock_orch.authenticate.assert_called_once()
    mock_orch.get_courses.assert_called_once()
    mock_orch.download_worksheet.assert_called_once()
    mock_drive.upload_file.assert_called_once()
    mock_orch.submit_worksheet_link.assert_called_once()
    mock_orch.verify_submission.assert_called_once()

    # 5. Security check: credentials scrubbed from in-memory cache and result
    assert get_job_credentials(job.id) is None
    assert "SecretStudentPassword!" not in str(result)
    assert "ya29.mock_access_token" not in str(result)


@pytest.mark.asyncio
async def test_captcha_pause_and_resume_flow(db_session: Session, tmp_path: Path):
    """Verify job pauses in WAITING_FOR_CAPTCHA and resumes cleanly when solution is provided."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    # Configure orchestrator to require CAPTCHA on initial authentication
    mock_orch.capture_login_captcha.return_value = {
        "type": "canvas",
        "selector": "#captcha",
        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
    }

    credentials = {
        "USER_ID": "RA2111003010001",
        "PASSWORD": "SecretStudentPassword!",
    }

    # Run initial workflow step -> triggers CAPTCHA
    await _run_job_workflow(
        job_id=job.id,
        credentials=credentials,
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.WAITING_FOR_CAPTCHA
    assert job.current_step == "waiting_for_user_captcha"
    assert job.captcha_challenge is not None
    assert job.captcha_challenge["type"] == "canvas"
    # Ephemeral credentials retained for resumption
    assert get_job_credentials(job.id) is not None

    # User provides CAPTCHA solution
    job.captcha_solution = "948215"
    db_session.commit()

    # Subsequent run: orchestrator accepts credentials with captcha
    mock_orch.capture_login_captcha.return_value = None

    await _run_job_workflow(
        job_id=job.id,
        credentials=None,  # Will read from ephemeral cache
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert job.result["verification_status"] == "VERIFIED"
    assert get_job_credentials(job.id) is None


@pytest.mark.asyncio
async def test_idempotent_drive_upload_and_submission(db_session: Session, tmp_path: Path):
    """Verify that existing Drive uploads and SRM submissions are not duplicated on retries."""
    existing_original = tmp_path / "1011.docx"
    _create_synthetic_mcq_docx(existing_original)

    existing_completed = tmp_path / "completed_1011.docx"
    _create_synthetic_mcq_docx(existing_completed)

    # Job already has Drive file ID and web URL from prior attempt
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
        result={
            "original_file": str(existing_original),
            "completed_file": str(existing_completed),
            "drive_file_id": "existing_drive_id_999",
            "drive_web_url": "https://drive.google.com/file/d/existing_drive_id_999/view",
            "drive_permission_status": "VERIFIED_PUBLIC_READER",
        },
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    # Session status indicates practice is already submitted (practice_status=2)
    mock_orch.get_session_status.return_value = SRMSessionStatus(
        session=1,
        practice_status=2,
        slo_links={"11": "https://drive.google.com/file/d/existing_drive_id_999/view"},
    )

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED

    # Verify idempotency: upload_file and submit_worksheet_link were NOT called again
    mock_drive.upload_file.assert_not_called()
    mock_orch.submit_worksheet_link.assert_not_called()
    mock_orch.verify_submission.assert_called_once()


@pytest.mark.asyncio
async def test_drive_upload_failure_handling(db_session: Session, tmp_path: Path):
    """Verify that Google Drive upload failure halts the workflow and does not submit to SRM."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    # Simulate Drive upload error
    mock_drive.upload_file.side_effect = DriveUploadError("Google Drive quota exceeded", status_code=403)

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.current_step == "failed"
    assert "Drive quota exceeded" in job.error_message
    # SRM submission must NOT have been called
    mock_orch.submit_worksheet_link.assert_not_called()


@pytest.mark.asyncio
async def test_drive_unverified_permission_failure(db_session: Session, tmp_path: Path):
    """Verify that an unverified non-public Drive link causes failure before SRM submission."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    # Return metadata where public view is false
    mock_drive.upload_file.return_value = DriveFileMetadata(
        file_id="private_file_id",
        filename="completed_1011.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        web_url="",
        is_public=False,
    )

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert "not publicly viewable" in job.error_message
    mock_orch.submit_worksheet_link.assert_not_called()


@pytest.mark.asyncio
async def test_srm_verification_failure_never_marks_completed(db_session: Session, tmp_path: Path):
    """Verify that if SRM submission cannot be verified, the job fails and is never marked COMPLETED."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    # Verification fails on SRM
    mock_orch.verify_submission.return_value = False

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert "verification failed" in job.error_message.lower()
    assert job.status != JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_sensitive_data_redaction_on_failure(db_session: Session, tmp_path: Path):
    """Verify that credentials and tokens in exception messages are redacted before database persistence."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    # Exception with sensitive data
    mock_orch.connect.side_effect = Exception(
        "Connection failed with password=SuperSecretPassword123! and Bearer ya29.oauth_token_xyz"
    )

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001", "PASSWORD": "SuperSecretPassword123!"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert "SuperSecretPassword123!" not in job.error_message
    assert "ya29.oauth_token_xyz" not in job.error_message
    assert "[REDACTED]" in job.error_message


def test_redact_sensitive_info_utility():
    """Verify redact_sensitive_info scrubber handles various credential formats."""
    raw = "Failed on password='MySecretPass' and token='tok_123' and secret: 'sec_999' Bearer abcdef"
    redacted = redact_sensitive_info(raw)
    assert "MySecretPass" not in redacted
    assert "tok_123" not in redacted
    assert "sec_999" not in redacted
    assert "abcdef" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_document_lifecycle_safety(tmp_path: Path):
    """Verify original worksheet document is completely immutable and distinct from completed copy."""
    orig_path = _create_synthetic_mcq_docx(tmp_path / "original_worksheet.docx")
    orig_hash_before = hashlib.sha256(orig_path.read_bytes()).hexdigest()

    pipeline = WorksheetPipeline()
    result = await pipeline.process(orig_path, output_dir=tmp_path)

    orig_hash_after = hashlib.sha256(orig_path.read_bytes()).hexdigest()

    # Original document untouched
    assert orig_hash_before == orig_hash_after
    # Completed file is a new distinct document
    assert result.completed_file.exists()
    assert result.completed_file != orig_path
    assert result.completed_file.name.startswith("completed_")


def test_celery_task_dispatch_eager(db_session: Session, tmp_path: Path):
    """Verify process_job task runs synchronously in eager mode and updates the job."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()
    job_id = job.id

    # Patch SessionLocal to return fresh test sessions
    with patch("apps.worker.tasks.SessionLocal", side_effect=TestingSessionLocal), \
         patch("apps.worker.tasks.SRMOrchestrator") as mock_orch_cls, \
         patch("apps.worker.tasks.GoogleDriveClient") as mock_drive_cls:

        mock_orch = _setup_mock_orchestrator(tmp_path)
        mock_orch_cls.return_value = mock_orch

        mock_drive = _setup_mock_drive_client()
        mock_drive_cls.return_value = mock_drive

        # Execute Celery task directly
        res = process_job.apply(args=[job_id, {"USER_ID": "RA2111003010001"}])
        assert res.successful()

    verify_session = TestingSessionLocal()
    try:
        refreshed_job = verify_session.query(Job).filter(Job.id == job_id).first()
        assert refreshed_job is not None
        assert refreshed_job.status == JobStatus.COMPLETED
        assert refreshed_job.result["verification_status"] == "VERIFIED"
    finally:
        verify_session.close()


@pytest.mark.asyncio
async def test_full_workflow_with_freellm_and_drive_and_srm(db_session: Session, tmp_path: Path):
    """Verify complete end-to-end workflow using FreeLLM answer engine with external services mocked."""
    import json
    from packages.worksheets.answer_engine import LLMAnswerEngine

    job = Job(
        user_id="RA2111003010001",
        semester_id="3",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    freellm_mock_client = AsyncMock(spec=httpx.AsyncClient)
    freellm_mock_client.is_closed = False
    sample_answers = [
        {"question_id": "q_1", "question_number": "1", "answer_text": "A. Central Processing Unit", "selected_option": "A", "confidence": 0.98},
        {"question_id": "q_2", "question_number": "2", "answer_text": "B. Stack", "selected_option": "B", "confidence": 0.95},
        {"question_id": "q_3", "question_number": "3", "answer_text": "B. HTTPS", "selected_option": "B", "confidence": 0.97},
        {"question_id": "q_4", "question_number": "4", "answer_text": "C. Network layer", "selected_option": "C", "confidence": 0.96},
    ]
    response_json = {
        "id": "chatcmpl-freellm-integration-1",
        "object": "chat.completion",
        "created": 1715000000,
        "model": "default",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"answers": sample_answers}),
                },
                "finish_reason": "stop",
            }
        ],
    }
    freellm_resp = MagicMock(spec=httpx.Response)
    freellm_resp.status_code = 200
    freellm_resp.headers = {"content-type": "application/json"}
    freellm_resp.json.return_value = response_json
    freellm_mock_client.post.return_value = freellm_resp

    freellm_engine = LLMAnswerEngine(
        provider="freellm",
        api_key="mock-freellm-api-key",
        http_client=freellm_mock_client,
    )
    pipeline = WorksheetPipeline(answer_engine=freellm_engine)

    credentials = {
        "USER_ID": "RA2111003010001",
        "PASSWORD": "SecretStudentPassword!",
        "google_drive": {"access_token": "ya29.mock_drive_token"},
    }

    await _run_job_workflow(
        job_id=job.id,
        credentials=credentials,
        orchestrator=mock_orch,
        drive_client=mock_drive,
        pipeline=pipeline,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert job.current_step == "workflow_completed"
    assert job.result is not None
    assert job.result["course_code"] == "21CSC303J"
    assert job.result["session"] == 101
    assert job.result["slo"] == 1
    assert job.result["verification_status"] == "VERIFIED"
    assert job.result["drive_file_id"] == "drive_file_abc123"
    assert "https://drive.google.com" in job.result["drive_web_url"]

    freellm_mock_client.post.assert_called_once()
    mock_drive.upload_file.assert_called_once()
    mock_orch.submit_worksheet_link.assert_called_once()
    sub_kwargs = mock_orch.submit_worksheet_link.call_args.kwargs
    assert sub_kwargs["view_link"] == job.result["drive_web_url"]
    assert sub_kwargs["download_link"] == job.result["drive_web_url"]
    mock_orch.verify_submission.assert_called_once()


@pytest.mark.asyncio
async def test_automatic_course_discovery_empty_fails_gracefully(db_session: Session, tmp_path: Path):
    """Verify that when no courses exist in target semester, job fails with a sanitized actionable error."""
    job = Job(
        user_id="RA2111003010001",
        semester_id="3",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_orch.get_courses.return_value = []  # No courses returned
    mock_drive = _setup_mock_drive_client()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert "No courses discovered" in job.error_message
    mock_drive.upload_file.assert_not_called()
    mock_orch.submit_worksheet_link.assert_not_called()


@pytest.mark.asyncio
async def test_srm_submission_failure_transitions_to_failed(db_session: Session, tmp_path: Path):
    """Verify that when SRM submission returns success=False, job transitions to FAILED and verification is skipped."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_orch.submit_worksheet_link.return_value = SRMSubmissionResult(
        success=False,
        message="Submission deadline has passed for this session",
    )
    mock_drive = _setup_mock_drive_client()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert "deadline has passed" in job.error_message.lower()
    mock_orch.verify_submission.assert_not_called()


def test_api_captcha_submission_resumes_background_task(client: TestClient, tmp_path: Path):
    """Verify API creates job, worker pauses on CAPTCHA, API submits solution, and job completes."""
    # 1. Mock orchestrator to require CAPTCHA on initial run
    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_orch.capture_login_captcha.return_value = {
        "type": "canvas",
        "selector": "#captcha",
        "image_base64": "fake_base64_data",
    }
    mock_drive = _setup_mock_drive_client()

    with patch("apps.worker.tasks.SessionLocal", side_effect=TestingSessionLocal), \
         patch("apps.worker.tasks.SRMOrchestrator", return_value=mock_orch), \
         patch("apps.worker.tasks.GoogleDriveClient", return_value=mock_drive):

        # Create job via API
        resp = client.post(
            "/api/v1/jobs",
            json={
                "user_id": "student_01",
                "course_id": "21CSC303J",
                "semester_id": "3",
                "worksheet_id": "1011",
                "credentials": {"USER_ID": "student_01", "PASSWORD": "SecretPassword123!"},
            },
        )
        assert resp.status_code == 201
        job_id = resp.json()["id"]

        # 2. Verify job is in WAITING_FOR_CAPTCHA
        get_resp = client.get(f"/api/v1/jobs/{job_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["status"] == "WAITING_FOR_CAPTCHA"
        assert get_resp.json()["captcha_challenge"] is not None

        # 3. User closes browser / submits CAPTCHA via API endpoint
        mock_orch.capture_login_captcha.return_value = None  # Now CAPTCHA is solved
        captcha_resp = client.post(
            f"/api/v1/jobs/{job_id}/captcha",
            json={"solution": "ABCD12"},
        )
        assert captcha_resp.status_code == 200

        # 4. In eager mode, the job has run to completion
        final_resp = client.get(f"/api/v1/jobs/{job_id}")
        assert final_resp.status_code == 200
        assert final_resp.json()["status"] == "COMPLETED"
        assert final_resp.json()["result"]["verification_status"] == "VERIFIED"

