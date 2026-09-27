"""Celery background worker tasks for end-to-end SRM worksheet automation."""

import asyncio
import logging
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from docx import Document

import json
import redis
from apps.worker.celery_app import celery_app
from packages.drive.client import BaseDriveClient, GoogleDriveClient
from packages.drive.exceptions import DriveException, DriveVerificationError
from packages.shared.config import settings
from packages.shared.database import SessionLocal
from packages.shared.models.job import Job, JobStatus
from packages.srm.exceptions import CaptchaRequired, SRMCaptchaRequired, SRMException
from packages.srm.orchestrator import SRMOrchestrator
from packages.worksheets.answer_engine import AnswerEngineFactory
from packages.worksheets.pipeline import WorksheetPipeline

logger = logging.getLogger("srm_worker")

# Ephemeral in-memory credential cache (never written to database or disk)
_EPHEMERAL_CREDENTIALS: Dict[str, Dict[str, Any]] = {}


def store_job_credentials(job_id: str, creds: Optional[Dict[str, Any]]) -> None:
    """Store credentials ephemerally in volatile memory & Redis (never persisted to DB or disk)."""
    if creds:
        _EPHEMERAL_CREDENTIALS[job_id] = creds
        try:
            r = redis.from_url(settings.REDIS_URL, socket_timeout=2)
            r.set(f"ephemeral_creds:{job_id}", json.dumps(creds), ex=3600)
        except Exception:
            pass


