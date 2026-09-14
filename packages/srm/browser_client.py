import logging
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from packages.srm.client import SRMClient
from packages.srm.models import (
    SRMCourse,
    SRMSemester,
    SRMSubject,
    SRMWorksheet,
    SRMSubmissionReceipt,
)
from packages.srm.exceptions import (
    SRMConnectionError,
    SRMAuthenticationError,
    SRMCaptchaRequired,
    SRMWorksheetNotFoundError,
    SRMSubmissionError,
)
from packages.shared.config import settings

logger = logging.getLogger(__name__)


class SRMBrowserClient(SRMClient):
    """Playwright headless Chromium browser transport for SRM portal.
    
    Used when direct HTTP/API communication is impossible (e.g., dynamic SPA,
    session state bound to complex client-side JS, or complex form workflows).
    
    Adheres to requirements:
    - Headless Chromium
    - Downloads enabled
    - Semantic locators (no coordinate clicks)
    - Diagnostic artifacts / screenshots saved on failure or challenge
    - CAPTCHA detection triggers SRMCaptchaRequired exception
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        headless: Optional[bool] = None,
        artifacts_dir: Optional[Path] = None,
    ):
        self.base_url = (base_url or settings.SRM_BASE_URL).rstrip("/")
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
                logger.info("Saved diagnostic artifacts to %s and %s", screenshot_path, dom_path)
            except Exception as exc:
                logger.warning("Failed to save diagnostic artifact: %s", exc)
        return screenshot_path

    async def connect(self) -> bool:
        page = await self._init_browser()
        try:
            logger.info("Navigating to SRM portal at %s (headless=%s)", self.base_url, self.headless)
            response = await page.goto(self.base_url, wait_until="networkidle", timeout=30000)
            if response and response.status >= 400:
                await self._save_diagnostic_artifact("connect_error")
                raise SRMConnectionError(f"Failed to load SRM portal, HTTP {response.status}")
            return True
        except Exception as exc:
            await self._save_diagnostic_artifact("connect_failed")
            logger.error("Failed to connect via browser: %s", exc)
            raise SRMConnectionError(f"Browser navigation error: {exc}") from exc

    async def _detect_captcha(self, page: Page) -> Optional[Dict[str, Any]]:
        """Detect CAPTCHA elements on the page without attempting to bypass it."""
        # Common semantic selectors for CAPTCHA elements
        captcha_selectors = [
            'img[src*="captcha" i]',
            'img[alt*="captcha" i]',
            'input[name*="captcha" i]',
            'input[placeholder*="captcha" i]',
            '#captcha',
            '.captcha-image',
        ]
        
        for selector in captcha_selectors:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                logger.warning("CAPTCHA element detected: %s", selector)
                
                # Take screenshot of the captcha element or page
                screenshot_bytes = await locator.screenshot() if selector.startswith("img") else await page.screenshot()
                base64_img = base64.b64encode(screenshot_bytes).decode("ascii")
                
                return {
                    "type": "image",
                    "selector": selector,
                    "image_base64": base64_img,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                }
        return None

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        page = await self._init_browser()
        username = credentials.get("username")
        password = credentials.get("password")
        captcha_solution = credentials.get("captcha_solution")

        if not username or not password:
            raise SRMAuthenticationError("Missing username or password in credentials")

        # Check if already at login page, otherwise navigate
        if self.base_url not in page.url:
            await self.connect()

        # Check for CAPTCHA before filling
        captcha_data = await self._detect_captcha(page)
        if captcha_data and not captcha_solution:
            await self._save_diagnostic_artifact("captcha_presented")
            raise SRMCaptchaRequired(
                message="SRM portal presented a CAPTCHA. User interaction required.",
                challenge_data=captcha_data,
            )

        try:
            # Semantic locators for login inputs (avoid brittle coordinate/index clicks)
            # Try username field
            username_field = page.get_by_label("Username", exact=False).or_(
                page.get_by_placeholder("Username", exact=False)
            ).or_(
                page.locator("input[name='username'], input[type='text'], input[name*='user' i]").first
            )
            await username_field.fill(username)

            # Try password field
            password_field = page.get_by_label("Password", exact=False).or_(
                page.get_by_placeholder("Password", exact=False)
            ).or_(
                page.locator("input[type='password']").first
            )
            await password_field.fill(password)

            # If user provided captcha solution, fill captcha field
            if captcha_solution:
                captcha_field = page.locator("input[name*='captcha' i], input[placeholder*='captcha' i]").first
                if await captcha_field.count() > 0:
                    await captcha_field.fill(captcha_solution)

            # Submit button using semantic role or button locator
            submit_btn = page.get_by_role("button", name="Login", exact=False).or_(
                page.get_by_role("button", name="Sign in", exact=False)
            ).or_(
                page.locator("button[type='submit'], input[type='submit']").first
            )
            await submit_btn.click()

            # Wait for response / navigation
            await page.wait_for_load_state("networkidle", timeout=15000)

            # Check if login was rejected or another CAPTCHA appeared
            post_captcha = await self._detect_captcha(page)
            if post_captcha:
                await self._save_diagnostic_artifact("post_login_captcha")
                raise SRMCaptchaRequired(
                    message="SRM portal presented or rejected CAPTCHA",
                    challenge_data=post_captcha,
                )

            # Check for error banners
            error_banner = page.locator(".alert-danger, .error-message, [role='alert']").first
            if await error_banner.count() > 0 and await error_banner.is_visible():
                err_text = await error_banner.inner_text()
                await self._save_diagnostic_artifact("auth_failed_banner")
                raise SRMAuthenticationError(f"Authentication rejected by portal: {err_text.strip()}")

            self._authenticated = True
            logger.info("Browser-based authentication completed successfully")
            return True

        except (SRMCaptchaRequired, SRMAuthenticationError):
            raise
        except Exception as exc:
            await self._save_diagnostic_artifact("auth_exception")
            logger.error("Browser authentication error: %s", exc)
            raise SRMAuthenticationError(f"Browser authentication encountered an error: {exc}") from exc

    async def discover_courses(self) -> List[SRMCourse]:
        page = await self._init_browser()
        # Find course elements or list
        courses: List[SRMCourse] = []
        course_elements = page.locator(".course-card, .subject-item, tr.course-row")
        count = await course_elements.count()
        for idx in range(count):
            elem = course_elements.nth(idx)
            text = (await elem.inner_text()).strip()
            courses.append(SRMCourse(id=f"course-{idx+1}", name=text.split("\n")[0]))
        return courses

    async def select_semester(self, semester_id: str) -> bool:
        page = await self._init_browser()
        # Try select dropdown or semantic link
        select_elem = page.locator("select[name*='semester' i]").first
        if await select_elem.count() > 0:
            await select_elem.select_option(value=semester_id)
            await page.wait_for_load_state("networkidle")
            return True
        return True

    async def select_subject(self, subject_id: str) -> bool:
        page = await self._init_browser()
        select_elem = page.locator("select[name*='subject' i]").first
        if await select_elem.count() > 0:
            await select_elem.select_option(value=subject_id)
            await page.wait_for_load_state("networkidle")
            return True
        return True

    async def discover_worksheets(self, subject_id: Optional[str] = None) -> List[SRMWorksheet]:
        page = await self._init_browser()
        worksheets: List[SRMWorksheet] = []
        items = page.locator(".worksheet-row, tr[data-worksheet], .assignment-card")
        count = await items.count()
        for idx in range(count):
            item = items.nth(idx)
            title = (await item.inner_text()).split("\n")[0]
            worksheets.append(SRMWorksheet(id=f"ws-{idx+1}", title=title, subject_id=subject_id))
        return worksheets

    async def download_worksheet(
        self,
        worksheet_id: str,
        destination_dir: Optional[Path] = None
    ) -> Path:
        page = await self._init_browser()
        dest = destination_dir or settings.download_path
        dest.mkdir(parents=True, exist_ok=True)
        target_path = dest / f"worksheet_{worksheet_id}.pdf"

        # Find download link using semantic text or attribute
        download_locator = page.get_by_role("link", name="Download", exact=False).or_(
            page.locator(f"a[href*='download'][href*='{worksheet_id}']")
        ).first

        if await download_locator.count() == 0:
            await self._save_diagnostic_artifact(f"download_not_found_{worksheet_id}")
            raise SRMWorksheetNotFoundError(f"Download trigger for worksheet {worksheet_id} not found")

        # Handle Playwright download event safely
        async with page.expect_download() as download_info:
            await download_locator.click()
        download = await download_info.value
        await download.save_as(str(target_path))
        logger.info("Downloaded worksheet to %s via browser", target_path)
        return target_path

    async def submit_worksheet(
        self,
        worksheet_id: str,
        file_path: Path,
        comments: Optional[str] = None
    ) -> SRMSubmissionReceipt:
        if not file_path.exists():
            raise SRMWorksheetNotFoundError(f"Local file not found: {file_path}")

        page = await self._init_browser()
        try:
            # Locate file input element
            file_input = page.locator("input[type='file']").first
            if await file_input.count() == 0:
                await self._save_diagnostic_artifact(f"submit_no_input_{worksheet_id}")
                raise SRMSubmissionError("File upload input not found on page")

            await file_input.set_input_files(str(file_path))

            # Fill optional comments
            if comments:
                comment_field = page.locator("textarea[name*='comment' i]").first
                if await comment_field.count() > 0:
                    await comment_field.fill(comments)

            # Submit
            submit_btn = page.get_by_role("button", name="Submit", exact=False).first
            await submit_btn.click()
            await page.wait_for_load_state("networkidle")

            return SRMSubmissionReceipt(
                worksheet_id=worksheet_id,
                submitted_at=datetime.now(timezone.utc),
                verification_status="PENDING_VERIFICATION",
                message="File uploaded via browser automation"
            )
        except Exception as exc:
            await self._save_diagnostic_artifact(f"submit_failure_{worksheet_id}")
            raise SRMSubmissionError(f"Failed to submit worksheet via browser: {exc}") from exc

    async def verify_submission(self, worksheet_id: str) -> bool:
        page = await self._init_browser()
        badge = page.get_by_text("Submitted", exact=False).or_(
            page.locator(".badge-success, .status-submitted")
        ).first
        return await badge.count() > 0 and await badge.is_visible()

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
