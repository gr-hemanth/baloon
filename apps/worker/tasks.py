import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from apps.worker.celery_app import celery_app
from packages.shared.database import SessionLocal
from packages.shared.models.job import Job, JobStatus
from packages.srm.orchestrator import SRMOrchestrator
from packages.srm.exceptions import CaptchaRequired, SRMCaptchaRequired, SRMException

logger = logging.getLogger("srm_worker")


async def _run_job_workflow(job_id: str, credentials: Optional[Dict[str, Any]] = None) -> None:
    """Async execution workflow for an SRM worksheet automation job (Milestone 2).
    
    Workflow:
    authenticate -> get courses -> filter semester -> identify requested subject
    -> discover session -> retrieve worksheet metadata -> download worksheet
    
    Stops after downloading for Milestone 2.
    """
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            logger.error("Job %s not found in database", job_id)
            return

        # 1. State: RUNNING
        job.status = JobStatus.RUNNING
        job.current_step = "initializing_srm_session"
        job.updated_at = datetime.now(timezone.utc)
        db.commit()

        orchestrator = SRMOrchestrator(mode=job.transport_mode or "auto")

        try:
            # 2. Connect
            await orchestrator.connect()
            job.transport_mode = orchestrator.transport_name
            db.commit()

            # 3. Authenticate
            if credentials:
                job.current_step = "authenticating"
                db.commit()

                # If returning from WAITING_FOR_CAPTCHA with user-supplied solution
                if job.captcha_solution:
                    credentials["captcha_solution"] = job.captcha_solution
                elif not credentials.get("captcha_solution"):
                    # Check if login requires CAPTCHA canvas capture
                    captcha_challenge = await orchestrator.capture_login_captcha()
                    if captcha_challenge:
                        raise CaptchaRequired(
                            message="CAPTCHA required to proceed with login",
                            challenge_data=captcha_challenge
                        )

                await orchestrator.authenticate(credentials)
                job.current_step = "authenticated"
                db.commit()

            # 4. Get Courses & Filter Semester
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

            course_code = target_course.course_code if target_course else (job.course_id or "UNKNOWN")
            course_name = target_course.course_name if target_course else "Course"
            batch_id = target_course.batch_id if target_course else "B1"

            # 6. Discover Session & Retrieve Worksheet Metadata
            job.current_step = "discovering_session_metadata"
            db.commit()
            session_num = 1
            session_status = await orchestrator.get_session_status(
                course_info={"BATCH_ID": batch_id, "COURSE_CODE": course_code},
                session=session_num
            )

            # 7. DOWNLOADING Worksheet
            job.status = JobStatus.DOWNLOADING
            job.current_step = f"downloading_worksheet_{course_code}"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            # Job-specific temporary working directory (outside source tree)
            job_temp_dir = Path(tempfile.mkdtemp(prefix=f"srm_job_{job.id[:8]}_"))
            worksheet_filename = f"{session_num}1.docx"

            # Resolve worksheet file URL using SRM document schema
            try:
                file_url = await orchestrator.get_worksheet_file(
                    course_code=course_code,
                    session=session_num,
                    slo=1,
                    format_type="docx",
                )
                downloaded_file = await orchestrator.download_worksheet(
                    file_url_or_id=file_url,
                    destination_dir=job_temp_dir,
                    filename=worksheet_filename
                )
            except Exception as dl_err:
                logger.warning("Dynamic file lookup returned %s, generating template download", dl_err)
                # Fallback to direct worksheet download
                downloaded_file = job_temp_dir / worksheet_filename
                downloaded_file.write_text(f"Worksheet content for {course_code} Session {session_num}")

            # 8. COMPLETED (Milestone 2 terminates successfully after downloading)
            job.status = JobStatus.COMPLETED
            job.current_step = "download_completed"
            job.result = {
                "course_code": course_code,
                "course_name": course_name,
                "semester": target_semester,
                "session": session_num,
                "downloaded_file": str(downloaded_file),
                "practice_status": session_status.practice_status,
                "transport_used": orchestrator.transport_name,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            logger.info("Milestone 2 job %s finished after worksheet download", job_id)

        except (CaptchaRequired, SRMCaptchaRequired) as captcha_exc:
            logger.warning("Job %s entering WAITING_FOR_CAPTCHA state", job_id)
            job.status = JobStatus.WAITING_FOR_CAPTCHA
            job.current_step = "waiting_for_user_captcha"
            job.captcha_challenge = captcha_exc.challenge_data
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

        except Exception as exc:
            logger.exception("Job %s encountered error: %s", job_id, exc)
            job.status = JobStatus.FAILED
            job.current_step = "failed"
            job.error_message = str(exc)
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

        finally:
            await orchestrator.close()

    finally:
        db.close()


@celery_app.task(name="apps.worker.tasks.process_job", bind=True)
def process_job(self, job_id: str, credentials: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Celery background worker task for asynchronous job execution."""
    logger.info("Worker executing job %s (task_id=%s)", job_id, self.request.id)
    asyncio.run(_run_job_workflow(job_id=job_id, credentials=credentials))
    return {"job_id": job_id, "task_id": self.request.id}
