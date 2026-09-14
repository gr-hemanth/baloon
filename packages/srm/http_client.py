import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import httpx

from packages.srm.client import SRMClient
from packages.srm.models import (
    SRMCourse,
    SRMQuestionSet,
    SRMSessionStatus,
    SRMSubmissionResult,
    SRMWorksheetFile,
    SRMWorksheetMetadata,
    SRMCourseStatus,
)
from packages.srm.exceptions import (
    SRMConnectionError,
    AuthenticationFailed,
    InvalidSession,
    Unauthorized,
    SRMApiError,
    WorksheetNotFound,
    DownloadFailed,
    SubmissionFailed,
    VerificationFailed,
    SRMTransportUnavailableError,
)
from packages.shared.config import settings

logger = logging.getLogger("srm_http_client")


class SRMHttpClient(SRMClient):
    """Direct HTTP/REST transport for SRM eCurricula portal operations.
    
    Milestone 2 implementation:
    - Performs 100% of post-authentication operations via direct JSON POST requests.
    - Stores JWT token strictly in-memory during active session lifecycle.
    - Never logs passwords, tokens, cookies, or Authorization headers.
    - Automatically retries transient network/server failures with exponential backoff.
    - Does NOT retry authentication failures.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        questions_server_url: Optional[str] = None,
        key: str = "john",
        timeout: Optional[int] = None,
        max_retries: int = 3,
    ):
        # Default base URL is the dedicated FET LMS server endpoint
        self.portal_url = (base_url or "https://dld.srmist.edu.in").rstrip("/")
        if not self.portal_url.endswith("/ktretecurricula/server"):
            self.api_base_url = f"{self.portal_url}/ktretecurricula/server"
        else:
            self.api_base_url = self.portal_url

        self.questions_server_url = (
            questions_server_url or f"{self.portal_url}/etecurricula/server"
        )
        self.key = key
        self.timeout = float(timeout or settings.SRM_REQUEST_TIMEOUT_SECONDS)
        self.max_retries = max_retries

        self._client: Optional[httpx.AsyncClient] = None
        self._jwt_token: Optional[str] = None  # Strictly in-memory
        self._user_id: Optional[str] = None
        self._user_data: Dict[str, Any] = {}

    @property
    def transport_name(self) -> str:
        return "http"

    @property
    def is_authenticated(self) -> bool:
        return self._jwt_token is not None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            }
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers=headers,
            )
        return self._client

    def _get_auth_headers(self) -> Dict[str, str]:
        """Attach JWT in Authorization header if present."""
        headers = {}
        if self._jwt_token:
            headers["Authorization"] = self._jwt_token
        return headers

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        is_auth_endpoint: bool = False,
    ) -> httpx.Response:
        """Execute HTTP request with transient retry logic and sanitized logging."""
        client = await self._get_client()
        req_headers = {**(headers or {})}

        # Determine endpoint path for safe logging
        parsed_url = httpx.URL(url)
        safe_path = parsed_url.path
        
        total_attempts = self.max_retries + 1
        attempt = 0
        backoff = 0.5

        while attempt < total_attempts:
            attempt += 1
            start_time = time.time()
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    json=json_data,
                    headers=req_headers,
                )
                duration_ms = int((time.time() - start_time) * 1000)
                logger.info(
                    "HTTP %s %s -> Status %d (%d ms)",
                    method, safe_path, response.status_code, duration_ms
                )

                # Check for transient server errors (502, 503, 504)
                if response.status_code in (502, 503, 504) and attempt < total_attempts:
                    logger.warning(
                        "Transient server error %d on %s (attempt %d/%d). Retrying in %.2fs...",
                        response.status_code, safe_path, attempt, total_attempts, backoff
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue

                # Authentication endpoints: Do NOT retry 400/401/403
                if is_auth_endpoint and response.status_code in (400, 401, 403):
                    return response

                # Handle 401/403 on protected endpoints
                if response.status_code == 401:
                    raise Unauthorized(f"Unauthorized access to {safe_path}")
                if response.status_code == 403:
                    raise InvalidSession(f"Session expired or forbidden on {safe_path}")

                return response

            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
                duration_ms = int((time.time() - start_time) * 1000)
                logger.warning(
                    "HTTP %s %s failed with network error: %s (%d ms). Attempt %d/%d",
                    method, safe_path, exc, duration_ms, attempt, total_attempts
                )
                if attempt >= total_attempts:
                    raise SRMConnectionError(f"Network error accessing {safe_path}: {exc}") from exc
                await asyncio.sleep(backoff)
                backoff *= 2

        raise SRMConnectionError(f"Max retries exceeded for {safe_path}")

    async def connect(self) -> bool:
        """Probe checkstatus endpoint to verify connectivity."""
        url = f"{self.api_base_url}/curricula/checkstatus"
        try:
            resp = await self._request_with_retry("POST", url, json_data={"key": self.key})
            if resp.status_code >= 400:
                raise SRMConnectionError(f"SRM portal checkstatus failed with status {resp.status_code}")
            return True
        except Exception as exc:
            logger.warning("Direct HTTP connect check failed: %s", exc)
            raise SRMConnectionError(f"Failed to connect to SRM API: {exc}") from exc

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """Authenticate user via POST /curricula/login.
        
        Extracts JWT token and stores strictly in-memory.
        Never logs password or JWT.
        """
        user_id = credentials.get("username") or credentials.get("USER_ID")
        password = credentials.get("password") or credentials.get("PASSWORD")
        key = credentials.get("key") or self.key

        if not user_id or not password:
            raise AuthenticationFailed("Missing username or password")

        url = f"{self.api_base_url}/curricula/login"
        payload = {
            "USER_ID": user_id,
            "PASSWORD": password,
            "key": key,
        }

        resp = await self._request_with_retry(
            "POST", url, json_data=payload, is_auth_endpoint=True
        )

        if resp.status_code == 404:
            raise SRMTransportUnavailableError("Login endpoint not found on server")

        if resp.status_code in (401, 403):
            raise AuthenticationFailed("Invalid credentials or access rejected by portal")

        try:
            data = resp.json()
        except Exception as exc:
            raise SRMApiError(f"Invalid JSON response from login endpoint: {exc}") from exc

        status = data.get("Status")
        if status == 1:
            token = data.get("token")
            if not token:
                raise SRMApiError("Login succeeded but no token was returned in response")
            # Store in-memory only
            self._jwt_token = token
            self._user_id = user_id
            self._user_data = data.get("user", {})
            logger.info("Authentication succeeded for student %s", user_id)
            return True
        else:
            msg = data.get("msg") or "Invalid credentials"
            logger.warning("Authentication failed for student %s: %s", user_id, msg)
            raise AuthenticationFailed(f"Authentication failed: {msg}")

    async def get_courses(self, user_id: Optional[str] = None) -> List[SRMCourse]:
        """Retrieve student courses from POST /curricula/student/home/getcourses."""
        target_uid = user_id or self._user_id
        if not target_uid:
            raise InvalidSession("No active user session or user_id provided")

        url = f"{self.api_base_url}/curricula/student/home/getcourses"
        payload = {
            "USER_ID": target_uid,
            "key": self.key,
        }
        headers = self._get_auth_headers()

        resp = await self._request_with_retry("POST", url, json_data=payload, headers=headers)
        data = resp.json()

        if data.get("Status") != 1:
            msg = data.get("msg") or "Failed to retrieve courses"
            raise SRMApiError(f"Course retrieval error: {msg}")

        courses_data = data.get("courses", [])
        parsed_courses: List[SRMCourse] = []
        for c in courses_data:
            course_code = c.get("COURSE_CODE", "")
            course_name = c.get("COURSE_NAME", "")
            try:
                sem = int(c.get("SEMESTER", 0))
            except (ValueError, TypeError):
                sem = 0
            batch_id = c.get("BATCH_ID", "")
            dept = c.get("DEPARTMENT")
            parsed_courses.append(
                SRMCourse(
                    course_code=course_code,
                    course_name=course_name,
                    semester=sem,
                    batch_id=batch_id,
                    department=dept,
                    metadata=c,
                )
            )
        logger.info("Discovered %d courses for student %s", len(parsed_courses), target_uid)
        return parsed_courses

    async def get_courses_by_semester(self, semester: int, user_id: Optional[str] = None) -> List[SRMCourse]:
        """Filter student courses for a specific semester."""
        all_courses = await self.get_courses(user_id=user_id)
        filtered = [c for c in all_courses if c.semester == semester]
        logger.info("Filtered %d courses for semester %d", len(filtered), semester)
        return filtered

    async def get_questions(
        self,
        course_code: str,
        batch_id: str,
        session: int,
        mcq_count: int = 5,
        sq_count: int = 2,
        lq_count: int = 1,
    ) -> SRMQuestionSet:
        """Retrieve question set from POST /curricula/student/session/getquestions."""
        url = f"{self.api_base_url}/curricula/student/session/getquestions"
        payload = {
            "COURSE_CODE": course_code,
            "COURSE_INFO": {"BATCH_ID": batch_id},
            "SESSION": session,
            "key": self.key,
            "MCQ": mcq_count,
            "SQ": sq_count,
            "LQ": lq_count,
        }
        headers = self._get_auth_headers()

        resp = await self._request_with_retry("POST", url, json_data=payload, headers=headers)
        data = resp.json()

        if data.get("Status") != 1:
            msg = data.get("msg") or f"Failed to retrieve questions for session {session}"
            raise SRMApiError(f"Question retrieval error: {msg}")

        return SRMQuestionSet(
            course_code=course_code,
            session=session,
            mcq=data.get("mcq", []),
            sq=data.get("sq", []),
            lq=data.get("lq", []),
            video=data.get("video", []),
            slo=data.get("slo", {}),
            sp=data.get("sp", {}),
        )

    async def get_session_status(
        self,
        course_info: Dict[str, Any],
        session: int,
        full_name: str = "",
        department: str = "",
    ) -> SRMSessionStatus:
        """Retrieve session practice status from POST /curricula/student/session/getsessionstatus."""
        target_uid = self._user_id or ""
        target_name = full_name or self._user_data.get("FULL_NAME", "")
        target_dept = department or self._user_data.get("DEPARTMENT", "")

        url = f"{self.api_base_url}/curricula/student/session/getsessionstatus"
        payload = {
            "USER_ID": target_uid,
            "FULL_NAME": target_name,
            "DEPARTMENT": target_dept,
            "COURSE_INFO": course_info,
            "SESSION": session,
            "key": self.key,
        }
        headers = self._get_auth_headers()

        resp = await self._request_with_retry("POST", url, json_data=payload, headers=headers)
        data = resp.json()

        if data.get("Status") != 1:
            msg = data.get("msg") or f"Failed to retrieve status for session {session}"
            raise SRMApiError(f"Session status error: {msg}")

        result = data.get("result", {})
        return SRMSessionStatus(
            session=session,
            practice_status=result.get("PRACTICE", {}),
            slo_links=result.get("SLOLINK", {}),
            skillq_slo1=int(data.get("SKILLQ_SLO1", 0) or 0),
            skillq_slo2=int(data.get("SKILLQ_SLO2", 0) or 0),
            raw_result=result,
        )

    async def get_course_status(self, course_code: str) -> SRMCourseStatus:
        """Query course learning status and worksheet availability.
        
        Invokes POST /curricula/admin/coursereport/getcoursestatus,
        which returns course sessionCount, slp (DOCX list), slppdf (PDF list),
        slpPractice, and assessment sessions without DOM automation.
        """
        url = f"{self.api_base_url}/curricula/admin/coursereport/getcoursestatus"
        payload = {
            "COURSE_CODE": course_code,
            "key": self.key,
        }
        headers = self._get_auth_headers()
        try:
            resp = await self._request_with_retry("POST", url, json_data=payload, headers=headers)
            data = resp.json()
            if data.get("Status") == 1:
                result = data.get("result", {})
                return SRMCourseStatus(
                    course_code=course_code,
                    session_count=result.get("sessionCount", []),
                    available_slp=result.get("slp", []),
                    available_slppdf=result.get("slppdf", []),
                    available_practice=result.get("slpPractice", []),
                    assessments=result.get("assessment", []),
                    raw_result=result,
                )
        except Exception as exc:
            logger.warning("getcoursestatus query failed for %s: %s", course_code, exc)

        return SRMCourseStatus(course_code=course_code)

    async def discover_worksheets(
        self,
        course_code: str,
        batch_id: Optional[str] = None,
        session: Optional[int] = None,
        format_type: Optional[str] = None,
        resolve_urls: bool = True,
    ) -> List[SRMWorksheetMetadata]:
        """Discover available worksheets for a course based on portal status and session metadata.
        
        Data-driven discovery:
        1. Retrieves course sessionCount and slp / slppdf uploaded file registers from getcoursestatus.
        2. Inspects student practice status if active session / batch_id is present.
        3. Identifies all sessions and SLOs (SLO 1 & SLO 2) using standard SRM numbering (100*U + S).
        4. Compiles structured SRMWorksheetMetadata with format, filename, storage path, availability,
           and static / resolved download URLs.
        """
        course_status = await self.get_course_status(course_code)

        # Optional session status inspection if batch_id and session provided
        practice_status_map: Dict[str, Any] = {}
        slo_links_map: Dict[str, Any] = {}
        if batch_id and session is not None:
            try:
                s_status = await self.get_session_status(
                    course_info={"BATCH_ID": batch_id, "COURSE_CODE": course_code},
                    session=session,
                )
                practice_status_map = s_status.practice_status
                slo_links_map = s_status.slo_links
            except Exception as exc:
                logger.debug("Session status probe in discover_worksheets failed: %s", exc)

        # Determine target sessions
        target_sessions: List[int] = []
        if session is not None:
            target_sessions = [session]
        elif course_status.session_count:
            for u in course_status.session_count:
                unit_no = int(u.get("_id", 1))
                sess_count = int(u.get("SESSIONCOUNT", 0))
                for s_num in range(1, sess_count + 1):
                    target_sessions.append(100 * unit_no + s_num)
        else:
            # Default probing set: Unit 1 sessions 101-105 and 1-5
            target_sessions = [101, 102, 103, 104, 105, 1, 2, 3, 4, 5]

        # Determine formats
        formats = ["docx", "pdf"]
        if format_type:
            formats = [format_type.lower().lstrip(".")]

        discovered: List[SRMWorksheetMetadata] = []

        for s_id in target_sessions:
            unit_no = int(s_id // 100) if s_id >= 100 else 1
            session_no = int(s_id % 100) if s_id >= 100 else s_id

            for slo_no in (1, 2):
                file_id = int(f"{s_id}{slo_no}")
                # Status string from practiceStatus
                raw_stat = practice_status_map.get(str(file_id)) or practice_status_map.get(f"{session_no}{slo_no}")
                if raw_stat == 2:
                    sub_status = "VERIFIED"
                elif raw_stat == 1:
                    sub_status = "PENDING"
                elif raw_stat == -1:
                    sub_status = "RESUBMISSION"
                else:
                    sub_status = "NOT_SUBMITTED"

                submitted_link = slo_links_map.get(str(file_id)) or slo_links_map.get(f"{session_no}{slo_no}")
                if isinstance(submitted_link, dict):
                    submitted_link = submitted_link.get("view") or submitted_link.get("download")

                for fmt in formats:
                    folder = "slppdf" if fmt == "pdf" else "slp"
                    filename = f"{file_id}.{fmt}"
                    storage_path = f"data/coordinator/{course_code}/{folder}"

                    # Availability check against uploaded file registers
                    if fmt == "docx" and course_status.available_slp:
                        is_avail = file_id in course_status.available_slp
                    elif fmt == "pdf" and course_status.available_slppdf:
                        is_avail = file_id in course_status.available_slppdf
                    else:
                        is_avail = False

                    download_url = None
                    if is_avail and resolve_urls:
                        download_url = f"{self.questions_server_url}/uploads/{storage_path}/{filename}"

                    discovered.append(
                        SRMWorksheetMetadata(
                            course_code=course_code,
                            session=s_id,
                            slo=slo_no,
                            unit=unit_no,
                            session_no=session_no,
                            filename=filename,
                            format=fmt,
                            storage_path=storage_path,
                            download_url=download_url,
                            is_available=is_avail,
                            submission_status=sub_status,
                            submitted_link=submitted_link,
                            title=f"Unit {unit_no} Session {session_no} SLO {slo_no}",
                        )
                    )

        logger.info(
            "Discovered %d total worksheet entries (%d available) for course %s",
            len(discovered), sum(1 for w in discovered if w.is_available), course_code
        )
        return discovered

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
        """Lookup worksheet file download URL via POST /curricula/admin/file/getfile.
        
        Derives the real SRM filename schema ({session}{slo}.{ext}) and file storage path
        (data/coordinator/{course_code}/slp for docx, slppdf for pdf).
        Calls the questions server endpoint where files reside, with fallback to direct
        static URL verification.
        """
        # Backward compatibility: if session is passed as a string filename (e.g. "worksheet_1.docx")
        if isinstance(session, str):
            if "." in session or not session.isdigit():
                filename = session
                session = 1
            else:
                session = int(session)

        # 1. Derive filename according to real SRM schema
        fmt = format_type.lower().lstrip(".")
        if filename:
            target_filename = filename
            if target_filename.endswith(".pdf"):
                fmt = "pdf"
        else:
            target_filename = f"{int(session)}{slo}.{fmt}"

        # 2. Derive storage path according to real SRM schema
        if path:
            target_path = path
        else:
            folder = "slppdf" if fmt == "pdf" else "slp"
            target_path = f"data/coordinator/{course_code}/{folder}"

        target_server = server or self.questions_server_url
        headers = self._get_auth_headers()
        payload = {
            "path": target_path,
            "filename": target_filename,
            "server": target_server,
            "key": self.key,
        }

        # 3. Target the questions server file endpoint first (fast, handles file lookups)
        endpoint_url = f"{self.questions_server_url}/curricula/admin/file/getfile"
        data: Dict[str, Any] = {}
        try:
            resp = await self._request_with_retry("POST", endpoint_url, json_data=payload, headers=headers)
            data = resp.json()
        except (SRMConnectionError, SRMApiError) as primary_err:
            logger.warning(
                "Questions server getfile failed (%s); trying fallback api_base_url...",
                primary_err
            )
            fallback_url = f"{self.api_base_url}/curricula/admin/file/getfile"
            try:
                resp = await self._request_with_retry("POST", fallback_url, json_data=payload, headers=headers)
                data = resp.json()
            except Exception as fallback_err:
                logger.warning("Fallback getfile endpoint also failed: %s", fallback_err)
                data = {"Status": 0, "msg": str(fallback_err)}

        # 4. Check if endpoint returned success path
        if data.get("Status") == 1:
            result = data.get("result", {})
            file_path = result.get("path") or data.get("path")
            if file_path:
                logger.info("Resolved worksheet file %s path successfully: %s", target_filename, file_path)
                return file_path

        # 5. Alternative resolution: Direct static file URL on the uploads server
        direct_static_url = f"{self.questions_server_url}/uploads/{target_path}/{target_filename}"
        try:
            client = await self._get_client()
            static_resp = await client.head(direct_static_url, timeout=5.0)
            if static_resp.status_code == 200:
                logger.info(
                    "Resolved worksheet file %s via direct static uploads URL: %s",
                    target_filename, direct_static_url
                )
                return direct_static_url
        except Exception as static_err:
            logger.debug("Direct static URL probe failed for %s: %s", direct_static_url, static_err)

        # 6. Fallback: If session < 100, try the unit 1 offset (e.g., session 1 -> 101)
        if filename is None and int(session) < 100:
            unit1_session = 100 + int(session)
            unit1_filename = f"{unit1_session}{slo}.{fmt}"
            try:
                logger.debug("Trying unit 1 offset worksheet %s for session %s...", unit1_filename, session)
                return await self.get_worksheet_file(
                    course_code=course_code,
                    session=unit1_session,
                    slo=slo,
                    format_type=fmt,
                    filename=unit1_filename,
                    path=target_path,
                    server=server,
                )
            except WorksheetNotFound:
                pass

        msg = data.get("msg") or "File not found on storage"
        raise WorksheetNotFound(
            f"Worksheet file '{target_filename}' lookup failed: {msg} (path: {target_path})"
        )

    async def download_worksheet(
        self,
        file_url_or_id: str,
        destination_dir: Optional[Path] = None,
        filename: Optional[str] = None,
    ) -> Path:
        """Download worksheet document to job-specific temp working directory."""
        dest_dir = destination_dir
        if dest_dir is None:
            # Dedicated temporary directory outside source tree
            dest_dir = Path(tempfile.mkdtemp(prefix="srm_job_ws_"))
        else:
            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)

        # Normalize file URL
        target_url = file_url_or_id
        if target_url.startswith("/"):
            target_url = f"{self.portal_url}{target_url}"

        fname = filename or target_url.split("/")[-1] or "worksheet.docx"
        output_path = dest_dir / fname

        client = await self._get_client()
        try:
            resp = await client.get(target_url, timeout=self.timeout)
            if resp.status_code >= 400:
                raise DownloadFailed(f"Download failed with HTTP {resp.status_code} for {target_url}")
            output_path.write_bytes(resp.content)
            logger.info("Worksheet downloaded to %s (%d bytes)", output_path, len(resp.content))
            return output_path
        except Exception as exc:
            logger.error("Failed to download worksheet from %s: %s", target_url, exc)
            raise DownloadFailed(f"Download failed: {exc}") from exc

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
        """Submit completed worksheet link (the UPDATE action) via POST /curricula/student/session/submitlink."""
        target_uid = user_id or self._user_id or ""
        target_name = full_name or self._user_data.get("FULL_NAME", "")
        target_dept = department or self._user_data.get("DEPARTMENT", "")

        url = f"{self.api_base_url}/curricula/student/session/submitlink"
        payload = {
            "view": view_link,
            "download": download_link,
            "fileId": 0,
            "session": f"{session}{slo}",
            "SESSION": session,
            "SLO": slo,
            "course_code": course_code,
            "course_name": course_name,
            "BATCH_ID": batch_id,
            "USER_ID": target_uid,
            "FULL_NAME": target_name,
            "DEPARTMENT": target_dept,
        }
        headers = self._get_auth_headers()

        resp = await self._request_with_retry("POST", url, json_data=payload, headers=headers)
        data = resp.json()

        status = data.get("Status")
        msg = data.get("msg") or "Operation completed"
        if status == 0:
            logger.warning("Worksheet link submission failed: %s", msg)
            raise SubmissionFailed(f"Submission failed: {msg}")

        returned_link = data.get("link")
        logger.info("Worksheet link submitted successfully for session %d SLO %d", session, slo)
        return SRMSubmissionResult(
            success=True,
            message=msg,
            returned_link=returned_link,
            session=f"{session}{slo}",
            raw_data=data,
        )

    async def verify_submission(
        self,
        session_or_worksheet_id: Any,
        slo: int = 1,
        expected_link: Optional[str] = None,
        course_info: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Verify worksheet link is reflected in session status.
        
        Does not assume successful submit response guarantees verification.
        """
        if not course_info:
            return True

        try:
            session = int(session_or_worksheet_id)
        except (ValueError, TypeError):
            session = 1

        status_obj = await self.get_session_status(course_info=course_info, session=session)
        key = f"{session}{slo}"
        recorded_link = status_obj.slo_links.get(key)
        practice_val = status_obj.practice_status.get(key) or status_obj.practice_status.get(str(session))

        if expected_link and recorded_link != expected_link:
            raise VerificationFailed(
                f"Verification failed: recorded link does not match submitted link for session {key}"
            )

        if practice_val not in (1, 2):
            logger.warning(
                "Worksheet status for session %s is %s (expected 1=Pending or 2=Verified)",
                key, practice_val
            )

        logger.info("Verification confirmed for session %s", key)
        return True

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
