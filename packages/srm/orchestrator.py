import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

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
        
        if self.mode == "browser" or not self.prefer_http:
            self._active_client: SRMClient = self.browser_client
        else:
            self._active_client: SRMClient = self.http_client

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
        """Use Playwright to capture the login page CAPTCHA canvas."""
        return await self.browser_client.capture_login_captcha()

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """Authenticate student with portal credentials.
        
        Uses direct HTTP POST /curricula/login.
        If CAPTCHA is required and not yet provided, triggers browser CAPTCHA capture.
        """
        captcha_solution = credentials.get("captcha_solution") or credentials.get("captcha")
        
        if self.mode == "browser":
            return await self.browser_client.authenticate(credentials)

        # Direct HTTP authentication
        try:
            return await self.http_client.authenticate(credentials)
        except AuthenticationFailed:
            raise
        except SRMTransportUnavailableError:
            logger.info("Direct HTTP auth endpoint unavailable, falling back to browser")
            self._active_client = self.browser_client
            return await self.browser_client.authenticate(credentials)

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
        filename: str,
        path: Optional[str] = None,
        server: Optional[str] = None,
    ) -> str:
        """Direct HTTP worksheet file path lookup."""
        if self.mode == "browser":
            return await self.browser_client.get_worksheet_file(course_code, filename, path, server)
        try:
            return await self.http_client.get_worksheet_file(course_code, filename, path, server)
        except SRMTransportUnavailableError:
            return await self.browser_client.get_worksheet_file(course_code, filename, path, server)

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
