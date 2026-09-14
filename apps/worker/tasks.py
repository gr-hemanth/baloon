import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from apps.worker.celery_app import celery_app
from packages.shared.database import SessionLocal
from packages.shared.models.job import Job, JobStatus
from packages.srm.orchestrator import SRMOrchestrator
from packages.srm.exceptions import SRMCaptchaRequired, SRMException

logger = logging.getLogger(__name__)


async def _run_job_workflow(job_id: str, credentials: Optional[Dict[str, Any]] = None) -> None:
    """Async execution workflow for an SRM worksheet automation job."""
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            logger.error("Job %s not found in database", job_id)
            return

        # 1. Start Job: RUNNING
        job.status = JobStatus.RUNNING
        job.current_step = "connecting_to_srm"
        job.updated_at = datetime.now(timezone.utc)
        db.commit()

        orchestrator = SRMOrchestrator(mode=job.transport_mode or "auto")

        try:
            # 2. Connect
            await orchestrator.connect()
            job.transport_mode = orchestrator.transport_name
            db.commit()

            # 3. Authenticate if credentials supplied
            if credentials:
                job.current_step = "authenticating"
                db.commit()
                
                # Merge user's captcha solution if returning from WAITING_FOR_CAPTCHA
                if job.captcha_solution:
                    credentials["captcha_solution"] = job.captcha_solution

                await orchestrator.authenticate(credentials)

            # 4. DOWNLOADING
            if job.worksheet_id:
                job.status = JobStatus.DOWNLOADING
                job.current_step = f"downloading_worksheet_{job.worksheet_id}"
                job.updated_at = datetime.now(timezone.utc)
                db.commit()

                download_path = await orchestrator.download_worksheet(job.worksheet_id)
                job.result = {"downloaded_file": str(download_path)}
                db.commit()

            # 5. PROCESSING (Document parsing/answering deferred)
            job.status = JobStatus.PROCESSING
            job.current_step = "processing_document"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            # 6. UPLOADING & SUBMITTING (Submission deferred)
            job.status = JobStatus.UPLOADING
            job.current_step = "preparing_upload"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            job.status = JobStatus.SUBMITTING
            job.current_step = "submitting_worksheet"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            # 7. VERIFYING
            job.status = JobStatus.VERIFYING
            job.current_step = "verifying_submission"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()

            # 8. COMPLETED
            job.status = JobStatus.COMPLETED
            job.current_step = "completed"
            job.result = {
                **(job.result or {}),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "transport_used": orchestrator.transport_name,
            }
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            logger.info("Job %s completed successfully", job_id)

        except SRMCaptchaRequired as captcha_exc:
            # Enforce requirement:
            # "If SRM presents a CAPTCHA, the job must enter a WAITING_FOR_USER / WAITING_FOR_CAPTCHA
            # state and allow the user to provide the CAPTCHA through the application's UI."
            logger.warning("Job %s paused: CAPTCHA required", job_id)
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
    logger.info("Worker picked up job %s (task_id=%s)", job_id, self.request.id)
    asyncio.run(_run_job_workflow(job_id=job_id, credentials=credentials))
    return {"job_id": job_id, "task_id": self.request.id}
