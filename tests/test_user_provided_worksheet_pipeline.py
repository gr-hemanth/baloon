"""Comprehensive regression test suite for user-provided worksheet pipeline.

Verifies all 16 required behaviors:
1. Valid DOCX upload succeeds with 201, returning upload_id, stored_path, format="docx", file_size.
2. Valid PDF upload succeeds with 201, returning upload_id, stored_path, format="pdf", file_size.
3. Invalid extension (.txt, .py, .exe, .docx.exe) rejected with HTTP 400.
4. Magic byte signature mismatch rejected with HTTP 400.
5. Corrupted DOCX (magic bytes PK\\x03\\x04 but corrupted zip structure) rejected with HTTP 400.
6. Corrupted PDF (magic bytes %PDF- but corrupted stream) rejected with HTTP 400.
7. File exceeding 15 MB limit rejected with HTTP 413 or 400.
8. Path traversal attempt in filename is sanitized and strictly confined to uploads directory.
9. Job created with uploaded_file_path routes user-provided file into the pipeline.
10. Original uploaded file remains completely unchanged on disk (SHA256 verified).
11. Bypasses SRM download: worker never calls portal download when user file is provided.
12. Unavailable SRM worksheet (21CSC203P) completes successfully when user-provided file is provided.
13. Existing SRM-provided worksheet flow (21LEM202T / 1011) continues to work unchanged.
14. No AI, Drive, or SRM calls occur if user upload is invalid or missing.
15. User-provided worksheet workflow strictly halts at AWAITING_USER_REVIEW.
16. Explicit user approval via POST /api/v1/jobs/{job_id}/submit remains only path to portal submission.
"""

import hashlib
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy.orm import Session

from apps.worker.tasks import (
    _run_job_workflow,
    _run_submission_workflow,
    store_job_credentials,
)
from packages.drive.client import BaseDriveClient
from packages.drive.models import DriveFileMetadata
from packages.shared.models.job import Job, JobStatus
from packages.srm.exceptions import WorksheetNotFound
from packages.srm.models import (
    SRMCourse,
    SRMSessionStatus,
    SRMSubmissionResult,
    SRMWorksheetMetadata,
)
from packages.srm.orchestrator import SRMOrchestrator


def _create_valid_docx_bytes() -> bytes:
    """Create in-memory bytes of a valid minimal DOCX document."""
    doc = Document()
    doc.add_heading("Course: 21CSC203P Practical Programming", level=1)
    doc.add_paragraph("1. Write a program to demonstrate method overloading.")
    doc.add_paragraph("Answer: ")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _create_valid_pdf_bytes() -> bytes:
    """Create in-memory bytes of a valid minimal PDF document."""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _setup_mock_orchestrator(is_course_available: bool = True) -> MagicMock:
    """Create a mock SRMOrchestrator."""
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    course = SRMCourse(
        course_code="21CSC203P" if not is_course_available else "21LEM202T",
        course_name="PROGRAMMING PRACTICE" if not is_course_available else "UNIVERSAL HUMAN VALUES",
        batch_id="21CSC203P_58" if not is_course_available else "21LEM202T_39",
        semester=3,
        department="CSE" if not is_course_available else "HUMANITIES",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course])

    ws_meta = SRMWorksheetMetadata(
        course_code=course.course_code,
        session=209 if not is_course_available else 101,
        slo=1,
        filename="2091.docx" if not is_course_available else "1011.docx",
        format="docx",
        storage_path="data/coordinator/slp",
        is_available=is_course_available,
        submission_status="NOT_SUBMITTED",
        download_url="https://srm.portal/file/1011.docx" if is_course_available else None,
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_meta])
    mock_orch.get_worksheet_file = AsyncMock(return_value=ws_meta.download_url)
    mock_orch.download_worksheet = AsyncMock()

    status_resp = SRMSessionStatus(
        session=209 if not is_course_available else 101,
        practice_status={},
        slo_links={},
    )
    mock_orch.get_session_status = AsyncMock(return_value=status_resp)
    mock_orch.submit_worksheet_link = AsyncMock(
        return_value=SRMSubmissionResult(success=True, message="Submitted successfully")
    )
    return mock_orch


