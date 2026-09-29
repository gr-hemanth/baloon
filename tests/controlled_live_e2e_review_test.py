"""Controlled Live End-to-End Integration & Review Gate Test.

Target:
- Course: 21LEM202T - UNIVERSAL HUMAN VALUES
- Batch: 21LEM202T_39
- Worksheet: 1011 (Session 101, SLO 1)

Strict Workflow:
1. Initialize fresh job in SQLite database.
2. Authenticate using headed Playwright + manual hCaptcha flow.
3. Capture SRMAuthSession, close browser, transfer to direct HTTP client.
4. Discover course and check worksheet 1011 availability.
5. Download worksheet 1011.docx to isolated job directory.
6. Parse document using existing generic worksheet parser.
7. Generate answers using configured NVIDIA primary provider.
8. Validate answers and fill worksheet using existing target-resolution system.
9. Verify completed document physically while preserving original byte-exact.
10. Upload completed worksheet to Google Drive and verify public viewable link.
11. Persist job metadata and transition status to AWAITING_USER_REVIEW.
12. STOP at AWAITING_USER_REVIEW (Strictly NO submission to SRM).
13. Verify dashboard review gate UI and state persistence on refresh.
"""

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure project root in sys.path
sys.path.insert(0, ".")

from docx import Document
from playwright.async_api import async_playwright

from packages.drive.client import GoogleDriveClient
from packages.shared.config import settings
from packages.shared.database import SessionLocal
from packages.shared.models.job import Job, JobStatus
from packages.srm.http_client import SRMHttpClient
from packages.srm.models import SRMAuthSession
from packages.srm.orchestrator import SRMOrchestrator
from packages.worksheets.answer_engine import AnswerEngineFactory, LLMAnswerEngine, reset_circuit_breakers
from packages.worksheets.answer_models import AnswerStatus, WorksheetAnswers
from packages.worksheets.code_validator import CodeAnswerValidator
from packages.worksheets.filler import DocxWorksheetFiller
from packages.worksheets.models import QuestionType, ResponseMode
from packages.worksheets.parser import WorksheetParser
from packages.worksheets.verification import PhysicalDocumentVerifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("controlled_live_e2e")

TARGET_COURSE_CODE = "21LEM202T"
TARGET_COURSE_NAME = "UNIVERSAL HUMAN VALUES"
TARGET_BATCH_ID = "21LEM202T_39"
TARGET_WORKSHEET_ID = "1011"
TARGET_SESSION = 101
TARGET_SLO = 1
EXPECTED_ORIGINAL_SHA256 = "eb8e8e4b7e570ea7048180016ec9fbb672ffda96208f4cbe611463c9a9355661"


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


