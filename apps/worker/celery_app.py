import os
from celery import Celery
from packages.shared.config import settings

celery_app = Celery(
    "srm_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["apps.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,       # 1 hour max
    task_soft_time_limit=3300,  # 55 minutes soft limit
    worker_prefetch_multiplier=1,
)

# Allow local test execution without live Redis if CELERY_TASK_ALWAYS_EAGER=True
if os.getenv("CELERY_TASK_ALWAYS_EAGER", "False").lower() in ("true", "1"):
    celery_app.conf.task_always_eager = True
