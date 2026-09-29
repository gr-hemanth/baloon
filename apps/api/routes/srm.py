"""SRM portal discovery and course querying endpoints with interactive browser support."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from packages.shared.database import SessionLocal
from packages.shared.models.job import Job, JobStatus
from packages.shared.schemas.job import (
    SRMCourseItem,
    SRMDiscoverRequest,
    SRMDiscoverResponse,
    SRMWorksheetItem,
)
from packages.srm.auth_manager import AuthPhase, auth_manager
from packages.srm.browser_launcher import launch_interactive_auth
from packages.srm.exceptions import (
    AuthenticationFailed,
    CaptchaRequired,
    SRMCaptchaRequired,
    SRMException,
)
from packages.srm.models import SRMAuthSession
from packages.srm.orchestrator import SRMOrchestrator

logger = logging.getLogger("api_srm")

router = APIRouter(prefix="/srm", tags=["SRM Discovery"])

# Ephemeral state for real-time authentication and discovery progress
_discovery_state: Dict[str, Any] = {
    "phase": "IDLE",  # "IDLE", "AUTHENTICATING", "OPENING_BROWSER", "WAITING_FOR_CAPTCHA", "AUTHENTICATED", "DISCOVERING", "SUCCESS", "FAILED"
    "message": "Ready",
    "user_id": None,
    "browser_confirmed": False,
    "error_message": None,
    "updated_at": datetime.now(timezone.utc).isoformat(),
}


def update_discovery_phase(
    phase: str,
    message: str,
    user_id: Optional[str] = None,
    browser_confirmed: Optional[bool] = None,
    error_message: Optional[str] = None,
) -> None:
    """Update global discovery state for UI synchronization."""
    _discovery_state["phase"] = phase
    _discovery_state["message"] = message
    _discovery_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    if user_id:
        _discovery_state["user_id"] = user_id
    if browser_confirmed is not None:
        _discovery_state["browser_confirmed"] = browser_confirmed
    if error_message is not None:
        _discovery_state["error_message"] = error_message
    logger.info("Discovery Phase [%s]: %s (browser_confirmed=%s)", phase, message, _discovery_state.get("browser_confirmed"))


class AuthLaunchRequest(BaseModel):
    """Payload to request an interactive authentication browser session."""
    job_id: Optional[str] = None
    user_id: Optional[str] = None
    password: Optional[str] = None
    force_headless: Optional[bool] = None
    timeout_seconds: Optional[int] = 300


class AuthStatusResponse(BaseModel):
    """Real-time status of an interactive authentication request."""
    request_id: str
    phase: str
    message: str
    browser_confirmed: bool = False
    is_authenticated: bool = False
    error_message: Optional[str] = None
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@router.get("/status")
def get_srm_discovery_status() -> Dict[str, Any]:
    """Retrieve real-time SRM authentication and discovery status for UI synchronization."""
    return dict(_discovery_state)


@router.post("/auth/launch", response_model=AuthStatusResponse)
async def launch_auth_browser(payload: AuthLaunchRequest) -> AuthStatusResponse:
    """Request an interactive Playwright headed browser session for CAPTCHA solving.
    
    Guarantees:
    - Only one active authentication browser per job or user discovery.
    - Duplicate clicks return the existing in-progress session.
    - Stale or non-existent jobs cannot claim a browser session.
    - Returns initial state: AUTHENTICATING -> OPENING_BROWSER.
    """
    request_id: str = ""
    uid: str = ""
    pwd: str = ""

    if payload.job_id:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == payload.job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail=f"Job {payload.job_id} not found")
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                raise HTTPException(
                    status_code=400,
                    detail=f"Job {payload.job_id} is in terminal state '{job.status.value}'. Stale jobs cannot launch authentication."
                )
            request_id = str(job.id)
            uid = job.user_id
        finally:
            db.close()

        # Retrieve ephemeral credentials
        from apps.worker.tasks import get_job_credentials
        creds = get_job_credentials(payload.job_id) or {}
        pwd = payload.password or creds.get("PASSWORD") or creds.get("password") or ""
        uid = payload.user_id or creds.get("USER_ID") or creds.get("username") or uid
    else:
        if not payload.user_id or not payload.password:
            raise HTTPException(
                status_code=400,
                detail="NetID/User ID and Password are required to launch discovery authentication."
            )
        request_id = f"discovery:{payload.user_id}"
        uid = payload.user_id
        pwd = payload.password

    timeout = payload.timeout_seconds or 300
    # Check or create auth request with duplicate protection
    auth_req, is_new = await auth_manager.create_or_get_request(
        request_id=request_id,
        user_id=uid,
        password=pwd,
        timeout_seconds=timeout,
    )

    if not is_new:
        logger.info("Returning existing active auth request for %s (phase: %s)", request_id, auth_req.phase.value)
        return AuthStatusResponse(
            request_id=request_id,
            phase=auth_req.phase.value,
            message=auth_req.message,
            browser_confirmed=auth_req.browser_confirmed,
            is_authenticated=auth_req.phase == AuthPhase.AUTHENTICATED,
            error_message=auth_req.error_message,
        )

    # Spawn interactive browser launcher in background task
    async def _run_launcher():
        try:
            session = await launch_interactive_auth(
                request_id=request_id,
                user_id=uid,
                password=pwd,
                timeout_seconds=timeout,
                force_headless=payload.force_headless,
            )
            # If launched for a job, hand session off to job credentials
            if payload.job_id:
                from apps.worker.tasks import get_job_credentials, store_job_credentials
                c = get_job_credentials(payload.job_id) or {}
                c["auth_session"] = session
                store_job_credentials(payload.job_id, c)
                logger.info("Handed authenticated session to job %s credentials", payload.job_id)
        except Exception as exc:
            logger.error("Background interactive auth launcher failed: %s", exc)

    asyncio.create_task(_run_launcher())

    return AuthStatusResponse(
        request_id=request_id,
        phase=auth_req.phase.value,
        message="Interactive login browser requested",
        browser_confirmed=False,
        is_authenticated=False,
    )


@router.get("/auth/status", response_model=AuthStatusResponse)
async def get_auth_browser_status(
    job_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuthStatusResponse:
    """Query live status of an interactive browser authentication session."""
    target_id = request_id or job_id
    if not target_id:
        raise HTTPException(status_code=400, detail="Either job_id or request_id must be provided")

    auth_req = await auth_manager.get_request(target_id)
    if not auth_req:
        # Check if already authenticated session exists
        existing_sess = await auth_manager.get_session(target_id)
        if existing_sess and existing_sess.is_valid:
            return AuthStatusResponse(
                request_id=target_id,
                phase=AuthPhase.AUTHENTICATED.value,
                message="Authentication successful",
                browser_confirmed=False,
                is_authenticated=True,
            )
        return AuthStatusResponse(
            request_id=target_id,
            phase=AuthPhase.IDLE.value,
            message="No active authentication request found",
            browser_confirmed=False,
            is_authenticated=False,
        )

    return AuthStatusResponse(
        request_id=target_id,
        phase=auth_req.phase.value,
        message=auth_req.message,
        browser_confirmed=auth_req.browser_confirmed,
        is_authenticated=auth_req.phase == AuthPhase.AUTHENTICATED,
        error_message=auth_req.error_message,
    )


@router.post("/auth/cancel")
async def cancel_auth_browser(
    job_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, str]:
    """Cancel an active interactive browser session and clean up resources."""
    target_id = request_id or job_id
    if not target_id:
        raise HTTPException(status_code=400, detail="Either job_id or request_id must be provided")

    await auth_manager.cancel_request(target_id, reason="Cancelled by user action")
    return {"status": "CANCELLED", "request_id": target_id}


class RunnerCallbackPayload(BaseModel):
    request_id: str
    phase: str
    message: str
    browser_confirmed: bool = False
    error_message: Optional[str] = None
    session_data: Optional[Dict[str, Any]] = None


@router.post("/auth/runner_callback")
async def runner_auth_callback(payload: RunnerCallbackPayload):
    """Receive live status update or captured session from desktop browser runner."""
    try:
        phase = AuthPhase(payload.phase)
    except ValueError:
        phase = AuthPhase.WAITING_FOR_CAPTCHA

    await auth_manager.update_phase(
        request_id=payload.request_id,
        phase=phase,
        message=payload.message,
        browser_confirmed=payload.browser_confirmed,
        error_message=payload.error_message,
    )

    if payload.session_data:
        session = SRMAuthSession.from_dict(payload.session_data)
        await auth_manager.store_session(payload.request_id, session)
        req = await auth_manager.get_request(payload.request_id)
        if req:
            req.auth_session = session
            if req.user_id:
                await auth_manager.store_session(req.user_id, session)

        from apps.worker.tasks import (
            celery_app,
            get_job_credentials,
            store_job_credentials,
            process_job,
            _run_job_workflow,
        )
        c = get_job_credentials(payload.request_id) or {}
        c["auth_session"] = session
        store_job_credentials(payload.request_id, c)
        logger.info("Runner callback stored session and handed off to job %s", payload.request_id)

        # If this authentication was for an active job, resume execution
        db = SessionLocal()
        try:
            target_job = db.query(Job).filter(Job.id == payload.request_id).first()
            if target_job and target_job.status in (JobStatus.PENDING, JobStatus.WAITING_FOR_CAPTCHA):
                if getattr(celery_app.conf, "task_always_eager", False):
                    asyncio.create_task(_run_job_workflow(job_id=payload.request_id, credentials=c, auto_submit=False))
                    logger.info("Dispatched resumed job %s in background async task (eager mode)", payload.request_id)
                else:
                    try:
                        process_job.delay(job_id=payload.request_id, credentials=c)
                        logger.info("Dispatched resumed job %s to Celery worker", payload.request_id)
                    except Exception as exc:
                        logger.warning("Celery dispatch failed: %s; falling back to async task", exc)
                        asyncio.create_task(_run_job_workflow(job_id=payload.request_id, credentials=c, auto_submit=False))
        finally:
            db.close()

    return {"status": "OK"}


@router.post("/discover", response_model=SRMDiscoverResponse)
@router.post("/discover/resume", response_model=SRMDiscoverResponse)
async def discover_srm_courses(req: SRMDiscoverRequest) -> SRMDiscoverResponse:
    """Authenticate with SRM and discover Semester 3 courses and available worksheets.

    Flow:
    1. Check for existing active SRMAuthSession; reuse directly if valid (no browser).
    2. AUTHENTICATING: Prepare login session.
    3. OPENING_BROWSER: Launch Playwright Chromium.
    4. WAITING_FOR_CAPTCHA: Await user manual CAPTCHA solve (only reported once browser confirmed open).
    5. AUTHENTICATED: Capture authenticated session (JWT, cookies) & close browser.
    6. DISCOVERING: Fetch courses & worksheets via direct HTTP client.
    7. SUCCESS: Return discovered courses and worksheets.
    """
    if not req.user_id or not req.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="NetID/User ID and Password are required.",
        )

    target_sem = req.semester or 3
    transport_mode = getattr(req, "transport_mode", "auto") or "auto"
    orchestrator = SRMOrchestrator(mode=transport_mode)
    captcha_sol = req.effective_solution

    # 1. Check if user already has a valid authenticated session in cache
    existing_session = await auth_manager.get_session(req.user_id)
    if existing_session and existing_session.is_valid:
        logger.info("Reusing existing authenticated session for user %s; skipping browser launch.", req.user_id)
        update_discovery_phase("AUTHENTICATED", "Active authentication session found. Proceeding to discovery...", req.user_id)
        try:
            await orchestrator.authenticate({"auth_session": existing_session})
            update_discovery_phase("DISCOVERING", "Discovering courses and worksheets via direct HTTP...", req.user_id)
            all_courses = await orchestrator.get_courses()
            sem_courses = [c for c in all_courses if c.semester == target_sem]

            course_items: list[SRMCourseItem] = []
            for course in sem_courses:
                try:
                    discovered_ws = await orchestrator.discover_worksheets(
                        course_code=course.course_code,
                        batch_id=course.batch_id,
                        format_type="docx",
                    )
                    ws_items = [
                        SRMWorksheetItem(
                            worksheet_id=w.identifier,
                            session=w.session or 1,
                            slo=w.slo or 1,
                            filename=w.filename,
                            format=w.format,
                            is_available=w.is_available,
                            submission_status=w.submission_status or "NOT_SUBMITTED",
                            title=w.title,
                            download_url=w.download_url,
                        )
                        for w in discovered_ws
                    ]
                except Exception as disc_err:
                    logger.warning("Could not discover worksheets for %s: %s", course.course_code, disc_err)
                    ws_items = []

                course_items.append(
                    SRMCourseItem(
                        course_code=course.course_code,
                        course_name=course.course_name,
                        batch_id=course.batch_id,
                        semester=course.semester,
                        department=course.department,
                        worksheets=ws_items,
                    )
                )

            update_discovery_phase("SUCCESS", f"Discovered {len(course_items)} courses for Semester {target_sem}.", req.user_id)
            return SRMDiscoverResponse(
                status="SUCCESS",
                message=f"Discovered {len(course_items)} courses for Semester {target_sem}.",
                semester=target_sem,
                courses=course_items,
                captcha_challenge=None,
            )
        except Exception as exc:
            logger.warning("Existing session invalid or expired (%s); falling back to fresh authentication", exc)

    # Register live status callback for UI polling
    async def _on_status_change(phase: str, msg: str):
        confirmed = phase == "WAITING_FOR_CAPTCHA"
        update_discovery_phase(phase, msg, req.user_id, browser_confirmed=confirmed)

    orchestrator.set_status_callback(_on_status_change)
    update_discovery_phase("AUTHENTICATING", "Authenticating with SRM portal...", req.user_id, browser_confirmed=False)

    try:
        await orchestrator.connect()

        # Check if CAPTCHA is presented by portal (backward compatibility for unit test mocks)
        if not captcha_sol:
            captcha_challenge = await orchestrator.capture_login_captcha()
            if captcha_challenge:
                update_discovery_phase(
                    "WAITING_FOR_CAPTCHA",
                    "CAPTCHA challenge required to authenticate.",
                    req.user_id,
                    browser_confirmed=True,
                )
                return SRMDiscoverResponse(
                    status="WAITING_FOR_CAPTCHA",
                    message="CAPTCHA challenge required to authenticate.",
                    semester=target_sem,
                    courses=[],
                    captcha_challenge=captcha_challenge,
                )

        credentials = {
            "USER_ID": req.user_id,
            "PASSWORD": req.password,
            "captcha_solution": captcha_sol,
            "captcha": captcha_sol,
        }

        try:
            await orchestrator.authenticate(credentials)
            update_discovery_phase("AUTHENTICATED", "Authentication successful! Proceeding to worksheet discovery...", req.user_id, browser_confirmed=False)

        except (CaptchaRequired, SRMCaptchaRequired) as captcha_exc:
            challenge = getattr(captcha_exc, "challenge_data", None) or {"message": str(captcha_exc)}
            update_discovery_phase(
                "WAITING_FOR_CAPTCHA",
                "Waiting for CAPTCHA – Please solve the CAPTCHA in the opened SRM browser window.",
                req.user_id,
                browser_confirmed=True,
            )
            return SRMDiscoverResponse(
                status="WAITING_FOR_CAPTCHA",
                message="CAPTCHA required for authentication.",
                semester=target_sem,
                courses=[],
                captcha_challenge=challenge,
            )
        except AuthenticationFailed as auth_err:
            update_discovery_phase("AUTHENTICATION_ERROR", f"Authentication failed: {auth_err}", req.user_id, browser_confirmed=False, error_message=str(auth_err))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Authentication failed: {auth_err}",
            )

        # Retrieve all courses and filter for target semester via direct HTTP
        update_discovery_phase("DISCOVERING", "Discovering courses and worksheets via direct HTTP...", req.user_id, browser_confirmed=False)
        all_courses = await orchestrator.get_courses()
        sem_courses = [c for c in all_courses if c.semester == target_sem]

        course_items = []
        for course in sem_courses:
            try:
                discovered_ws = await orchestrator.discover_worksheets(
                    course_code=course.course_code,
                    batch_id=course.batch_id,
                    format_type="docx",
                )
                ws_items = [
                    SRMWorksheetItem(
                        worksheet_id=w.identifier,
                        session=w.session or 1,
                        slo=w.slo or 1,
                        filename=w.filename,
                        format=w.format,
                        is_available=w.is_available,
                        submission_status=w.submission_status or "NOT_SUBMITTED",
                        title=w.title,
                        download_url=w.download_url,
                    )
                    for w in discovered_ws
                ]
            except Exception as disc_err:
                logger.warning("Could not discover worksheets for %s: %s", course.course_code, disc_err)
                ws_items = []

            course_items.append(
                SRMCourseItem(
                    course_code=course.course_code,
                    course_name=course.course_name,
                    batch_id=course.batch_id,
                    semester=course.semester,
                    department=course.department,
                    worksheets=ws_items,
                )
            )

        update_discovery_phase("SUCCESS", f"Discovered {len(course_items)} courses for Semester {target_sem}.", req.user_id, browser_confirmed=False)
        return SRMDiscoverResponse(
            status="SUCCESS",
            message=f"Discovered {len(course_items)} courses for Semester {target_sem}.",
            semester=target_sem,
            courses=course_items,
            captcha_challenge=None,
        )

    except HTTPException:
        raise
    except Exception as exc:
        err_msg = str(exc)
        update_discovery_phase("AUTHENTICATION_ERROR", f"Error during discovery: {err_msg}", req.user_id, browser_confirmed=False, error_message=err_msg)
        logger.error("Error during SRM discovery (%s): %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to discover SRM courses: {err_msg or type(exc).__name__}",
        )
    finally:
        try:
            if hasattr(orchestrator, "close"):
                res = orchestrator.close()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:
            pass
