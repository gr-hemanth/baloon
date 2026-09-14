from datetime import datetime
from typing import Optional, Any, Dict
from pydantic import BaseModel, ConfigDict
from packages.shared.models.job import JobStatus


class JobCreate(BaseModel):
    user_id: Optional[str] = None
    course_id: Optional[str] = None
    semester_id: Optional[str] = None
    subject_id: Optional[str] = None
    worksheet_id: Optional[str] = None
    transport_mode: Optional[str] = "auto"  # "http", "browser", "auto"
    credentials: Optional[Dict[str, str]] = None  # Ephemeral user credentials, never persisted to DB


class CaptchaSubmit(BaseModel):
    solution: str


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: Optional[str] = None
    status: JobStatus
    course_id: Optional[str] = None
    semester_id: Optional[str] = None
    subject_id: Optional[str] = None
    worksheet_id: Optional[str] = None
    transport_mode: str
    current_step: Optional[str] = None
    captcha_challenge: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
