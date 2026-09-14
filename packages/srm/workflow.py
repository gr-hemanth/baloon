"""SRM eCurricula Workflow Architecture & Classification Models.

Defines discovered endpoint specifications, operation classifications,
and sanitized payload builders for SRM eCurricula operations.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, List, Optional


class OperationClassification(str, Enum):
    DIRECT_HTTP_POSSIBLE = "DIRECT_HTTP_POSSIBLE"
    BROWSER_REQUIRED = "BROWSER_REQUIRED"
    UNKNOWN = "UNKNOWN"


# Known portal configuration constants discovered from bundle inspection
PORTAL_CONFIG = {
    "BASE_URL": "https://dld.srmist.edu.in",
    "CAMPUS_SERVERS": {
        ("KTR", "Engineering & Technology"): "https://dld.srmist.edu.in/ktretecurricula",
        ("KTR", "Science & Humanities"): "https://dld.srmist.edu.in/fshecurricula",
        ("VDP", "Engineering & Technology"): "https://dld.srmist.edu.in/vdpetecurricula",
        ("RMP", "Engineering & Technology"): "https://dld.srmist.edu.in/rmpetecurricula",
        ("TRY", "Engineering & Technology"): "https://dld.srmist.edu.in/tryetecurricula",
        ("NCR", "Engineering & Technology"): "https://dld.srmist.edu.in/ncretecurricula",
    },
    "CURRICULA_SERVER_SUFFIX": "/server",
    "STATIC_KEY": "john",
}


@dataclass
class SRMEndpointSpec:
    path: str
    method: str
    description: str
    classification: OperationClassification
    request_fields: List[str]
    response_fields: List[str]
    requires_auth_header: bool = True


@dataclass
class WorkflowOperation:
    name: str
    category: str
    classification: OperationClassification
    endpoint_path: Optional[str] = None
    method: Optional[str] = None
    rationale: str = ""
    sanitized_request_schema: Dict[str, str] = field(default_factory=dict)
    sanitized_response_schema: Dict[str, str] = field(default_factory=dict)


def get_server_url_for_campus(campus: str, faculty: str) -> Optional[str]:
    """Map campus and faculty to the corresponding eCurricula portal URL."""
    campus_clean = campus.strip().upper()
    # Normalize campus abbreviations
    if "KATTANKULATHUR" in campus_clean or campus_clean == "KTR":
        code = "KTR"
    elif "VADAPALANI" in campus_clean or campus_clean == "VDP":
        code = "VDP"
    elif "RAMAPURAM" in campus_clean or campus_clean == "RMP":
        code = "RMP"
    elif "TRICHY" in campus_clean or "TIRUCHIRAPPALLI" in campus_clean or campus_clean == "TRY":
        code = "TRY"
    elif "NCR" in campus_clean or "DELHI" in campus_clean:
        code = "NCR"
    else:
        code = campus_clean

    for (c, f), link in PORTAL_CONFIG["CAMPUS_SERVERS"].items():
        if c == code and f.lower() in faculty.lower():
            return link
    return None


def classify_operation(op_name: str) -> OperationClassification:
    """Classify whether an operation can be performed via Direct HTTP or requires Browser."""
    op_lower = op_name.lower()
    if "captcha" in op_lower:
        return OperationClassification.BROWSER_REQUIRED
    elif any(k in op_lower for k in ("course", "semester", "session", "question", "worksheet", "download", "submit", "status", "profile")):
        return OperationClassification.DIRECT_HTTP_POSSIBLE
    return OperationClassification.UNKNOWN


def build_login_payload(user_id: str, password: str, key: str = "john") -> Dict[str, Any]:
    """Build payload for /curricula/login (sanitized)."""
    return {
        "USER_ID": user_id,
        "PASSWORD": password,
        "key": key,
    }


def build_getcourses_payload(user_id: str, key: str = "john") -> Dict[str, Any]:
    """Build payload for /curricula/student/home/getcourses."""
    return {
        "USER_ID": user_id,
        "key": key,
    }


def build_getquestions_payload(
    course_code: str,
    course_info: Dict[str, Any],
    session: int,
    key: str = "john"
) -> Dict[str, Any]:
    """Build payload for /curricula/student/session/getquestions."""
    return {
        "COURSE_CODE": course_code,
        "COURSE_INFO": course_info,
        "SESSION": session,
        "key": key,
        "MCQ": 5,
        "SQ": 2,
        "LQ": 1,
    }


def build_submitlink_payload(
    view_link: str,
    download_link: str,
    session: int,
    slo: int,
    course_code: str,
    course_name: str,
    batch_id: str,
    user_id: str,
    full_name: str,
    department: str,
) -> Dict[str, Any]:
    """Build payload for /curricula/student/session/submitlink (the UPDATE action)."""
    return {
        "view": view_link,
        "download": download_link,
        "fileId": 0,
        "session": f"{session}{slo}",
        "SESSION": session,
        "SLO": slo,
        "course_code": course_code,
        "course_name": course_name,
        "BATCH_ID": batch_id,
        "USER_ID": user_id,
        "FULL_NAME": full_name,
        "DEPARTMENT": department,
    }


def sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize dictionary payload removing sensitive values while preserving field names."""
    sensitive_keys = {
        "password", "passwd", "pwd", "token", "jwt", "secret", "authorization",
        "cookie", "session_id", "sessionid", "session_token", "jsessionid",
        "api_key", "client_secret", "user_id"
    }
    sanitized = {}
    for k, v in payload.items():
        k_lower = k.lower()
        if any(s in k_lower for s in sensitive_keys) or k_lower == "key":
            sanitized[k] = "[REDACTED]"
        elif isinstance(v, dict):
            sanitized[k] = sanitize_payload(v)
        else:
            sanitized[k] = v
    return sanitized
