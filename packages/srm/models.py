from dataclasses import dataclass, field
from datetime import datetime, timezone
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
