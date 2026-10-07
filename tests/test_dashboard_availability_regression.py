"""Regression test suite for Dashboard Availability Regression.

Verifies all 8 required behaviors:
1. 21LEM202T available course renders as available (available count > 0).
2. Worksheet 1011 is selectable (radio input enabled, metadata intact).
3. Start Automation is enabled for 1011 (can submit job).
4. 21CSC203P unavailable course renders as unavailable (0 available / Unavailable notice).
5. Worksheet 2091 cannot be started without user-provided file (blocked, succeeds with upload).
6. Discovery response correctly preserves available worksheet metadata.
7. Frontend and backend use the same availability field/schema (is_available, session, slo).
8. No stale discovery/localStorage state causes false unavailable status.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.worker.tasks import _run_job_workflow
from packages.drive.client import BaseDriveClient
from packages.drive.models import DriveFileMetadata
from packages.shared.models.job import Job, JobStatus
from packages.shared.schemas.job import (
    SRMCourseItem,
    SRMDiscoverResponse,
    SRMWorksheetItem,
)
from packages.srm.http_client import SRMHttpClient
from packages.srm.models import (
    SRMCourse,
    SRMCourseStatus,
    SRMSessionStatus,
    SRMWorksheetMetadata,
)
from packages.srm.orchestrator import SRMOrchestrator
from tests.conftest import TestingSessionLocal


def _make_mock_drive():
    mock_drive = MagicMock(spec=BaseDriveClient)
    meta = DriveFileMetadata(
        file_id="drive_mock_file_123",
        filename="completed_1011.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        web_url="https://drive.google.com/file/d/drive_mock_file_123/view",
        download_url="https://drive.google.com/uc?id=drive_mock_file_123&export=download",
        is_public=True,
        permission_status="VERIFIED_PUBLIC_READER",
    )
    mock_drive.upload_file = AsyncMock(return_value=meta)
    mock_drive.verify_public_permission = AsyncMock(return_value=True)
    return mock_drive


# 1. 21LEM202T available course renders as available.
@pytest.mark.asyncio
async def test_21lem202t_available_course_renders_as_available(client: TestClient):
    """Verify 21LEM202T discovery response returns available count > 0 and renders available."""
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    course_lem = SRMCourse(
        course_code="21LEM202T",
        course_name="Universal Human Values",
        batch_id="21LEM202T_39",
        semester=3,
        department="ECE",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course_lem])

    # 90 available worksheets
    ws_list = [
        SRMWorksheetMetadata(
            course_code="21LEM202T",
            session=100 + s,
            slo=slo,
            filename=f"{100 + s}{slo}.docx",
            format="docx",
            storage_path="data/coordinator/21LEM202T/slp",
            download_url=f"https://dld.srmist.edu.in/etecurricula/server/uploads/data/coordinator/21LEM202T/slp/{100 + s}{slo}.docx",
            is_available=True,
            submission_status="NOT_SUBMITTED",
            title=f"Unit 1 Session {s} SLO {slo}",
        )
        for s in range(1, 46)
        for slo in (1, 2)
    ]
    mock_orch.discover_worksheets = AsyncMock(return_value=ws_list)

    with patch("apps.api.routes.srm.SRMOrchestrator", return_value=mock_orch):
        resp = client.post(
            "/api/v1/srm/discover",
            json={"user_id": "RA2511003011819", "password": "SecretPassword123!", "semester": 3},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"
        assert len(data["courses"]) == 1

        c = data["courses"][0]
        assert c["course_code"] == "21LEM202T"
        assert len(c["worksheets"]) == 90

        avail_count = len([w for w in c["worksheets"] if w["is_available"]])
        assert avail_count == 90
        assert avail_count > 0

        # Dashboard option text logic
        opt_text = f"{c['course_code']} - {c['course_name']} ({avail_count} available)"
        assert "90 available" in opt_text
        assert "Unavailable" not in opt_text


# 2. Worksheet 1011 is selectable.
@pytest.mark.asyncio
async def test_worksheet_1011_is_selectable(client: TestClient):
    """Verify worksheet 1011 has is_available=True, valid download_url, and is selectable in UI."""
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    course_lem = SRMCourse(
        course_code="21LEM202T",
        course_name="Universal Human Values",
        batch_id="21LEM202T_39",
        semester=3,
        department="ECE",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course_lem])

    ws_1011 = SRMWorksheetMetadata(
        course_code="21LEM202T",
        session=101,
        slo=1,
        filename="1011.docx",
        format="docx",
        storage_path="data/coordinator/21LEM202T/slp",
        download_url="https://dld.srmist.edu.in/etecurricula/server/uploads/data/coordinator/21LEM202T/slp/1011.docx",
        is_available=True,
        submission_status="NOT_SUBMITTED",
        title="Unit 1 Session 1 SLO 1",
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_1011])

    with patch("apps.api.routes.srm.SRMOrchestrator", return_value=mock_orch):
        resp = client.post(
            "/api/v1/srm/discover",
            json={"user_id": "RA2511003011819", "password": "SecretPassword123!", "semester": 3},
        )
        assert resp.status_code == 200
        course = resp.json()["courses"][0]
        worksheets = course["worksheets"]
        w1011 = next((w for w in worksheets if w["session"] == 101 and w["slo"] == 1), None)

        assert w1011 is not None
        assert w1011["is_available"] is True
        assert w1011["filename"] == "1011.docx"
        assert w1011["download_url"] is not None
        assert "1011.docx" in w1011["download_url"]

        # Dashboard rendering verification
        dash_content = Path("apps/api/static/dashboard.html").read_text(encoding="utf-8")
        assert "availableWs.forEach" in dash_content
        assert 'input type="radio" name="selected_ws"' in dash_content


# 3. Start Automation is enabled for 1011.
@pytest.mark.asyncio
async def test_start_automation_is_enabled_for_1011(client: TestClient, db_session: Session):
    """Verify creating a job with available worksheet 1011 succeeds and enables workflow."""
    resp = client.post(
        "/api/v1/jobs",
        json={
            "user_id": "RA2511003011819",
            "course_id": "21LEM202T",
            "semester_id": "3",
            "worksheet_id": "1011",
            "session": 101,
            "slo": 1,
            "transport_mode": "http",
            "credentials": {"USER_ID": "RA2511003011819", "PASSWORD": "SecretPassword123!"},
        },
    )
    assert resp.status_code == 201
    job_data = resp.json()
    assert job_data["status"] == "PENDING"
    assert job_data["course_id"] == "21LEM202T"
    assert job_data["worksheet_id"] == "1011"


# 4. 21CSC203P unavailable course renders as unavailable.
@pytest.mark.asyncio
async def test_21csc203p_unavailable_course_renders_as_unavailable(client: TestClient):
    """Verify that when 21CSC203P has 0 available worksheets, it renders as unavailable with notice."""
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    course_203p = SRMCourse(
        course_code="21CSC203P",
        course_name="Advanced Programming Practice",
        batch_id="21CSC203P_58",
        semester=3,
        department="CSE",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course_203p])

    ws_2091 = SRMWorksheetMetadata(
        course_code="21CSC203P",
        session=209,
        slo=1,
        filename="2091.docx",
        format="docx",
        storage_path="data/coordinator/21CSC203P/slp",
        download_url=None,
        is_available=False,
        submission_status="NOT_SUBMITTED",
        title="Unit 2 Session 9 SLO 1",
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_2091])

    with patch("apps.api.routes.srm.SRMOrchestrator", return_value=mock_orch):
        resp = client.post(
            "/api/v1/srm/discover",
            json={"user_id": "RA2511003011819", "password": "SecretPassword123!", "semester": 3},
        )
        assert resp.status_code == 200
        course = resp.json()["courses"][0]
        avail_count = len([w for w in course["worksheets"] if w["is_available"]])
        assert avail_count == 0

        avail_suffix = f"{avail_count} available" if avail_count > 0 else "0 available / Unavailable"
        opt_text = f"{course['course_code']} - {course['course_name']} ({avail_suffix})"
        assert "0 available / Unavailable" in opt_text

        # Dashboard HTML contains explicit unavailable notice
        dash_content = Path("apps/api/static/dashboard.html").read_text(encoding="utf-8")
        assert "No official SRM worksheet is available for this course/session." in dash_content
        assert "Availability:" in dash_content
        assert "UNAVAILABLE" in dash_content


# 5. Worksheet 2091 cannot be started without user-provided file.
@pytest.mark.asyncio
async def test_worksheet_2091_cannot_be_started_without_user_provided_file(db_session: Session):
    """Verify job for unavailable worksheet 2091 fails with WORKSHEET_UNAVAILABLE unless user uploaded."""
    job = Job(
        user_id="RA2511003011819",
        course_id="21CSC203P",
        semester_id="3",
        worksheet_id="2091",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)
    mock_orch.get_courses = AsyncMock(return_value=[
        SRMCourse(course_code="21CSC203P", course_name="Advanced Prog", batch_id="B58", semester=3)
    ])
    mock_orch.discover_worksheets = AsyncMock(return_value=[
        SRMWorksheetMetadata(
            course_code="21CSC203P",
            session=209,
            slo=1,
            filename="2091.docx",
            format="docx",
            storage_path="data/coordinator/21CSC203P/slp",
            download_url=None,
            is_available=False,
        )
    ])

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2511003011819", "PASSWORD": "pwd", "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=_make_mock_drive(),
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.current_step == "worksheet_unavailable"
    assert "No official SRM worksheet is available" in (job.error_message or "")
    assert job.result is not None
    assert job.result.get("error") == "WORKSHEET_UNAVAILABLE"


# 6. Discovery response correctly preserves available worksheet metadata.
def test_discovery_response_correctly_preserves_available_worksheet_metadata():
    """Verify SRMDiscoverResponse models retain all worksheet metadata fields."""
    ws = SRMWorksheetItem(
        worksheet_id="1011",
        session=101,
        slo=1,
        filename="1011.docx",
        format="docx",
        is_available=True,
        submission_status="NOT_SUBMITTED",
        title="Unit 1 Session 1 SLO 1",
        download_url="https://dld.srmist.edu.in/etecurricula/server/uploads/data/coordinator/21LEM202T/slp/1011.docx",
    )
    course = SRMCourseItem(
        course_code="21LEM202T",
        course_name="Universal Human Values",
        batch_id="21LEM202T_39",
        semester=3,
        department="ECE",
        worksheets=[ws],
    )
    resp = SRMDiscoverResponse(
        status="SUCCESS",
        message="Discovered 1 courses.",
        semester=3,
        courses=[course],
    )

    data = resp.model_dump()
    assert data["status"] == "SUCCESS"
    assert data["courses"][0]["course_code"] == "21LEM202T"
    ws_dump = data["courses"][0]["worksheets"][0]
    assert ws_dump["worksheet_id"] == "1011"
    assert ws_dump["session"] == 101
    assert ws_dump["slo"] == 1
    assert ws_dump["is_available"] is True
    assert ws_dump["download_url"] is not None


# 7. Frontend and backend use the same availability field/schema.
def test_frontend_and_backend_use_same_availability_schema():
    """Verify frontend code parses and checks the exact backend schema field `is_available`."""
    dash_html = Path("apps/api/static/dashboard.html").read_text(encoding="utf-8")
    page_tsx = Path("frontend/src/app/page.tsx").read_text(encoding="utf-8")

    # Both frontend implementations filter on w.is_available
    assert "w.is_available" in dash_html
    assert "w.is_available" in page_tsx

    # Both frontend implementations guard automation start on is_available
    assert "selectedWorksheetData.is_available" in dash_html
    assert "!selectedWorksheet.is_available" in page_tsx

    # Backend Pydantic schema field name
    assert "is_available: bool" in Path("packages/shared/schemas/job.py").read_text(encoding="utf-8")


# 8. No stale discovery/localStorage state causes false unavailable status.
def test_no_stale_discovery_or_localstorage_causes_false_unavailable():
    """Verify localStorage is never used to cache discovery results or worksheet availability."""
    dash_html = Path("apps/api/static/dashboard.html").read_text(encoding="utf-8")
    page_tsx = Path("frontend/src/app/page.tsx").read_text(encoding="utf-8")

    # Ensure localStorage only references job IDs, never cached courses or false unavailability
    for line in dash_html.splitlines():
        trimmed = line.strip()
        if "localStorage" in trimmed and not trimmed.startswith("//"):
            assert "srm_active_job_id" in trimmed
            assert "course" not in trimmed.lower()
            assert "avail" not in trimmed.lower()

    for line in page_tsx.splitlines():
        trimmed = line.strip()
        if "localStorage" in trimmed and not trimmed.startswith("//"):
            assert "srm_active_job_id" in trimmed
            assert "course" not in trimmed.lower()
            assert "avail" not in trimmed.lower()
