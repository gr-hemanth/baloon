import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
import httpx

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
    SRMTransportUnavailableError,
    SRMWorksheetNotFoundError,
)
from packages.shared.config import settings

logger = logging.getLogger(__name__)


class SRMHttpClient(SRMClient):
    """Direct HTTP/REST/Form-based transport for SRM portal operations.
    
    Prefers direct API communication to avoid browser overhead.
    If an endpoint is unavailable, requires JavaScript execution, or is
    incompatible with direct requests, raises SRMTransportUnavailableError
    so the orchestrator can fall back to SRMBrowserClient.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[int] = None):
        self.base_url = (base_url or settings.SRM_BASE_URL).rstrip("/")
        self.timeout = timeout or settings.SRM_REQUEST_TIMEOUT_SECONDS
        self._client: Optional[httpx.AsyncClient] = None
        self._authenticated = False
        self._auth_token: Optional[str] = None
        self._cookies: Dict[str, str] = {}
        self._current_semester_id: Optional[str] = None
        self._current_subject_id: Optional[str] = None

    @property
    def transport_name(self) -> str:
        return "http"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/html, application/xhtml+xml, */*",
                "Accept-Language": "en-US,en;q=0.9",
            }
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=float(self.timeout),
                follow_redirects=True,
                headers=headers,
            )
        return self._client

    async def connect(self) -> bool:
        client = await self._get_client()
        try:
            response = await client.get("/")
            if response.status_code >= 400:
                raise SRMConnectionError(f"SRM portal returned status {response.status_code}")
            logger.info("Direct HTTP connection established with %s", self.base_url)
            return True
        except httpx.RequestError as exc:
            logger.warning("Direct HTTP connection failed: %s", exc)
            raise SRMConnectionError(f"Failed to connect to SRM portal: {exc}") from exc

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """Attempt authentication via direct HTTP POST."""
        client = await self._get_client()
        username = credentials.get("username")
        password = credentials.get("password")
        captcha_solution = credentials.get("captcha_solution")

        if not username or not password:
            raise SRMAuthenticationError("Missing username or password in credentials")

        # Probe candidate authentication endpoints
        auth_payload = {
            "username": username,
            "password": password,
        }
        if captcha_solution:
            auth_payload["captcha"] = captcha_solution

        try:
            # Common SRM authentication candidate endpoints
            response = await client.post("/api/auth/login", json=auth_payload)
            
            # Check if direct API exists
            if response.status_code == 404:
                # Direct API does not exist, check if form POST is supported
                form_response = await client.post("/login", data=auth_payload)
                if form_response.status_code == 404:
                    raise SRMTransportUnavailableError(
                        "No direct HTTP login endpoint found; browser transport required"
                    )
                response = form_response

            # Check for CAPTCHA response
            if "captcha" in response.text.lower() and response.status_code in (400, 403):
                raise SRMCaptchaRequired(
                    message="SRM portal presented a CAPTCHA challenge",
                    challenge_data={"endpoint": str(response.url)}
                )

            if response.status_code in (200, 302):
                self._authenticated = True
                logger.info("Direct HTTP authentication succeeded")
                return True
            else:
                raise SRMAuthenticationError(f"Authentication failed with status {response.status_code}")

        except (httpx.RequestError, SRMTransportUnavailableError) as exc:
            if isinstance(exc, (SRMCaptchaRequired, SRMAuthenticationError)):
                raise
            logger.info("HTTP auth failed or endpoint unsupported, fallback indicated: %s", exc)
            raise SRMTransportUnavailableError(str(exc)) from exc

    async def discover_courses(self) -> List[SRMCourse]:
        client = await self._get_client()
        try:
            response = await client.get("/api/courses")
            if response.status_code == 404:
                raise SRMTransportUnavailableError("Direct courses API not available")
            response.raise_for_status()
            data = response.json()
            return [
                SRMCourse(
                    id=str(c.get("id", idx)),
                    name=c.get("name", "Unknown Course"),
                    code=c.get("code"),
                    department=c.get("department")
                )
                for idx, c in enumerate(data if isinstance(data, list) else data.get("courses", []))
            ]
        except (httpx.RequestError, ValueError) as exc:
            raise SRMTransportUnavailableError(f"Direct courses discovery failed: {exc}") from exc

    async def select_semester(self, semester_id: str) -> bool:
        self._current_semester_id = semester_id
        return True

    async def select_subject(self, subject_id: str) -> bool:
        self._current_subject_id = subject_id
        return True

    async def discover_worksheets(self, subject_id: Optional[str] = None) -> List[SRMWorksheet]:
        target_subject = subject_id or self._current_subject_id
        client = await self._get_client()
        try:
            response = await client.get(f"/api/subjects/{target_subject}/worksheets")
            if response.status_code == 404:
                raise SRMTransportUnavailableError("Direct worksheets API not available")
            response.raise_for_status()
            data = response.json()
            return [
                SRMWorksheet(
                    id=str(w.get("id")),
                    title=w.get("title", "Untitled Worksheet"),
                    subject_id=target_subject,
                    due_date=w.get("due_date"),
                    download_url=w.get("download_url"),
                    submission_url=w.get("submission_url"),
                )
                for w in (data if isinstance(data, list) else data.get("worksheets", []))
            ]
        except (httpx.RequestError, ValueError) as exc:
            raise SRMTransportUnavailableError(f"Direct worksheets discovery failed: {exc}") from exc

    async def download_worksheet(
        self,
        worksheet_id: str,
        destination_dir: Optional[Path] = None
    ) -> Path:
        client = await self._get_client()
        dest = destination_dir or settings.download_path
        dest.mkdir(parents=True, exist_ok=True)
        target_path = dest / f"worksheet_{worksheet_id}.pdf"

        try:
            response = await client.get(f"/api/worksheets/{worksheet_id}/download")
            if response.status_code == 404:
                raise SRMTransportUnavailableError("Direct download API not available")
            response.raise_for_status()

            target_path.write_bytes(response.content)
            logger.info("Worksheet %s downloaded via HTTP to %s", worksheet_id, target_path)
            return target_path
        except (httpx.RequestError, SRMTransportUnavailableError) as exc:
            raise SRMTransportUnavailableError(f"Direct download failed: {exc}") from exc

    async def submit_worksheet(
        self,
        worksheet_id: str,
        file_path: Path,
        comments: Optional[str] = None
    ) -> SRMSubmissionReceipt:
        if not file_path.exists():
            raise SRMWorksheetNotFoundError(f"Local file does not exist: {file_path}")

        client = await self._get_client()
        try:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.name, f, "application/pdf")}
                data = {"worksheet_id": worksheet_id, "comments": comments or ""}
                response = await client.post(
                    f"/api/worksheets/{worksheet_id}/submit",
                    files=files,
                    data=data
                )
            if response.status_code == 404:
                raise SRMTransportUnavailableError("Direct submission API not available")
            response.raise_for_status()
            
            return SRMSubmissionReceipt(
                worksheet_id=worksheet_id,
                submitted_at=httpx._utils.now(),
                verification_status="PENDING_VERIFICATION",
                message="Submitted via direct HTTP"
            )
        except (httpx.RequestError, SRMTransportUnavailableError) as exc:
            raise SRMTransportUnavailableError(f"Direct submission failed: {exc}") from exc

    async def verify_submission(self, worksheet_id: str) -> bool:
        client = await self._get_client()
        try:
            response = await client.get(f"/api/worksheets/{worksheet_id}/status")
            if response.status_code == 404:
                raise SRMTransportUnavailableError("Direct status API not available")
            response.raise_for_status()
            data = response.json()
            return data.get("status") in ("SUBMITTED", "EVALUATED", "VERIFIED")
        except Exception as exc:
            raise SRMTransportUnavailableError(f"Direct verification check failed: {exc}") from exc

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