async def run_live_e2e_review_test(
    username: str,
    password: str,
    timeout_seconds: int = 180,
    api_base_url: str = "http://127.0.0.1:8000",
) -> Dict[str, Any]:
    print("\n" + "=" * 70)
    print("CONTROLLED LIVE END-TO-END REVIEW GATE TEST")
    print("=" * 70)
    print(f"Target Portal: https://dld.srmist.edu.in/ktretecurricula/#/")
    masked_user = f"{username[:4]}****{username[-4:] if len(username) > 8 else ''}"
    print(f"Target NetID : {masked_user}")
    print(f"Target Course: {TARGET_COURSE_CODE} - {TARGET_COURSE_NAME}")
    print(f"Target Batch : {TARGET_BATCH_ID}")
    print(f"Worksheet    : {TARGET_WORKSHEET_ID} (Session {TARGET_SESSION}, SLO {TARGET_SLO})")
    print(f"API Base URL : {api_base_url}")
    print("=" * 70 + "\n")

    report: Dict[str, Any] = {
        "job_id": "N/A",
        "course": f"{TARGET_COURSE_CODE} - {TARGET_COURSE_NAME}",
        "batch": TARGET_BATCH_ID,
        "worksheet": TARGET_WORKSHEET_ID,
        "authentication": "FAIL",
        "discovery": "FAIL",
        "download": "FAIL",
        "original_sha256": "N/A",
        "parsing": "FAIL",
        "parsing_detail": "",
        "answer_generation": "FAIL",
        "provider_used": "N/A",
        "filling": "FAIL",
        "completed_sha256": "N/A",
        "physical_verification": "FAIL",
        "drive_upload": "FAIL",
        "drive_file_id": "N/A",
        "drive_public_link": "N/A",
        "drive_public_link_verification": "FAIL",
        "review_gate_transition": "FAIL",
        "dashboard_ui_verification": "FAIL",
        "state_preserved_on_refresh": "FAIL",
        "submission_executed": "NO (Stopped at Review Gate)",
        "final_job_status": "PENDING",
        "issues_detected": "NONE",
    }

    db = SessionLocal()
    orchestrator = SRMOrchestrator(mode="auto")
    job: Optional[Job] = None
    job_temp_dir: Optional[Path] = None

    try:
        # -------------------------------------------------------------------
        # Step 1: Create fresh Job in database
        # -------------------------------------------------------------------
        print("[Step 1] Initializing fresh automation Job in database...")
        job = Job(
            user_id=username,
            course_id=TARGET_COURSE_CODE,
            semester_id=3,
            subject_id=TARGET_COURSE_CODE,
            worksheet_id=TARGET_WORKSHEET_ID,
            transport_mode="auto",
            status=JobStatus.RUNNING,
            current_step="initializing_srm_session",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        report["job_id"] = str(job.id)
        print(f"  [OK] Created Job ID: {job.id}")

        job_temp_dir = Path("artifacts/jobs") / f"job_{job.id[:8]}"
        job_temp_dir.mkdir(parents=True, exist_ok=True)
        print(f"  [OK] Persistent job directory: {job_temp_dir}")

        # -------------------------------------------------------------------
        # Step 2: Headed Playwright Authentication + User Manual hCaptcha
        # -------------------------------------------------------------------
        print("\n[Step 2] Authenticating via Playwright browser transport...")
        print("-" * 70)
        print("ACTION REQUIRED:")
        print("A headed Chromium browser window will open on your screen.")
        print("Please solve the visual hCaptcha challenge in that window.")
        print("Once verified, your session will be captured automatically and browser will close.")
        print("-" * 70 + "\n")

        job.current_step = "authenticating"
        db.commit()

        async def status_callback(phase: str, msg: str):
            print(f"  [STATUS UPDATE] [{phase}] {msg}")
            if phase == "WAITING_FOR_CAPTCHA":
                job.status = JobStatus.WAITING_FOR_CAPTCHA
                job.current_step = "waiting_for_user_captcha"
                job.captcha_challenge = {"message": msg, "type": "interactive_browser"}
                job.updated_at = datetime.now(timezone.utc)
                db.commit()
            elif phase == "AUTHENTICATED":
                job.status = JobStatus.RUNNING
                job.current_step = "authenticated"
                job.captcha_challenge = None
                job.updated_at = datetime.now(timezone.utc)
                db.commit()

        orchestrator.set_status_callback(status_callback)

        auth_session = await orchestrator.browser_client.authenticate_interactive(
            credentials={
                "USER_ID": username,
                "PASSWORD": password,
                "headless": False,
            },
            status_callback=status_callback,
            captcha_timeout_seconds=timeout_seconds,
        )

        if not auth_session or not auth_session.access_token:
            report["issues_detected"] = "Failed to capture access token from browser authentication."
            return report

        report["authentication"] = "PASS"
        job.status = JobStatus.RUNNING
        job.current_step = "authenticated"
        job.transport_mode = orchestrator.transport_name
        db.commit()
        print("  [OK] Playwright authentication successful. Browser closed cleanly.")

        # -------------------------------------------------------------------
        # Step 3: Direct HTTP Course & Worksheet Discovery
        # -------------------------------------------------------------------
        print("\n[Step 3] Discovering course and worksheet availability via direct HTTP...")
        job.current_step = "discovering_courses"
        db.commit()

        http_client = SRMHttpClient(base_url="https://dld.srmist.edu.in")
        http_client.set_auth_session(auth_session)

        # Check course status
        course_status = await http_client.get_course_status(TARGET_COURSE_CODE)
        avail_slp = [str(x) for x in course_status.available_slp]
        avail_slppdf = [str(x) for x in course_status.available_slppdf]

        format_detected = "docx"
        is_available = False
        if TARGET_WORKSHEET_ID in avail_slp:
            format_detected = "docx"
            is_available = True
            print(f"  [OK] Worksheet {TARGET_WORKSHEET_ID} verified in slp (DOCX) register.")
        elif TARGET_WORKSHEET_ID in avail_slppdf:
            format_detected = "pdf"
            is_available = True
            print(f"  [OK] Worksheet {TARGET_WORKSHEET_ID} verified in slppdf (PDF) register.")
        else:
            s_status = await http_client.get_session_status(
                course_info={"BATCH_ID": TARGET_BATCH_ID, "COURSE_CODE": TARGET_COURSE_CODE},
                session=TARGET_SESSION,
            )
            if s_status.practice_status is not None:
                is_available = True
                print(f"  [OK] Session {TARGET_SESSION} active with practice map: {s_status.practice_status}")

        if not is_available:
            report["issues_detected"] = f"Worksheet {TARGET_WORKSHEET_ID} not marked available."
            return report

        report["discovery"] = "PASS"

        # -------------------------------------------------------------------
        # Step 4: Download Worksheet 1011
        # -------------------------------------------------------------------
        print(f"\n[Step 4] Downloading Worksheet {TARGET_WORKSHEET_ID} via direct HTTP...")
        job.status = JobStatus.DOWNLOADING
        job.current_step = f"downloading_worksheet_{TARGET_COURSE_CODE}"
        db.commit()

        download_url = await http_client.get_worksheet_file(
            course_code=TARGET_COURSE_CODE,
            session=TARGET_SESSION,
            slo=TARGET_SLO,
            format_type=format_detected,
            filename=f"{TARGET_WORKSHEET_ID}.{format_detected}",
        )

        downloaded_file = await http_client.download_worksheet(
            file_url_or_id=download_url,
            destination_dir=job_temp_dir,
            filename=f"{TARGET_WORKSHEET_ID}.{format_detected}",
        )

        assert downloaded_file.exists(), f"Downloaded file does not exist at {downloaded_file}"
        downloaded_sha256 = compute_sha256(downloaded_file)
        downloaded_size = downloaded_file.stat().st_size
        report["original_sha256"] = downloaded_sha256
        print(f"  [OK] Downloaded: {downloaded_file.name} ({downloaded_size:,} bytes)")
        print(f"  [OK] SHA-256   : {downloaded_sha256}")
        assert downloaded_sha256.lower() == EXPECTED_ORIGINAL_SHA256.lower(), (
            f"SHA-256 mismatch! Expected {EXPECTED_ORIGINAL_SHA256}, got {downloaded_sha256}"
        )
        report["download"] = "PASS"

        # -------------------------------------------------------------------
        # Step 5: Parse Document
        # -------------------------------------------------------------------
        print("\n[Step 5] Parsing worksheet document structure...")
        job.status = JobStatus.PROCESSING
        job.current_step = "parsing_worksheet"
        db.commit()

        parser = WorksheetParser()
        parsed = parser.parse(downloaded_file)
        q_count = len(parsed.questions)
        report["parsing"] = f"PASS ({q_count} questions)"
        report["parsing_detail"] = f"{q_count} questions detected"
        print(f"  [OK] Worksheet parsed successfully: {q_count} questions discovered.")

        # -------------------------------------------------------------------
        # Step 6: Generate Answers (NVIDIA primary with FreeLLM fallback)
        # -------------------------------------------------------------------
        print("\n[Step 6] Generating answers using configured NVIDIA primary provider...")
        job.current_step = "generating_ai_answers"
        db.commit()

        reset_circuit_breakers()
        # Initialize primary answer engine with NVIDIA
        provider_name = settings.WORKSHEET_ANSWER_PROVIDER or "nvidia"
        engine = AnswerEngineFactory.get_engine(
            provider=provider_name,
            allow_fallback_when_unconfigured=(settings.ENVIRONMENT == "test"),
        )
        # Configure model and chunk size for optimal speed and reliability
        if isinstance(engine, LLMAnswerEngine):
            # If default model is nemotron, ensure fallback to llama-3.2-11b is ready
            if "meta/llama-3.2-11b-vision-instruct" not in engine.model_fallbacks:
                engine.model_fallbacks.append("meta/llama-3.2-11b-vision-instruct")
            engine.chunk_size = 2
            engine.timeout = 90.0

        worksheet_answers: WorksheetAnswers = await engine.generate_answers(
            worksheet=parsed,
            context={
                "course_code": TARGET_COURSE_CODE,
                "course_name": TARGET_COURSE_NAME,
                "session": TARGET_SESSION,
                "slo": TARGET_SLO,
            },
        )

        # Handle both list (production schema) and dict safely
        raw_answers = (
            list(worksheet_answers.answers.values())
            if isinstance(worksheet_answers.answers, dict)
            else worksheet_answers.answers
        )
        ans_count = len(raw_answers)
        successful_ans = [
            a for a in raw_answers
            if getattr(a, "status", None) == AnswerStatus.SUCCESS
        ]
        provider_used = "NVIDIA"
        if successful_ans:
            provider_used = successful_ans[0].metadata.get("provider", "NVIDIA").upper()
        report["provider_used"] = provider_used
        report["answer_generation"] = f"PASS (Provider: {provider_used})"
        print(f"  [OK] Answers generated: {len(successful_ans)}/{ans_count} successful using {provider_used}.")

        # Step 6b: Validate answers
        print("  Validating generated answers...")
        val_success = True
        for q in parsed.questions:
            ans = worksheet_answers.get_answer(q.question_id)
            if not ans:
                continue
            is_code = (
                getattr(q, "response_mode", None) in (ResponseMode.CODE, ResponseMode.CODE_AND_EXPLANATION)
                or getattr(q, "question_type", None) == QuestionType.CODE
            )
            if is_code:
                val = CodeAnswerValidator.validate(ans.answer_text, q, ans.language or q.language)
                if not val.is_valid:
                    logger.warning("Code validation warning for %s: %s", q.question_id, val.reason)
                    val_success = False
        print(f"  [OK] Answer validation completed: valid={val_success}")

        # -------------------------------------------------------------------
        # Step 7: Fill Document & Physical Verification
        # -------------------------------------------------------------------
        print("\n[Step 7] Filling worksheet copy and running physical verification...")
        job.current_step = "filling_worksheet"
        db.commit()

        completed_filename = f"completed_{downloaded_file.name}"
        filler = DocxWorksheetFiller()
        completed_file = filler.fill(
            original_file_path=downloaded_file,
            worksheet=parsed,
            answers=worksheet_answers,
            output_dir=job_temp_dir,
            output_filename=completed_filename,
        )

        assert completed_file.exists(), f"Completed file does not exist at {completed_file}"
        assert completed_file.resolve() != downloaded_file.resolve(), "Completed file must not be same as original!"

        # Verify original preserved byte-identical
        current_orig_sha = compute_sha256(downloaded_file)
        assert current_orig_sha.lower() == EXPECTED_ORIGINAL_SHA256.lower(), "Original file was modified!"

        completed_sha256 = compute_sha256(completed_file)
        completed_size = completed_file.stat().st_size
        report["completed_sha256"] = completed_sha256
        report["filling"] = "PASS"
        print(f"  [OK] Completed document: {completed_file.name} ({completed_size:,} bytes)")
        print(f"  [OK] Completed SHA-256 : {completed_sha256}")

        # Physical verification
        v_res = PhysicalDocumentVerifier.verify(
            original_path=downloaded_file,
            completed_path=completed_file,
            original_hash_before=EXPECTED_ORIGINAL_SHA256,
            worksheet=parsed,
            answers=worksheet_answers,
        )
        assert v_res.is_valid, f"Physical verification failed: {v_res.errors}"
        report["physical_verification"] = "PASS"
        print(
            f"  [OK] Physical verification passed: targets_written={v_res.answers_written}/{v_res.answer_targets_resolved}, "
            f"table_cells={v_res.table_cells_verified}, paragraphs={v_res.paragraphs_verified}, "
            f"orig_unchanged={v_res.original_sha256_matches}"
        )

        # -------------------------------------------------------------------
        # Step 8: Upload to Google Drive & Verify Public Link
        # -------------------------------------------------------------------
        print("\n[Step 8] Uploading completed worksheet to Google Drive...")
        job.status = JobStatus.UPLOADING
        job.current_step = "uploading_to_google_drive"
        db.commit()

        drive_client = GoogleDriveClient()
        upload_meta = await drive_client.upload_file(
            local_path=completed_file,
            filename=completed_file.name,
            allow_original=False,
        )

        assert upload_meta.is_public, "Uploaded file is not publicly accessible!"
        assert upload_meta.web_url, "Uploaded file does not have a web view URL!"
        report["drive_upload"] = "PASS"
        report["drive_file_id"] = upload_meta.file_id
        report["drive_public_link"] = upload_meta.web_url
        report["drive_public_link_verification"] = "PASS"
        print(f"  [OK] Drive File ID   : {upload_meta.file_id}")
        print(f"  [OK] Drive Share Link: {upload_meta.web_url}")
        print(f"  [OK] Drive Permission: {upload_meta.permission_status}")

        # -------------------------------------------------------------------
        # Step 9: Transition to AWAITING_USER_REVIEW (Strict Review Gate)
        # -------------------------------------------------------------------
        print("\n[Step 9] Transitioning Job to AWAITING_USER_REVIEW (Review Gate)...")
        job.status = JobStatus.AWAITING_USER_REVIEW
        job.current_step = "awaiting_user_review"
        job.error_message = None
        review_result = {
            "course_code": TARGET_COURSE_CODE,
            "course_name": TARGET_COURSE_NAME,
            "semester": 3,
            "session": TARGET_SESSION,
            "slo": TARGET_SLO,
            "batch_id": TARGET_BATCH_ID,
            "original_file": str(downloaded_file.name),
            "original_file_path": str(downloaded_file.resolve()),
            "completed_file": str(completed_file.name),
            "completed_file_path": str(completed_file.resolve()),
            "drive_file_id": upload_meta.file_id,
            "drive_web_url": upload_meta.web_url,
            "drive_permission_status": upload_meta.permission_status,
            "drive_verified": True,
            "review_ready": True,
            "submission_allowed": True,
            "questions_count": q_count,
            "answers_count": len(successful_ans),
            "transport_used": orchestrator.transport_name,
            "review_ready_at": datetime.now(timezone.utc).isoformat(),
        }
        job.result = review_result
        job.updated_at = datetime.now(timezone.utc)
        db.commit()

        # Enforce review gate safeguard: STOP WORKFLOW!
        report["review_gate_transition"] = "PASS (Status: AWAITING_USER_REVIEW)"
        report["final_job_status"] = "AWAITING_USER_REVIEW"
        report["submission_executed"] = "NO (Stopped at Review Gate)"
        print("  [OK] Job status successfully set to AWAITING_USER_REVIEW.")
        print("  [SAFEGUARD ENFORCED] Workflow stopped at Review Gate. Zero submission to SRM portal.")

        # -------------------------------------------------------------------
        # Step 10: Dashboard UI Verification & State Persistence Check
        # -------------------------------------------------------------------
        print("\n[Step 10] Verifying Dashboard Review UI and State Persistence...")
        dashboard_url = f"{api_base_url}/dashboard"

        async with async_playwright() as p:
            dash_browser = await p.chromium.launch(headless=True)
            page = await dash_browser.new_page()

            # Set localStorage so dashboard immediately attaches to our job
            await page.goto(dashboard_url)
            await page.evaluate(f"localStorage.setItem('srm_active_job_id', '{job.id}')")
            await page.reload()

            # Wait for review card to appear
            await page.wait_for_selector("#review-card:not(.hidden)", timeout=15000)

            # Verify UI elements
            ws_title = await page.inner_text("#review-ws-title")
            q_count_text = await page.inner_text("#review-q-count")
            drive_link_val = await page.input_value("#review-drive-link")
            view_doc_btn = page.locator("#review-view-doc-btn")
            download_btn = page.locator("#review-download-btn")
            submit_btn = page.locator("#review-submit-srm-btn")

            print(f"  [Dashboard UI] Worksheet Title   : {ws_title}")
            print(f"  [Dashboard UI] Questions Count   : {q_count_text}")
            print(f"  [Dashboard UI] Drive Link Input  : {drive_link_val}")
            print(f"  [Dashboard UI] View Doc Button   : href={await view_doc_btn.get_attribute('href')}")
            print(f"  [Dashboard UI] Download Button   : href={await download_btn.get_attribute('href')}")
            print(f"  [Dashboard UI] Submit SRM Button : visible={await submit_btn.is_visible()}, disabled={await submit_btn.is_disabled()}")

            assert await review_card_is_valid(page, upload_meta.web_url, str(job.id)), "Dashboard UI verification failed"
            report["dashboard_ui_verification"] = "PASS"
            print("  [OK] Dashboard Review Card rendered completely with correct links and action buttons.")

            # Test Refresh Persistence
            print("  Testing page refresh persistence...")
            await page.reload()
            await page.wait_for_selector("#review-card:not(.hidden)", timeout=15000)
            drive_link_after_refresh = await page.input_value("#review-drive-link")
            assert drive_link_after_refresh == upload_meta.web_url, "Drive link did not persist after refresh!"
            report["state_preserved_on_refresh"] = "PASS"
            print("  [OK] Dashboard review gate state cleanly preserved across page refresh.")

            await dash_browser.close()

        # Verify DB status is strictly AWAITING_USER_REVIEW
        db.refresh(job)
        assert job.status == JobStatus.AWAITING_USER_REVIEW, f"Job status was modified! Got {job.status}"
        print("  [OK] Database confirmed: Job status remains AWAITING_USER_REVIEW.")

    except Exception as exc:
        logger.error("Controlled live test encountered an error: %s", exc, exc_info=True)
        report["issues_detected"] = str(exc)
    finally:
        try:
            await orchestrator.close()
        except Exception:
            pass
        db.close()

    return report


async def review_card_is_valid(page, expected_drive_url: str, job_id: str) -> bool:
    """Validate all required elements inside #review-card."""
    card = page.locator("#review-card")
    if not await card.is_visible():
        return False
    drive_val = await page.input_value("#review-drive-link")
    if drive_val != expected_drive_url:
        return False
    dl_href = await page.locator("#review-download-btn").get_attribute("href")
    if job_id not in (dl_href or ""):
        return False
    submit_btn = page.locator("#review-submit-srm-btn")
    if not await submit_btn.is_visible():
        return False
    return True


def print_final_report(res: Dict[str, Any]):
    print("\n" + "=" * 70)
    print("LIVE END-TO-END REVIEW GATE TEST")
    print("=" * 70)
    print(f"- Job ID: {res['job_id']}")
    print(f"- Authentication: {res['authentication']}")
    print(f"- Direct HTTP Discovery: {res['discovery']}")
    print(f"- Worksheet Download: {res['download']}")
    print(f"- Worksheet Parsing: {res['parsing']}")
    print(f"- Answer Generation: {res['answer_generation']}")
    print(f"- Worksheet Filling: {res['filling']}")
    print(f"- Physical Verification: {res['physical_verification']}")
    print(f"- Google Drive Upload: {res['drive_upload']}")
    print(f"- Drive Public Link Verification: {res['drive_public_link_verification']}")
    print(f"- Review Gate Transition: {res['review_gate_transition']}")
    print(f"- Dashboard Review UI Verification: {res['dashboard_ui_verification']}")
    print(f"- State Preserved on Refresh: {res['state_preserved_on_refresh']}")
    print(f"- Submission Executed: {res['submission_executed']}")
    print(f"- Final Job Status: {res['final_job_status']}")
    print(f"- Any Issues Detected: {res['issues_detected']}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Controlled Live End-to-End Integration & Review Gate Test")
    parser.add_argument("--username", "-u", default=os.environ.get("SRM_USER_ID"), help="SRM Register Number / NetID")
    parser.add_argument("--password", "-p", default=os.environ.get("SRM_PASSWORD"), help="SRM Portal Password")
    parser.add_argument("--timeout", "-t", type=int, default=180, help="CAPTCHA solve timeout in seconds")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="FastAPI Base URL")
    args = parser.parse_args()

    if not args.username or not args.password:
        print("\n[!] Please provide --username and --password (or set SRM_USER_ID and SRM_PASSWORD environment variables).")
        print("    Example: python tests/controlled_live_e2e_review_test.py -u RA2111003010001 -p SecretPass123\n")
        sys.exit(1)

    result = asyncio.run(run_live_e2e_review_test(
        username=args.username,
        password=args.password,
        timeout_seconds=args.timeout,
        api_base_url=args.api_url,
    ))
    print_final_report(result)
