import enum
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Enum, Text, JSON
from packages.shared.database import Base


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_FOR_CAPTCHA = "WAITING_FOR_CAPTCHA"
    DOWNLOADING = "DOWNLOADING"
    PROCESSING = "PROCESSING"
    UPLOADING = "UPLOADING"
    AWAITING_USER_REVIEW = "AWAITING_USER_REVIEW"
    SUBMITTING = "SUBMITTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(100), nullable=True, index=True)
    status = Column(
        Enum(JobStatus, native_enum=False, length=50),
        default=JobStatus.PENDING,
        nullable=False,
        index=True
    )
    
    # Target SRM parameters
    course_id = Column(String(100), nullable=True)
    semester_id = Column(String(50), nullable=True)
    subject_id = Column(String(100), nullable=True)
    worksheet_id = Column(String(100), nullable=True)

    # Execution transport metadata
    transport_mode = Column(String(20), default="auto", nullable=False)  # "http", "browser", "auto"
    current_step = Column(String(100), nullable=True)

    # CAPTCHA handling data (for WAITING_FOR_CAPTCHA state)
    captcha_challenge = Column(JSON, nullable=True)
    captcha_solution = Column(String(100), nullable=True)

    # Result & Error tracking
    result = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "status": self.status.value if isinstance(self.status, JobStatus) else self.status,
            "course_id": self.course_id,
            "semester_id": self.semester_id,
            "subject_id": self.subject_id,
            "worksheet_id": self.worksheet_id,
            "transport_mode": self.transport_mode,
            "current_step": self.current_step,
            "captcha_challenge": self.captcha_challenge,
            "result": self.result,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
