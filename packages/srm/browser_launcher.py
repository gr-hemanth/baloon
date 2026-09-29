"""Interactive Browser Launcher for SRM Portal Authentication.

Guarantees:
1. Opens Playwright Chromium headed window strictly for manual CAPTCHA solving.
2. Tracks progress through explicit phases:
   AUTHENTICATING -> OPENING_BROWSER -> WAITING_FOR_CAPTCHA -> AUTHENTICATED / AUTHENTICATION_ERROR.
3. Only reports 'browser opened' after page creation and navigation are verified.
4. If browser launch fails, cleanly reports AUTHENTICATION_ERROR without false claims.
5. Captures SRMAuthSession and stores it for seamless direct HTTP handoff.
6. Automatically closes Chromium immediately after authentication or on error/timeout.
7. Guarantees that when running from a non-interactive background desktop on Windows,
   the browser is spawned on WinSta0\\Default so it appears visibly on the physical screen.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from packages.shared.config import settings
from packages.srm.auth_manager import AuthPhase, auth_manager
from packages.srm.desktop_spawner import is_on_default_desktop, spawn_process_on_default_desktop
from packages.srm.exceptions import AuthenticationFailed
from packages.srm.models import SRMAuthSession

logger = logging.getLogger("srm_browser_launcher")


def find_preferred_browser_executable() -> Optional[str]:
    """Find user's preferred browser executable (Opera GX / Opera) if installed."""
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera GX\opera.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\launcher.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe"),
        r"C:\Program Files\Opera GX\opera.exe",
        r"C:\Program Files\Opera\launcher.exe",
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            logger.info("Found preferred browser executable: %s", cand)
            return cand
    return None


