"""Standalone Desktop Browser Runner for Interactive SRM Authentication.

Executed on WinSta0\\Default to guarantee that Playwright Chromium opens
visibly on the physical interactive Windows desktop monitor.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from packages.srm.models import SRMAuthSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] desktop_browser_runner: %(message)s",
)
logger = logging.getLogger("desktop_browser_runner")


async def notify_api(
    api_url: str,
    request_id: str,
    phase: str,
    message: str,
    browser_confirmed: bool = False,
    error_message: Optional[str] = None,
    session_dict: Optional[Dict[str, Any]] = None,
) -> None:
    """Send status updates or captured session back to the FastAPI backend."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{api_url}/api/v1/srm/auth/runner_callback",
                json={
                    "request_id": request_id,
                    "phase": phase,
                    "message": message,
                    "browser_confirmed": browser_confirmed,
                    "error_message": error_message,
                    "session_data": session_dict,
                },
            )
    except Exception as exc:
        logger.warning("Could not notify API at %s: %s", api_url, exc)


async def run_desktop_auth(
    request_id: str,
    user_id: str,
    password: str,
    api_url: str,
    result_file: Optional[str] = None,
    timeout_seconds: int = 180,
    force_headless: bool = False,
) -> None:
    """Execute the interactive browser authentication flow on WinSta0\\Default."""
    from packages.srm.browser_launcher import _run_playwright_auth_in_process

    async def _on_status(phase_name: str, msg: str):
        confirmed = phase_name == "WAITING_FOR_CAPTCHA"
        logger.info("[%s] %s (browser_confirmed=%s)", phase_name, msg, confirmed)
        await notify_api(
            api_url=api_url,
            request_id=request_id,
            phase=phase_name,
            message=msg,
            browser_confirmed=confirmed,
        )

    try:
        await _on_status("AUTHENTICATING", "Preparing SRM authentication session on interactive desktop...")
        await _on_status("OPENING_BROWSER", "Launching visible Chromium window on your screen...")

        auth_session = await _run_playwright_auth_in_process(
            request_id=request_id,
            user_id=user_id,
            password=password,
            timeout_seconds=timeout_seconds,
            status_callback=_on_status,
            force_headless=force_headless,
        )

        sess_dict = auth_session.to_dict()

        # Save to result file as backup
        if result_file:
            try:
                Path(result_file).write_text(json.dumps(sess_dict), encoding="utf-8")
                logger.info("Wrote captured session to result file %s", result_file)
            except Exception as fe:
                logger.warning("Could not write result file: %s", fe)

        # Notify API of completion
        await notify_api(
            api_url=api_url,
            request_id=request_id,
            phase="AUTHENTICATED",
            message="Authentication successful! Session captured.",
            browser_confirmed=False,
            session_dict=sess_dict,
        )
        logger.info("Authentication complete. Runner exiting.")

    except Exception as exc:
        err_msg = str(exc)
        logger.error("Authentication failed in desktop runner: %s", err_msg)
        await notify_api(
            api_url=api_url,
            request_id=request_id,
            phase="AUTHENTICATION_ERROR",
            message=f"Authentication failed: {err_msg}",
            browser_confirmed=False,
            error_message=err_msg,
        )
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Desktop Browser Runner for SRM Auth")
    parser.add_argument("--request-id", required=True, help="Auth Request ID")
    parser.add_argument("--user-id", required=True, help="SRM Register Number / NetID")
    parser.add_argument("--password-file", required=True, help="Path to ephemeral password file")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="FastAPI backend URL")
    parser.add_argument("--result-file", default=None, help="Path to write captured session JSON")
    parser.add_argument("--timeout", type=int, default=180, help="CAPTCHA solve timeout in seconds")
    parser.add_argument("--headless", action="store_true", help="Force headless Chromium")

    args = parser.parse_args()

    # Read password and securely delete ephemeral file
    pw_path = Path(args.password_file)
    if not pw_path.exists():
        logger.error("Password file not found: %s", args.password_file)
        sys.exit(1)

    try:
        password = pw_path.read_text(encoding="utf-8").strip()
    finally:
        try:
            pw_path.unlink(missing_ok=True)
        except Exception:
            pass

    asyncio.run(run_desktop_auth(
        request_id=args.request_id,
        user_id=args.user_id,
        password=password,
        api_url=args.api_url,
        result_file=args.result_file,
        timeout_seconds=args.timeout,
        force_headless=args.headless,
    ))


if __name__ == "__main__":
    main()
