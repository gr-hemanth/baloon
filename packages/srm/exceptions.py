from typing import Optional, Dict, Any


class SRMException(Exception):
    """Base exception for all SRM integration errors."""
    pass


class SRMConnectionError(SRMException):
    """Raised when unable to reach the SRM portal."""
    pass


class SRMAuthenticationError(SRMException):
    """Raised when authentication credentials are invalid or rejected."""
    pass


class AuthenticationFailed(SRMAuthenticationError):
    """Specific exception for invalid credentials or authentication failure."""
    pass


class SRMCaptchaRequired(SRMException):
    """Raised when SRM requires a CAPTCHA solve.
    
    The job must enter WAITING_FOR_CAPTCHA state so the user can solve it via UI.
    Do NOT attempt automated solving.
    """
    def __init__(
        self,
        message: str = "CAPTCHA required by SRM portal",
        challenge_type: str = "image",
        challenge_data: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.challenge_type = challenge_type
        self.challenge_data = challenge_data or {}


class CaptchaRequired(SRMCaptchaRequired):
    """Alias for SRMCaptchaRequired."""
    pass


class InvalidSession(SRMException):
    """Raised when an existing session is expired or token has been invalidated."""
    pass


class Unauthorized(SRMException):
    """Raised when access to an SRM resource is unauthorized (HTTP 401/403)."""
    pass


class SRMApiError(SRMException):
    """Raised when an SRM API returns Status == 0 or an application-level error."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data or {}


class SRMTransportUnavailableError(SRMException):
    """Raised when direct HTTP transport cannot fulfill an operation
    and fallback to browser automation is needed.
    """
    pass


class SRMWorksheetNotFoundError(SRMException):
    """Raised when a requested worksheet is not found on the portal."""
    pass


class WorksheetNotFound(SRMWorksheetNotFoundError):
    """Alias for SRMWorksheetNotFoundError."""
    pass


class DownloadFailed(SRMException):
    """Raised when downloading a worksheet file fails."""
    pass


class SRMSubmissionError(SRMException):
    """Raised when worksheet upload or submission fails."""
    pass


class SubmissionFailed(SRMSubmissionError):
    """Specific exception when worksheet link submission fails."""
    pass


class VerificationFailed(SRMException):
    """Raised when verifying a submitted worksheet fails to confirm on the portal."""
    pass
