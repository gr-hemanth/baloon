import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from packages.shared.database import get_db
from packages.shared.models.job import Job, JobStatus
from packages.shared.schemas.job import JobCreate, JobResponse, CaptchaSubmit, JobSubmitRequest
from packages.srm.auth_manager import auth_manager

from apps.worker.tasks import (
    process_job,
    submit_job,
    get_job_credentials,
    store_job_credentials,
    clear_job_credentials,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(job_in: JobCreate, db: Session = Depends(get_db)):
    """Create a new automation job and dispatch to background worker."""
    # Prevent duplicate job submissions if an active job exists for this user/course/worksheet
    if not job_in.force and job_in.user_id:
        active_statuses = [
            JobStatus.PENDING,
            JobStatus.RUNNING,
            JobStatus.WAITING_FOR_CAPTCHA,
            JobStatus.DOWNLOADING,
            JobStatus.PROCESSING,
            JobStatus.UPLOADING,
            JobStatus.AWAITING_USER_REVIEW,
            JobStatus.SUBMITTING,
            JobStatus.VERIFYING,
        ]
        query = db.query(Job).filter(
            Job.user_id == job_in.user_id,
            Job.status.in_(active_statuses),
        )
        if job_in.course_id:
            query = query.filter(Job.course_id == job_in.course_id)
        if job_in.worksheet_id:
            query = query.filter(Job.worksheet_id == job_in.worksheet_id)

        existing_active = query.first()
        if existing_active:
            # Check if this active job is an orphaned/stale job (> 10 minutes with no progress)
            now = datetime.now(timezone.utc)
            ref_time = existing_active.updated_at or existing_active.created_at
            if ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=timezone.utc)
            job_age = (now - ref_time).total_seconds()

            if job_age > 600 and existing_active.status != JobStatus.AWAITING_USER_REVIEW:
                logger.warning(
                    "Auto-failing stale orphaned job %s (age: %.1fs, status: %s)",
                    existing_active.id, job_age, existing_active.status.value
                )
                existing_active.status = JobStatus.FAILED
                existing_active.current_step = "timed_out_orphaned"
                existing_active.error_message = "Job timed out or orphaned before worker execution"
                existing_active.updated_at = now
                db.commit()
            else:
                logger.info(
                    "Duplicate job submission prevented for user %s: active job %s already exists in state %s",
                    job_in.user_id,
                    existing_active.id,
                    existing_active.status.value,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"An active job '{existing_active.id}' is already processing (status: {existing_active.status.value}). Duplicate submission prevented.",
                    headers={"X-Existing-Job-Id": existing_active.id},
                )

    try:
        new_job = Job(
            user_id=job_in.user_id,
            course_id=job_in.course_id,
            semester_id=job_in.semester_id,
            subject_id=job_in.subject_id,
            worksheet_id=job_in.worksheet_id,
            transport_mode=job_in.transport_mode or "auto",
            status=JobStatus.PENDING,
            current_step="queued",
        )
        db.add(new_job)
        db.commit()
        db.refresh(new_job)
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.error("Database error creating job: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to persist job record: {exc}",
        )

    creds = dict(job_in.credentials or {})
    if job_in.session is not None:
        creds["requested_session"] = job_in.session
    if job_in.slo is not None:
        creds["requested_slo"] = job_in.slo

    # Auto-attach active auth session if already authenticated (e.g. from discovery)
    cached_session = auth_manager._sessions.get(job_in.user_id)
    if cached_session and getattr(cached_session, "is_valid", False):
        creds["auth_session"] = cached_session
        logger.info("Auto-attached existing active auth_session for user %s to job %s", job_in.user_id, new_job.id)

    if creds:
        store_job_credentials(new_job.id, creds)


    # Dispatch to Celery background worker
    try:
        process_job.delay(job_id=new_job.id, credentials=creds)
        logger.info("Job %s enqueued to Celery worker", new_job.id)
    except Exception as exc:
        logger.warning(
            "Could not dispatch to Celery broker (broker offline or eager mode disabled): %s",
            exc
        )
        # Fallback to local eager execution when broker is offline/unreachable
        try:
            logger.info("Executing job %s eagerly via local fallback", new_job.id)
            process_job.apply(args=[new_job.id], kwargs={"credentials": creds})
        except Exception as fallback_exc:
            logger.error("Local fallback execution failed for job %s: %s", new_job.id, fallback_exc)

    db.expire_all()
    db.refresh(new_job)
    return new_job


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: str, db: Session = Depends(get_db)):
    """Retrieve current status and details for an automation job."""
    db.expire_all()
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found"
        )
    return job


