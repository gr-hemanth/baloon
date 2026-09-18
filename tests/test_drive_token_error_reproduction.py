"""Test reproducing the exact Drive token refresh 400 error and redaction behavior."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from packages.drive.client import GoogleDriveClient
from packages.drive.exceptions import DriveTokenExpiredError
from apps.worker.tasks import redact_sensitive_info


@pytest.fixture
def drive_client() -> GoogleDriveClient:
    return GoogleDriveClient(
        client_id="mock-client-id.apps.googleusercontent.com",
        client_secret="mock-client-secret",
        redirect_uri="http://localhost:8000/callback",
        access_token="initial_mock_access_token",
        refresh_token="initial_mock_refresh_token",
    )


@pytest.mark.asyncio
async def test_drive_token_refresh_400_invalid_grant_reproduction(drive_client: GoogleDriveClient):
    """Reproduce exact Google OAuth token refresh 400 invalid_grant failure.
    
    When Google's OAuth token endpoint returns HTTP 400 Bad Request with:
        {"error": "invalid_grant", "error_description": "Bad Request"}
    GoogleDriveClient raises DriveTokenExpiredError:
        "Token refresh failed (400): Bad Request"
    And worker's redact_sensitive_info converts this to:
        "Token [REDACTED] failed (400): Bad Request"
    """
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 400
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "error": "invalid_grant",
        "error_description": "Bad Request",
    }

    with patch.object(drive_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_http

        with pytest.raises(DriveTokenExpiredError) as exc_info:
            await drive_client.refresh_access_token()

        raw_error_message = str(exc_info.value)
        assert raw_error_message == "Token refresh failed (400): Bad Request"

        # Verify that redact_sensitive_info preserves the natural error message without corrupting "refresh"
        redacted_error_message = redact_sensitive_info(raw_error_message)
        assert redacted_error_message == "Token refresh failed (400): Bad Request"
