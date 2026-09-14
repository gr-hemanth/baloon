"""SRM Integration Package.

Provides client interfaces, HTTP transport, Playwright browser transport,
orchestration fallback, and discovery tooling.
"""

from packages.srm.client import SRMClient
from packages.srm.http_client import SRMHttpClient
from packages.srm.browser_client import SRMBrowserClient
from packages.srm.orchestrator import SRMOrchestrator
from packages.srm.exceptions import (
    SRMException,
    SRMConnectionError,
    SRMAuthenticationError,
    SRMCaptchaRequired,
    SRMTransportUnavailableError,
    SRMWorksheetNotFoundError,
    SRMSubmissionError,
)
from packages.srm.models import (
    SRMCourse,
    SRMSemester,
    SRMSubject,
    SRMWorksheet,
    SRMSubmissionReceipt,
)

__all__ = [
    "SRMClient",
    "SRMHttpClient",
    "SRMBrowserClient",
    "SRMOrchestrator",
    "SRMException",
    "SRMConnectionError",
    "SRMAuthenticationError",
    "SRMCaptchaRequired",
    "SRMTransportUnavailableError",
    "SRMWorksheetNotFoundError",
    "SRMSubmissionError",
    "SRMCourse",
    "SRMSemester",
    "SRMSubject",
    "SRMWorksheet",
    "SRMSubmissionReceipt",
]
