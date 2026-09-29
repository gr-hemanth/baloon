from datetime import datetime
from typing import Optional, Any, Dict
from pydantic import BaseModel, ConfigDict, computed_field
from packages.shared.models.job import JobStatus


class JobCreate(BaseModel):
    user_id: Optional[str] = None
    course_id: Optional[str] = None
    semester_id: Optional[str] = None
    subject_id: Optional[str] = None
    worksheet_id: Optional[str] = None
    session: Optional[int] = None
    slo: Optional[int] = None
    transport_mode: Optional[str] = "auto"  # "http", "browser", "auto"
    credentials: Optional[Dict[str, Any]] = None  # Ephemeral user credentials, never persisted to DB
    force: Optional[bool] = False  # If True, bypasses active duplicate check


class JobSubmitRequest(BaseModel):
    """Payload for explicit user-triggered SRM submission."""
    credentials: Optional[Dict[str, Any]] = None


class SRMDiscoverRequest(BaseModel):
    user_id: str
    password: str
    semester: Optional[int] = 3
    captcha_solution: Optional[str] = None
    solution: Optional[str] = None
    transport_mode: Optional[str] = "auto"

    @property
    def effective_solution(self) -> Optional[str]:
        return self.captcha_solution or self.solution


class SRMWorksheetItem(BaseModel):
    worksheet_id: Optional[str] = None
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

    @computed_field
    @property
    def review_ready(self) -> bool:
        """True when the job has uploaded and verified Drive link and is awaiting review."""
        if self.status != JobStatus.AWAITING_USER_REVIEW:
            return False
        return bool(self.result and self.result.get("review_ready"))

    @computed_field
    @property
    def completed_file_name(self) -> Optional[str]:
        """Safe completed worksheet filename without exposing filesystem paths."""
        if self.result:
            return self.result.get("completed_file") or self.result.get("completed_file_name")
        return None

    @computed_field
    @property
    def drive_file_id(self) -> Optional[str]:
        """Public Google Drive file ID."""
        if self.result:
            return self.result.get("drive_file_id")
        return None

    @computed_field
    @property
    def drive_web_view_link(self) -> Optional[str]:
        """Verified public Google Drive view link."""
        if self.result:
            return self.result.get("drive_web_url") or self.result.get("drive_web_view_link")
        return None

    @computed_field
    @property
    def drive_verified(self) -> bool:
        """True when Drive accessibility and public viewer permissions are confirmed."""
        if not self.result:
            return False
        perm = str(self.result.get("drive_permission_status", "")).upper()
        return bool(
            self.drive_file_id
            and self.drive_web_view_link
            and ("PUBLIC" in perm or "READER" in perm or perm == "VERIFIED" or self.result.get("drive_verified"))
        )

    @computed_field
    @property
    def submission_allowed(self) -> bool:
        """True when the job is in review state and Drive link is verified."""
        if self.status != JobStatus.AWAITING_USER_REVIEW:
            return False
        if self.result and self.result.get("submission_allowed") is True:
            return True
        return bool(self.drive_verified or self.drive_web_view_link)

    @computed_field
    @property
    def questions_count(self) -> Optional[int]:
        """Total questions in the worksheet."""
        if self.result:
            return self.result.get("questions_count")
        return None

    @computed_field
    @property
    def answers_count(self) -> Optional[int]:
        """Number of answers generated."""
        if self.result:
            return self.result.get("answers_count")
        return None

    @computed_field
    @property
    def submission_started_at(self) -> Optional[str]:
        """Timestamp when explicit user submission started."""
        if self.result:
            return self.result.get("submission_started_at")
        return None

    @computed_field
    @property
    def submission_verified_at(self) -> Optional[str]:
        """Timestamp when portal verified the submission."""
        if self.result:
            return self.result.get("submission_verified_at") or self.result.get("completed_at")
        return None
