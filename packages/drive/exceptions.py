"""Exception hierarchy for Google Drive operations."""

from typing import Any, Dict, Optional


class DriveException(Exception):
    """Base exception for all Drive integration errors."""
    pass


class DriveAuthenticationError(DriveException):
    """Raised when OAuth 2.0 exchange, token refresh, or authorization fails."""
    pass


class DriveTokenExpiredError(DriveAuthenticationError):
    """Raised when access token is expired and refresh token is unavailable or invalid."""
    pass


class DriveUploadError(DriveException):
    """Raised when uploading a worksheet document to Drive fails."""
    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response or {}


class DrivePermissionError(DriveException):
    """Raised when configuring 'anyone with link' reader permissions fails."""
    pass


class DriveVerificationError(DriveException):
    """Raised when verifying public shareable access on an uploaded file fails."""
    pass


class DriveFileNotFoundError(DriveException):
    """Raised when target file does not exist locally or in Drive."""
    pass