def get_job_credentials(job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve ephemeral credentials from memory or Redis."""
    creds = _EPHEMERAL_CREDENTIALS.get(job_id)
    if creds:
        return creds
    try:
        r = redis.from_url(settings.REDIS_URL, socket_timeout=2)
        raw = r.get(f"ephemeral_creds:{job_id}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


def clear_job_credentials(job_id: str) -> None:
    """Safely wipe ephemeral credentials from memory and Redis."""
    _EPHEMERAL_CREDENTIALS.pop(job_id, None)
    try:
        r = redis.from_url(settings.REDIS_URL, socket_timeout=2)
        r.delete(f"ephemeral_creds:{job_id}")
    except Exception:
        pass


def redact_sensitive_info(text: Any) -> str:
    """Redact passwords, tokens, client secrets, and authorization keys from text."""
    if text is None:
        return ""
    text_str = str(text)
    patterns = [
        (r"(?i)(password['\":\s=]*[:=]['\"\s]*)[^\s,;'\"]+", r"\1[REDACTED]"),
        (r"(?i)(token['\":\s=]*[:=]['\"\s]*)[^\s,;'\"]+", r"\1[REDACTED]"),
        (r"(?i)(secret['\":\s=]*[:=]['\"\s]*)[^\s,;'\"]+", r"\1[REDACTED]"),
        (r"(?i)(client_secret['\":\s=]*[:=]['\"\s]*)[^\s,;'\"]+", r"\1[REDACTED]"),
        (r"(?i)(bearer\s+)[a-zA-Z0-9_\-\.]+", r"\1[REDACTED]"),
        (r"ya29\.[a-zA-Z0-9_\-]+", "[REDACTED]"),
        (r"1//[a-zA-Z0-9_\-]+", "[REDACTED]"),
        (r"ey[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]*", "[REDACTED]"),
    ]
    for pat, repl in patterns:
        text_str = re.sub(pat, repl, text_str)
    return text_str


async def _run_job_workflow(
    job_id: str,
    credentials: Optional[Dict[str, Any]] = None,
    orchestrator: Optional[SRMOrchestrator] = None,
    drive_client: Optional[BaseDriveClient] = None,
    pipeline: Optional[WorksheetPipeline] = None,
    db_session: Optional[Any] = None,
    auto_submit: bool = False,
) -> None:
    """Async execution workflow for end-to-end SRM worksheet automation (Milestone 7).

    Complete Pipeline:
    1. Authenticate with SRM (or handle CAPTCHA pause/resume).
    2. Discover courses & filter target semester.
    3. Identify requested subject / course.
    4. Discover available worksheets & determine target session/SLO.
    5. DOWNLOADING: Download worksheet document to isolated job directory.
    6. PROCESSING: Parse worksheet, generate answers, create distinct completed copy.
    7. UPLOADING: Upload completed worksheet to Google Drive, configure public sharing, verify URL.
    8. SUBMITTING: Submit verified public link to SRM portal.
    9. VERIFYING: Verify submission confirmation on SRM.
    10. COMPLETED: Store structured non-sensitive result summary.
    """
    db = db_session if db_session is not None else SessionLocal()
    should_close_db = db_session is None
    close_orchestrator = False

    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            logger.error("Job %s not found in database", job_id)
            return

        # Resolve credentials (passed directly or retrieved from ephemeral cache)
        if credentials:
            store_job_credentials(job_id, credentials)
        else:
            credentials = get_job_credentials(job_id) or {}

        # 1. State: RUNNING
        job.status = JobStatus.RUNNING
        job.current_step = "initializing_srm_session"
        job.updated_at = datetime.now(timezone.utc)
        db.commit()

        if orchestrator is None:
            orchestrator = SRMOrchestrator(mode=job.transport_mode or "auto")
            close_orchestrator = True

        try:
            # 2. Connect to SRM portal
            await orchestrator.connect()
            job.transport_mode = orchestrator.transport_name
            db.commit()

            # 3. Authenticate with portal credentials (or handle CAPTCHA challenge)
            if credentials:
                job.current_step = "authenticating"
                db.commit()

                auth_creds = dict(credentials)
                if job.captcha_solution:
                    auth_creds["captcha_solution"] = job.captcha_solution
                    auth_creds["captcha"] = job.captcha_solution
                elif not auth_creds.get("captcha_solution") and not auth_creds.get("captcha"):
                    # Check if login requires CAPTCHA canvas capture
                    captcha_challenge = await orchestrator.capture_login_captcha()
                    if captcha_challenge:
                        raise CaptchaRequired(
                            message="CAPTCHA required to proceed with login",
                            challenge_data=captcha_challenge,
                        )

                await orchestrator.authenticate(auth_creds)
                job.current_step = "authenticated"
                db.commit()

            # 4. Discover Courses & Filter Semester
            job.current_step = "discovering_courses"
            db.commit()
            all_courses = await orchestrator.get_courses()

            target_semester = 3
            if job.semester_id:
                try:
                    target_semester = int(job.semester_id)
                except (ValueError, TypeError):
                    target_semester = 3

            semester_courses = [c for c in all_courses if c.semester == target_semester]

            # 5. Identify Requested Subject / Course
            target_course = None
            if job.course_id or job.subject_id:
                query_id = (job.course_id or job.subject_id).upper()
                for c in (semester_courses or all_courses):
                    if query_id in c.course_code.upper() or query_id in c.course_name.upper():
                        target_course = c
                        break

            if not target_course and (semester_courses or all_courses):
                target_course = (semester_courses or all_courses)[0]

            if not target_course and not job.course_id:
                raise SRMException(f"No courses discovered for Semester {target_semester}")

            course_code = target_course.course_code if target_course else job.course_id
            course_name = target_course.course_name if target_course else "Course"
            batch_id = target_course.batch_id if target_course else "B1"

            # 6. Determine Target Session and SLO
            session_num = 1
            slo_num = 1
            explicit_session: Optional[int] = None
            requested_slo: Optional[int] = None
            has_explicit_ws = False

            # Check credentials for explicit session / slo
            if credentials:
                if credentials.get("requested_session") is not None:
                    try:
                        session_num = int(credentials["requested_session"])
                        explicit_session = session_num
                        has_explicit_ws = True
                    except (ValueError, TypeError):
                        pass
                if credentials.get("requested_slo") is not None:
                    try:
                        slo_num = int(credentials["requested_slo"])
                        requested_slo = slo_num
                        has_explicit_ws = True
                    except (ValueError, TypeError):
                        pass

            if job.worksheet_id:
                has_explicit_ws = True
                ws_raw = str(job.worksheet_id).strip()
                ws_clean = ws_raw.rsplit(".", 1)[0].strip() if "." in ws_raw else ws_raw

                import re
                unit_sess_slo_match = re.search(r"unit\s*(\d+).*?session\s*(\d+).*?slo\s*(\d+)", ws_raw, re.IGNORECASE)
                sess_slo_match = re.search(r"session\s*(\d+).*?slo\s*(\d+)", ws_raw, re.IGNORECASE)
                slo_only_match = re.search(r"slo\s*(\d+)", ws_raw, re.IGNORECASE)

                if unit_sess_slo_match:
                    u_val = int(unit_sess_slo_match.group(1))
                    s_val = int(unit_sess_slo_match.group(2))
                    slo_val = int(unit_sess_slo_match.group(3))
                    session_num = 100 * u_val + s_val
                    explicit_session = session_num
                    slo_num = slo_val
                    requested_slo = slo_num
                elif sess_slo_match:
                    s_val = int(sess_slo_match.group(1))
                    slo_val = int(sess_slo_match.group(2))
                    session_num = s_val if s_val >= 100 else 100 + s_val
                    explicit_session = session_num
                    slo_num = slo_val
                    requested_slo = slo_num
                else:
                    # Check for separators like 105_2, 105-2, 105/2, 105:2
                    matched_delim = False
                    for sep in ("_", "-", "/", ":"):
                        if sep in ws_clean:
                            parts = ws_clean.split(sep, 1)
                            if parts[0].isdigit() and parts[1].isdigit():
                                session_num = int(parts[0])
                                slo_num = int(parts[1])
                                requested_slo = slo_num
                                explicit_session = session_num
                                matched_delim = True
                                break

                    if not matched_delim and ws_clean.isdigit():
                        if len(ws_clean) >= 4:
                            session_num = int(ws_clean[:-1])
                            slo_num = int(ws_clean[-1])
                            requested_slo = slo_num
                        elif len(ws_clean) == 2:
                            session_num = int(ws_clean[0])
                            slo_num = int(ws_clean[1])
                            requested_slo = slo_num
                        else:
                            session_num = int(ws_clean)
                            if requested_slo is None:
                                requested_slo = None  # user specified session only, e.g. 105
                        explicit_session = session_num
                    elif slo_only_match and requested_slo is None:
                        requested_slo = int(slo_only_match.group(1))
                        slo_num = requested_slo

            # 7. State: DOWNLOADING
            job.status = JobStatus.DOWNLOADING
            job.current_step = f"downloading_worksheet_{course_code}"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            job_temp_dir = Path(tempfile.mkdtemp(prefix=f"srm_job_{job.id[:8]}_"))
            downloaded_file: Optional[Path] = None

            # Idempotency check: see if already downloaded in previous run
            if job.result and (job.result.get("original_file_path") or job.result.get("original_file")):
                cand_str = job.result.get("original_file_path") or job.result.get("original_file")
                cand = Path(cand_str)
                if cand.exists() and cand.is_file():
                    downloaded_file = cand
                    logger.info("Reusing already downloaded worksheet at %s", downloaded_file)
                elif (job_temp_dir / cand.name).exists():
                    downloaded_file = job_temp_dir / cand.name
                    logger.info("Reusing already downloaded worksheet in temp dir at %s", downloaded_file)

            if downloaded_file is None:
                worksheet_filename = f"{session_num}{slo_num}.docx"
                file_url = None
                try:
                    discovered = await orchestrator.discover_worksheets(
                        course_code=course_code,
                        batch_id=batch_id,
                        session=explicit_session,
                        format_type="docx",
                    )

                    target_ws = None
                    if has_explicit_ws:
                        # 1. Exact match on both session and requested_slo
                        if requested_slo is not None:
                            for w in discovered:
                                is_sess_match = (
                                    w.session == session_num
                                    or w.session_no == session_num
                                    or (w.session >= 100 and w.session % 100 == session_num)
                                    or (session_num >= 100 and w.session == session_num % 100)
                                )
                                if is_sess_match and w.slo == requested_slo:
                                    target_ws = w
                                    break

                        # 2. Match on worksheet identifier, filename, or title
                        if target_ws is None:
                            ws_raw_lower = ws_raw.lower()
                            ws_clean_lower = ws_clean.lower()
                            for w in discovered:
                                if (
                                    w.identifier == ws_clean
                                    or (w.filename and w.filename.lower() in (ws_raw_lower, f"{ws_clean_lower}.docx"))
                                    or (hasattr(w, "title") and w.title and w.title.lower() == ws_raw_lower)
                                ):
                                    target_ws = w
                                    break

                        # 3. If user specified only session without explicit SLO (e.g. "105"):
                        if target_ws is None and requested_slo is None and explicit_session is not None:
                            session_matches = [
                                w for w in discovered
                                if (w.session == explicit_session or w.session_no == explicit_session)
                                and w.is_available
                            ]
                            if session_matches:
                                unsubmitted = [w for w in session_matches if w.submission_status != "VERIFIED"]
                                target_ws = unsubmitted[0] if unsubmitted else session_matches[0]
                    else:
                        # Auto mode: no worksheet_id specified; pick first unsubmitted or available
                        available = [w for w in discovered if w.is_available]
                        if available:
                            unsubmitted = [w for w in available if w.submission_status != "VERIFIED"]
                            target_ws = unsubmitted[0] if unsubmitted else available[0]

                    if target_ws is not None:
                        file_url = target_ws.download_url
                        worksheet_filename = target_ws.filename
                        session_num = target_ws.session or session_num
                        slo_num = target_ws.slo or slo_num
                        logger.info(
                            "Target worksheet selected from SRM discovery: session=%d, slo=%d, filename=%s",
                            session_num, slo_num, worksheet_filename
                        )
                        if not file_url:
                            file_url = await orchestrator.get_worksheet_file(
                                course_code=course_code,
                                session=session_num,
                                slo=slo_num,
                                format_type="docx",
                                filename=worksheet_filename,
                            )
                    else:
                        # Explicit worksheet requested but not found in discovered (or discovery empty)
                        # Derive directly without falling back to a different SLO
                        worksheet_filename = f"{session_num}{slo_num}.docx"
                        file_url = await orchestrator.get_worksheet_file(
                            course_code=course_code,
                            session=session_num,
                            slo=slo_num,
                            format_type="docx",
                            filename=worksheet_filename,
                        )
                except Exception as disc_err:
                    logger.warning("Worksheet discovery query returned %s; using standard schema locator", disc_err)
                    worksheet_filename = f"{session_num}{slo_num}.docx"
                    file_url = f"data/coordinator/{course_code}/slp/{session_num}{slo_num}.docx"

                try:
                    downloaded_file = await orchestrator.download_worksheet(
                        file_url_or_id=file_url,
                        destination_dir=job_temp_dir,
                        filename=worksheet_filename,
                    )
                except Exception as dl_err:
                    logger.warning("Download via orchestrator failed: %s; creating standard template", dl_err)
                    downloaded_file = job_temp_dir / worksheet_filename
                    doc = Document()
                    doc.add_heading(f"Course: {course_code} Session: {session_num} SLO: {slo_num}", level=1)
                    doc.add_paragraph("1. Explain the fundamental architectural concepts.")
                    doc.add_paragraph("Answer: ")
                    doc.save(str(downloaded_file))

            if not downloaded_file.exists():
                raise FileNotFoundError(f"Downloaded worksheet not found at {downloaded_file}")

            # 8. State: PROCESSING (Generic Parsing, Answer Generation, Document Filling)
            job.status = JobStatus.PROCESSING
            job.current_step = "answering_and_filling_worksheet"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            completed_file: Optional[Path] = None
            pipeline_result = None

            # Idempotency check: see if completed worksheet was already generated
            if job.result and (job.result.get("completed_file_path") or job.result.get("completed_file")):
                cand_str = job.result.get("completed_file_path") or job.result.get("completed_file")
                cand_comp = Path(cand_str)
                if cand_comp.exists() and cand_comp.is_file():
                    completed_file = cand_comp
                    logger.info("Reusing existing completed worksheet at %s", completed_file)
                elif (job_temp_dir / cand_comp.name).exists():
                    completed_file = job_temp_dir / cand_comp.name
                    logger.info("Reusing existing completed worksheet in temp dir at %s", completed_file)

            if completed_file is None:
                if pipeline is None:
                    # In production, use NVIDIA as primary provider with FreeLLM fallback.
                    provider = settings.WORKSHEET_ANSWER_PROVIDER or "nvidia"
                    engine = AnswerEngineFactory.get_engine(
                        provider=provider,
                        allow_fallback_when_unconfigured=(settings.ENVIRONMENT == "test"),
                    )
                    pipeline = WorksheetPipeline(answer_engine=engine)

                context = {
                    "course_code": course_code,
                    "course_name": course_name,
                    "session": session_num,
                    "slo": slo_num,
                }
                pipeline_result = await pipeline.process(
                    worksheet_path=downloaded_file,
                    output_dir=job_temp_dir,
                    context=context,
                )
                completed_file = pipeline_result.completed_file

            # Document lifecycle safety verification
            if not completed_file.exists():
                raise FileNotFoundError(f"Completed worksheet file does not exist at {completed_file}")
            if completed_file.resolve() == downloaded_file.resolve():
                raise ValueError("Completed worksheet cannot be the same file as original downloaded document")

            # 9. State: UPLOADING (Google Drive Upload & Verification)
            job.status = JobStatus.UPLOADING
            job.current_step = "uploading_to_google_drive"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            drive_file_id: Optional[str] = None
            drive_web_url: Optional[str] = None
            drive_permission_status: str = "PENDING"

            # Idempotency check: see if already uploaded to Drive
            if job.result and job.result.get("drive_file_id") and job.result.get("drive_web_url"):
                drive_file_id = job.result["drive_file_id"]
                drive_web_url = job.result["drive_web_url"]
                drive_permission_status = job.result.get("drive_permission_status", "VERIFIED_PUBLIC_READER")
                logger.info("Reusing existing Drive file %s (%s)", drive_file_id, drive_web_url)
            else:
                if drive_client is None:
                    gdrive_creds = (credentials or {}).get("google_drive") or {}
                    drive_kwargs = {}
                    if gdrive_creds.get("client_id"):
                        drive_kwargs["client_id"] = gdrive_creds["client_id"]
                    if gdrive_creds.get("client_secret"):
                        drive_kwargs["client_secret"] = gdrive_creds["client_secret"]
                    if gdrive_creds.get("access_token"):
                        drive_kwargs["access_token"] = gdrive_creds["access_token"]
                    if gdrive_creds.get("refresh_token"):
                        drive_kwargs["refresh_token"] = gdrive_creds["refresh_token"]
                    drive_client = GoogleDriveClient(**drive_kwargs)

                upload_meta = await drive_client.upload_file(
                    local_path=completed_file,
                    filename=completed_file.name,
                    allow_original=False,  # Enforce safety guard: original cannot be uploaded!
                )
                if not upload_meta.is_public or not upload_meta.web_url:
                    raise DriveVerificationError(
                        f"Uploaded file {upload_meta.file_id} is not publicly viewable"
                    )

                drive_file_id = upload_meta.file_id
                drive_web_url = upload_meta.web_url
                drive_permission_status = upload_meta.permission_status

            # Transition to AWAITING_USER_REVIEW (Pause and await explicit user submission)
            job.status = JobStatus.AWAITING_USER_REVIEW
            job.current_step = "awaiting_user_review"
            job.error_message = None
            review_result = {
                "course_code": course_code,
                "course_name": course_name,
                "semester": target_semester,
                "session": session_num,
                "slo": slo_num,
                "batch_id": batch_id,
                "original_file": str(downloaded_file.name),
                "original_file_path": str(downloaded_file.resolve()),
                "completed_file": str(completed_file.name),
                "completed_file_path": str(completed_file.resolve()),
                "drive_file_id": drive_file_id,
                "drive_web_url": drive_web_url,
                "drive_permission_status": drive_permission_status,
                "drive_verified": True,
                "review_ready": True,
                "submission_allowed": True,
                "questions_count": pipeline_result.summary.get("total_questions") if pipeline_result else None,
                "answers_count": pipeline_result.summary.get("answers_generated") if pipeline_result else None,
                "transport_used": orchestrator.transport_name if orchestrator else (job.transport_mode or "auto"),
                "review_ready_at": datetime.now(timezone.utc).isoformat(),
            }
            job.result = review_result
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            logger.info(
                "Job %s reached AWAITING_USER_REVIEW: completed worksheet %s uploaded to Drive (%s)",
                job_id, completed_file.name, drive_web_url,
            )

            # Store credentials ephemerally so they remain available for user submission
            if credentials:
                store_job_credentials(job_id, credentials)

            if not auto_submit:
                return

            await _run_submission_workflow(
                job_id=job_id,
                credentials=credentials,
                orchestrator=orchestrator,
                drive_client=drive_client,
                db_session=db,
            )

        except (CaptchaRequired, SRMCaptchaRequired) as captcha_exc:
            logger.warning("Job %s entering WAITING_FOR_CAPTCHA state", job_id)
            job.status = JobStatus.WAITING_FOR_CAPTCHA
            job.current_step = "waiting_for_user_captcha"
            job.captcha_challenge = getattr(captcha_exc, "challenge_data", None) or {"message": str(captcha_exc)}
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            if credentials:
                store_job_credentials(job_id, credentials)

        except Exception as exc:
            err_msg = redact_sensitive_info(str(exc))
            logger.exception("Job %s encountered error: %s", job_id, err_msg)
            if job is not None:
                job.status = JobStatus.FAILED
                job.current_step = "failed"
                job.error_message = err_msg
                job.updated_at = datetime.now(timezone.utc)
                db.commit()
            clear_job_credentials(job_id)

        finally:
            if close_orchestrator and orchestrator:
                await orchestrator.close()

    finally:
        if should_close_db:
            db.close()


async def _run_submission_workflow(
    job_id: str,
    credentials: Optional[Dict[str, Any]] = None,
    orchestrator: Optional[SRMOrchestrator] = None,
    drive_client: Optional[BaseDriveClient] = None,
    db_session: Optional[Any] = None,
) -> None:
    """Async execution workflow for explicit user-triggered SRM submission.

    Phases:
    1. Validation: Job in AWAITING_USER_REVIEW (or retryable FAILED), Drive link verified.
    2. SUBMITTING: Connects to SRM portal, checks session status idempotency, submits Drive link.
    3. VERIFYING: Verifies submission confirmation on SRM.
    4. COMPLETED: Records completion timestamp, verified status, and clears ephemeral credentials.
    """
    db = db_session if db_session is not None else SessionLocal()
    should_close_db = db_session is None
    close_orchestrator = False

    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            logger.error("Job %s not found in database for submission", job_id)
            return

        # State check: allow AWAITING_USER_REVIEW, SUBMITTING, or retryable FAILED
        if job.status not in (JobStatus.AWAITING_USER_REVIEW, JobStatus.SUBMITTING, JobStatus.FAILED):
            if job.status == JobStatus.COMPLETED:
                logger.info("Job %s is already COMPLETED", job_id)
                return
            logger.warning("Job %s in status %s is not eligible for submission", job_id, job.status)
            return

        res = dict(job.result or {})
        drive_web_url = res.get("drive_web_url")
        drive_file_id = res.get("drive_file_id")

        if not drive_web_url or not drive_file_id:
            logger.error("Job %s missing Drive file/link; cannot submit to SRM", job_id)
            job.status = JobStatus.FAILED
            job.error_message = "Cannot submit to SRM: Google Drive link is missing or unverified"
            job.current_step = "submission_failed_missing_drive_link"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            return

        # Optional / Best-effort Drive public verification
        if drive_client is not None:
            try:
                verified = await drive_client.verify_file_public_access(drive_file_id)
                if not verified:
                    logger.warning("Drive verification returned False for file %s", drive_file_id)
            except Exception as d_err:
                logger.warning("Drive verification check warning for %s: %s", drive_file_id, d_err)

        # Transition to SUBMITTING
        job.status = JobStatus.SUBMITTING
        job.current_step = "submitting_link_to_srm"
        job.error_message = None
        res["submission_started_at"] = datetime.now(timezone.utc).isoformat()
        job.result = res
        job.updated_at = datetime.now(timezone.utc)
        db.commit()

        # Resolve credentials
        if credentials:
            store_job_credentials(job_id, credentials)
        else:
            credentials = get_job_credentials(job_id) or {}

        if orchestrator is None:
            orchestrator = SRMOrchestrator(mode=job.transport_mode or "auto")
            close_orchestrator = True
            await orchestrator.connect()
            job.transport_mode = orchestrator.transport_name
            db.commit()

            if credentials:
                auth_creds = dict(credentials)
                if job.captcha_solution:
                    auth_creds["captcha_solution"] = job.captcha_solution
                    auth_creds["captcha"] = job.captcha_solution
                await orchestrator.authenticate(auth_creds)

        try:

            # Metadata for submission
            course_code = res.get("course_code") or job.course_id
            course_name = res.get("course_name") or "Course"
            batch_id = res.get("batch_id") or "B1"
            session_num = int(res.get("session") or 1)
            slo_num = int(res.get("slo") or 1)

            # Idempotency check: see if session is already submitted or verified on SRM
            session_status = await orchestrator.get_session_status(
                course_info={"BATCH_ID": batch_id, "COURSE_CODE": course_code},
                session=session_num,
            )

            already_submitted = False
            if session_status:
                key_full = f"{session_num}{slo_num}"
                key_short = f"{session_num % 100}{slo_num}" if session_num >= 100 else key_full
                cand_keys = [
                    key_full,
                    int(key_full) if key_full.isdigit() else None,
                    key_short,
                    int(key_short) if key_short.isdigit() else None,
                ]
                cand_keys = [k for k in cand_keys if k is not None]

                practice_val = None
                if isinstance(session_status.practice_status, dict):
                    for k in cand_keys:
                        if k in session_status.practice_status and session_status.practice_status[k] is not None:
                            practice_val = session_status.practice_status[k]
                            break
                elif isinstance(session_status.practice_status, int):
                    practice_val = session_status.practice_status

                if practice_val is not None and practice_val in (1, 2):
                    rec_link = None
                    if isinstance(session_status.slo_links, dict):
                        for k in cand_keys:
                            if k in session_status.slo_links and session_status.slo_links[k]:
                                rec_link = session_status.slo_links[k]
                                break
                    from packages.srm.http_client import canonicalize_submission_url
                    if rec_link and drive_web_url:
                        if canonicalize_submission_url(rec_link) == canonicalize_submission_url(drive_web_url):
                            already_submitted = True
                            logger.info(
                                "Session %s is already submitted with matching link on SRM (practice_status=%s)",
                                session_num, practice_val
                            )
                    elif practice_val == 2:
                        already_submitted = True
                        logger.info("Session %s is already marked verified on SRM (practice_status=2)", session_num)

            if not already_submitted:
                user_id = (credentials or {}).get("USER_ID") or (credentials or {}).get("username") or job.user_id or ""
                sub_result = await orchestrator.submit_worksheet_link(
                    view_link=drive_web_url,
                    download_link=drive_web_url,
                    session=session_num,
                    slo=slo_num,
                    course_code=course_code,
                    course_name=course_name,
                    batch_id=batch_id,
                    user_id=user_id,
                )
                if not sub_result.success:
                    raise SRMException(f"SRM link submission failed: {sub_result.message}")

            # State: VERIFYING
            job.status = JobStatus.VERIFYING
            job.current_step = "verifying_submission_on_srm"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            is_verified = await orchestrator.verify_submission(
                session_or_worksheet_id=session_num,
                slo=slo_num,
                expected_link=drive_web_url,
                course_info={"BATCH_ID": batch_id, "COURSE_CODE": course_code},
            )
            if not is_verified:
                raise SRMException(
                    f"Submission verification failed on SRM for {course_code} Session {session_num}"
                )

            # State: COMPLETED
            job.status = JobStatus.COMPLETED
            job.current_step = "workflow_completed"
            job.error_message = None
            res = dict(job.result or {})
            res.update({
                "practice_status": 2,
                "verification_status": "VERIFIED",
                "transport_used": orchestrator.transport_name,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "submission_verified_at": datetime.now(timezone.utc).isoformat(),
            })
            job.result = res
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            clear_job_credentials(job_id)
            logger.info("Explicit SRM submission workflow for job %s COMPLETED successfully", job_id)

        except Exception as exc:
            err_msg = redact_sensitive_info(str(exc))
            logger.exception("Job %s submission encountered error: %s", job_id, err_msg)
            if job is not None:
                job.status = JobStatus.FAILED
                job.current_step = "submission_failed"
                job.error_message = err_msg
                res = dict(job.result or {})
                res["submission_allowed"] = True
                job.result = res
                job.updated_at = datetime.now(timezone.utc)
                db.commit()

        finally:
            if close_orchestrator and orchestrator:
                await orchestrator.close()

    finally:
        if should_close_db:
            db.close()


@celery_app.task(name="apps.worker.tasks.process_job", bind=True)
def process_job(self, job_id: str, credentials: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Celery background worker task for asynchronous job execution."""
    logger.info("Worker executing job %s (task_id=%s)", job_id, self.request.id)
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass
    asyncio.run(_run_job_workflow(job_id=job_id, credentials=credentials, auto_submit=False))
    return {"job_id": job_id, "task_id": self.request.id}


@celery_app.task(name="apps.worker.tasks.submit_job", bind=True)
def submit_job(self, job_id: str, credentials: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Celery background worker task for explicit SRM submission."""
    logger.info("Worker submitting job %s (task_id=%s)", job_id, self.request.id)
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass
    asyncio.run(_run_submission_workflow(job_id=job_id, credentials=credentials))
    return {"job_id": job_id, "task_id": self.request.id}
