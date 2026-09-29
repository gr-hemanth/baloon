"""Regression tests for SRM worksheet unavailability and truthfulness safeguards.

Verifies:
1. SRM worksheet unavailable -> no synthetic file created.
2. SRM getfile 404 -> job stops with explicit worksheet-unavailable error.
3. No AI call when worksheet is unavailable.
4. No Google Drive upload when worksheet is unavailable.
5. No SRM submission when worksheet is unavailable.
6. Dashboard disables automation for unavailable worksheets.
7. Existing available worksheet 1011 for 21LEM202T still works unchanged.
8. A failed download cannot create a fake worksheet.
9. Session/SLO mapping is preserved and never replaced with available[0].
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from apps.worker.tasks import _run_job_workflow
from packages.drive.client import BaseDriveClient
from packages.drive.models import DriveFileMetadata
from packages.shared.models.job import Job, JobStatus
from packages.srm.exceptions import DownloadFailed, SRMWorksheetNotFoundError, WorksheetNotFound
from packages.srm.models import (
    SRMCourse,
    SRMSessionStatus,
    SRMWorksheetMetadata,
)
from packages.srm.orchestrator import SRMOrchestrator
from packages.worksheets.pipeline import WorksheetPipeline
from tests.test_worksheet_parser import _create_synthetic_mcq_docx


def _setup_unavailable_orchestrator(course_code: str = "21CSC203P", session_num: int = 209, slo_num: int = 1) -> MagicMock:
    """Mock orchestrator where the requested worksheet is NOT available on SRM."""
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    course = SRMCourse(
        course_code=course_code,
        course_name="Advanced Programming Practice",
        batch_id=f"{course_code}_58",
        semester=3,
        department="CSE",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course])

    ws_meta = SRMWorksheetMetadata(
        course_code=course_code,
        session=session_num,
        slo=slo_num,
        filename=f"{session_num}{slo_num}.docx",
        format="docx",
        storage_path=f"data/coordinator/{course_code}/slp",
        download_url=None,
        is_available=False,  # Explicitly unavailable on portal
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_meta])
    mock_orch.get_worksheet_file = AsyncMock(
        side_effect=SRMWorksheetNotFoundError(f"File not found on storage: {session_num}{slo_num}.docx")
    )
    mock_orch.download_worksheet = AsyncMock(
        side_effect=DownloadFailed(f"File {session_num}{slo_num}.docx not found on portal storage")
    )
    mock_orch.submit_worksheet_link = AsyncMock()
    return mock_orch


def _setup_available_orchestrator(tmp_path: Path, course_code: str = "21LEM202T", session_num: int = 101, slo_num: int = 1) -> MagicMock:
    """Mock orchestrator where an authentic coordinator worksheet exists."""
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    course = SRMCourse(
        course_code=course_code,
        course_name="Universal Human Values",
        batch_id=f"{course_code}_39",
        semester=3,
        department="ECE",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course])

    ws_meta = SRMWorksheetMetadata(
        course_code=course_code,
        session=session_num,
        slo=slo_num,
        filename=f"{session_num}{slo_num}.docx",
        format="docx",
        storage_path=f"data/coordinator/{course_code}/slp",
        download_url=f"https://questions.srmist.edu.in/uploads/data/coordinator/{course_code}/slp/{session_num}{slo_num}.docx",
        is_available=True,
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_meta])
    mock_orch.get_worksheet_file = AsyncMock(
        return_value=f"https://questions.srmist.edu.in/uploads/data/coordinator/{course_code}/slp/{session_num}{slo_num}.docx"
    )

    real_sample_docx = _create_synthetic_mcq_docx(tmp_path / f"{session_num}{slo_num}.docx")

    async def fake_download(file_url_or_id, destination_dir=None, filename=None):
        dest_dir = destination_dir or tmp_path
        target = dest_dir / (filename or f"{session_num}{slo_num}.docx")
        target.write_bytes(real_sample_docx.read_bytes())
        return target

    mock_orch.download_worksheet = AsyncMock(side_effect=fake_download)
    mock_orch.get_session_status = AsyncMock(
        return_value=SRMSessionStatus(session=session_num, practice_status=0, slo_links={})
    )
    mock_orch.submit_worksheet_link = AsyncMock()
    return mock_orch


def _setup_mock_drive() -> MagicMock:
    mock_drive = MagicMock(spec=BaseDriveClient)
    meta = DriveFileMetadata(
        file_id="drive_file_valid_123",
        filename="completed_worksheet.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        web_url="https://drive.google.com/file/d/drive_file_valid_123/view",
        download_url="https://drive.google.com/uc?id=drive_file_valid_123&export=download",
        is_public=True,
        permission_status="VERIFIED_PUBLIC_READER",
    )
    mock_drive.upload_file = AsyncMock(return_value=meta)
    mock_drive.verify_public_permission = AsyncMock(return_value=True)
    return mock_drive


# 1. SRM worksheet unavailable -> no synthetic file created.
@pytest.mark.asyncio
async def test_unavailable_worksheet_creates_no_synthetic_file(db_session: Session, tmp_path: Path):
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

    mock_orch = _setup_unavailable_orchestrator("21CSC203P", 209, 1)
    mock_drive = _setup_mock_drive()

    credentials = {
        "USER_ID": "RA2511003011819",
        "PASSWORD": "SecretPassword123!",
        "requested_session": 209,
        "requested_slo": 1,
    }

    await _run_job_workflow(
        job_id=job.id,
        credentials=credentials,
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.current_step == "worksheet_unavailable"
    assert "No official SRM worksheet is available" in (job.error_message or "")
    assert job.result is not None
    assert job.result.get("error") == "WORKSHEET_UNAVAILABLE"
    assert job.result.get("is_available") is False

    # Verify no fabricated .docx exists anywhere in tmp_path
    docx_files = list(tmp_path.glob("*.docx"))
    assert len(docx_files) == 0


# 2. SRM getfile 404 -> job stops with explicit worksheet-unavailable error.
@pytest.mark.asyncio
async def test_srm_getfile_404_stops_with_explicit_error(db_session: Session, tmp_path: Path):
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
    mock_orch.discover_worksheets = AsyncMock(return_value=[])
    mock_orch.get_worksheet_file = AsyncMock(
        side_effect=SRMWorksheetNotFoundError("File not found on storage (404)")
    )

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2511003011819", "PASSWORD": "pwd", "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=_setup_mock_drive(),
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.current_step == "worksheet_unavailable"
    assert "File not found on storage" in (job.error_message or "")
    assert job.result is not None
    assert job.result.get("error") == "WORKSHEET_UNAVAILABLE"


# 3. No AI call when worksheet is unavailable.
@pytest.mark.asyncio
async def test_no_ai_call_when_worksheet_unavailable(db_session: Session, tmp_path: Path):
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

    mock_orch = _setup_unavailable_orchestrator("21CSC203P", 209, 1)
    mock_pipeline = MagicMock(spec=WorksheetPipeline)
    mock_pipeline.process = AsyncMock()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2511003011819", "PASSWORD": "pwd", "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        pipeline=mock_pipeline,
        drive_client=_setup_mock_drive(),
        db_session=db_session,
    )

    mock_pipeline.process.assert_not_called()


# 4. No Google Drive upload when worksheet is unavailable.
@pytest.mark.asyncio
async def test_no_google_drive_upload_when_worksheet_unavailable(db_session: Session, tmp_path: Path):
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

    mock_orch = _setup_unavailable_orchestrator("21CSC203P", 209, 1)
    mock_drive = _setup_mock_drive()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2511003011819", "PASSWORD": "pwd", "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    mock_drive.upload_file.assert_not_called()
    mock_drive.verify_public_permission.assert_not_called()


# 5. No SRM submission when worksheet is unavailable.
@pytest.mark.asyncio
async def test_no_srm_submission_when_worksheet_unavailable(db_session: Session, tmp_path: Path):
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

    mock_orch = _setup_unavailable_orchestrator("21CSC203P", 209, 1)

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2511003011819", "PASSWORD": "pwd", "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=_setup_mock_drive(),
        db_session=db_session,
    )

    mock_orch.submit_worksheet_link.assert_not_called()


# 6. Dashboard disables automation for unavailable worksheets.
def test_dashboard_disables_automation_for_unavailable_worksheets():
    dashboard_html_path = Path("apps/api/static/dashboard.html")
    assert dashboard_html_path.exists()
    content = dashboard_html_path.read_text(encoding="utf-8")

    # Guard in onCourseSelected
    assert "No official SRM worksheet is available for this course/session." in content
    assert "Availability:" in content
    assert "UNAVAILABLE" in content
    assert "selectedWorksheetData.is_available" in content
    assert "document.getElementById(\"start-job-btn\").disabled = true" in content

    # Guard in startAutomationJob
    assert "if (!selectedWorksheetData || !selectedWorksheetData.is_available)" in content
    assert "Cannot start job: No official SRM worksheet is available for this course/session." in content

    # React frontend verification
    frontend_page_path = Path("frontend/src/app/page.tsx")
    if frontend_page_path.exists():
        fe_content = frontend_page_path.read_text(encoding="utf-8")
        assert "No official SRM worksheet is available for this course/session." in fe_content
        assert "!selectedWorksheet.is_available" in fe_content


# 7. Existing available worksheet 1011 for 21LEM202T still works unchanged.
@pytest.mark.asyncio
async def test_existing_available_worksheet_1011_works_unchanged(db_session: Session, tmp_path: Path):
    job = Job(
        user_id="RA2511003011819",
        course_id="21LEM202T",
        semester_id="3",
        worksheet_id="1011",
        status=JobStatus.PENDING,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_available_orchestrator(tmp_path, "21LEM202T", 101, 1)
    mock_drive = _setup_mock_drive()

    credentials = {
        "USER_ID": "RA2511003011819",
        "PASSWORD": "SecretPassword123!",
        "requested_session": 101,
        "requested_slo": 1,
    }

    await _run_job_workflow(
        job_id=job.id,
        credentials=credentials,
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    # Available worksheet reaches AWAITING_USER_REVIEW
    assert job.status == JobStatus.AWAITING_USER_REVIEW
    assert job.current_step == "awaiting_user_review"
    assert job.result is not None
    drive_url = job.result.get("drive_web_url") or job.result.get("drive_link")
    assert drive_url is not None
    assert "drive.google.com" in drive_url
    assert job.result.get("drive_file_id") == "drive_file_valid_123"
    # Never submitted automatically
    mock_orch.submit_worksheet_link.assert_not_called()


# 8. A failed download cannot create a fake worksheet.
@pytest.mark.asyncio
async def test_failed_download_cannot_create_fake_worksheet(db_session: Session, tmp_path: Path):
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
            download_url="https://questions.srmist.edu.in/uploads/data/coordinator/21CSC203P/slp/2091.docx",
            is_available=True,
        )
    ])
    mock_orch.get_worksheet_file = AsyncMock(return_value="https://questions.srmist.edu.in/uploads/data/coordinator/21CSC203P/slp/2091.docx")
    # Simulate network download failure / corrupted link
    mock_orch.download_worksheet = AsyncMock(side_effect=DownloadFailed("HTTP 404 Storage Error"))

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2511003011819", "PASSWORD": "pwd", "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=_setup_mock_drive(),
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.current_step == "worksheet_unavailable"
    assert "Official worksheet download failed" in (job.error_message or "")

    # Ensure no fabricated text exists in any files
    for p in tmp_path.rglob("*"):
        if p.is_file():
            content = p.read_bytes()
            assert b"Explain the fundamental architectural concepts" not in content


# 9. Session/SLO mapping is preserved and never replaced with available[0].
@pytest.mark.asyncio
async def test_session_slo_mapping_preserved_never_replaced_with_available_zero(db_session: Session, tmp_path: Path):
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

    # Discovered has an unrelated session 101 available, but the requested 209 is unavailable
    ws_101 = SRMWorksheetMetadata(
        course_code="21CSC203P",
        session=101,
        slo=1,
        filename="1011.docx",
        format="docx",
        storage_path="data/coordinator/21CSC203P/slp",
        download_url="https://questions.srmist.edu.in/uploads/1011.docx",
        is_available=True,
    )
    ws_209 = SRMWorksheetMetadata(
        course_code="21CSC203P",
        session=209,
        slo=1,
        filename="2091.docx",
        format="docx",
        storage_path="data/coordinator/21CSC203P/slp",
        download_url=None,
        is_available=False,
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_101, ws_209])

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2511003011819", "PASSWORD": "pwd", "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=_setup_mock_drive(),
        db_session=db_session,
    )

    db_session.refresh(job)
    # Must fail because session 209 slo 1 was requested, NOT silently substitute ws_101
    assert job.status == JobStatus.FAILED
    assert job.current_step == "worksheet_unavailable"
    assert "Session 209 SLO 1" in (job.error_message or "")
    assert job.result is not None
    assert job.result["session"] == 209
    assert job.result["slo"] == 1
    assert job.result["worksheet_id"] == "2091"
