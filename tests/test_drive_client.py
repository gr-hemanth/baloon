"""Unit tests for Google Drive client, OAuth 2.0 authorization, upload, and sharing."""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from packages.drive.client import GoogleDriveClient
from packages.drive.exceptions import (
    DriveAuthenticationError,
    DriveFileNotFoundError,
    DrivePermissionError,
    DriveTokenExpiredError,
    DriveUploadError,
    DriveVerificationError,
)
from packages.drive.models import DriveFileMetadata, OAuthTokens


@pytest.fixture
def drive_client() -> GoogleDriveClient:
    """Fixture providing initialized GoogleDriveClient with mock credentials."""
    return GoogleDriveClient(
        client_id="mock-client-id.apps.googleusercontent.com",
        client_secret="mock-client-secret",
        redirect_uri="http://localhost:8000/callback",
        access_token="initial_mock_access_token",
        refresh_token="initial_mock_refresh_token",
    )


def test_authorization_url_generation(drive_client: GoogleDriveClient):
    """Verify Google OAuth 2.0 authorization URL contains required parameters."""
    url = drive_client.get_authorization_url(state="test_state_123")
    assert "https://accounts.google.com/o/oauth2/v2/auth?" in url
    assert "?response_type=code&" in url
    assert "client_id=mock-client-id.apps.googleusercontent.com" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fcallback" in url
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.file" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=test_state_123" in url

    # Verify auto-generated state when omitted
    auto_url = drive_client.get_authorization_url()
    assert "?response_type=code&" in auto_url
    assert "state=" in auto_url
    assert drive_client.state is not None


def test_authorization_url_missing_client_id():
    """Verify error raised when client_id is absent."""
    client = GoogleDriveClient(client_id=None)
    with pytest.raises(DriveAuthenticationError):
        client.get_authorization_url()