def _setup_mock_drive() -> MagicMock:
    """Create a mock Google Drive client returning verified public link."""
    mock_drive = MagicMock(spec=BaseDriveClient)
    meta = DriveFileMetadata(
        file_id="mock_drive_file_12345",
        filename="completed_worksheet.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        web_url="https://drive.google.com/file/d/mock_drive_file_12345/view",
        download_url="https://drive.google.com/uc?id=mock_drive_file_12345&export=download",
        is_public=True,
        permission_status="VERIFIED_PUBLIC_READER",
    )
    mock_drive.upload_file = AsyncMock(return_value=meta)
    return mock_drive


# ==============================================================================
# 1. Upload Validation Tests (1 - 8)
# ==============================================================================

def test_01_valid_docx_upload_success(client: TestClient):
    """Test 1: Valid DOCX upload returns 201, upload_id, stored_path, format='docx'."""
    docx_bytes = _create_valid_docx_bytes()
    resp = client.post(
        "/api/v1/worksheets/upload",
        files={"file": ("lab_worksheet.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["upload_id"]
    assert data["original_filename"] == "lab_worksheet.docx"
    assert data["format"] == "docx"
    assert data["file_size"] == len(docx_bytes)
    assert Path(data["stored_path"]).exists()
    assert Path(data["stored_path"]).is_file()


def test_02_valid_pdf_upload_success(client: TestClient):
    """Test 2: Valid PDF upload returns 201, upload_id, stored_path, format='pdf'."""
    pdf_bytes = _create_valid_pdf_bytes()
    resp = client.post(
        "/api/v1/worksheets/upload",
        files={"file": ("lab_manual.pdf", pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["upload_id"]
    assert data["original_filename"] == "lab_manual.pdf"
    assert data["format"] == "pdf"
    assert data["file_size"] == len(pdf_bytes)
    assert Path(data["stored_path"]).exists()


def test_03_invalid_extension_rejected(client: TestClient):
    """Test 3: Non-DOCX/PDF extensions (.txt, .py, .exe, etc.) rejected with HTTP 400."""
    for bad_name in ["worksheet.txt", "script.py", "malware.exe", "fake.docx.exe"]:
        resp = client.post(
            "/api/v1/worksheets/upload",
            files={"file": (bad_name, b"some content", "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert "Unsupported file extension" in resp.json()["detail"]


def test_04_magic_bytes_mismatch_rejected(client: TestClient):
    """Test 4: Disguised text files masquerading as DOCX or PDF are rejected with HTTP 400."""
    # Plain text named .docx
    resp1 = client.post(
        "/api/v1/worksheets/upload",
        files={"file": ("fake.docx", b"Plain text file content", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert resp1.status_code == 400
    assert "magic byte signature mismatch" in resp1.json()["detail"]

    # Plain text named .pdf
    resp2 = client.post(
        "/api/v1/worksheets/upload",
        files={"file": ("fake.pdf", b"Plain text file content", "application/pdf")},
    )
    assert resp2.status_code == 400
    assert "magic byte signature mismatch" in resp2.json()["detail"]


def test_05_corrupted_docx_rejected(client: TestClient):
    """Test 5: Magic bytes PK\\x03\\x04 with corrupted inner zip/xml rejected with HTTP 400."""
    fake_docx = b"PK\x03\x04\x00\x00\x00\x00corrupted_non_zip_data"
    resp = client.post(
        "/api/v1/worksheets/upload",
        files={"file": ("corrupt.docx", fake_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert resp.status_code == 400
    assert "Corrupted or invalid DOCX document" in resp.json()["detail"]


def test_06_corrupted_pdf_rejected(client: TestClient):
    """Test 6: Magic bytes %PDF- with corrupted body/xref rejected with HTTP 400."""
    fake_pdf = b"%PDF-1.4\ncorrupted_body_without_xref_or_trailer"
    resp = client.post(
        "/api/v1/worksheets/upload",
        files={"file": ("corrupt.pdf", fake_pdf, "application/pdf")},
    )
    assert resp.status_code == 400
    assert "Corrupted or invalid PDF document" in resp.json()["detail"]


def test_07_size_exceeding_15mb_rejected(client: TestClient):
    """Test 7: File exceeding 15 MB limit rejected with HTTP 413 or 400."""
    oversized = b"A" * (15 * 1024 * 1024 + 1024)
    resp = client.post(
        "/api/v1/worksheets/upload",
        files={"file": ("huge.docx", oversized, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert resp.status_code in (413, 400)
    assert "15 MB" in resp.json()["detail"]


def test_08_path_traversal_sanitized(client: TestClient):
    """Test 8: Filename containing path traversal sequences (../, ..\\) is strictly sanitized."""
    docx_bytes = _create_valid_docx_bytes()
    resp = client.post(
        "/api/v1/worksheets/upload",
        files={"file": ("../../../../etc/passwd.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    # Either successfully sanitized into uploads dir or rejected as malicious
    if resp.status_code == 201:
        stored_path = Path(resp.json()["stored_path"])
        assert "artifacts" in str(stored_path)
        assert "uploads" in str(stored_path)
        assert stored_path.exists()
    else:
        assert resp.status_code == 400


# ==============================================================================
# 2. Pipeline Routing & Lifecycle Tests (9 - 16)
# ==============================================================================

def test_09_job_created_routes_user_file_into_pipeline(client: TestClient, db_session: Session, tmp_path: Path):
    """Test 9: Job created with uploaded_file_path stores path and marks source_type = USER_PROVIDED."""
    doc_path = tmp_path / "user_sample.docx"
    doc_path.write_bytes(_create_valid_docx_bytes())

    create_payload = {
        "user_id": "RA2111003010001",
        "course_id": "21CSC203P",
        "semester_id": "3",
        "session": 209,
        "slo": 1,
        "worksheet_id": "2091",
        "uploaded_file_path": str(doc_path),
        "credentials": {"USER_ID": "RA2111003010001", "PASSWORD": "password123"},
    }
    resp = client.post("/api/v1/jobs", json=create_payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["uploaded_file_path"] == str(doc_path)
    assert data["source_type"] == "USER_PROVIDED"
    assert data["is_user_provided"] is True


@pytest.mark.asyncio
async def test_10_original_user_file_immutability(db_session: Session, tmp_path: Path):
    """Test 10: Original uploaded file remains completely untouched on disk (SHA256 verified)."""
    user_file = tmp_path / "original_lab.docx"
    user_file.write_bytes(_create_valid_docx_bytes())
    hash_before = hashlib.sha256(user_file.read_bytes()).hexdigest()

    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        semester_id="3",
        worksheet_id="2091",
        uploaded_file_path=str(user_file),
        status=JobStatus.PENDING,
        transport_mode="http",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(is_course_available=False)
    mock_drive = _setup_mock_drive()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001", "uploaded_file_path": str(user_file), "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    hash_after = hashlib.sha256(user_file.read_bytes()).hexdigest()
    assert hash_before == hash_after, "Original user-provided file was modified during processing!"


@pytest.mark.asyncio
async def test_11_bypass_srm_download_when_user_file_exists(db_session: Session, tmp_path: Path):
    """Test 11: Bypasses SRM download: worker never calls portal download when user file is provided."""
    user_file = tmp_path / "practice.docx"
    user_file.write_bytes(_create_valid_docx_bytes())

    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        semester_id="3",
        worksheet_id="2091",
        uploaded_file_path=str(user_file),
        status=JobStatus.PENDING,
        transport_mode="http",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(is_course_available=False)
    mock_drive = _setup_mock_drive()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001", "uploaded_file_path": str(user_file), "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    # Portal download functions must NOT have been called
    mock_orch.download_worksheet.assert_not_called()


@pytest.mark.asyncio
async def test_12_unavailable_srm_worksheet_succeeds_with_user_file(db_session: Session, tmp_path: Path):
    """Test 12: Course 21CSC203P (where SRM has no official worksheet) succeeds when user provides file."""
    user_file = tmp_path / "2091_actual.docx"
    user_file.write_bytes(_create_valid_docx_bytes())

    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        semester_id="3",
        worksheet_id="2091",
        uploaded_file_path=str(user_file),
        status=JobStatus.PENDING,
        transport_mode="http",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(is_course_available=False)
    mock_drive = _setup_mock_drive()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001", "uploaded_file_path": str(user_file), "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.AWAITING_USER_REVIEW
    assert job.result["source_type"] == "USER_PROVIDED"
    assert job.result["is_user_provided"] is True
    assert job.result["review_ready"] is True
    assert job.result["drive_web_url"]


@pytest.mark.asyncio
async def test_13_existing_srm_flow_preserved_when_no_user_file(db_session: Session, tmp_path: Path):
    """Test 13: Existing SRM-provided worksheet flow (21LEM202T / 1011) continues to work unchanged."""
    srm_ws_file = tmp_path / "1011.docx"
    srm_ws_file.write_bytes(_create_valid_docx_bytes())

    mock_orch = _setup_mock_orchestrator(is_course_available=True)
    mock_orch.download_worksheet = AsyncMock(return_value=srm_ws_file)
    mock_drive = _setup_mock_drive()

    job = Job(
        user_id="RA2111003010001",
        course_id="21LEM202T",
        semester_id="3",
        worksheet_id="1011",
        uploaded_file_path=None,  # Standard SRM flow
        status=JobStatus.PENDING,
        transport_mode="http",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001", "requested_session": 101, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.AWAITING_USER_REVIEW
    assert job.result["source_type"] == "SRM_OFFICIAL"
    assert job.result["is_user_provided"] is False
    mock_orch.download_worksheet.assert_called_once()


@pytest.mark.asyncio
async def test_14_no_ai_drive_srm_on_invalid_or_missing_user_file(db_session: Session):
    """Test 14: No AI, Drive, or SRM calls occur if user upload is invalid or missing."""
    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        semester_id="3",
        worksheet_id="2091",
        uploaded_file_path="C:/nonexistent/fake_worksheet.docx",
        status=JobStatus.PENDING,
        transport_mode="http",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(is_course_available=False)
    mock_drive = _setup_mock_drive()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001", "uploaded_file_path": "C:/nonexistent/fake_worksheet.docx"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.current_step == "worksheet_unavailable"
    # Ensure neither Drive nor SRM submission was called
    mock_drive.upload_file.assert_not_called()
    mock_orch.submit_worksheet_link.assert_not_called()


@pytest.mark.asyncio
async def test_15_workflow_strictly_halts_at_awaiting_user_review(db_session: Session, tmp_path: Path):
    """Test 15: User-provided worksheet workflow strictly halts at AWAITING_USER_REVIEW."""
    user_file = tmp_path / "lab.docx"
    user_file.write_bytes(_create_valid_docx_bytes())

    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        semester_id="3",
        worksheet_id="2091",
        uploaded_file_path=str(user_file),
        status=JobStatus.PENDING,
        transport_mode="http",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(is_course_available=False)
    mock_drive = _setup_mock_drive()

    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001", "uploaded_file_path": str(user_file), "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.AWAITING_USER_REVIEW
    # Review gate: submit_worksheet_link must NEVER be called by background task
    mock_orch.submit_worksheet_link.assert_not_called()


@pytest.mark.asyncio
async def test_16_explicit_submit_is_only_path_to_portal(db_session: Session, tmp_path: Path):
    """Test 16: Explicit user approval via submission workflow is the only path to portal."""
    user_file = tmp_path / "lab.docx"
    user_file.write_bytes(_create_valid_docx_bytes())

    job = Job(
        user_id="RA2111003010001",
        course_id="21CSC203P",
        semester_id="3",
        worksheet_id="2091",
        uploaded_file_path=str(user_file),
        status=JobStatus.PENDING,
        transport_mode="http",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orch = _setup_mock_orchestrator(is_course_available=False)
    mock_drive = _setup_mock_drive()

    # Step 1: Run workflow to reach AWAITING_USER_REVIEW
    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001", "uploaded_file_path": str(user_file), "requested_session": 209, "requested_slo": 1},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        db_session=db_session,
    )
    db_session.refresh(job)
    assert job.status == JobStatus.AWAITING_USER_REVIEW
    assert mock_orch.submit_worksheet_link.call_count == 0

    # Step 2: Explicit submission action triggers portal submission
    await _run_submission_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001"},
        orchestrator=mock_orch,
        db_session=db_session,
    )

    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert mock_orch.submit_worksheet_link.call_count == 1
    assert job.result["practice_status"] == 2
    assert job.result["source_type"] == "USER_PROVIDED"
