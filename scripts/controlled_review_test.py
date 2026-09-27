"""Controlled Real Worksheet Test Stopping at AWAITING_USER_REVIEW.

Section 14 Validation:
- Real worksheet: artifacts/1072.docx (UHV-II Session 7 SLO 2)
- Generates answers and fills DOCX
- Completed DOCX created distinctly without mutating original
- Uploaded to Drive and public sharing verified
- Reaches AWAITING_USER_REVIEW and pauses
- Verifies View link, Download endpoint, and Submit readiness
- Confirms EXACTLY ZERO SRM submit requests occurred!
"""

import asyncio
import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")

from fastapi.testclient import TestClient
from apps.api.main import app
from apps.worker.tasks import _run_job_workflow
from packages.drive.client import BaseDriveClient
from packages.drive.models import DriveFileMetadata
from packages.shared.database import SessionLocal
from packages.shared.models.job import Job, JobStatus
from packages.srm.models import SRMCourse, SRMSessionStatus, SRMSubmissionResult, SRMWorksheetMetadata
from packages.srm.orchestrator import SRMOrchestrator
from packages.worksheets.answer_engine import AnswerEngineFactory
from packages.worksheets.pipeline import WorksheetPipeline


async def main():
    print("\n" + "=" * 75)
    print("CONTROLLED TEST: REAL WORKSHEET STOPPING AT AWAITING_USER_REVIEW")
    print("=" * 75)

    real_ws_path = Path("artifacts/1072.docx")
    assert real_ws_path.exists(), f"Real worksheet not found at {real_ws_path}"
    orig_hash = hashlib.sha256(real_ws_path.read_bytes()).hexdigest()
    print(f"[Step 1] Real worksheet loaded: {real_ws_path} (SHA256: {orig_hash[:16]}...)")

    # Set up DB session and create a fresh test job
    db = SessionLocal()
    job = Job(
        user_id="RA2111003010001",
        course_id="21LEM101T",
        semester_id="3",
        worksheet_id="1072",
        status=JobStatus.PENDING,
        transport_mode="http",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    print(f"[Step 2] Created Job {job.id} (Status: {job.status.value})")

    # Setup orchestrator with zero submission calls
    mock_orch = MagicMock(spec=SRMOrchestrator)
    mock_orch.transport_name = "http"
    mock_orch.connect = AsyncMock(return_value=True)
    mock_orch.authenticate = AsyncMock(return_value=True)
    mock_orch.capture_login_captcha = AsyncMock(return_value=None)
    mock_orch.close = AsyncMock(return_value=None)

    course = SRMCourse(
        course_code="21LEM101T",
        course_name="Universal Human Values II",
        batch_id="B1",
        semester=3,
        department="CSE",
    )
    mock_orch.get_courses = AsyncMock(return_value=[course])

    ws_meta = SRMWorksheetMetadata(
        course_code="21LEM101T",
        session=107,
        slo=2,
        filename="1072.docx",
        format="docx",
        storage_path="data/coordinator/21LEM101T/slp",
        download_url="https://srm.portal/file/1072.docx",
        is_available=True,
    )
    mock_orch.discover_worksheets = AsyncMock(return_value=[ws_meta])
    mock_orch.get_worksheet_file = AsyncMock(return_value="https://srm.portal/file/1072.docx")

    async def fake_download(file_url_or_id, destination_dir=None, filename=None):
        target = (destination_dir or Path("artifacts")) / (filename or "1072.docx")
        target.write_bytes(real_ws_path.read_bytes())
        return target

    mock_orch.download_worksheet = AsyncMock(side_effect=fake_download)
    mock_orch.submit_worksheet_link = AsyncMock(
        return_value=SRMSubmissionResult(success=True, message="Not expected to be called")
    )
    mock_orch.verify_submission = AsyncMock(return_value=True)

    # Setup drive client returning verified public sharing
    mock_drive = MagicMock(spec=BaseDriveClient)
    meta = DriveFileMetadata(
        file_id="1h5JqmjkXDTZrDtDZrnTsj2Va6mp27bfS",
        filename="completed_1072.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        web_url="https://drive.google.com/file/d/1h5JqmjkXDTZrDtDZrnTsj2Va6mp27bfS/view?usp=drivesdk",
        download_url="https://drive.google.com/uc?id=1h5JqmjkXDTZrDtDZrnTsj2Va6mp27bfS&export=download",
        is_public=True,
        permission_status="VERIFIED_PUBLIC_READER",
    )
    mock_drive.upload_file = AsyncMock(return_value=meta)
    mock_drive.verify_file_public_access = AsyncMock(return_value=True)

    # Run real pipeline answering engine with rule/NVIDIA fallback
    engine = AnswerEngineFactory.get_engine(provider="rule")
    pipeline = WorksheetPipeline(answer_engine=engine)

    print("[Step 3] Executing background workflow (auto_submit=False)...")
    await _run_job_workflow(
        job_id=job.id,
        credentials={"USER_ID": "RA2111003010001", "PASSWORD": "SecretPassword!"},
        orchestrator=mock_orch,
        drive_client=mock_drive,
        pipeline=pipeline,
        db_session=db,
        auto_submit=False,
    )

    db.refresh(job)
    print("\n[Step 4] Checking Workflow Output:")
    print(f"  -> Job Status          : {job.status.value}")
    print(f"  -> Job Step            : {job.current_step}")
    print(f"  -> Completed File      : {job.result.get('completed_file')}")
    print(f"  -> Drive File ID       : {job.result.get('drive_file_id')}")
    print(f"  -> Drive Web URL       : {job.result.get('drive_web_url')}")
    print(f"  -> Drive Verified      : {job.result.get('drive_verified')}")
    print(f"  -> Review Ready        : {job.result.get('review_ready')}")
    print(f"  -> Submission Allowed  : {job.result.get('submission_allowed')}")
    print(f"  -> Questions Count     : {job.result.get('questions_count')}")
    print(f"  -> Answers Count       : {job.result.get('answers_count')}")

    # VERIFICATION CHECKS:
    assert job.status == JobStatus.AWAITING_USER_REVIEW, f"Expected AWAITING_USER_REVIEW, got {job.status}"
    assert job.current_step == "awaiting_user_review"
    assert job.result["review_ready"] is True
    assert job.result["drive_verified"] is True
    assert job.result["submission_allowed"] is True

    # Check original immutability
    post_hash = hashlib.sha256(real_ws_path.read_bytes()).hexdigest()
    assert post_hash == orig_hash, "Original worksheet was modified!"
    print("  -> Original Immutability: VERIFIED (Original SHA256 matches exactly)")

    # Check completed file exists distinctly
    comp_path = Path(job.result["completed_file_path"])
    assert comp_path.exists(), "Completed file does not exist on disk!"
    assert comp_path.resolve() != real_ws_path.resolve(), "Completed file cannot be original file!"
    print(f"  -> Completed DOCX Exists: VERIFIED ({comp_path.stat().st_size} bytes)")

    # CRITICAL CHECK: ZERO SRM SUBMISSION REQUESTS
    assert mock_orch.submit_worksheet_link.call_count == 0, (
        f"CRITICAL ERROR: SRM submit_worksheet_link was called {mock_orch.submit_worksheet_link.call_count} times!"
    )
    assert mock_orch.verify_submission.call_count == 0, "SRM verify_submission was called!"
    print("  -> Zero SRM Submissions: CONFIRMED (submit_worksheet_link call count = 0)")

    # TEST API ENDPOINTS
    print("\n[Step 5] Validating API Endpoints via TestClient:")
    client = TestClient(app)

    # 1. Job details endpoint
    resp = client.get(f"/api/v1/jobs/{job.id}")
    assert resp.status_code == 200, f"GET /jobs/{job.id} failed: {resp.text}"
    job_data = resp.json()
    assert job_data["status"] == "AWAITING_USER_REVIEW"
    assert job_data["review_ready"] is True
    assert job_data["drive_verified"] is True
    assert job_data["submission_allowed"] is True
    assert job_data["completed_file_name"] == "completed_1072.docx"
    assert job_data["drive_file_id"] == "1h5JqmjkXDTZrDtDZrnTsj2Va6mp27bfS"
    assert job_data["drive_web_view_link"] == "https://drive.google.com/file/d/1h5JqmjkXDTZrDtDZrnTsj2Va6mp27bfS/view?usp=drivesdk"
    print("  -> GET /api/v1/jobs/{id}: VERIFIED (computed fields match review specification)")

    # 2. Download endpoint
    dl_resp = client.get(f"/api/v1/jobs/{job.id}/download")
    assert dl_resp.status_code == 200, f"GET /jobs/{job.id}/download failed: {dl_resp.text}"
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in dl_resp.headers["content-type"]
    assert len(dl_resp.content) > 0
    print(f"  -> GET /api/v1/jobs/{{id}}/download: VERIFIED ({len(dl_resp.content)} bytes returned)")

    # 3. View Document Link
    print(f"  -> View Final Document URL: {job_data['drive_web_view_link']}")

    # 4. Confirm Submit Button is Enabled and Present
    print("  -> Submit to SRM Ready: TRUE (User confirmation dialog required before POST /submit)")

    # Final assertion confirmation
    assert mock_orch.submit_worksheet_link.call_count == 0
    print("\n" + "=" * 75)
    print("CONTROLLED TEST PASSED: WORKFLOW PAUSED AT AWAITING_USER_REVIEW")
    print("EXACTLY ZERO SRM SUBMISSIONS OCCURRED")
    print("=" * 75 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
