from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List


@dataclass
class SRMCourse:
    course_code: str
    course_name: str
    semester: int
    batch_id: str
    department: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.course_code

    @property
    def name(self) -> str:
        return self.course_name

    @property
    def code(self) -> str:
        return self.course_code


@dataclass
class SRMSemester:
    id: str
    name: str
    academic_year: Optional[str] = None
    is_current: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SRMSubject:
    id: str
    name: str
    code: Optional[str] = None
    semester_id: Optional[str] = None
    faculty_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SRMQuestionSet:
    course_code: str
    session: int
    mcq: List[Dict[str, Any]] = field(default_factory=list)
    sq: List[Dict[str, Any]] = field(default_factory=list)
    lq: List[Dict[str, Any]] = field(default_factory=list)
    video: List[Dict[str, Any]] = field(default_factory=list)
    slo: Dict[str, Any] = field(default_factory=dict)
    sp: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SRMSessionStatus:
    session: int
    practice_status: Dict[str, Any] = field(default_factory=dict)
    slo_links: Dict[str, Any] = field(default_factory=dict)
    skillq_slo1: int = 0
    skillq_slo2: int = 0
    raw_result: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SRMCourseStatus:
    course_code: str
    session_count: List[Dict[str, Any]] = field(default_factory=list)
    available_slp: List[int] = field(default_factory=list)  # Uploaded DOCX worksheet IDs
    available_slppdf: List[int] = field(default_factory=list)  # Uploaded PDF worksheet IDs
    available_practice: List[int] = field(default_factory=list)
    assessments: List[int] = field(default_factory=list)
    raw_result: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SRMWorksheetMetadata:
    course_code: str
    session: int
    slo: int
    filename: str
    format: str  # "docx" or "pdf"
    storage_path: str  # e.g. "data/coordinator/21CSC303J/slp"
    unit: int = 1
    session_no: int = 1
    download_url: Optional[str] = None
    is_available: bool = False
    submission_status: str = "NOT_SUBMITTED"  # "NOT_SUBMITTED", "PENDING", "VERIFIED", "RESUBMISSION"
    submitted_link: Optional[str] = None
    title: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def identifier(self) -> str:
        return f"{self.session}{self.slo}"


@dataclass
class SRMWorksheetFile:
    file_url: str
    filename: str
    path: str
    server: str
    local_path: Optional[str] = None


@dataclass
class SRMSubmissionResult:
    success: bool
    message: str
    returned_link: Optional[str] = None
    session: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SRMWorksheet:
    id: str
    title: str
    subject_id: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    status: str = "PENDING"
    download_url: Optional[str] = None
    submission_url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SRMSubmissionReceipt:
    worksheet_id: str
    submitted_at: datetime
    verification_status: str  # PENDING_VERIFICATION, VERIFIED, FAILED
    submission_id: Optional[str] = None
    message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkDiscoveryEntry:
    url: str
    method: str
    resource_type: str
    status_code: Optional[int] = None
    content_type: Optional[str] = None
    timestamp: str = ""
    is_api_candidate: bool = False


@dataclass
class DiscoveryReport:
    initial_url: str
    final_url: str
    page_title: str
    captured_at: str
    total_requests: int
    navigation_requests: List[NetworkDiscoveryEntry] = field(default_factory=list)
    xhr_fetch_requests: List[NetworkDiscoveryEntry] = field(default_factory=list)
    other_requests: List[NetworkDiscoveryEntry] = field(default_factory=list)
    detected_endpoints: List[str] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SRMAuthSession:
    """Clean representation of an authenticated SRM portal session.
    
    Contains JWT tokens, cookies, authorization headers, and expiry timestamps
    captured from browser-assisted authentication or direct exchange.
    Decoupled from Playwright internals.
    """
    access_token: str
    cookies: Dict[str, str] = field(default_factory=dict)
    user_id: Optional[str] = None
    user_data: Dict[str, Any] = field(default_factory=dict)
    authenticated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def token(self) -> str:
        """Alias for access_token."""
        return self.access_token

    @property
    def is_valid(self) -> bool:
        """Verify whether session has access token and is unexpired."""
        if not self.access_token:
            return False
        if self.expires_at is not None:
            now = datetime.now(timezone.utc)
            exp = self.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now > exp:
                return False
        return True

    @property
    def is_expired(self) -> bool:
        """Return True if session has expired or is invalid."""
        return not self.is_valid

    def get_auth_headers(self) -> Dict[str, str]:
        """Return authorization headers for HTTP requests."""
        h = dict(self.headers)
        if self.access_token:
            h["Authorization"] = self.access_token
        return h

    def to_dict(self) -> Dict[str, Any]:
        """Serialize session to dictionary."""
        return {
            "access_token": self.access_token,
            "cookies": self.cookies,
            "user_id": self.user_id,
            "user_data": self.user_data,
            "authenticated_at": self.authenticated_at.isoformat() if self.authenticated_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "headers": self.headers,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SRMAuthSession":
        """Reconstruct session from dictionary."""
        auth_at = data.get("authenticated_at")
        if isinstance(auth_at, str):
            try:
                auth_at = datetime.fromisoformat(auth_at)
            except Exception:
                auth_at = datetime.now(timezone.utc)
        elif not isinstance(auth_at, datetime):
            auth_at = datetime.now(timezone.utc)

        exp_at = data.get("expires_at")
        if isinstance(exp_at, str):
            try:
                exp_at = datetime.fromisoformat(exp_at)
            except Exception:
                exp_at = None

        return cls(
            access_token=data.get("access_token", ""),
            cookies=dict(data.get("cookies") or {}),
            user_id=data.get("user_id"),
            user_data=dict(data.get("user_data") or {}),
            authenticated_at=auth_at,
            expires_at=exp_at,
            headers=dict(data.get("headers") or {}),
        )

    @classmethod
    def from_browser_capture(
        cls,
        token: str,
        cookies: Optional[List[Dict[str, Any]]] = None,
        user_id: Optional[str] = None,
        user_data: Optional[Dict[str, Any]] = None,
        expires_in_seconds: Optional[int] = 86400,
    ) -> "SRMAuthSession":
        """Factory creating an authenticated session from Playwright captured state."""
        cookie_dict: Dict[str, str] = {}
        if cookies:
            for c in cookies:
                if isinstance(c, dict) and "name" in c and "value" in c:
                    cookie_dict[c["name"]] = c["value"]

        exp_dt: Optional[datetime] = None
        if expires_in_seconds:
            exp_dt = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)

        extracted_user_id = user_id
        extracted_user_data = dict(user_data or {})

        if token and "." in token:
            try:
                import base64
                import json
                parts = token.split(".")
                if len(parts) >= 2:
                    p = parts[1]
                    p += "=" * ((4 - len(p) % 4) % 4)
                    payload_data = json.loads(base64.urlsafe_b64decode(p.encode("ascii")).decode("utf-8"))
                    if not extracted_user_id:
                        extracted_user_id = str(payload_data.get("USER_ID") or payload_data.get("sub") or "")
                    if "exp" in payload_data:
                        exp_dt = datetime.fromtimestamp(payload_data["exp"], tz=timezone.utc)
                    if not extracted_user_data and isinstance(payload_data, dict):
                        extracted_user_data = payload_data
            except Exception:
                pass

        return cls(
            access_token=token,
            cookies=cookie_dict,
            user_id=extracted_user_id,
            user_data=extracted_user_data,
            authenticated_at=datetime.now(timezone.utc),
            expires_at=exp_dt,
            headers={"Authorization": token} if token else {},
        )
