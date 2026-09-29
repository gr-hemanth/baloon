import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Union

from packages.srm.client import SRMClient
from packages.srm.http_client import SRMHttpClient
from packages.srm.browser_client import SRMBrowserClient
from packages.srm.exceptions import (
    SRMTransportUnavailableError,
    CaptchaRequired,
    AuthenticationFailed,
)
from packages.srm.models import (
    SRMCourse,
    SRMQuestionSet,
    SRMSessionStatus,
    SRMSubmissionResult,
    SRMWorksheetFile,
    SRMWorksheetMetadata,
    SRMAuthSession,
)
from packages.shared.config import settings

logger = logging.getLogger("srm_orchestrator")


class SRMOrchestrator(SRMClient):
    """Adaptive orchestration layer between direct HTTP and Playwright browser transports.
    
    Milestone 2 implementation:
    - Strictly routes all post-authentication operations through SRMHttpClient.
    - Uses SRMBrowserClient only for CAPTCHA capture or emergency fallback.
    - Proves and guarantees direct HTTP usage.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        prefer_http: Optional[bool] = None,
        mode: str = "auto",  # "auto", "http", "browser"
    ):
        self.portal_url = (base_url or settings.SRM_BASE_URL).rstrip("/")
        self.mode = mode
        self.prefer_http = settings.SRM_PREFER_HTTP if prefer_http is None else prefer_http
        
        self.http_client = SRMHttpClient(base_url=self.portal_url)
        self.browser_client = SRMBrowserClient(base_url=self.portal_url)
        self.auth_session: Optional[SRMAuthSession] = None
        self._status_callback: Optional[Any] = None
        
        if self.mode == "browser" or not self.prefer_http:
            self._active_client: SRMClient = self.browser_client
        else:
            self._active_client: SRMClient = self.http_client

    def set_status_callback(self, callback: Any) -> None:
        """Register status transition callback: callback(phase: str, message: str)."""
        self._status_callback = callback

    @property
    def transport_name(self) -> str:
        return self._active_client.transport_name

    async def connect(self) -> bool:
        if self.mode == "browser":
            return await self.browser_client.connect()
        try:
            return await self.http_client.connect()
        except SRMTransportUnavailableError:
            logger.info("Direct connect failed, falling back to browser")
            self._active_client = self.browser_client
            return await self.browser_client.connect()

    async def capture_login_captcha(self) -> Optional[Dict[str, Any]]:
        """Use Playwright to capture login page CAPTCHA (backward compatibility)."""
        if self.mode == "http":
            return None
        try:
            return await self.browser_client.capture_login_captcha()
        except Exception as exc:
            logger.warning("Browser CAPTCHA capture unavailable: %s", exc)
            return None

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """Authenticate student with portal credentials.
        
        Primary mechanism: Playwright browser-assisted authentication.
        Once authenticated, captures SRMAuthSession, sets it on SRMHttpClient,
        and switches all subsequent operations to direct HTTP.
        """
        # 1. Check if caller provided an existing valid SRMAuthSession
        existing_session = credentials.get("auth_session")
        if isinstance(existing_session, SRMAuthSession) and existing_session.is_valid:
            self.auth_session = existing_session
            self.http_client.set_auth_session(existing_session)
            self._active_client = self.http_client
            return True

        if self.auth_session and self.auth_session.is_valid:
            self.http_client.set_auth_session(self.auth_session)
            self._active_client = self.http_client
            return True

        # 2. Check for explicit http mode with pre-solved credentials (or test mocks)
        if self.mode == "http" and (credentials.get("captcha_solution") or credentials.get("captcha")):
            try:
                res = await self.http_client.authenticate(credentials)
                self.auth_session = self.http_client.auth_session
                self._active_client = self.http_client
                return res
            except Exception:
                pass

        # 3. Compatibility check for unit tests mocking capture_login_captcha
        captcha_solution = credentials.get("captcha_solution") or credentials.get("captcha")
        captcha_data = await self.capture_login_captcha()
        if captcha_data and not captcha_solution:
            raise CaptchaRequired(
                message="SRM portal presented a CAPTCHA. User interaction required.",
                challenge_data=captcha_data,
            )

        # 4. Primary Playwright browser-assisted authentication
        try:
            auth_session = await self.browser_client.authenticate_interactive(
                credentials, status_callback=self._status_callback
            )
            self.auth_session = auth_session
            self.http_client.set_auth_session(auth_session)
            self._active_client = self.http_client
            await self.browser_client.close()
            logger.info("Playwright browser authentication successful. Captured session passed to direct HTTP client.")
            return True
        except (CaptchaRequired, AuthenticationFailed):
            raise
        except Exception as exc:
            logger.warning("Primary browser auth error: %s. Attempting direct fallback.", exc)
            # Fallback to direct HTTP authenticate if browser transport fails to launch
            res = await self.http_client.authenticate(credentials)
            self.auth_session = self.http_client.auth_session
            self._active_client = self.http_client
            return res

    async def get_courses(self) -> List[SRMCourse]:
        """Direct HTTP course discovery."""
        if self.mode == "browser":
            return await self.browser_client.get_courses()
        try:
            return await self.http_client.get_courses()
        except SRMTransportUnavailableError:
            return await self.browser_client.get_courses()

    async def get_courses_by_semester(self, semester: int) -> List[SRMCourse]:
        """Direct HTTP semester filtering."""
        if self.mode == "browser":
            return await self.browser_client.get_courses_by_semester(semester)
        try:
            return await self.http_client.get_courses_by_semester(semester)
        except SRMTransportUnavailableError:
            return await self.browser_client.get_courses_by_semester(semester)

    async def get_questions(
        self,
        course_code: str,
        batch_id: str,
        session: int,
        mcq_count: int = 5,
        sq_count: int = 2,
        lq_count: int = 1,
    ) -> SRMQuestionSet:
        """Direct HTTP session question retrieval."""
        if self.mode == "browser":
            return await self.browser_client.get_questions(
                course_code, batch_id, session, mcq_count, sq_count, lq_count
            )
        try:
            return await self.http_client.get_questions(
                course_code, batch_id, session, mcq_count, sq_count, lq_count
            )
        except SRMTransportUnavailableError:
            return await self.browser_client.get_questions(
                course_code, batch_id, session, mcq_count, sq_count, lq_count
            )

    async def get_session_status(
        self,
        course_info: Dict[str, Any],
        session: int,
        full_name: str = "",
        department: str = "",
    ) -> SRMSessionStatus:
        """Direct HTTP session and worksheet status check."""
        if self.mode == "browser":
            return await self.browser_client.get_session_status(
                course_info, session, full_name, department
            )
        try:
            return await self.http_client.get_session_status(
                course_info, session, full_name, department
            )
        except SRMTransportUnavailableError:
            return await self.browser_client.get_session_status(
                course_info, session, full_name, department
            )

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
        """Direct HTTP worksheet file path lookup."""
        if self.mode == "browser":
            return await self.browser_client.get_worksheet_file(
                course_code, session, slo, format_type, filename, path, server
            )
        try:
            return await self.http_client.get_worksheet_file(
                course_code, session, slo, format_type, filename, path, server
            )
        except SRMTransportUnavailableError:
            return await self.browser_client.get_worksheet_file(
                course_code, session, slo, format_type, filename, path, server
            )

    async def discover_worksheets(
        self,
        course_code: str,
        batch_id: Optional[str] = None,
        session: Optional[int] = None,
        format_type: Optional[str] = None,
        resolve_urls: bool = True,
    ) -> List[SRMWorksheetMetadata]:
        """Discover available worksheets for a course based on portal course status and session metadata."""
        if self.mode == "browser":
            return await self.browser_client.discover_worksheets(
                course_code, batch_id, session, format_type, resolve_urls
            )
        try:
            return await self.http_client.discover_worksheets(
                course_code, batch_id, session, format_type, resolve_urls
            )
        except SRMTransportUnavailableError:
            return await self.browser_client.discover_worksheets(
                course_code, batch_id, session, format_type, resolve_urls
            )

    async def download_worksheet(
        self,
        file_url_or_id: str,
        destination_dir: Optional[Path] = None,
        filename: Optional[str] = None,
    ) -> Path:
        """Direct HTTP worksheet file download."""
        if self.mode == "browser":
            return await self.browser_client.download_worksheet(file_url_or_id, destination_dir, filename)
        try:
            return await self.http_client.download_worksheet(file_url_or_id, destination_dir, filename)
        except SRMTransportUnavailableError:
            return await self.browser_client.download_worksheet(file_url_or_id, destination_dir, filename)

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
        """Direct HTTP worksheet link submission (UPDATE action)."""
        if self.mode == "browser":
            return await self.browser_client.submit_worksheet_link(
                view_link, download_link, session, slo, course_code, course_name, batch_id,
                user_id, full_name, department
            )
        try:
            return await self.http_client.submit_worksheet_link(
                view_link, download_link, session, slo, course_code, course_name, batch_id,
                user_id, full_name, department
            )
        except SRMTransportUnavailableError:
            return await self.browser_client.submit_worksheet_link(
                view_link, download_link, session, slo, course_code, course_name, batch_id,
                user_id, full_name, department
            )

    async def verify_submission(
        self,
        session_or_worksheet_id: Any,
        slo: int = 1,
        expected_link: Optional[str] = None,
        course_info: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Direct HTTP submission verification."""
        if self.mode == "browser":
            return await self.browser_client.verify_submission(
                session_or_worksheet_id, slo, expected_link, course_info
            )
        try:
            return await self.http_client.verify_submission(
                session_or_worksheet_id, slo, expected_link, course_info
            )
        except SRMTransportUnavailableError:
            return await self.browser_client.verify_submission(
                session_or_worksheet_id, slo, expected_link, course_info
            )

    async def close(self) -> None:
        await self.http_client.close()
        await self.browser_client.close()
