import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from packages.shared.database import get_db
from packages.shared.models.job import Job, JobStatus
from packages.shared.schemas.job import JobCreate, JobResponse, CaptchaSubmit
from apps.worker.tasks import process_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(job_in: JobCreate, db: Session = Depends(get_db)):
    """Create a new automation job and dispatch to background worker."""
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

    # Dispatch to Celery background worker
    try:
        process_job.delay(job_id=new_job.id, credentials=job_in.credentials)
        logger.info("Job %s enqueued to Celery worker", new_job.id)
    except Exception as exc:
        logger.warning(
            "Could not dispatch to Celery broker (broker offline or eager mode disabled): %s",
            exc
        )
        # In case Celery/Redis is temporarily offline during local dev without Docker,
        # the job record remains created in PENDING state.

    return new_job


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: str, db: Session = Depends(get_db)):
    """Retrieve current status and details for an automation job."""
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


@router.post("/{job_id}/captcha", response_model=JobResponse)
def submit_captcha_solution(
    job_id: str,
    captcha_data: CaptchaSubmit,
    db: Session = Depends(get_db)
):
    """Submit CAPTCHA solution when a job is in WAITING_FOR_CAPTCHA state."""
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

    job.captcha_solution = captcha_data.solution
    job.status = JobStatus.PENDING
    job.current_step = "captcha_submitted_resuming"
    db.commit()
    db.refresh(job)

    # Re-dispatch job to Celery worker
    try:
        process_job.delay(job_id=job.id)
        logger.info("Resumed job %s with user-provided CAPTCHA", job.id)
    except Exception as exc:
        logger.warning("Failed to re-dispatch resumed job to Celery: %s", exc)

    return job