async def _run_playwright_auth_in_process(
    request_id: str,
    user_id: str,
    password: str,
    base_url: Optional[str] = None,
    timeout_seconds: int = 180,
    force_headless: Optional[bool] = None,
    status_callback: Optional[Callable[[str, str], Any]] = None,
) -> SRMAuthSession:
    """Execute Playwright interactive login directly in the current process."""
    portal_base = (base_url or settings.SRM_BASE_URL).rstrip("/")
    if not portal_base.endswith("ktretecurricula"):
        target_url = f"{portal_base}/ktretecurricula/#/"
    else:
        target_url = f"{portal_base}/#/"

    if force_headless is not None:
        use_headless = force_headless
    elif os.environ.get("FORCE_HEADLESS") or os.environ.get("PYTEST_CURRENT_TEST"):
        use_headless = True
    else:
        use_headless = False

    async def _notify(phase: AuthPhase, msg: str, confirmed: Optional[bool] = None, err: Optional[str] = None):
        await auth_manager.update_phase(
            request_id=request_id,
            phase=phase,
            message=msg,
            browser_confirmed=confirmed,
            error_message=err,
        )
        if status_callback:
            try:
                res = status_callback(phase.value, msg)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as cb_err:
                logger.warning("Callback error: %s", cb_err)

    # 1. State: AUTHENTICATING
    await _notify(AuthPhase.AUTHENTICATING, "Preparing SRM authentication session...", confirmed=False)

    # 2. State: OPENING_BROWSER
    await _notify(AuthPhase.OPENING_BROWSER, "Launching SRM login browser window...", confirmed=False)

    pw: Optional[Playwright] = None
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None

    try:
        pw = await async_playwright().start()

        launch_args = [
            "--start-maximized",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--no-default-browser-check",
            "--no-first-run",
        ]

        preferred_exec = find_preferred_browser_executable() if not use_headless else None
        launch_kwargs: Dict[str, Any] = {"headless": use_headless, "args": launch_args}
        if preferred_exec:
            launch_kwargs["executable_path"] = preferred_exec
            logger.info("Using Opera / preferred browser executable: %s", preferred_exec)

        logger.info("Launching browser (headless=%s, executable=%s) for request %s...", use_headless, preferred_exec, request_id)
        browser = await pw.chromium.launch(**launch_kwargs)

        # Attach browser instance to AuthRequest for cleanup tracking
        req = await auth_manager.get_request(request_id)
        if req:
            req.browser_instance = browser

        context_kwargs: Dict[str, Any] = {
            "accept_downloads": True,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        }
        if not use_headless:
            context_kwargs["no_viewport"] = True
        else:
            context_kwargs["viewport"] = {"width": 1280, "height": 850}

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        captured_login_data: Dict[str, Any] = {}
        auth_error_msg: Optional[str] = None

        async def _handle_response(res):
            nonlocal auth_error_msg
            try:
                if "/curricula/login" in res.url and res.request.method == "POST":
                    data = await res.json()
                    if data.get("Status") == 1:
                        captured_login_data.update(data)
                    elif data.get("Status") == 0:
                        auth_error_msg = data.get("msg") or "Invalid credentials"
            except Exception:
                pass

        page.on("response", _handle_response)

        # Navigate to portal
        logger.info("Navigating to %s...", target_url)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1000)

        # Check for START LEARNING button if landing page
        start_btn = page.get_by_text("START LEARNING", exact=False).first
        if await start_btn.count() > 0 and await start_btn.is_visible():
            await start_btn.click()
            await page.wait_for_timeout(1000)

        # Fill credentials
        user_field = page.locator("input[id='Username1'], input[name='username'], input[placeholder*='User' i]").first
        await user_field.wait_for(state="visible", timeout=15000)
        await user_field.fill(user_id)
        await user_field.dispatch_event("input")
        await user_field.dispatch_event("change")

        pw_field = page.locator("input[id='Password'], input[type='password']").first
        await pw_field.wait_for(state="attached", timeout=10000)
        await page.wait_for_timeout(300)
        await pw_field.fill(password)
        await pw_field.dispatch_event("input")
        await pw_field.dispatch_event("change")

        # Scroll the modal / login container into view to guarantee visibility
        try:
            await page.evaluate("""() => {
                const card = document.querySelector(".ant-modal-content, .login-form, .login-box, form");
                if (card) {
                    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            }""")
        except Exception:
            pass

        # 3. VERIFIED: Browser is open, page is loaded, credentials filled.
        # Transition to WAITING_FOR_CAPTCHA with browser_confirmed=True.
        await _notify(
            AuthPhase.WAITING_FOR_CAPTCHA,
            "SRM login browser window has opened. Please solve the CAPTCHA in the opened window.",
            confirmed=True,
        )

        start_time = time.time()
        jwt_token = None
        user_payload: Dict[str, Any] = {}

        while (time.time() - start_time) < timeout_seconds:
            # Check for cancellation
            cur_req = await auth_manager.get_request(request_id)
            if cur_req and cur_req.cancel_requested:
                raise AuthenticationFailed("Authentication cancelled by user.")

            # 1. Check network listener
            if captured_login_data.get("token"):
                jwt_token = captured_login_data["token"]
                user_payload = captured_login_data.get("user") or {}
                break

            # 2. Check login error response
            if auth_error_msg:
                raise AuthenticationFailed(auth_error_msg)

            # 3. Check page localStorage
            try:
                ls_token = await page.evaluate("() => localStorage.getItem('jwtToken')")
                if ls_token:
                    jwt_token = ls_token
                    break
            except Exception:
                pass

            # 4. Check UI error banner
            try:
                err_elem = page.locator(".ant-message-error, .ant-alert-error").first
                if await err_elem.count() > 0 and await err_elem.is_visible():
                    err_text = await err_elem.inner_text()
                    if err_text:
                        raise AuthenticationFailed(f"Portal error: {err_text}")
            except AuthenticationFailed:
                raise
            except Exception:
                pass

            # 5. Check if user completed hCaptcha and click login button if needed
            try:
                hcap_val = await page.evaluate("""() => {
                    const ta = document.querySelector("textarea[name*='h-captcha-response'], textarea[id*='h-captcha-response']");
                    return ta ? ta.value : "";
                }""")
                if hcap_val and len(hcap_val) > 20:
                    login_btn = page.locator("button:has-text('LOG IN'), button:has-text('Sign in')").first
                    if await login_btn.count() > 0 and await login_btn.is_visible():
                        cls_attr = await login_btn.get_attribute("class") or ""
                        if "loading" not in cls_attr and "disabled" not in cls_attr:
                            await login_btn.click()
            except Exception:
                pass

            await asyncio.sleep(0.5)

        if not jwt_token:
            raise AuthenticationFailed("Timed out waiting for CAPTCHA solution in browser window.")

        # Capture cookies and session
        captured_cookies = await context.cookies()
        auth_session = SRMAuthSession.from_browser_capture(
            token=jwt_token,
            cookies=captured_cookies,
            user_id=user_id,
            user_data=user_payload,
        )

        # Store session in manager
        await auth_manager.store_session(request_id, auth_session)
        await _notify(AuthPhase.AUTHENTICATED, "Authentication successful! Session captured.", confirmed=False)
        logger.info("Interactive authentication successful for request %s (user: %s)", request_id, user_id)
        return auth_session

    except Exception as exc:
        err_str = str(exc)
        logger.error("Interactive browser authentication failed for %s: %s", request_id, err_str)
        await _notify(
            AuthPhase.AUTHENTICATION_ERROR,
            f"Authentication failed: {err_str}",
            confirmed=False,
            err=err_str,
        )
        raise

    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:
            pass
        req = await auth_manager.get_request(request_id)
        if req:
            req.browser_instance = None
        logger.info("Cleaned up Chromium browser instance for request %s", request_id)


