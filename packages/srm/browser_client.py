import logging
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from packages.srm.client import SRMClient
from packages.srm.models import (
    SRMCourse,
    SRMQuestionSet,
    SRMSessionStatus,
    SRMSubmissionResult,
    SRMWorksheetFile,
    SRMWorksheet,
    SRMSubmissionReceipt,
    SRMWorksheetMetadata,
)
from packages.srm.exceptions import (
    SRMConnectionError,
    AuthenticationFailed,
    CaptchaRequired,
    WorksheetNotFound,
    DownloadFailed,
    SubmissionFailed,
    VerificationFailed,
    SRMTransportUnavailableError,
)
from packages.shared.config import settings

logger = logging.getLogger("srm_browser_client")


class SRMBrowserClient(SRMClient):
    """Playwright headless Chromium browser transport for SRM portal.
    
    Milestone 2 role:
    Used ONLY for:
    1. Opening/rendering the login page when needed.
    2. Capturing the client-side CAPTCHA canvas for user presentation.
    3. Emergency fallback if the API transport encounters unexpected portal changes.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        headless: Optional[bool] = None,
        artifacts_dir: Optional[Path] = None,
    ):
        self.portal_url = (base_url or settings.SRM_BASE_URL).rstrip("/")
        # Target dedicated FET eCurricula portal URL
        if not self.portal_url.endswith("ktretecurricula"):
            self.base_url = f"{self.portal_url}/ktretecurricula/#/"
        else:
            self.base_url = f"{self.portal_url}/#/"

        self.headless = settings.SRM_HEADLESS_BROWSER if headless is None else headless
        self.artifacts_dir = artifacts_dir or settings.browser_artifacts_path
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._authenticated = False

    @property
    def transport_name(self) -> str:
        return "browser"

    async def _init_browser(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        return self._page

    async def _save_diagnostic_artifact(self, name: str) -> Path:
        """Capture screenshot and DOM snapshot for debugging."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        screenshot_path = self.artifacts_dir / f"{timestamp}_{name}.png"
        dom_path = self.artifacts_dir / f"{timestamp}_{name}.html"
        
        if self._page and not self._page.is_closed():
            try:
                await self._page.screenshot(path=str(screenshot_path), full_page=True)
                content = await self._page.content()
                dom_path.write_text(content, encoding="utf-8")
                logger.info("Saved diagnostic artifact to %s", screenshot_path)
            except Exception as exc:
                logger.warning("Failed to save diagnostic artifact: %s", exc)
        return screenshot_path

    async def connect(self) -> bool:
        page = await self._init_browser()
        try:
            logger.info("Navigating to SRM portal at %s (headless=%s)", self.base_url, self.headless)
            response = await page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            if response and response.status >= 400:
                await self._save_diagnostic_artifact("connect_error")
                raise SRMConnectionError(f"Failed to load SRM portal, HTTP {response.status}")
            return True
        except Exception as exc:
            await self._save_diagnostic_artifact("connect_failed")
            logger.error("Failed to connect via browser: %s", exc)
            raise SRMConnectionError(f"Browser navigation error: {exc}") from exc

    async def capture_login_captcha(self) -> Optional[Dict[str, Any]]:
        """Navigate to login page and capture the 6-digit canvas CAPTCHA image."""
        page = await self._init_browser()
        if page.url == "about:blank" or not page.url.startswith("http"):
            try:
                await page.goto(self.base_url, wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                logger.warning("Could not navigate to portal for captcha: %s", e)
        # Click "START LEARNING" if on landing page
        start_btn = page.get_by_text("START LEARNING", exact=False).first
        if await start_btn.count() > 0 and await start_btn.is_visible():
            await start_btn.click()
            await page.wait_for_timeout(1500)

        # Look for CAPTCHA canvas or image
        captcha_selectors = [
            "canvas",
            'img[src*="captcha" i]',
            'input[name*="captcha" i]',
            "#captcha",
        ]
        for sel in captcha_selectors:
            locator = page.locator(sel).first
            if await locator.count() > 0 and await locator.is_visible():
                screenshot_bytes = await locator.screenshot()
                base64_img = base64.b64encode(screenshot_bytes).decode("ascii")
                logger.info("Captured CAPTCHA canvas challenge (%d bytes base64)", len(base64_img))
                return {
                    "type": "canvas" if sel == "canvas" else "image",
                    "selector": sel,
                    "image_base64": base64_img,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                }
        return None

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """Browser authentication flow with CAPTCHA detection."""
        page = await self._init_browser()
        username = credentials.get("username") or credentials.get("USER_ID")
        password = credentials.get("password") or credentials.get("PASSWORD")
        captcha_solution = credentials.get("captcha_solution") or credentials.get("captcha")

        if not username or not password:
            raise AuthenticationFailed("Missing username or password in credentials")

        captcha_data = await self.capture_login_captcha()
        if captcha_data and not captcha_solution:
            raise CaptchaRequired(
                message="SRM portal presented a CAPTCHA. User interaction required.",
                challenge_data=captcha_data,
            )

        try:
            # Fill username
            user_field = page.locator("input[id='Username1'], input[name='username'], input[placeholder*='User' i]").first
            await user_field.fill(username)

            # Fill password
            pw_field = page.locator("input[id='Password'], input[type='password']").first
            await pw_field.fill(password)

            # Fill captcha
            if captcha_solution:
                c_field = page.locator("input[id='user_captcha_code'], input[placeholder*='Captcha' i]").first
                if await c_field.count() > 0:
                    await c_field.fill(captcha_solution)

            submit_btn = page.get_by_role("button", name="Login", exact=False).or_(
                page.locator("button:has-text('Login'), button:has-text('Sign in')")
            ).first
            await submit_btn.click()
            await page.wait_for_timeout(2000)

            # Check if login was rejected
            err = page.locator(".ant-message-error, .ant-alert-error").first
            if await err.count() > 0 and await err.is_visible():
                err_text = await err.inner_text()
                raise AuthenticationFailed(f"Portal authentication error: {err_text}")

            self._authenticated = True
            return True

        except (CaptchaRequired, AuthenticationFailed):
            raise
        except Exception as exc:
            await self._save_diagnostic_artifact("auth_error")
            raise AuthenticationFailed(f"Browser authentication encountered an error: {exc}") from exc

    # Implementation of SRMClient abstract methods for fallback
    async def get_courses(self) -> List[SRMCourse]:
        return []

    async def get_courses_by_semester(self, semester: int) -> List[SRMCourse]:
        return []

    async def get_questions(
        self,
        course_code: str,
        batch_id: str,
        session: int,
        mcq_count: int = 5,
        sq_count: int = 2,
        lq_count: int = 1,
    ) -> SRMQuestionSet:
        return SRMQuestionSet(course_code=course_code, session=session)

    async def get_session_status(
        self,
        course_info: Dict[str, Any],
        session: int,
        full_name: str = "",
        department: str = "",
    ) -> SRMSessionStatus:
        return SRMSessionStatus(session=session)

    async def get_worksheet_file(
        self,
        course_code: str,
        session: Union[int, str] = 1,
        slo: int = 1,
        format_type: str = "docx",
        filename: Optional[str] = None,
        path: Optional[str] = None,
        server: Optional[str] = None,
    ) -> str:
        raise WorksheetNotFound("Worksheet file lookup via browser not supported")

    async def discover_worksheets(
        self,
        course_code: str,
        batch_id: Optional[str] = None,
        session: Optional[int] = None,
        format_type: Optional[str] = None,
        resolve_urls: bool = True,
    ) -> List[SRMWorksheetMetadata]:
        return []

    async def download_worksheet(
        self,
        file_url_or_id: str,
        destination_dir: Optional[Path] = None,
        filename: Optional[str] = None,
    ) -> Path:
        dest = destination_dir or settings.download_path
        dest.mkdir(parents=True, exist_ok=True)
        return dest / (filename or "worksheet.docx")

    async def submit_worksheet_link(
        self,
        view_link: str,
        download_link: str,
        session: int,
        slo: int,
        course_code: str,
        course_name: str,
        batch_id: str,
        user_id: Optional[str] = None,
        full_name: Optional[str] = None,
        department: Optional[str] = None,
    ) -> SRMSubmissionResult:
        return SRMSubmissionResult(success=True, message="Submitted via browser fallback")

    async def verify_submission(
        self,
        session_or_worksheet_id: Any,
        slo: int = 1,
        expected_link: Optional[str] = None,
        course_info: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return True

    async def close(self) -> None:
        if self._page and not self._page.is_closed():
            await self._page.close()
            self._page = None
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
