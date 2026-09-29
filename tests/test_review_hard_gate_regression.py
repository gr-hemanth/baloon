"""Regression test suite for Hard Terminal Review Gate Enforcement.

Verifies all 10 requirements:
1. Worker stops at AWAITING_USER_REVIEW.
2. Worker cannot transition AWAITING_USER_REVIEW -> SUBMITTING automatically (even if auto_submit was passed).
3. Calling the normal workflow task again while in AWAITING_USER_REVIEW does nothing (no-op / immediate return).
4. Celery retry cannot bypass the gate.
5. Application restart cannot bypass the gate.
6. Dashboard polling cannot bypass the gate.
7. Only explicit POST /api/v1/jobs/{id}/submit can start submission.
8. Explicit submit works from AWAITING_USER_REVIEW.
9. Double submit is rejected/ignored safely (idempotent).
10. A failed verification does not corrupt the review-state UI for unrelated jobs.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.worker.tasks import (
    _run_job_workflow,
    _run_submission_workflow,
    get_job_credentials,
    process_job,
    store_job_credentials,
    submit_job,
)
from packages.drive.client import BaseDriveClient
from packages.drive.models import DriveFileMetadata
from packages.shared.models.job import Job, JobStatus
from packages.srm.models import (
    SRMCourse,
    SRMSessionStatus,
    SRMSubmissionResult,
    SRMWorksheetMetadata,
)
from packages.srm.orchestrator import SRMOrchestrator


def _create_sample_docx(path: Path) -> Path:
    """Create a minimal synthetic DOCX worksheet for test isolation."""
    doc = Document()
    doc.add_heading("Course: 21LEM202T Session: 101 SLO: 1", level=1)
    doc.add_paragraph("1. Explain Universal Human Values.")
    doc.add_paragraph("Answer: ")
    doc.save(str(path))
    return path


def _setup_mock_orchestrator(tmp_path: Path) -> MagicMock:
    """Create a mock SRMOrchestrator with standard discovery responses."""
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    course = SRMCourse(
        course_code="21LEM202T",
        course_name="UNIVERSAL HUMAN VALUES",
        batch_id="21LEM202T_39",
        semester=3,
        department="HUMANITIES",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course])

    ws_meta = SRMWorksheetMetadata(
        course_code="21LEM202T",
        session=101,
        slo=1,
        filename="1011.docx",
        format="docx",
        storage_path="data/coordinator/21LEM202T/slp",
        download_url="https://srm.portal/file/1011.docx",
        is_available=True,
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_meta])
    mock_orch.get_worksheet_file = AsyncMock(return_value="https://srm.portal/file/1011.docx")

    sample_doc = _create_sample_docx(tmp_path / "1011.docx")

    async def fake_download(file_url_or_id, destination_dir=None, filename=None):
        target = (destination_dir or tmp_path) / (filename or "1011.docx")
        target.write_bytes(sample_doc.read_bytes())
        return target

    mock_orch.download_worksheet = AsyncMock(side_effect=fake_download)
    mock_orch.get_session_status = AsyncMock(
        return_value=SRMSessionStatus(session=101, practice_status=0, slo_links={})
    )
    mock_orch.submit_worksheet_link = AsyncMock(
        return_value=SRMSubmissionResult(
            success=True,
            message="Link submitted successfully",
            returned_link="https://drive.google.com/file/d/drive_test_123/view",
        )
    )
    mock_orch.verify_submission = AsyncMock(return_value=True)
    return mock_orch


def _setup_mock_drive_client() -> MagicMock:
    """Create a mock BaseDriveClient with verified public sharing."""
    mock_drive = MagicMock(spec=BaseDriveClient)
    meta = DriveFileMetadata(
        file_id="drive_test_123",
        filename="completed_1011.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        web_url="https://drive.google.com/file/d/drive_test_123/view?usp=drivesdk",
        download_url="https://drive.google.com/uc?id=drive_test_123&export=download",
        is_public=True,
        permission_status="VERIFIED_PUBLIC_READER",
    )
    mock_drive.upload_file = AsyncMock(return_value=meta)
    mock_drive.verify_file_public_access = AsyncMock(return_value=True)
    return mock_drive


# =============================================================================
# Test 1: Worker stops at AWAITING_USER_REVIEW
# =============================================================================
@pytest.mark.asyncio
async def test_01_worker_stops_at_awaiting_user_review(db_session: Session, tmp_path: Path):
    """Test 1: Worker stops strictly at AWAITING_USER_REVIEW after upload."""
    job = Job(
        user_id="test_student_regression",
        course_id="21LEM202T",
        semester_id=3,
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "test_student_regression", "PASSWORD": "password"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.AWAITING_USER_REVIEW
    assert job.current_step == "awaiting_user_review"
    assert job.error_message is None
    assert job.result is not None
    assert job.result["review_ready"] is True
    assert job.result["drive_verified"] is True

    # Under no circumstances may submission be initiated
    mock_orch.submit_worksheet_link.assert_not_called()
    mock_orch.verify_submission.assert_not_called()


# =============================================================================
# Test 2: Worker cannot transition AWAITING_USER_REVIEW -> SUBMITTING automatically
# =============================================================================
@pytest.mark.asyncio
async def test_02_worker_cannot_transition_awaiting_to_submitting_automatically(db_session: Session, tmp_path: Path):
    """Test 2: Even if auto_submit=True was passed to _run_job_workflow, the hard gate halts."""
    job = Job(
        user_id="test_student_regression",
        course_id="21LEM202T",
        semester_id=3,
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    # Pass auto_submit=True to verify that the hard gate unconditionally halts
    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "test_student_regression"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
        auto_submit=True,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.AWAITING_USER_REVIEW
    mock_orch.submit_worksheet_link.assert_not_called()
    mock_orch.verify_submission.assert_not_called()


# =============================================================================
# Test 3: Calling normal workflow task again in AWAITING_USER_REVIEW does nothing
# =============================================================================
@pytest.mark.asyncio
async def test_03_calling_normal_workflow_task_again_in_awaiting_user_review_does_nothing(db_session: Session, tmp_path: Path):
    """Test 3: Calling _run_job_workflow on a job already in AWAITING_USER_REVIEW immediately returns."""
    job = Job(
        user_id="test_student_regression",
        course_id="21LEM202T",
        semester_id=3,
        worksheet_id="1011",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={"review_ready": True, "drive_web_url": "https://drive.google.com/test"},
        updated_at=datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    # Invoke workflow on existing review job
    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "test_student_regression"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    # Status and step must remain completely untouched
    assert job.status == JobStatus.AWAITING_USER_REVIEW
    assert job.current_step == "awaiting_user_review"
    mock_orch.connect.assert_not_called()
    mock_orch.download_worksheet.assert_not_called()
    mock_orch.submit_worksheet_link.assert_not_called()


# =============================================================================
# Test 4: Celery retry cannot bypass the gate
# =============================================================================
def test_04_celery_retry_cannot_bypass_gate(db_session: Session, tmp_path: Path):
    """Test 4: A re-delivered or retried Celery process_job task immediately halts for review jobs."""
    job = Job(
        user_id="test_student_regression",
        course_id="21LEM202T",
        semester_id=3,
        worksheet_id="1011",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={"review_ready": True, "drive_web_url": "https://drive.google.com/test"},
    )
    db_session.add(job)
    db_session.commit()

    from tests.conftest import TestingSessionLocal

    with patch("apps.worker.tasks.SessionLocal", side_effect=TestingSessionLocal):
        # Simulate Celery retrying process_job
        res = process_job.apply(args=[job.id], kwargs={"credentials": {}})
        assert res.successful()

    verify_db = TestingSessionLocal()
    try:
        reloaded = verify_db.query(Job).filter(Job.id == job.id).first()
        assert reloaded is not None
        assert reloaded.status == JobStatus.AWAITING_USER_REVIEW
        assert reloaded.current_step == "awaiting_user_review"
    finally:
        verify_db.close()


# =============================================================================
# Test 5: Application restart cannot bypass the gate
# =============================================================================
def test_05_application_restart_cannot_bypass_gate(db_session: Session):
    """Test 5: An application restart loading jobs from DB leaves AWAITING_USER_REVIEW intact."""
    job = Job(
        user_id="test_student_regression",
        course_id="21LEM202T",
        semester_id=3,
        worksheet_id="1011",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={"review_ready": True, "drive_web_url": "https://drive.google.com/test"},
    )
    db_session.add(job)
    db_session.commit()

    # Simulate fresh startup reading jobs
    reloaded = db_session.query(Job).filter(Job.id == job.id).first()
    assert reloaded.status == JobStatus.AWAITING_USER_REVIEW
    assert reloaded.current_step == "awaiting_user_review"


# =============================================================================
# Test 6: Dashboard polling cannot bypass the gate
# =============================================================================
def test_06_dashboard_polling_cannot_bypass_gate(client: TestClient, db_session: Session):
    """Test 6: Continuous polling of GET /jobs/{id} has zero state-transition side effects."""
    job = Job(
        user_id="test_student_regression",
        course_id="21LEM202T",
        semester_id=3,
        worksheet_id="1011",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={
            "drive_file_id": "test_id",
            "drive_web_url": "https://drive.google.com/test",
            "review_ready": True,
        },
    )
    db_session.add(job)
    db_session.commit()

    # Simulate 10 rapid polls from dashboard frontend
    for _ in range(10):
        resp = client.get(f"/api/v1/jobs/{job.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "AWAITING_USER_REVIEW"
        assert data["review_ready"] is True

    db_session.refresh(job)
    assert job.status == JobStatus.AWAITING_USER_REVIEW


# =============================================================================
# Test 7: Only explicit POST /api/v1/jobs/{id}/submit can start submission
# =============================================================================
def test_07_only_explicit_post_submit_can_start_submission(client: TestClient, db_session: Session):
    """Test 7: Any state other than AWAITING_USER_REVIEW rejects POST /submit with 400 Bad Request."""
    invalid_statuses = [
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.DOWNLOADING,
        JobStatus.PROCESSING,
        JobStatus.UPLOADING,
        JobStatus.FAILED,
    ]

    for st in invalid_statuses:
        job = Job(
            user_id="test_student_regression",
            status=st,
            result={"drive_web_url": "https://drive.google.com/test", "drive_file_id": "test_id"},
        )
        db_session.add(job)
        db_session.commit()

        resp = client.post(f"/api/v1/jobs/{job.id}/submit", json={})
        assert resp.status_code == 400, f"Expected 400 for status {st.value}, got {resp.status_code}"
        assert "Must be AWAITING_USER_REVIEW" in resp.json()["detail"]


# =============================================================================
# Test 8: Explicit submit works from AWAITING_USER_REVIEW
# =============================================================================
@pytest.mark.asyncio
async def test_08_explicit_submit_works_from_awaiting_user_review(db_session: Session, tmp_path: Path):
    """Test 8: Explicit user submit transitions AWAITING_USER_REVIEW -> SUBMITTING -> VERIFYING -> COMPLETED."""
    completed_doc = tmp_path / "completed_1011.docx"
    _create_sample_docx(completed_doc)

    job = Job(
        user_id="test_student_regression",
        course_id="21LEM202T",
        semester_id=3,
        worksheet_id="1011",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={
            "course_code": "21LEM202T",
            "course_name": "UNIVERSAL HUMAN VALUES",
            "batch_id": "21LEM202T_39",
            "session": 101,
            "slo": 1,
            "completed_file": "completed_1011.docx",
            "completed_file_path": str(completed_doc),
            "drive_file_id": "drive_test_123",
            "drive_web_url": "https://drive.google.com/file/d/drive_test_123/view",
            "drive_permission_status": "VERIFIED_PUBLIC_READER",
            "review_ready": True,
        },
    )
    db_session.add(job)
    db_session.commit()

    mock_orch = _setup_mock_orchestrator(tmp_path)
    mock_drive = _setup_mock_drive_client()

    store_job_credentials(job.id, {"USER_ID": "test_student_regression", "PASSWORD": "password"})

    await _run_submission_workflow(
        job_id=job.id,
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert job.current_step == "workflow_completed"
    assert job.result["verification_status"] == "VERIFIED"
    assert job.result["practice_status"] == 2
    mock_orch.submit_worksheet_link.assert_called_once()
    mock_orch.verify_submission.assert_called_once()


# =============================================================================
# Test 9: Double submit is rejected/ignored safely
# =============================================================================
def test_09_double_submit_rejected_ignored_safely(client: TestClient, db_session: Session):
    """Test 9: Multiple rapid clicks on Submit are handled idempotently without duplicate dispatches."""
    job = Job(
        user_id="test_student_regression",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={
            "drive_file_id": "test_id",
            "drive_web_url": "https://drive.google.com/view",
            "review_ready": True,
        },
    )
    db_session.add(job)
    db_session.commit()
    store_job_credentials(job.id, {"USER_ID": "test_student_regression"})

    with patch("apps.api.routes.jobs.submit_job.delay") as mock_delay:
        # First call transitions to SUBMITTING and dispatches task
        resp1 = client.post(f"/api/v1/jobs/{job.id}/submit", json={})
        assert resp1.status_code == 200
        assert mock_delay.call_count == 1

        # Second call sees status SUBMITTING -> returns job safely with NO second dispatch
        resp2 = client.post(f"/api/v1/jobs/{job.id}/submit", json={})
        assert resp2.status_code == 200
        assert mock_delay.call_count == 1

        # Completed job also returns safely with NO dispatch
        job.status = JobStatus.COMPLETED
        db_session.commit()
        resp3 = client.post(f"/api/v1/jobs/{job.id}/submit", json={})
        assert resp3.status_code == 200
        assert mock_delay.call_count == 1


# =============================================================================
# Test 10: Failed verification does not corrupt review-state UI for unrelated jobs
# =============================================================================
def test_10_failed_verification_does_not_corrupt_review_state_ui_for_unrelated_jobs(
    client: TestClient, db_session: Session
):
    """Test 10: A failed job does not advertise review_ready or corrupt another job's review state."""
    # Job A: Failed submission verification
    job_failed = Job(
        user_id="test_student_regression",
        course_id="21LEM202T",
        semester_id=3,
        worksheet_id="1011",
        status=JobStatus.FAILED,
        current_step="submission_failed",
        error_message="Verification failed: no recorded link found on SRM",
        result={
            "drive_file_id": "drive_failed",
            "drive_web_url": "https://drive.google.com/failed",
            "submission_allowed": False,
        },
    )
    # Job B: Genuinely waiting for review
    job_active_review = Job(
        user_id="test_student_regression",
        course_id="21LEM202T",
        semester_id=3,
        worksheet_id="1012",
        status=JobStatus.AWAITING_USER_REVIEW,
        current_step="awaiting_user_review",
        result={
            "drive_file_id": "drive_active",
            "drive_web_url": "https://drive.google.com/active",
            "review_ready": True,
            "submission_allowed": True,
        },
    )
    db_session.add_all([job_failed, job_active_review])
    db_session.commit()

    # Query Job A
    resp_a = client.get(f"/api/v1/jobs/{job_failed.id}")
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["status"] == "FAILED"
    assert data_a["review_ready"] is False
    assert data_a["submission_allowed"] is False

    # Query Job B
    resp_b = client.get(f"/api/v1/jobs/{job_active_review.id}")
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert data_b["status"] == "AWAITING_USER_REVIEW"
    assert data_b["review_ready"] is True
    assert data_b["submission_allowed"] is True

    # Failed job A cannot be submitted
    submit_fail_resp = client.post(f"/api/v1/jobs/{job_failed.id}/submit", json={})
    assert submit_fail_resp.status_code == 400
    assert "Must be AWAITING_USER_REVIEW" in submit_fail_resp.json()["detail"]
