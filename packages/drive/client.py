from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Dict, Any


class GoogleDriveClient(ABC):
    """Abstract interface for Google Drive OAuth 2.0 and file storage.
    
    NOTE: Google Drive integration is explicitly deferred to later iterations.
    """

    @abstractmethod
    def authenticate(self, auth_code: str) -> Dict[str, Any]:
        """Exchange OAuth authorization code for user access and refresh tokens."""
        raise NotImplementedError("Google Drive OAuth integration not implemented yet")

    @abstractmethod
    def upload_file(self, file_path: Path, folder_id: Optional[str] = None) -> str:
        """Upload a worksheet artifact to Google Drive and return file ID."""
        raise NotImplementedError("Google Drive upload not implemented yet")

    @abstractmethod
    def download_file(self, file_id: str, destination_path: Path) -> Path:
        """Download file from Google Drive."""
        raise NotImplementedError("Google Drive download not implemented yet")
