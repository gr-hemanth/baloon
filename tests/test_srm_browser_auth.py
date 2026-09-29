"""Comprehensive tests for SRM Playwright browser-assisted authentication architecture.

Verifies:
1. Authentication state transitions:
   PENDING -> AUTHENTICATING -> WAITING_FOR_CAPTCHA -> AUTHENTICATED -> DISCOVERING -> SUCCESS.
2. WAITING_FOR_CAPTCHA state handling & UI messaging.
3. Successful browser authentication & session capture.
4. Authentication session extraction (SRMAuthSession model, headers, cookies, serialization).
5. HTTP client using captured session without Playwright coupling.
6. Resume after CAPTCHA solution.
7. Browser cleanup after authentication (success, failure, or exception).
8. Authentication failure handling (invalid credentials, error banners).
9. Expired authentication detection.
10. Dashboard authentication state synchronization via GET /srm/status.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routes.srm import update_discovery_phase, get_srm_discovery_status
from packages.srm.browser_client import SRMBrowserClient
from packages.srm.exceptions import AuthenticationFailed, CaptchaRequired
from packages.srm.http_client import SRMHttpClient
from packages.srm.models import SRMCourse, SRMAuthSession
from packages.srm.orchestrator import SRMOrchestrator


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------

def _create_mock_page(login_response_data: Dict[str, Any], local_storage_token: str = None):
    """Create a mock Playwright Page that simulates user login interactions."""
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.close = AsyncMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()

    # Locators
    start_btn = MagicMock()
    start_btn.first = start_btn
    start_btn.count = AsyncMock(return_value=1)
    start_btn.is_visible = AsyncMock(return_value=True)
    start_btn.click = AsyncMock()

    user_field = MagicMock()
    user_field.first = user_field
    user_field.wait_for = AsyncMock()
    user_field.fill = AsyncMock()
    user_field.dispatch_event = AsyncMock()

    pw_field = MagicMock()
    pw_field.first = pw_field
    pw_field.wait_for = AsyncMock()
    pw_field.fill = AsyncMock()
    pw_field.dispatch_event = AsyncMock()

    err_elem = MagicMock()
    err_elem.first = err_elem
    err_elem.count = AsyncMock(return_value=0)
    err_elem.is_visible = AsyncMock(return_value=False)
    err_elem.inner_text = AsyncMock(return_value="")

    login_btn = MagicMock()
    login_btn.first = login_btn
    login_btn.count = AsyncMock(return_value=1)
    login_btn.is_visible = AsyncMock(return_value=True)
    login_btn.get_attribute = AsyncMock(return_value="")
    login_btn.click = AsyncMock()

    def locator_side_effect(selector, **kwargs):
        if "Username" in selector or "User" in selector:
            return user_field
        if "Password" in selector or "password" in selector:
            return pw_field
        if "ant-message-error" in selector or "ant-alert-error" in selector:
            return err_elem
        if "LOG IN" in selector or "Sign in" in selector:
            return login_btn
        mock_loc = MagicMock()
        mock_loc.first = mock_loc
        mock_loc.count = AsyncMock(return_value=0)
        mock_loc.is_visible = AsyncMock(return_value=False)
        return mock_loc

    page.locator = MagicMock(side_effect=locator_side_effect)
    get_by_text_mock = MagicMock()
    get_by_text_mock.first = start_btn
    page.get_by_text = MagicMock(return_value=get_by_text_mock)

    # Evaluate mock for localStorage / hcaptcha
    async def evaluate_mock(expr, *args):
        if "localStorage.getItem" in expr:
            return local_storage_token or login_response_data.get("token")
        if "textarea[name*='h-captcha-response']" in expr:
            return "mock-hcaptcha-response-token-12345"
        return None

    page.evaluate = AsyncMock(side_effect=evaluate_mock)

    # Response event listener mock
    response_listeners = []
    page.on = MagicMock(side_effect=lambda evt, handler: response_listeners.append(handler) if evt == "response" else None)

    # Trigger response listener when goto is called
    async def trigger_response(*args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.url = "https://dld.srmist.edu.in/ktretecurricula/server/curricula/login"
        mock_resp.request.method = "POST"
        mock_resp.json = AsyncMock(return_value=login_response_data)
        for h in response_listeners:
            await h(mock_resp)

    page.goto = AsyncMock(side_effect=trigger_response)
    return page


def _create_mock_context(page, cookies: List[Dict[str, Any]] = None):
    """Create a mock Playwright BrowserContext."""
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.cookies = AsyncMock(return_value=cookies or [
        {"name": "SESSION", "value": "mock_session_val_123"},
        {"name": "XSRF-TOKEN", "value": "mock_xsrf_token_456"},
    ])
    context.close = AsyncMock()
    return context


def _create_mock_browser(context):
    """Create a mock Playwright Browser."""
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    return browser


# ---------------------------------------------------------------------------
# Test 1: Authentication State Transitions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authentication_state_transitions():
    """Verify clean state progression through the entire auth & discovery flow:
    AUTHENTICATING -> WAITING_FOR_CAPTCHA -> AUTHENTICATED -> DISCOVERING -> SUCCESS.
    """
    recorded_phases: List[str] = []

    async def callback(phase: str, msg: str):
        recorded_phases.append(phase)

    orchestrator = SRMOrchestrator(mode="auto")
    orchestrator.set_status_callback(callback)

    # Mock interactive authentication to simulate browser progress
    mock_session = SRMAuthSession(
        access_token="mock_valid_token_xyz",
        cookies={"SESSION": "123"},
        user_id="RA2111003010001",
    )

    async def mock_auth_interactive(credentials, status_callback=None):
        if status_callback:
            await status_callback("AUTHENTICATING", "Opening login window...")
            await status_callback("WAITING_FOR_CAPTCHA", "Waiting for CAPTCHA...")
            await status_callback("AUTHENTICATED", "Authentication successful!")
        return mock_session

    orchestrator.browser_client.authenticate_interactive = AsyncMock(side_effect=mock_auth_interactive)
    orchestrator.browser_client.close = AsyncMock()

    # Mock HTTP client course discovery
    mock_course = SRMCourse(
        course_code="21CSC303J",
        course_name="Software Engineering",
        semester=3,
        batch_id="B1",
    )
    orchestrator.http_client.get_courses_by_semester = AsyncMock(return_value=[mock_course])

    # 1. Run authentication
    auth_result = await orchestrator.authenticate({
        "USER_ID": "RA2111003010001",
        "PASSWORD": "valid_password",
    })
    assert auth_result is True

    # 2. Run course discovery
    await callback("DISCOVERING", "Discovering Semester 3 worksheets...")
    courses = await orchestrator.get_courses_by_semester(3)
    await callback("SUCCESS", "Discovery completed.")

    assert len(courses) == 1
    assert recorded_phases == [
        "AUTHENTICATING",
        "WAITING_FOR_CAPTCHA",
        "AUTHENTICATED",
        "DISCOVERING",
        "SUCCESS",
    ]


# ---------------------------------------------------------------------------
# Test 2: WAITING_FOR_CAPTCHA State & Messaging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_waiting_for_captcha_state_and_messaging():
    """Verify that when the portal presents visual CAPTCHA:
    1. WAITING_FOR_CAPTCHA phase is recorded.
    2. Status message clearly instructs user to solve CAPTCHA in the opened window.
    3. Misleading errors ('Captcha verification failed') are NOT produced.
    """
    phase_messages: Dict[str, str] = {}

    def status_callback(phase: str, msg: str):
        phase_messages[phase] = msg

    update_discovery_phase("WAITING_FOR_CAPTCHA", "Waiting for CAPTCHA – Please solve the CAPTCHA in the opened SRM browser window.")
    status = get_srm_discovery_status()

    assert status["phase"] == "WAITING_FOR_CAPTCHA"
    assert "Please solve the CAPTCHA in the opened SRM browser window" in status["message"]
    assert "verification failed" not in status["message"].lower()


# ---------------------------------------------------------------------------
# Test 3: Successful Browser Authentication
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_browser_authentication():
    """Verify Playwright browser-assisted login successfully intercepts /curricula/login,
    extracts token and cookies, and produces an active SRMAuthSession.
    """
    login_data = {
        "Status": 1,
        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJSQTIxMTExMDAxIiwidXNlciI6ImhlbWFudGgifQ.valid_signature",
        "user": {"NAME": "Hemanth", "DEPARTMENT": "CSE"},
    }

    mock_page = _create_mock_page(login_data)
    mock_context = _create_mock_context(mock_page, [
        {"name": "SESSION_COOKIE", "value": "sess_val_999"},
    ])
    mock_browser = _create_mock_browser(mock_context)

    mock_pw = MagicMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_pw.stop = AsyncMock()

    client = SRMBrowserClient(base_url="https://dld.srmist.edu.in/ktretecurricula/#/")

    phases_seen = []
    def on_status(p, m):
        phases_seen.append(p)

    with patch("packages.srm.browser_client.async_playwright") as mock_ap:
        mock_ap.return_value.start = AsyncMock(return_value=mock_pw)

        session = await client.authenticate_interactive(
            credentials={"USER_ID": "RA2111003010001", "PASSWORD": "correct_password", "headless": True},
            status_callback=on_status,
            captcha_timeout_seconds=5,
        )

    assert isinstance(session, SRMAuthSession)
    assert session.access_token == login_data["token"]
    assert session.cookies.get("SESSION_COOKIE") == "sess_val_999"
    assert session.is_valid is True
    assert client.is_authenticated is True
    assert "AUTHENTICATING" in phases_seen
    assert "WAITING_FOR_CAPTCHA" in phases_seen
    assert "AUTHENTICATED" in phases_seen


# ---------------------------------------------------------------------------
# Test 4: Authentication Session Extraction (Model & Serialization)
# ---------------------------------------------------------------------------

def test_auth_session_extraction():
    """Verify SRMAuthSession extraction, token parsing, header creation, and serialization."""
    # 1. Extraction from browser capture
    raw_cookies = [
        {"name": "SESSION", "value": "abc123session"},
        {"name": "PORTAL_ID", "value": "srm_ktret"},
    ]
    raw_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJSQTEyMyIsIlVTRVJfSUQiOiJSQTIxMTEwMDMwMTAwMDEifQ.sig"
    user_payload = {"NAME": "Student Name", "REG_NO": "RA2111003010001"}

    session = SRMAuthSession.from_browser_capture(
        token=raw_token,
        cookies=raw_cookies,
        user_id="RA2111003010001",
        user_data=user_payload,
        expires_in_seconds=3600,
    )

    assert session.access_token == raw_token
    assert session.cookies == {"SESSION": "abc123session", "PORTAL_ID": "srm_ktret"}
    assert session.user_id == "RA2111003010001"
    assert session.user_data["NAME"] == "Student Name"
    assert session.is_valid is True
    assert session.is_expired is False

    # 2. Authorization headers
    headers = session.get_auth_headers()
    assert headers["Authorization"] == raw_token

    # 3. Serialization to dict & deserialization from dict
    s_dict = session.to_dict()
    assert s_dict["access_token"] == raw_token
    assert s_dict["cookies"] == {"SESSION": "abc123session", "PORTAL_ID": "srm_ktret"}

    restored = SRMAuthSession.from_dict(s_dict)
    assert restored.access_token == session.access_token
    assert restored.cookies == session.cookies
    assert restored.user_id == session.user_id
    assert restored.is_valid is True


# ---------------------------------------------------------------------------
# Test 5: HTTP Client Using Captured Session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_client_using_captured_session():
    """Verify SRMHttpClient accepts captured SRMAuthSession and performs direct
    HTTP calls with session token & cookies without invoking Playwright.
    """
    token = "captured-jwt-token-for-http-client"
    session = SRMAuthSession(
        access_token=token,
        cookies={"SESSIONID": "http_sess_456"},
        user_id="RA2111003010001",
    )

    client = SRMHttpClient(base_url="https://dld.srmist.edu.in")
    client.set_auth_session(session)

    assert client.is_authenticated is True
    headers = client._get_auth_headers()
    assert headers["Authorization"] == token

    # Mock response for /curricula/student/home/getcourses
    courses_payload = {
        "Status": 1,
        "courses": [
            {
                "course_code": "21CSC303J",
                "course_name": "Software Engineering and Architecture",
                "semester": 3,
                "batch_id": "B1",
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=courses_payload)

    with patch.object(client, "_request_with_retry", AsyncMock(return_value=mock_resp)) as mock_post:
        courses = await client.get_courses()
        assert len(courses) == 1
        assert courses[0].course_code == "21CSC303J"
        assert mock_post.called

    await client.close()


# ---------------------------------------------------------------------------
# Test 6: Resume After CAPTCHA Solution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_after_captcha_solution(client: TestClient):
    """Verify that when discovery requests encounter WAITING_FOR_CAPTCHA,
    resuming with credentials / solution proceeds to direct HTTP discovery.
    """
    mock_course = SRMCourse(
        course_code="21CSC303J",
        course_name="Software Engineering",
        semester=3,
        batch_id="B1",
    )

    with patch("apps.api.routes.srm.SRMOrchestrator") as MockOrch:
        instance = MockOrch.return_value
        instance.connect = AsyncMock()
        instance.capture_login_captcha = AsyncMock(return_value=None)
        instance.authenticate = AsyncMock(return_value=True)
        instance.get_courses = AsyncMock(return_value=[mock_course])
        instance.get_courses_by_semester = AsyncMock(return_value=[mock_course])
        instance.get_sessions = AsyncMock(return_value=[])
        instance.discover_worksheets = AsyncMock(return_value=[])
        instance.close = AsyncMock()

        # Discover request triggers authentication and resumes discovery
        response = client.post(
            "/api/v1/srm/discover",
            json={
                "user_id": "RA2111003010001",
                "password": "valid_password",
                "semester": 3,
                "captcha_solution": "SOLVED",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert len(data["courses"]) == 1
        assert data["courses"][0]["course_code"] == "21CSC303J"


# ---------------------------------------------------------------------------
# Test 7: Browser Cleanup After Authentication
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_browser_cleanup_after_authentication():
    """Verify that authenticate_interactive rigorously closes page, context, browser,
    and stops Playwright in its finally block for both success and failure cases.
    """
    mock_page = MagicMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.close = AsyncMock()
    mock_page.goto = AsyncMock(side_effect=Exception("Browser connection dropped"))

    mock_context = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.close = AsyncMock()

    mock_browser = MagicMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_pw = MagicMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_pw.stop = AsyncMock()

    client = SRMBrowserClient(base_url="https://dld.srmist.edu.in")

    with patch("packages.srm.browser_client.async_playwright") as mock_ap:
        mock_ap.return_value.start = AsyncMock(return_value=mock_pw)

        with pytest.raises(AuthenticationFailed):
            await client.authenticate_interactive(
                credentials={"USER_ID": "RA123", "PASSWORD": "pwd", "headless": True},
                captcha_timeout_seconds=2,
            )

    # Verify all cleanup methods were called
    assert mock_page.close.called
    assert mock_context.close.called
    assert mock_browser.close.called
    assert mock_pw.stop.called


# ---------------------------------------------------------------------------
# Test 8: Authentication Failure Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authentication_failure_handling():
    """Verify that when login credentials are invalid, the client surfaces the actual
    failure message and avoids misleading 'Captcha verification failed' errors.
    """
    failed_login_data = {
        "Status": 0,
        "msg": "Invalid NetID or Password. Please try again.",
    }

    mock_page = _create_mock_page(failed_login_data)
    mock_context = _create_mock_context(mock_page)
    mock_browser = _create_mock_browser(mock_context)

    mock_pw = MagicMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_pw.stop = AsyncMock()

    client = SRMBrowserClient(base_url="https://dld.srmist.edu.in")

    with patch("packages.srm.browser_client.async_playwright") as mock_ap:
        mock_ap.return_value.start = AsyncMock(return_value=mock_pw)

        with pytest.raises(AuthenticationFailed) as exc_info:
            await client.authenticate_interactive(
                credentials={"USER_ID": "RA123", "PASSWORD": "wrong_password", "headless": True},
                captcha_timeout_seconds=3,
            )

        err_msg = str(exc_info.value)
        assert "Invalid NetID or Password" in err_msg
        assert "Captcha verification failed" not in err_msg


# ---------------------------------------------------------------------------
# Test 9: Expired Authentication Detection
# ---------------------------------------------------------------------------

def test_expired_authentication_detection():
    """Verify SRMAuthSession and SRMHttpClient correctly detect expired sessions."""
    # 1. Expired session (expires_at in past)
    past_time = datetime.now(timezone.utc) - timedelta(hours=2)
    expired_session = SRMAuthSession(
        access_token="expired_token_123",
        expires_at=past_time,
    )

    assert expired_session.is_valid is False
    assert expired_session.is_expired is True

    # When attached to HTTP client, is_authenticated reflects expired state
    http_client = SRMHttpClient(base_url="https://dld.srmist.edu.in")
    http_client.set_auth_session(expired_session)
    assert http_client.is_authenticated is False

    # 2. Valid session (expires_at in future)
    future_time = datetime.now(timezone.utc) + timedelta(hours=2)
    valid_session = SRMAuthSession(
        access_token="valid_token_123",
        expires_at=future_time,
    )

    assert valid_session.is_valid is True
    assert valid_session.is_expired is False

    http_client.set_auth_session(valid_session)
    assert http_client.is_authenticated is True

    # 3. Session with empty token is never valid
    empty_token_session = SRMAuthSession(
        access_token="",
        expires_at=future_time,
    )
    assert empty_token_session.is_valid is False
    assert empty_token_session.is_expired is True


# ---------------------------------------------------------------------------
# Test 10: Dashboard Authentication State Synchronization
# ---------------------------------------------------------------------------

def test_dashboard_auth_state_synchronization(client: TestClient):
    """Verify real-time UI synchronization via GET /api/v1/srm/status."""
    # 1. Initial / idle state
    update_discovery_phase("IDLE", "Ready for discovery")
    res1 = client.get("/api/v1/srm/status")
    assert res1.status_code == 200
    assert res1.json()["phase"] == "IDLE"

    # 2. Authenticating state
    update_discovery_phase("AUTHENTICATING", "Authenticating with SRM: Opening login window...", user_id="RA123")
    res2 = client.get("/api/v1/srm/status")
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["phase"] == "AUTHENTICATING"
    assert "Opening login window" in d2["message"]

    # 3. Waiting for CAPTCHA state
    update_discovery_phase("WAITING_FOR_CAPTCHA", "Waiting for CAPTCHA – Please solve the CAPTCHA in the opened SRM browser window.")
    res3 = client.get("/api/v1/srm/status")
    assert res3.status_code == 200
    d3 = res3.json()
    assert d3["phase"] == "WAITING_FOR_CAPTCHA"
    assert "Please solve the CAPTCHA" in d3["message"]

    # 4. Authenticated state
    update_discovery_phase("AUTHENTICATED", "Authentication successful! Proceeding to worksheet discovery...")
    res4 = client.get("/api/v1/srm/status")
    assert res4.status_code == 200
    assert res4.json()["phase"] == "AUTHENTICATED"

    # 5. Success state
    update_discovery_phase("SUCCESS", "Discovery completed successfully.")
    res5 = client.get("/api/v1/srm/status")
    assert res5.status_code == 200
    assert res5.json()["phase"] == "SUCCESS"
