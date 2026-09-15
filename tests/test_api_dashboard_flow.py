"""Integration tests for the user-facing application layer & dashboard API.

Verifies:
1. Production dashboard HTML serving at GET /dashboard.
2. Google Drive OAuth endpoints (/auth/google/status, /auth/google/url, /auth/google/callback).
3. SRM course and worksheet discovery endpoint (/srm/discover) for Semester 3.
4. CAPTCHA detection during discovery and job execution.
5. Duplicate job prevention (HTTP 409 Conflict when active job exists).
6. Complete user flow: discovery -> job creation -> CAPTCHA modal -> resumption -> completion.
7. Zero-persistence guarantee: SRM passwords and OAuth secrets are never saved in DB.
8. Safe testing: Zero real submissions to SRM portal.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from packages.drive.client import GoogleDriveClient
from packages.drive.models import DriveFileMetadata
from packages.shared.models.job import Job, JobStatus
from packages.srm.exceptions import AuthenticationFailed, CaptchaRequired
from packages.srm.models import (
    SRMCourse,
    SRMSessionStatus,
    SRMSubmissionResult,
    SRMWorksheetMetadata,
)
from packages.srm.orchestrator import SRMOrchestrator
from tests.conftest import TestingSessionLocal


def _setup_mock_srm_orchestrator():
    """Create mock SRMOrchestrator returning Semester 3 courses and worksheets."""
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    course_sem3 = SRMCourse(
        course_code="21CSC303J",
        course_name="Software Engineering and Architecture",
        batch_id="B1",
        semester=3,
        department="CSE",
    )
    course_sem4 = SRMCourse(
        course_code="21CSC304J",
        course_name="Computer Networks",
        batch_id="B1",
        semester=4,
        department="CSE",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course_sem3, course_sem4])
    mock_orch.get_courses_by_semester = AsyncMock(return_value=[course_sem3])

    ws_meta = SRMWorksheetMetadata(
        course_code="21CSC303J",
        session=101,
        slo=1,
        filename="1011.docx",
        format="docx",
        storage_path="data/coordinator/21CSC303J/slp",
        download_url="https://srm.portal/uploads/1011.docx",
        is_available=True,
        submission_status="NOT_SUBMITTED",
        title="Unit 1 Session 1 SLO 1",
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_meta])
    mock_orch.get_worksheet_file = AsyncMock(return_value="https://srm.portal/uploads/1011.docx")

    # Download returns a valid temp docx
    async def fake_download(file_url_or_id, destination_dir=None, filename=None):
        from docx import Document
        target = Path(destination_dir or ".") / (filename or "1011.docx")
        doc = Document()
        doc.add_heading("21CSC303J Session 101", level=1)
        doc.add_paragraph("Explain the fundamental architectural principles.")
        doc.save(str(target))
        return target

    mock_orch.download_worksheet = AsyncMock(side_effect=fake_download)
    mock_orch.get_session_status = AsyncMock(
        return_value=SRMSessionStatus(session=101, practice_status={"1011": 0}, slo_links={})
    )
    mock_orch.submit_worksheet_link = AsyncMock(
        return_value=SRMSubmissionResult(
            success=True,
            message="Submitted successfully",
            returned_link="https://docs.google.com/document/d/mock123/edit",
        )
    )
    mock_orch.verify_submission = AsyncMock(return_value=True)
    return mock_orch


def _setup_mock_drive_client():
    """Create mock GoogleDriveClient returning verified public sharing metadata."""
    mock_drive = MagicMock(spec=GoogleDriveClient)
    meta = DriveFileMetadata(
        file_id="drive_mock_file_999",
        filename="completed_1011.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        web_url="https://docs.google.com/document/d/drive_mock_file_999/edit?usp=drivesdk",
        download_url="https://drive.google.com/uc?id=drive_mock_file_999&export=download",
        is_public=True,
        permission_status="VERIFIED_PUBLIC_READER",
    )
    mock_drive.upload_file = AsyncMock(return_value=meta)
    mock_drive.verify_public_permission = AsyncMock(return_value=True)
    mock_drive.close = AsyncMock(return_value=None)
    return mock_drive


def test_dashboard_page_serves_html(client: TestClient):
    """Verify GET /dashboard serves the production single-page application."""
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "SRM eCurricula Automator" in resp.text
    assert "Live Job Execution Pipeline" in resp.text
    assert "WAITING_FOR_CAPTCHA" in resp.text


def test_drive_oauth_status_endpoint(client: TestClient):
    """Verify GET /api/v1/auth/google/status returns connection metadata without exposing secrets."""
    resp = client.get("/api/v1/auth/google/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "connected" in data
    assert "client_configured" in data
    assert "redirect_uri" in data
    # Secrets must NEVER be in the response
    assert "client_secret" not in data
    assert "refresh_token" not in data
    assert "access_token" not in data


def test_drive_oauth_url_endpoint(client: TestClient):
    """Verify GET /api/v1/auth/google/url generates authorization URL."""
    resp = client.get("/api/v1/auth/google/url")
    assert resp.status_code == 200
    data = resp.json()
    assert "authorization_url" in data
    assert "accounts.google.com" in data["authorization_url"]
    assert "client_id=" in data["authorization_url"]


def test_drive_oauth_callback_error_handling(client: TestClient):
    """Verify OAuth callback error parameter returns error HTML without crashing."""
    resp = client.get("/api/v1/auth/google/callback?error=access_denied")
    assert resp.status_code == 400
    assert "Authorization Denied" in resp.text


@pytest.mark.asyncio
async def test_srm_discovery_success_semester_3(client: TestClient):
    """Verify POST /api/v1/srm/discover returns Semester 3 courses and available worksheets."""
    mock_orch = _setup_mock_srm_orchestrator()

    with patch("apps.api.routes.srm.SRMOrchestrator", return_value=mock_orch):
        resp = client.post(
            "/api/v1/srm/discover",
            json={
                "user_id": "RA2111003010001",
                "password": "SecretStudentPassword!",
                "semester": 3,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"
        assert data["semester"] == 3
        assert len(data["courses"]) == 1
        course = data["courses"][0]
        assert course["course_code"] == "21CSC303J"
        assert course["semester"] == 3
        assert len(course["worksheets"]) == 1
        ws = course["worksheets"][0]
        assert ws["filename"] == "1011.docx"
        assert ws["session"] == 101
        assert ws["slo"] == 1
        assert ws["is_available"] is True

        # Security check: Password must not appear in response
        assert "SecretStudentPassword!" not in resp.text


def test_srm_discovery_captcha_challenge_flow(client: TestClient):
    """Verify that when login requires CAPTCHA, endpoint returns WAITING_FOR_CAPTCHA with challenge."""
    mock_orch = _setup_mock_srm_orchestrator()
    mock_orch.capture_login_captcha.return_value = {
        "type": "canvas",
        "image_base64": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
    }

    with patch("apps.api.routes.srm.SRMOrchestrator", return_value=mock_orch):
        resp = client.post(
            "/api/v1/srm/discover",
            json={
                "user_id": "RA2111003010001",
                "password": "SecretStudentPassword!",
                "semester": 3,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "WAITING_FOR_CAPTCHA"
        assert data["captcha_challenge"] is not None
        assert "image_base64" in data["captcha_challenge"]


def test_srm_discovery_invalid_credentials(client: TestClient):
    """Verify invalid credentials return HTTP 401 Unauthorized with sanitized error."""
    mock_orch = _setup_mock_srm_orchestrator()
    mock_orch.authenticate.side_effect = AuthenticationFailed("Invalid user ID or password")

    with patch("apps.api.routes.srm.SRMOrchestrator", return_value=mock_orch):
        resp = client.post(
            "/api/v1/srm/discover",
            json={
                "user_id": "RA2111003010001",
                "password": "WrongPassword!",
                "semester": 3,
            },
        )
        assert resp.status_code == 401
        assert "Authentication failed" in resp.json()["detail"]


def test_duplicate_job_submission_prevented(client: TestClient, db_session: Session):
    """Verify that submitting a duplicate job for an active user/course/worksheet returns 409 Conflict."""
    # Create an existing active job in PENDING state
    active_job = Job(
        user_id="RA2111003010001",
        course_id="21CSC303J",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.RUNNING,
    )
    db_session.add(active_job)
    db_session.commit()

    # Attempt to submit duplicate job
    resp = client.post(
        "/api/v1/jobs",
        json={
            "user_id": "RA2111003010001",
            "course_id": "21CSC303J",
            "semester_id": "3",
            "worksheet_id": "1011",
            "credentials": {"USER_ID": "RA2111003010001", "PASSWORD": "SecretPassword!"},
        },
    )
    assert resp.status_code == 409
    assert "already processing" in resp.json()["detail"].lower()
    assert resp.headers.get("X-Existing-Job-Id") == active_job.id

    # If force=True is set, duplicate check is bypassed
    with patch("apps.worker.tasks.process_job.delay"):
        resp_forced = client.post(
            "/api/v1/jobs",
            json={
                "user_id": "RA2111003010001",
                "course_id": "21CSC303J",
                "semester_id": "3",
                "worksheet_id": "1011",
                "force": True,
            },
        )
        assert resp_forced.status_code == 201


def test_complete_user_flow_discovery_to_completed_job(client: TestClient, db_session: Session, tmp_path: Path):
    """Verify end-to-end user flow: discover -> create job -> CAPTCHA modal -> resume -> COMPLETED."""
    mock_orch = _setup_mock_srm_orchestrator()
    # Step A: First authentication requires CAPTCHA
    mock_orch.capture_login_captcha.return_value = {
        "type": "canvas",
        "image_base64": "fake_captcha_data",
    }
    mock_drive = _setup_mock_drive_client()

    with patch("apps.worker.tasks.SessionLocal", side_effect=TestingSessionLocal), \
         patch("apps.worker.tasks.SRMOrchestrator", return_value=mock_orch), \
         patch("apps.worker.tasks.GoogleDriveClient", return_value=mock_drive):

        # 1. User dispatches job from UI
        create_resp = client.post(
            "/api/v1/jobs",
            json={
                "user_id": "student_flow_user",
                "course_id": "21CSC303J",
                "semester_id": "3",
                "worksheet_id": "1011",
                "credentials": {"USER_ID": "student_flow_user", "PASSWORD": "SuperSecretPassword123!"},
            },
        )
        assert create_resp.status_code == 201
        job_id = create_resp.json()["id"]

        # 2. Worker paused on CAPTCHA -> UI polls status
        poll_1 = client.get(f"/api/v1/jobs/{job_id}")
        assert poll_1.status_code == 200
        assert poll_1.json()["status"] == "WAITING_FOR_CAPTCHA"
        assert poll_1.json()["captcha_challenge"] is not None

        # 3. User solves CAPTCHA and submits via UI modal
        mock_orch.capture_login_captcha.return_value = None  # Solved
        solve_resp = client.post(
            f"/api/v1/jobs/{job_id}/captcha",
            json={"solution": "49XBC2"},
        )
        assert solve_resp.status_code == 200

        # 4. Job resumes in eager mode and completes
        poll_final = client.get(f"/api/v1/jobs/{job_id}")
        assert poll_final.status_code == 200
        final_data = poll_final.json()
        assert final_data["status"] == "COMPLETED"
        assert final_data["result"] is not None
        assert final_data["result"]["verification_status"] == "VERIFIED"
        assert "drive_web_url" in final_data["result"]
        assert "https://docs.google.com" in final_data["result"]["drive_web_url"]

        # 5. Zero-Persistence Guarantee: Check database record directly
        verify_db = TestingSessionLocal()
        try:
            db_job = verify_db.query(Job).filter(Job.id == job_id).first()
            assert db_job is not None
            # Password must NEVER be anywhere in database
            assert "SuperSecretPassword123!" not in str(db_job.__dict__)
            assert db_job.status == JobStatus.COMPLETED
        finally:
            verify_db.close()