@pytest.mark.asyncio
async def test_oauth_code_exchange_success(drive_client: GoogleDriveClient):
    """Verify authorization code exchange yields OAuthTokens."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "access_token": "new_access_token_xyz",
        "refresh_token": "new_refresh_token_abc",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "https://www.googleapis.com/auth/drive.file",
    }

    with patch.object(drive_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_http

        tokens = await drive_client.exchange_code("mock_auth_code_123")
        assert tokens.access_token == "new_access_token_xyz"
        assert tokens.refresh_token == "new_refresh_token_abc"
        assert tokens.expires_in == 3600
        assert tokens.is_expired() is False


@pytest.mark.asyncio
async def test_oauth_code_exchange_failure(drive_client: GoogleDriveClient):
    """Verify OAuth code exchange error handling on invalid grant."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 400
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "error": "invalid_grant",
        "error_description": "Code has expired or been revoked.",
    }

    with patch.object(drive_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_http

        with pytest.raises(DriveAuthenticationError) as exc_info:
            await drive_client.exchange_code("expired_code")
        assert "invalid_grant" in str(exc_info.value)


@pytest.mark.asyncio
async def test_token_refresh_success(drive_client: GoogleDriveClient):
    """Verify token refresh successfully updates access token."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "access_token": "refreshed_access_token",
        "expires_in": 3600,
    }

    with patch.object(drive_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_http

        tokens = await drive_client.refresh_access_token()
        assert tokens.access_token == "refreshed_access_token"
        assert tokens.refresh_token == "initial_mock_refresh_token"


@pytest.mark.asyncio
async def test_completed_worksheet_upload_and_sharing_success(drive_client: GoogleDriveClient, tmp_path: Path):
    """Verify complete upload -> set public permission -> verify -> get metadata flow."""
    # Create test completed file
    completed_file = tmp_path / "completed_21CSC303J_1011.docx"
    completed_file.write_bytes(b"PK\x03\x04" + b"\x00" * 200)

    # 1. Mock upload response
    mock_upload_resp = MagicMock(spec=httpx.Response)
    mock_upload_resp.status_code = 200
    mock_upload_resp.json.return_value = {
        "id": "drive_file_id_999",
        "name": "completed_21CSC303J_1011.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    # 2. Mock permission creation response
    mock_perm_resp = MagicMock(spec=httpx.Response)
    mock_perm_resp.status_code = 200
    mock_perm_resp.json.return_value = {
        "id": "anyoneWithLink",
        "type": "anyone",
        "role": "reader",
    }

    # 3. Mock permission verification response
    mock_verify_resp = MagicMock(spec=httpx.Response)
    mock_verify_resp.status_code = 200
    mock_verify_resp.json.return_value = {
        "permissions": [
            {"id": "anyoneWithLink", "type": "anyone", "role": "reader"}
        ]
    }

    # 4. Mock metadata retrieval response
    mock_meta_resp = MagicMock(spec=httpx.Response)
    mock_meta_resp.status_code = 200
    mock_meta_resp.json.return_value = {
        "id": "drive_file_id_999",
        "name": "completed_21CSC303J_1011.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "webViewLink": "https://drive.google.com/file/d/drive_file_id_999/view?usp=drivesdk",
        "webContentLink": "https://drive.google.com/uc?id=drive_file_id_999&export=download",
        "size": "204",
        "createdTime": "2026-09-15T01:30:00.000Z",
    }

    with patch.object(drive_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_http = MagicMock(spec=httpx.AsyncClient)
        # Sequence of calls: post(upload), post(permission), get(verify), get(metadata)
        mock_http.post = AsyncMock(side_effect=[mock_upload_resp, mock_perm_resp])
        mock_http.get = AsyncMock(side_effect=[mock_verify_resp, mock_meta_resp])
        mock_get_client.return_value = mock_http

        metadata = await drive_client.upload_file(completed_file)

        assert metadata.file_id == "drive_file_id_999"
        assert metadata.filename == "completed_21CSC303J_1011.docx"
        assert metadata.web_url == "https://drive.google.com/file/d/drive_file_id_999/view?usp=drivesdk"
        assert metadata.download_url == "https://drive.google.com/uc?id=drive_file_id_999&export=download"
        assert metadata.is_public is True
        assert metadata.permission_status == "VERIFIED_PUBLIC_READER"


@pytest.mark.asyncio
async def test_original_worksheet_safety_guard(drive_client: GoogleDriveClient, tmp_path: Path):
    """Verify safety check rejects uploading original/uncompleted worksheets by default."""
    raw_worksheet = tmp_path / "1011.docx"
    raw_worksheet.write_bytes(b"dummy")

    with pytest.raises(ValueError) as exc_info:
        await drive_client.upload_file(raw_worksheet)
    assert "Safety violation" in str(exc_info.value)
    assert "completed worksheet" in str(exc_info.value)


@pytest.mark.asyncio
async def test_upload_failure_handling(drive_client: GoogleDriveClient, tmp_path: Path):
    """Verify DriveUploadError is raised when Drive upload returns 500."""
    completed_file = tmp_path / "completed_test.docx"
    completed_file.write_bytes(b"dummy")

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 500
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "error": {"message": "Backend storage unavailable", "code": 500}
    }

    with patch.object(drive_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_http

        with pytest.raises(DriveUploadError) as exc_info:
            await drive_client.upload_file(completed_file)
        assert exc_info.value.status_code == 500
        assert "Backend storage unavailable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_permission_failure_handling(drive_client: GoogleDriveClient, tmp_path: Path):
    """Verify DrivePermissionError is raised when setting permissions fails."""
    completed_file = tmp_path / "completed_test.docx"
    completed_file.write_bytes(b"dummy")

    mock_upload_resp = MagicMock(spec=httpx.Response)
    mock_upload_resp.status_code = 200
    mock_upload_resp.json.return_value = {"id": "file_123", "name": "completed_test.docx"}

    mock_perm_resp = MagicMock(spec=httpx.Response)
    mock_perm_resp.status_code = 403
    mock_perm_resp.headers = {"content-type": "application/json"}
    mock_perm_resp.json.return_value = {
        "error": {"message": "Permission denied for this domain."}
    }

    with patch.object(drive_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(side_effect=[mock_upload_resp, mock_perm_resp])
        mock_get_client.return_value = mock_http

        with pytest.raises(DrivePermissionError) as exc_info:
            await drive_client.upload_file(completed_file)
        assert "Permission denied" in str(exc_info.value)


@pytest.mark.asyncio
async def test_permission_verification_failure(drive_client: GoogleDriveClient, tmp_path: Path):
    """Verify DriveVerificationError is raised when public permissions cannot be verified."""
    completed_file = tmp_path / "completed_test.docx"
    completed_file.write_bytes(b"dummy")

    mock_upload_resp = MagicMock(spec=httpx.Response)
    mock_upload_resp.status_code = 200
    mock_upload_resp.json.return_value = {"id": "file_123", "name": "completed_test.docx"}

    mock_perm_resp = MagicMock(spec=httpx.Response)
    mock_perm_resp.status_code = 200
    mock_perm_resp.json.return_value = {"id": "anyoneWithLink"}

    # Verification returns empty permissions list
    mock_verify_resp = MagicMock(spec=httpx.Response)
    mock_verify_resp.status_code = 200
    mock_verify_resp.json.return_value = {"permissions": []}

    with patch.object(drive_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_http = MagicMock(spec=httpx.AsyncClient)
        mock_http.post = AsyncMock(side_effect=[mock_upload_resp, mock_perm_resp])
        mock_http.get = AsyncMock(return_value=mock_verify_resp)
        mock_get_client.return_value = mock_http

        with pytest.raises(DriveVerificationError):
            await drive_client.upload_file(completed_file)


def test_sensitive_token_redaction_in_models(caplog):
    """Verify OAuth tokens are strictly redacted in string representations and logs."""
    tokens = OAuthTokens(
        access_token="SUPER_SECRET_OAUTH_ACCESS_TOKEN_XYZ",
        refresh_token="SUPER_SECRET_REFRESH_TOKEN_ABC",
    )

    # String / Repr representation must redact
    repr_str = repr(tokens)
    assert "SUPER_SECRET_OAUTH_ACCESS_TOKEN_XYZ" not in repr_str
    assert "[REDACTED]" in repr_str

    str_str = str(tokens)
    assert "SUPER_SECRET_REFRESH_TOKEN_ABC" not in str_str


@pytest.mark.asyncio
async def test_file_not_found_handling(drive_client: GoogleDriveClient, tmp_path: Path):
    """Verify DriveFileNotFoundError when local file does not exist."""
    missing = tmp_path / "completed_nonexistent.docx"
    with pytest.raises(DriveFileNotFoundError):
        await drive_client.upload_file(missing)