@router.get("", response_model=List[JobResponse])
def list_jobs(
    user_id: Optional[str] = None,
    status_filter: Optional[JobStatus] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """List existing jobs with optional filtering."""
    query = db.query(Job)
    if user_id:
        query = query.filter(Job.user_id == user_id)
    if status_filter:
        query = query.filter(Job.status == status_filter)
    jobs = query.order_by(Job.created_at.desc()).offset(offset).limit(limit).all()
    return jobs


@router.post("/{job_id}/resume", response_model=JobResponse)
@router.post("/{job_id}/captcha", response_model=JobResponse)
def submit_captcha_solution(
    job_id: str,
    captcha_data: CaptchaSubmit,
    db: Session = Depends(get_db)
):
    """Submit CAPTCHA solution and resume automation when a job is in WAITING_FOR_CAPTCHA state."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found"
        )

    if job.status != JobStatus.WAITING_FOR_CAPTCHA:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job is not waiting for CAPTCHA (current status: {job.status.value})"
        )

    sol = captcha_data.effective_solution or captcha_data.solution or captcha_data.captcha_solution
    if not sol or not sol.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing CAPTCHA solution. Please provide 'solution' or 'captcha_solution'."
        )

    sol = sol.strip()
    job.captcha_solution = sol
    job.captcha_challenge = None
    job.status = JobStatus.PENDING
    job.current_step = "captcha_submitted_resuming"
    db.commit()
    db.refresh(job)

    cached_creds = get_job_credentials(job.id) or {}
    if captcha_data.credentials:
        cached_creds.update(captcha_data.credentials)
    cached_creds["captcha_solution"] = sol
    cached_creds["captcha"] = sol
    store_job_credentials(job.id, cached_creds)

    # Re-dispatch job to Celery worker
    try:
        process_job.delay(job_id=job.id, credentials=cached_creds)
        logger.info("Resumed job %s with user-provided CAPTCHA", job.id)
    except Exception as exc:
        logger.warning(
            "Failed to re-dispatch resumed job to Celery: %s. Executing via local fallback.",
            exc
        )
        try:
            logger.info("Executing resumed job %s eagerly via local fallback", job.id)
            process_job.apply(args=[job.id], kwargs={"credentials": cached_creds})
        except Exception as fallback_exc:
            logger.error("Local fallback execution failed for resumed job %s: %s", job.id, fallback_exc)

    db.expire_all()
    db.refresh(job)
    return job


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    """Safely cancel an active or waiting job and clear ephemeral credentials."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found"
        )

    job.status = JobStatus.FAILED
    job.current_step = "cancelled_by_user"
    job.error_message = "Job cancelled by user"
    job.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)

    clear_job_credentials(job_id)
    logger.info("Job %s cancelled by user request", job_id)
    return job


@router.post("/{job_id}/submit", response_model=JobResponse)
def submit_job_to_srm(
    job_id: str,
    submit_in: Optional[JobSubmitRequest] = None,
    db: Session = Depends(get_db),
):
    """Explicitly submit the reviewed worksheet link to the SRM portal."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found",
        )

    # Check for duplicate / already active submission (Idempotency)
    if job.status in (JobStatus.SUBMITTING, JobStatus.VERIFYING):
        logger.info("Job %s is already submitting or verifying (status: %s)", job_id, job.status.value)
        return job

    if job.status == JobStatus.COMPLETED:
        logger.info("Job %s is already COMPLETED", job_id)
        return job

    # HARD TERMINAL GATE: Only AWAITING_USER_REVIEW can transition to SUBMITTING via explicit user submit
    if job.status != JobStatus.AWAITING_USER_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job cannot be submitted from status '{job.status.value}'. Must be AWAITING_USER_REVIEW.",
        )

    # Validate Drive verification
    res = dict(job.result or {})
    drive_url = res.get("drive_web_url")
    drive_file_id = res.get("drive_file_id")
    if not drive_url or not drive_file_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot submit to SRM: Google Drive link is missing or unverified.",
        )

    # Resolve ephemeral credentials
    req_creds = submit_in.credentials if submit_in else None
    cached_creds = get_job_credentials(job_id) or {}
    if req_creds:
        cached_creds.update(req_creds)
        store_job_credentials(job_id, cached_creds)

    # Transition state to SUBMITTING
    job.status = JobStatus.SUBMITTING
    job.current_step = "submitting_link_to_srm"
    job.error_message = None
    res["submission_started_at"] = datetime.now(timezone.utc).isoformat()
    job.result = res
    job.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)

    # Dispatch Celery submit_job task
    try:
        submit_job.delay(job_id=job.id, credentials=cached_creds)
        logger.info("Job %s submission dispatched to Celery worker", job.id)
    except Exception as exc:
        logger.warning(
            "Could not dispatch submission to Celery broker: %s. Executing via local fallback.",
            exc,
        )
        try:
            logger.info("Executing submission for job %s eagerly via local fallback", job.id)
            submit_job.apply(args=[job.id], kwargs={"credentials": cached_creds})
        except Exception as fallback_exc:
            logger.error("Local fallback submission failed for job %s: %s", job.id, fallback_exc)

    db.expire_all()
    db.refresh(job)
    return job


@router.get("/{job_id}/download")
def download_completed_worksheet(job_id: str, db: Session = Depends(get_db)):
    """Safely download the completed worksheet document without exposing server filesystem paths."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found",
        )

    res = job.result or {}
    file_path_str = res.get("completed_file_path")
    if not file_path_str:
        comp_file_name = res.get("completed_file")
        if not comp_file_name:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Completed worksheet document is not available for download yet",
            )
        file_path_str = comp_file_name

    file_path = Path(file_path_str)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Completed worksheet file not found on disk",
        )

    download_filename = res.get("completed_file") or file_path.name
    if not download_filename.lower().endswith(".docx"):
        download_filename = f"{download_filename}.docx"

    return FileResponse(
        path=str(file_path.resolve()),
        filename=download_filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
