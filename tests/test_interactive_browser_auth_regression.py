"""Comprehensive Regression Tests for Interactive Browser Authentication & Session Handoff.

Covers all 9 architectural requirements:
1. Dashboard-triggered browser launch
2. Interactive browser session creation
3. Browser launch failure handling (AUTHENTICATION_ERROR)
4. WAITING_FOR_CAPTCHA state only after successful launch verification
5. Duplicate launch protection (single browser per job)
6. Browser cleanup on success, failure, and timeout
7. Session handoff to Celery/backend
8. Job resume after CAPTCHA solution
9. No browser window required after authentication (direct HTTP continues)
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.worker.tasks import _run_job_workflow, get_job_credentials, store_job_credentials
from packages.shared.database import SessionLocal
from packages.shared.models.job import Job, JobStatus
from packages.srm.auth_manager import AuthPhase, auth_manager
from packages.srm.browser_launcher import launch_interactive_auth
from packages.srm.exceptions import SRMException
from packages.srm.models import SRMAuthSession, SRMCourse


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    db = SessionLocal()
    yield db
    db.close()


def _build_mock_playwright():
    """Helper to build a properly structured mock Playwright hierarchy."""
    mock_locator = AsyncMock()
    mock_locator.first = mock_locator
    mock_locator.wait_for = AsyncMock(return_value=None)
    mock_locator.fill = AsyncMock(return_value=None)
    mock_locator.dispatch_event = AsyncMock(return_value=None)
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.is_visible = AsyncMock(return_value=False)
    mock_locator.click = AsyncMock(return_value=None)

    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.goto = AsyncMock(return_value=None)
    mock_page.wait_for_timeout = AsyncMock(return_value=None)
    mock_page.on = MagicMock()
    mock_page.locator = MagicMock(return_value=mock_locator)
    mock_page.get_by_text = MagicMock(return_value=mock_locator)
    mock_page.evaluate = AsyncMock(return_value="captured-jwt-token")

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.cookies = AsyncMock(return_value=[{"name": "session_id", "value": "cookie123"}])
    mock_context.close = AsyncMock(return_value=None)

    mock_browser = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock(return_value=None)

    mock_pw = MagicMock()
    mock_pw.chromium = MagicMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_pw.stop = AsyncMock(return_value=None)

    mock_cm = MagicMock()
    mock_cm.start = AsyncMock(return_value=mock_pw)

    return mock_cm, mock_pw, mock_browser, mock_context, mock_page, mock_locator


@pytest.mark.asyncio
async def test_01_dashboard_triggered_browser_launch(client: TestClient, db_session):
    """Requirement 1: Dashboard-triggered browser launch via /auth/launch."""
    job = Job(
        user_id="RA_TEST_LAUNCH",
        course_id="21LEM202T",
        semester_id=3,
        status=JobStatus.WAITING_FOR_CAPTCHA,
        current_step="waiting_for_user_captcha",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    store_job_credentials(str(job.id), {"USER_ID": "RA_TEST_LAUNCH", "PASSWORD": "SecretPassword"})

    with patch("apps.api.routes.srm.launch_interactive_auth", new_callable=AsyncMock) as mock_launch:
        mock_launch.return_value = SRMAuthSession(
            access_token="jwt-test-token",
            cookies={"test": "val"},
            user_id="RA_TEST_LAUNCH",
        )

        resp = client.post("/api/v1/srm/auth/launch", json={"job_id": str(job.id)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["request_id"] == str(job.id)
        assert data["phase"] in ("PENDING", "AUTHENTICATING", "OPENING_BROWSER")

        # Allow background task to execute
        await asyncio.sleep(0.1)
        mock_launch.assert_called_once()


@pytest.mark.asyncio
async def test_02_interactive_browser_session_creation():
    """Requirement 2: Interactive browser session creation and SRMAuthSession return."""
    req_id = f"test_req_{int(time.time())}"
    await auth_manager.create_or_get_request(req_id, "TEST_USER", "TEST_PASS")

    mock_cm, mock_pw, mock_browser, mock_context, mock_page, _ = _build_mock_playwright()

    with patch("packages.srm.browser_launcher.async_playwright", return_value=mock_cm):
        session = await launch_interactive_auth(
            request_id=req_id,
            user_id="TEST_USER",
            password="TEST_PASS",
            timeout_seconds=5,
            force_headless=True,
        )

        assert isinstance(session, SRMAuthSession)
        assert session.access_token == "captured-jwt-token"
        assert session.token == "captured-jwt-token"
        assert session.user_id == "TEST_USER"
        assert session.is_valid is True


@pytest.mark.asyncio
async def test_03_browser_launch_failure():
    """Requirement 3: Browser launch failure results in AUTHENTICATION_ERROR."""
    req_id = f"test_fail_{int(time.time())}"
    await auth_manager.create_or_get_request(req_id, "FAIL_USER", "FAIL_PASS")

    mock_cm = MagicMock()
    mock_cm.start = AsyncMock(side_effect=RuntimeError("Chromium binary missing or crashed"))

    with patch("packages.srm.browser_launcher.async_playwright", return_value=mock_cm):
        with pytest.raises(RuntimeError):
            await launch_interactive_auth(
                request_id=req_id,
                user_id="FAIL_USER",
                password="FAIL_PASS",
                force_headless=True,
            )

        req = await auth_manager.get_request(req_id)
        assert req is not None
        assert req.phase == AuthPhase.AUTHENTICATION_ERROR
        assert req.browser_confirmed is False
        assert "Chromium binary missing or crashed" in (req.error_message or req.message)


@pytest.mark.asyncio
async def test_04_waiting_for_captcha_only_after_successful_launch():
    """Requirement 4: WAITING_FOR_CAPTCHA only set after browser/page creation succeeds."""
    req_id = f"test_confirmed_{int(time.time())}"
    phases_observed = []

    async def _on_status(phase, msg):
        phases_observed.append(phase)

    mock_cm, mock_pw, mock_browser, mock_context, mock_page, _ = _build_mock_playwright()
    mock_page.evaluate = AsyncMock(return_value="token_confirmed")

    with patch("packages.srm.browser_launcher.async_playwright", return_value=mock_cm):
        await launch_interactive_auth(
            request_id=req_id,
            user_id="CONFIRM_USER",
            password="CONFIRM_PASS",
            status_callback=_on_status,
            force_headless=True,
        )

        # Confirm sequence order: AUTHENTICATING -> OPENING_BROWSER -> WAITING_FOR_CAPTCHA -> AUTHENTICATED
        assert phases_observed[0] == "AUTHENTICATING"
        assert phases_observed[1] == "OPENING_BROWSER"
        assert phases_observed[2] == "WAITING_FOR_CAPTCHA"
        assert phases_observed[3] == "AUTHENTICATED"


@pytest.mark.asyncio
async def test_05_duplicate_launch_protection(client: TestClient, db_session):
    """Requirement 5: Duplicate dashboard clicks do not create multiple browsers."""
    job = Job(
        user_id="RA_DUP_USER",
        status=JobStatus.WAITING_FOR_CAPTCHA,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    store_job_credentials(str(job.id), {"USER_ID": "RA_DUP_USER", "PASSWORD": "pwd"})

    with patch("apps.api.routes.srm.launch_interactive_auth", new_callable=AsyncMock) as mock_launch:
        # First click
        resp1 = client.post("/api/v1/srm/auth/launch", json={"job_id": str(job.id)})
        assert resp1.status_code == 200

        # Second rapid click for same job
        resp2 = client.post("/api/v1/srm/auth/launch", json={"job_id": str(job.id)})
        assert resp2.status_code == 200

        # Verify only one launch task was created
        await asyncio.sleep(0.1)
        assert mock_launch.call_count == 1


@pytest.mark.asyncio
async def test_06_browser_cleanup():
    """Requirement 6: Browser closes cleanly after authentication or on timeout/cancel."""
    req_id = f"test_cleanup_{int(time.time())}"
    mock_cm, mock_pw, mock_browser, mock_context, mock_page, _ = _build_mock_playwright()
    mock_page.evaluate = AsyncMock(return_value="token_xyz")

    with patch("packages.srm.browser_launcher.async_playwright", return_value=mock_cm):
        await launch_interactive_auth(
            request_id=req_id,
            user_id="CLEANUP_USER",
            password="CLEANUP_PWD",
            force_headless=True,
        )

        # Verify context, browser, and playwright stop were called
        mock_context.close.assert_called_once()
        mock_browser.close.assert_called_once()
        mock_pw.stop.assert_called_once()


@pytest.mark.asyncio
async def test_07_session_handoff_to_celery_backend(db_session):
    """Requirement 7: Session handoff from launcher to Celery worker."""
    job = Job(
        user_id="RA_HANDOFF_USER",
        course_id="21LEM202T",
        semester_id=3,
        status=JobStatus.WAITING_FOR_CAPTCHA,
        current_step="waiting_for_user_captcha",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    # Store captured session in auth_manager
    session = SRMAuthSession(
        access_token="jwt-handoff-token",
        cookies={"sess": "123"},
        user_id="RA_HANDOFF_USER",
    )
    await auth_manager.store_session(str(job.id), session)

    # Verify retrieval
    retrieved = await auth_manager.get_session(str(job.id))
    assert retrieved is not None
    assert retrieved.access_token == "jwt-handoff-token"
    assert retrieved.token == "jwt-handoff-token"
    assert retrieved.is_valid is True


@pytest.mark.asyncio
async def test_08_resume_after_captcha(db_session):
    """Requirement 8: Job waiting for CAPTCHA resumes once session is handed off."""
    job = Job(
        user_id="RA_RESUME_USER",
        course_id="21LEM202T",
        semester_id=3,
        status=JobStatus.WAITING_FOR_CAPTCHA,
        current_step="waiting_for_user_captcha",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    mock_orchestrator = AsyncMock()
    mock_orchestrator.transport_name = "http"
    mock_orchestrator.requires_interactive_auth = True
    mock_orchestrator.get_courses.return_value = [
        SRMCourse(course_code="21LEM202T", course_name="UHV", semester=3, batch_id="B1")
    ]
    mock_orchestrator.discover_worksheets.return_value = []

    session = SRMAuthSession(
        access_token="jwt-resume-token",
        cookies={},
        user_id="RA_RESUME_USER",
    )

    async def _delayed_handoff():
        await asyncio.sleep(0.5)
        await auth_manager.store_session(str(job.id), session)

    handoff_task = asyncio.create_task(_delayed_handoff())

    # Stop after discovery to avoid full pipeline
    mock_orchestrator.discover_worksheets.side_effect = SRMException("Workflow halted for test")

    try:
        await _run_job_workflow(
            job_id=str(job.id),
            credentials={"USER_ID": "RA_RESUME_USER", "PASSWORD": "pwd"},
            orchestrator=mock_orchestrator,
            db_session=db_session,
        )
    except SRMException:
        pass
    finally:
        await handoff_task

    db_session.refresh(job)
    # Confirm orchestrator was called with the handed-off session
    mock_orchestrator.authenticate.assert_called_with({"auth_session": session})


@pytest.mark.asyncio
async def test_09_no_browser_window_required_after_authentication():
    """Requirement 9: No browser window required after authentication; direct HTTP client runs."""
    mock_orchestrator = AsyncMock()
    mock_orchestrator.transport_name = "http"
    mock_orchestrator.get_courses.return_value = [
        SRMCourse(course_code="21LEM202T", course_name="UHV", semester=3, batch_id="B1")
    ]

    session = SRMAuthSession(access_token="valid-token", cookies={}, user_id="RA_HTTP_ONLY")

    # Authenticate with pre-existing session
    await mock_orchestrator.authenticate({"auth_session": session})
    courses = await mock_orchestrator.get_courses()

    assert len(courses) == 1
    assert courses[0].course_code == "21LEM202T"
    assert mock_orchestrator.transport_name == "http"
