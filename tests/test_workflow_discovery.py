import json
from pathlib import Path
from packages.srm.workflow import (
    OperationClassification,
    classify_operation,
    get_server_url_for_campus,
    build_login_payload,
    build_getcourses_payload,
    build_getquestions_payload,
    build_submitlink_payload,
    sanitize_payload,
)


def test_classify_operation():
    """Verify operation classification rules."""
    assert classify_operation("eCurricula Course Discovery") == OperationClassification.DIRECT_HTTP_POSSIBLE
    assert classify_operation("Semester Selection") == OperationClassification.DIRECT_HTTP_POSSIBLE
    assert classify_operation("Worksheet Download") == OperationClassification.DIRECT_HTTP_POSSIBLE
    assert classify_operation("Worksheet Submission") == OperationClassification.DIRECT_HTTP_POSSIBLE
    assert classify_operation("Login Captcha Solve") == OperationClassification.BROWSER_REQUIRED
    assert classify_operation("Random Mystery Operation") == OperationClassification.UNKNOWN


def test_campus_server_mapping():
    """Verify campus and faculty resolution to correct LMS portal URLs."""
    ktr_eng = get_server_url_for_campus("Kattankulathur", "Faculty of Engineering & Technology")
    assert ktr_eng == "https://dld.srmist.edu.in/ktretecurricula"

    ktr_abbr = get_server_url_for_campus("KTR", "Engineering & Technology")
    assert ktr_abbr == "https://dld.srmist.edu.in/ktretecurricula"

    vdp_eng = get_server_url_for_campus("Vadapalani", "Engineering & Technology")
    assert vdp_eng == "https://dld.srmist.edu.in/vdpetecurricula"

    rmp_eng = get_server_url_for_campus("RMP", "Engineering & Technology")
    assert rmp_eng == "https://dld.srmist.edu.in/rmpetecurricula"

    unknown = get_server_url_for_campus("UnknownCampus", "Engineering")
    assert unknown is None


def test_payload_builders():
    """Verify payload generation for discovered SRM API endpoints."""
    # 1. Login
    login_p = build_login_payload("student123", "secret_pass")
    assert login_p["USER_ID"] == "student123"
    assert login_p["PASSWORD"] == "secret_pass"
    assert login_p["key"] == "john"

    # 2. Get Courses
    courses_p = build_getcourses_payload("student123")
    assert courses_p["USER_ID"] == "student123"
    assert courses_p["key"] == "john"

    # 3. Get Questions
    questions_p = build_getquestions_payload("CSE101", {"BATCH_ID": "b1"}, 3)
    assert questions_p["COURSE_CODE"] == "CSE101"
    assert questions_p["SESSION"] == 3
    assert questions_p["MCQ"] == 5

    # 4. Submit Link (UPDATE action)
    submit_p = build_submitlink_payload(
        view_link="https://drive.google.com/view",
        download_link="https://drive.google.com/download",
        session=3,
        slo=1,
        course_code="21CSC301J",
        course_name="Operating Systems",
        batch_id="batch_4",
        user_id="RA2111003010001",
        full_name="John Doe",
        department="CINTEL"
    )
    assert submit_p["view"] == "https://drive.google.com/view"
    assert submit_p["session"] == "31"
    assert submit_p["SESSION"] == 3
    assert submit_p["SLO"] == 1
    assert submit_p["course_code"] == "21CSC301J"
    assert submit_p["BATCH_ID"] == "batch_4"


def test_sanitize_payload():
    """Verify payload sanitization redacts sensitive fields."""
    raw = {
        "USER_ID": "RA12345",
        "PASSWORD": "secretpassword",
        "key": "john",
        "authorization": "Bearer eyJhbGciOi...",
        "course_code": "CSE101",
        "session": 3
    }
    sanitized = sanitize_payload(raw)
    assert sanitized["USER_ID"] == "[REDACTED]"
    assert sanitized["PASSWORD"] == "[REDACTED]"
    assert sanitized["key"] == "[REDACTED]"
    assert sanitized["authorization"] == "[REDACTED]"
    assert sanitized["course_code"] == "CSE101"
    assert sanitized["session"] == 3


def test_discovery_artifacts_integrity():
    """Verify discovery JSON artifact exists and contains valid sanitized data."""
    json_path = Path("artifacts/srm_workflow_discovery.json")
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert "target_portal" in data
    assert "operations" in data
    assert len(data["operations"]) > 0

    # Ensure no plain passwords or tokens are stored in the artifact
    raw_text = json_path.read_text(encoding="utf-8")
    assert "secretpassword" not in raw_text
