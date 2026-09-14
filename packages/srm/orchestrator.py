import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

from packages.srm.client import SRMClient
from packages.srm.http_client import SRMHttpClient
from packages.srm.browser_client import SRMBrowserClient
from packages.srm.exceptions import (
    SRMTransportUnavailableError,
    SRMCaptchaRequired,
)
from packages.srm.models import (
    SRMCourse,
    SRMSemester,
    SRMSubject,
    SRMWorksheet,
    SRMSubmissionReceipt,
)
from packages.shared.config import settings

logger = logging.getLogger(__name__)


class SRMOrchestrator(SRMClient):
    """Adaptive orchestration layer between direct HTTP and Playwright browser transports.
    
    Prefers direct HTTP/API communication wherever possible to minimize overhead.
    Transparently falls back to Playwright headless browser when direct HTTP endpoints
    are missing, require JavaScript execution, or fail with transport errors.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        prefer_http: Optional[bool] = None,
        mode: str = "auto",  # "auto", "http", "browser"
    ):
        self.base_url = (base_url or settings.SRM_BASE_URL).rstrip("/")
        self.mode = mode
        self.prefer_http = settings.SRM_PREFER_HTTP if prefer_http is None else prefer_http
        
        self.http_client = SRMHttpClient(base_url=self.base_url)
        self.browser_client = SRMBrowserClient(base_url=self.base_url)
        
        # Determine initial active transport
        if self.mode == "browser" or not self.prefer_http:
            self._active_client: SRMClient = self.browser_client
        else:
            self._active_client: SRMClient = self.http_client

    @property
    def transport_name(self) -> str:
        return self._active_client.transport_name

    async def _fallback_to_browser(self, reason: str) -> None:
        """Switch active transport from HTTP to Browser."""
        if self._active_client.transport_name != "browser":
            logger.info("Falling back to Playwright headless browser transport: %s", reason)
            self._active_client = self.browser_client

    async def connect(self) -> bool:
        if self.mode == "browser":
            return await self.browser_client.connect()
        try:
            return await self.http_client.connect()
        except SRMTransportUnavailableError as exc:
            await self._fallback_to_browser(str(exc))
            return await self.browser_client.connect()

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        if self.mode == "browser":
            return await self.browser_client.authenticate(credentials)
        try:
            return await self.http_client.authenticate(credentials)
        except SRMTransportUnavailableError as exc:
            await self._fallback_to_browser(str(exc))
            return await self.browser_client.authenticate(credentials)

    async def discover_courses(self) -> List[SRMCourse]:
        if self.mode == "browser":
            return await self.browser_client.discover_courses()
        try:
            return await self.http_client.discover_courses()
        except SRMTransportUnavailableError as exc:
            await self._fallback_to_browser(str(exc))
            return await self.browser_client.discover_courses()

    async def select_semester(self, semester_id: str) -> bool:
        if self.mode == "browser":
            return await self.browser_client.select_semester(semester_id)
        try:
            return await self.http_client.select_semester(semester_id)
        except SRMTransportUnavailableError as exc:
            await self._fallback_to_browser(str(exc))
            return await self.browser_client.select_semester(semester_id)

    async def select_subject(self, subject_id: str) -> bool:
        if self.mode == "browser":
            return await self.browser_client.select_subject(subject_id)
        try:
            return await self.http_client.select_subject(subject_id)
        except SRMTransportUnavailableError as exc:
            await self._fallback_to_browser(str(exc))
            return await self.browser_client.select_subject(subject_id)

    async def discover_worksheets(self, subject_id: Optional[str] = None) -> List[SRMWorksheet]:
        if self.mode == "browser":
            return await self.browser_client.discover_worksheets(subject_id)
        try:
            return await self.http_client.discover_worksheets(subject_id)
        except SRMTransportUnavailableError as exc:
            await self._fallback_to_browser(str(exc))
            return await self.browser_client.discover_worksheets(subject_id)

    async def download_worksheet(
        self,
        worksheet_id: str,
        destination_dir: Optional[Path] = None
    ) -> Path:
        if self.mode == "browser":
            return await self.browser_client.download_worksheet(worksheet_id, destination_dir)
        try:
            return await self.http_client.download_worksheet(worksheet_id, destination_dir)
        except SRMTransportUnavailableError as exc:
            await self._fallback_to_browser(str(exc))
            return await self.browser_client.download_worksheet(worksheet_id, destination_dir)

    async def submit_worksheet(
        self,
        worksheet_id: str,
        file_path: Path,
        comments: Optional[str] = None
    ) -> SRMSubmissionReceipt:
        if self.mode == "browser":
            return await self.browser_client.submit_worksheet(worksheet_id, file_path, comments)
        try:
            return await self.http_client.submit_worksheet(worksheet_id, file_path, comments)
        except SRMTransportUnavailableError as exc:
            await self._fallback_to_browser(str(exc))
            return await self.browser_client.submit_worksheet(worksheet_id, file_path, comments)

    async def verify_submission(self, worksheet_id: str) -> bool:
        if self.mode == "browser":
            return await self.browser_client.verify_submission(worksheet_id)
        try:
            return await self.http_client.verify_submission(worksheet_id)
        except SRMTransportUnavailableError as exc:
            await self._fallback_to_browser(str(exc))
            return await self.browser_client.verify_submission(worksheet_id)

    async def close(self) -> None:
        await self.http_client.close()
        await self.browser_client.close()
