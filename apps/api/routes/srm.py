"""SRM portal discovery and course querying endpoints."""

import logging
from typing import Any, Dict
from fastapi import APIRouter, HTTPException, status

from packages.shared.schemas.job import (
    SRMCourseItem,
    SRMDiscoverRequest,
    SRMDiscoverResponse,
    SRMWorksheetItem,
)
from packages.srm.exceptions import (
    AuthenticationFailed,
    CaptchaRequired,
    SRMCaptchaRequired,
    SRMException,
)
from packages.srm.orchestrator import SRMOrchestrator

logger = logging.getLogger("api_srm")

router = APIRouter(prefix="/srm", tags=["SRM Discovery"])


@router.post("/discover", response_model=SRMDiscoverResponse)
async def discover_srm_courses(req: SRMDiscoverRequest) -> SRMDiscoverResponse:
    """Authenticate with SRM and discover Semester 3 courses and available worksheets.

    Guarantees:
    - Never persists password in database or on disk.
    - Wipes credentials upon completion.
    - Returns CAPTCHA challenge if portal requires challenge resolution.
    """
    if not req.user_id or not req.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="NetID/User ID and Password are required.",
        )

    target_sem = req.semester or 3
    transport_mode = getattr(req, "transport_mode", "auto") or "auto"
    orchestrator = SRMOrchestrator(mode=transport_mode)

    try:
        await orchestrator.connect()

        # Check if CAPTCHA is presented by portal
        if not req.captcha_solution:
            captcha_challenge = await orchestrator.capture_login_captcha()
            if captcha_challenge:
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
            "captcha_solution": req.captcha_solution,
            "captcha": req.captcha_solution,
        }

        try:
            await orchestrator.authenticate(credentials)
        except (CaptchaRequired, SRMCaptchaRequired) as captcha_exc:
            challenge = getattr(captcha_exc, "challenge_data", None) or {"message": str(captcha_exc)}
            return SRMDiscoverResponse(
                status="WAITING_FOR_CAPTCHA",
                message="CAPTCHA required for authentication.",
                semester=target_sem,
                courses=[],
                captcha_challenge=challenge,
            )
        except AuthenticationFailed as auth_err:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Authentication failed: {auth_err}",
            )

        # Retrieve all courses and filter for target semester
        all_courses = await orchestrator.get_courses()
        sem_courses = [c for c in all_courses if c.semester == target_sem]

        course_items: list[SRMCourseItem] = []
        for course in sem_courses:
            # Discover available worksheets for each course
            try:
                discovered_ws = await orchestrator.discover_worksheets(
                    course_code=course.course_code,
                    batch_id=course.batch_id,
                    format_type="docx",
                )
                ws_items = [
                    SRMWorksheetItem(
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

        return SRMDiscoverResponse(
            status="SUCCESS",
            message=f"Discovered {len(course_items)} courses for Semester {target_sem}.",
            semester=target_sem,
            courses=course_items,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error during SRM discovery (%s): %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to discover SRM courses: {exc or type(exc).__name__}",
        )
    finally:
        await orchestrator.close()
