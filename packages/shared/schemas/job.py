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
    credentials: Optional[Dict[str, Any]] = None  # Ephemeral user credentials, never persisted to DB
    force: Optional[bool] = False  # If True, bypasses active duplicate check


class SRMDiscoverRequest(BaseModel):
    user_id: str
    password: str
    semester: Optional[int] = 3
    captcha_solution: Optional[str] = None
    transport_mode: Optional[str] = "auto"


class SRMWorksheetItem(BaseModel):
    session: int
    slo: int
    filename: str
    format: str
    is_available: bool
    submission_status: str
    title: Optional[str] = None
    download_url: Optional[str] = None


class SRMCourseItem(BaseModel):
    course_code: str
    course_name: str
    batch_id: str
    semester: int
    department: Optional[str] = None
    worksheets: list[SRMWorksheetItem] = []


class SRMDiscoverResponse(BaseModel):
    status: str
    message: Optional[str] = None
    semester: int
    courses: list[SRMCourseItem] = []
    captcha_challenge: Optional[Dict[str, Any]] = None


class CaptchaSubmit(BaseModel):
    solution: Optional[str] = None
    captcha_solution: Optional[str] = None
    credentials: Optional[Dict[str, Any]] = None

    @property
    def effective_solution(self) -> Optional[str]:
        return self.solution or self.captcha_solution


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