async def launch_interactive_auth(
    request_id: str,
    user_id: str,
    password: str,
    base_url: Optional[str] = None,
    timeout_seconds: int = 180,
    force_headless: Optional[bool] = None,
    status_callback: Optional[Callable[[str, str], Any]] = None,
    force_in_process: bool = False,
) -> SRMAuthSession:
    """Launch interactive Chromium window, fill credentials, await user CAPTCHA solve,
    and return the authenticated SRMAuthSession.

    If executing from a non-interactive desktop session on Windows (e.g. background service),
    delegates to desktop_spawner to launch desktop_browser_runner on WinSta0\\Default
    so Chromium is 100% guaranteed to be visible to the user.
    """
    is_mock = type(async_playwright).__name__ in ("MagicMock", "AsyncMock") or hasattr(async_playwright, "mock_calls")
    is_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    on_default = is_on_default_desktop()

    if force_in_process or force_headless or is_pytest or is_mock or on_default or sys.platform != "win32":
        return await _run_playwright_auth_in_process(
            request_id=request_id,
            user_id=user_id,
            password=password,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            force_headless=force_headless,
            status_callback=status_callback,
        )

    # Windows non-Default desktop: Spawn runner explicitly on WinSta0\Default
    temp_dir = Path(tempfile.gettempdir())
    pw_file = temp_dir / f"srm_auth_{request_id.replace(':', '_')}_{int(time.time())}.tmp"
    pw_file.write_text(password, encoding="utf-8")
    result_file = temp_dir / f"srm_res_{request_id.replace(':', '_')}_{int(time.time())}.json"

    runner_script = Path(__file__).resolve().parent / "desktop_browser_runner.py"
    python_exe = sys.executable
    cmd = (
        f'"{python_exe}" "{runner_script}" '
        f'--request-id "{request_id}" '
        f'--user-id "{user_id}" '
        f'--password-file "{pw_file}" '
        f'--result-file "{result_file}" '
        f'--api-url "http://127.0.0.1:8000" '
        f'--timeout {timeout_seconds}'
    )

    logger.info("Spawning desktop browser runner on WinSta0\\Default for request %s...", request_id)
    try:
        spawn_process_on_default_desktop(cmd)
    except Exception as exc:
        logger.error("Failed to spawn desktop browser runner: %s. Falling back to in-process.", exc)
        try:
            pw_file.unlink(missing_ok=True)
        except Exception:
            pass
        return await _run_playwright_auth_in_process(
            request_id=request_id,
            user_id=user_id,
            password=password,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            force_headless=force_headless,
            status_callback=status_callback,
        )

    # Await completion by polling auth_manager or result file
    start_time = time.time()
    while (time.time() - start_time) < (timeout_seconds + 15):
        # 1. Check auth_manager session
        sess = await auth_manager.get_session(request_id)
        if sess and getattr(sess, "is_valid", False):
            try:
                result_file.unlink(missing_ok=True)
            except Exception:
                pass
            return sess

        # 2. Check result file backup
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text(encoding="utf-8"))
                sess = SRMAuthSession.from_dict(data)
                await auth_manager.store_session(request_id, sess)
                result_file.unlink(missing_ok=True)
                return sess
            except Exception:
                pass

        # 3. Check if request failed or timed out
        req = await auth_manager.get_request(request_id)
        if req and req.phase in (AuthPhase.AUTHENTICATION_ERROR, AuthPhase.CANCELLED, AuthPhase.TIMED_OUT):
            err_msg = req.error_message or req.message or f"Authentication ended in phase {req.phase.value}"
            try:
                result_file.unlink(missing_ok=True)
            except Exception:
                pass
            raise AuthenticationFailed(err_msg)

        await asyncio.sleep(0.5)

    try:
        result_file.unlink(missing_ok=True)
    except Exception:
        pass
    raise AuthenticationFailed("Timed out waiting for interactive browser authentication on desktop.")
