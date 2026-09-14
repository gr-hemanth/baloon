from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, List, Dict, Any
from packages.srm.models import (
    SRMCourse,
    SRMSemester,
    SRMSubject,
    SRMWorksheet,
    SRMSubmissionReceipt,
    SRMQuestionSet,
    SRMSessionStatus,
    SRMSubmissionResult,
)


class SRMClient(ABC):
    """Abstract interface for SRM eCurricula portal operations.
    
    Both direct HTTP and Playwright headless browser transports
    implement this contract.
    """

    @property
    @abstractmethod
    def transport_name(self) -> str:
        """Name of the transport (e.g., 'http', 'browser')."""
        pass

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection / session with the SRM portal."""
        pass

    @abstractmethod
    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """Authenticate user with portal credentials.
        
        Raises CaptchaRequired if a CAPTCHA challenge is encountered.
        Raises AuthenticationFailed on invalid credentials.
        """
        pass

    @abstractmethod
    async def get_courses(self) -> List[SRMCourse]:
        """Discover enrolled courses for the authenticated user."""
        pass

    @abstractmethod
    async def get_courses_by_semester(self, semester: int) -> List[SRMCourse]:
        """Filter courses for a specific semester."""
        pass

    @abstractmethod
    async def get_questions(
        self,
        course_code: str,
        batch_id: str,
        session: int,
        mcq_count: int = 5,
        sq_count: int = 2,
        lq_count: int = 1,
    ) -> SRMQuestionSet:
        """Retrieve question paper components for a course session."""
        pass

    @abstractmethod
    async def get_session_status(
        self,
        course_info: Dict[str, Any],
        session: int,
        full_name: str = "",
        department: str = "",
    ) -> SRMSessionStatus:
        """Retrieve student practice status and submission links for a session."""
        pass

    @abstractmethod
    async def get_worksheet_file(
        self,
        course_code: str,
        filename: str,
        path: Optional[str] = None,
        server: Optional[str] = None,
    ) -> str:
        """Resolve worksheet file storage path to download URL."""
        pass

    @abstractmethod
    async def download_worksheet(
        self,
        file_url_or_id: str,
        destination_dir: Optional[Path] = None,
        filename: Optional[str] = None,
    ) -> Path:
        """Download worksheet document to local destination directory.
        
        Returns path to the downloaded file.
        """
        pass

    @abstractmethod
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
        """Submit completed worksheet link (e.g. Google Drive link) to portal."""
        pass

    @abstractmethod
    async def verify_submission(
        self,
        session_or_worksheet_id: Any,
        slo: int = 1,
        expected_link: Optional[str] = None,
        course_info: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Verify that a submitted worksheet has been registered by the portal."""
        pass

    # Compatibility methods
    async def discover_courses(self) -> List[SRMCourse]:
        return await self.get_courses()

    async def select_semester(self, semester_id: str) -> bool:
        return True

    async def select_subject(self, subject_id: str) -> bool:
        return True

    async def discover_worksheets(self, subject_id: Optional[str] = None) -> List[SRMWorksheet]:
        return []

    async def submit_worksheet(
        self,
        worksheet_id: str,
        file_path: Path,
        comments: Optional[str] = None,
    ) -> SRMSubmissionReceipt:
        return SRMSubmissionReceipt(
            worksheet_id=worksheet_id,
            submitted_at=httpx._utils.now() if hasattr(httpx, "_utils") else datetime.now(timezone.utc),
            verification_status="PENDING_VERIFICATION"
        )

    @abstractmethod
    async def close(self) -> None:
        """Clean up network sessions or browser contexts."""
        pass

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
