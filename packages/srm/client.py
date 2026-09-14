from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, List, Dict, Any
from packages.srm.models import (
    SRMCourse,
    SRMSemester,
    SRMSubject,
    SRMWorksheet,
    SRMSubmissionReceipt,
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
        
        Raises SRMCaptchaRequired if a CAPTCHA challenge is encountered.
        Raises SRMAuthenticationError on invalid credentials.
        """
        pass

    @abstractmethod
    async def discover_courses(self) -> List[SRMCourse]:
        """Discover enrolled courses for the authenticated user."""
        pass

    @abstractmethod
    async def select_semester(self, semester_id: str) -> bool:
        """Select an active semester."""
        pass

    @abstractmethod
    async def select_subject(self, subject_id: str) -> bool:
        """Select an active subject within the semester."""
        pass

    @abstractmethod
    async def discover_worksheets(self, subject_id: Optional[str] = None) -> List[SRMWorksheet]:
        """Discover available worksheets for the current or specified subject."""
        pass

    @abstractmethod
    async def download_worksheet(
        self,
        worksheet_id: str,
        destination_dir: Optional[Path] = None
    ) -> Path:
        """Download worksheet document to local destination directory.
        
        Returns path to the downloaded file.
        """
        pass

    @abstractmethod
    async def submit_worksheet(
        self,
        worksheet_id: str,
        file_path: Path,
        comments: Optional[str] = None
    ) -> SRMSubmissionReceipt:
        """Submit completed worksheet file to the portal."""
        pass

    @abstractmethod
    async def verify_submission(self, worksheet_id: str) -> bool:
        """Verify that a submitted worksheet has been registered by the portal."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Clean up network sessions or browser contexts."""
        pass

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
