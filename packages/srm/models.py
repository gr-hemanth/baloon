from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List


@dataclass
class SRMCourse:
    id: str
    name: str
    code: Optional[str] = None
    department: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


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
class SRMWorksheet:
    id: str
    title: str
    subject_id: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    status: str = "PENDING"  # PENDING, SUBMITTED, EVALUATED
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
    resource_type: str  # document, xhr, fetch, stylesheet, script, etc.
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
