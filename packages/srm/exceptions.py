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


class SRMTransportUnavailableError(SRMException):
    """Raised when direct HTTP transport cannot fulfill an operation
    and fallback to browser automation is needed.
    """
    pass


class SRMWorksheetNotFoundError(SRMException):
    """Raised when a requested worksheet is not found on the portal."""
    pass


class SRMSubmissionError(SRMException):
    """Raised when worksheet upload or submission fails."""
    pass
