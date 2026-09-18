"""Regression test suite for Google Drive OAuth token lifecycle, synchronization, and error recovery."""

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.worker.tasks import redact_sensitive_info
from packages.drive.client import (
    GoogleDriveClient,
    clear_drive_tokens_from_env,
    get_active_drive_tokens,
    save_drive_tokens_to_env,
)
from packages.drive.exceptions import (
    DriveAuthenticationError,
    DriveTokenExpiredError,
)
from packages.drive.models import OAuthTokens
from packages.shared.config import settings


@pytest.fixture
def temp_env_file(tmp_path: Path) -> Path:
    """Provide an isolated temporary .env file for testing persistence."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EXISTING_KEY=keep_this_intact\n"
        "GOOGLE_DRIVE_REFRESH_TOKEN=dummy_temp_refresh\n"
        "GOOGLE_DRIVE_ACCESS_TOKEN=dummy_temp_access\n",
        encoding="utf-8",
    )
    return env_file


def test_get_active_drive_tokens_prioritizes_env(temp_env_file: Path):
    """Ensure get_active_drive_tokens dynamically reads fresh tokens from .env file."""
    rf, at = get_active_drive_tokens(env_path=temp_env_file)
    assert rf == "dummy_temp_refresh"
    assert at == "dummy_temp_access"


def test_save_and_clear_drive_tokens_to_custom_env(temp_env_file: Path):
    """Verify save_drive_tokens_to_env updates custom env file without destroying other keys."""
    save_drive_tokens_to_env(
        refresh_token="new_active_refresh_token_12345",
        access_token="new_active_access_token_67890",
        env_path=temp_env_file,
    )
    content = temp_env_file.read_text(encoding="utf-8")
    assert "EXISTING_KEY=keep_this_intact" in content
    assert "GOOGLE_DRIVE_REFRESH_TOKEN=new_active_refresh_token_12345" in content
    assert "GOOGLE_DRIVE_ACCESS_TOKEN=new_active_access_token_67890" in content

    # Clear tokens
    clear_drive_tokens_from_env(env_path=temp_env_file)
    cleared_content = temp_env_file.read_text(encoding="utf-8")
    assert "EXISTING_KEY=keep_this_intact" in cleared_content
    assert "GOOGLE_DRIVE_REFRESH_TOKEN" not in cleared_content
    assert "GOOGLE_DRIVE_ACCESS_TOKEN" not in cleared_content


def test_pytest_isolation_protects_root_env():
    """Verify that calling save/clear without an explicit path during pytest will NEVER touch real .env."""
    assert os.environ.get("PYTEST_CURRENT_TEST") is not None
    # Even if called with fake tokens, save_drive_tokens_to_env should return early
    save_drive_tokens_to_env(refresh_token="should_never_be_written_999")
    real_env = Path(".env")
    if real_env.exists():
        assert "should_never_be_written_999" not in real_env.read_text(encoding="utf-8")


def test_redaction_preserves_error_phrases_while_scrubbing_tokens():
    """Verify redact_sensitive_info does not replace English words like 'refresh' or 'has'."""
    raw_error = "Token refresh failed (400): Token has been expired or revoked."
    redacted = redact_sensitive_info(raw_error)
    assert redacted == "Token refresh failed (400): Token has been expired or revoked."
    assert "Token [REDACTED] failed" not in redacted

    # Verify real secrets are scrubbed
    secret_text = "Bearer ya29.a0AWY7C_mock_token and 1//04_refresh_secret and password='MyPass' and token: 'tok_abc'"
    scrubbed = redact_sensitive_info(secret_text)
    assert "ya29.a0AWY7C_mock_token" not in scrubbed
    assert "1//04_refresh_secret" not in scrubbed
    assert "MyPass" not in scrubbed
    assert "tok_abc" not in scrubbed
    assert "[REDACTED]" in scrubbed


@pytest.mark.asyncio
async def test_automatic_token_refresh_on_401_upload(tmp_path: Path):
    """Verify upload_file catches 401, refreshes the token, and retries successfully."""
    client = GoogleDriveClient(
        client_id="mock-client-id",
        client_secret="mock-client-secret",
        redirect_uri="http://localhost:8000/callback",
        access_token="initial_expired_token",
        refresh_token="valid_refresh_token",
    )

    test_file = tmp_path / "completed_worksheet.docx"
    test_file.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

    # Responses:
    # 1. Initial upload -> 401 Unauthorized
    resp_401 = MagicMock(spec=httpx.Response)
    resp_401.status_code = 401
    resp_401.json.return_value = {"error": {"message": "Invalid Credentials"}}

    # 2. Token refresh -> 200 OK
    resp_refresh = MagicMock(spec=httpx.Response)
    resp_refresh.status_code = 200
    resp_refresh.headers = {"content-type": "application/json"}
    resp_refresh.json.return_value = {"access_token": "brand_new_access_token_777", "expires_in": 3600}

    # 3. Retried upload -> 200 OK
    resp_upload_retry = MagicMock(spec=httpx.Response)
    resp_upload_retry.status_code = 200
    resp_upload_retry.json.return_value = {"id": "drive_file_abc_123", "name": "completed_worksheet.docx"}

    # 4. Permissions call -> 200 OK
    resp_perm = MagicMock(spec=httpx.Response)
    resp_perm.status_code = 200
    resp_perm.json.return_value = {"id": "perm_id_1"}

    # 5. Verify permissions call -> 200 OK
    resp_verify = MagicMock(spec=httpx.Response)
    resp_verify.status_code = 200
    resp_verify.json.return_value = {"permissions": [{"type": "anyone", "role": "reader"}]}

    # 6. Metadata query -> 200 OK
    resp_meta = MagicMock(spec=httpx.Response)
    resp_meta.status_code = 200
    resp_meta.json.return_value = {
        "id": "drive_file_abc_123",
        "name": "completed_worksheet.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "webViewLink": "https://drive.google.com/file/d/drive_file_abc_123/view",
        "webContentLink": "https://drive.google.com/uc?id=drive_file_abc_123&export=download",
    }

    mock_http = MagicMock(spec=httpx.AsyncClient)
    # Sequence of POST calls:
    # 1. upload -> resp_401
    # 2. refresh -> resp_refresh
    # 3. upload retry -> resp_upload_retry
    # 4. set permission -> resp_perm
    mock_http.post = AsyncMock(side_effect=[resp_401, resp_refresh, resp_upload_retry, resp_perm])
    # Sequence of GET calls:
    # 1. verify permissions -> resp_verify
    # 2. get metadata -> resp_meta
    mock_http.get = AsyncMock(side_effect=[resp_verify, resp_meta])

    with patch.object(client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_get_client.return_value = mock_http

        metadata = await client.upload_file(test_file, allow_original=False)
        assert metadata.file_id == "drive_file_abc_123"
        assert metadata.is_public is True
        assert client.access_token == "brand_new_access_token_777"
        assert mock_http.post.call_count == 4


@pytest.mark.asyncio
async def test_invalid_grant_revocation_marks_client_invalid():
    """Verify that when Google returns invalid_grant, client marks auth invalid and clears state."""
    client = GoogleDriveClient(
        client_id="mock-client-id",
        client_secret="mock-client-secret",
        redirect_uri="http://localhost:8000/callback",
        access_token="expired_access_token",
        refresh_token="revoked_refresh_token",
    )

    resp_invalid_grant = MagicMock(spec=httpx.Response)
    resp_invalid_grant.status_code = 400
    resp_invalid_grant.headers = {"content-type": "application/json"}
    resp_invalid_grant.json.return_value = {
        "error": "invalid_grant",
        "error_description": "Token has been expired or revoked.",
    }

    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(return_value=resp_invalid_grant)

    with patch.object(client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_get_client.return_value = mock_http

        with pytest.raises(DriveTokenExpiredError) as exc_info:
            await client.refresh_access_token()

        assert "Token refresh failed (400)" in str(exc_info.value)
        assert "Token has been expired or revoked." in str(exc_info.value)
        # Client tokens should have been marked invalid and cleared
        assert client._tokens is None
        assert client.refresh_token is None
        assert client.access_token is None


def test_status_endpoint_reports_reauthorization_required_on_expired_token():
    """Verify /api/v1/auth/google/status reports reauthorization_required: True when token is revoked."""
    api_client = TestClient(app)

    # Mock GoogleDriveClient.get_user_info to raise DriveTokenExpiredError
    with patch("apps.api.routes.auth.get_active_drive_tokens", return_value=("fake_rf", "fake_at")), \
         patch("apps.api.routes.auth.GoogleDriveClient.get_user_info", new_callable=AsyncMock) as mock_user_info:
        mock_user_info.side_effect = DriveTokenExpiredError("Token has been expired or revoked.")

        resp = api_client.get("/api/v1/auth/google/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False
        assert data["valid"] is False
        assert data["reauthorization_required"] is True
        assert "re-authorize" in data["error"].lower()


def test_status_endpoint_reports_valid_connected_on_success():
    """Verify /api/v1/auth/google/status reports connected: True, valid: True when probe succeeds."""
    api_client = TestClient(app)

    with patch("apps.api.routes.auth.get_active_drive_tokens", return_value=("valid_rf", "valid_at")), \
         patch("apps.api.routes.auth.GoogleDriveClient.get_user_info", new_callable=AsyncMock) as mock_user_info:
        mock_user_info.return_value = {
            "displayName": "SRM Test User",
            "emailAddress": "student@srmist.edu.in",
        }

        resp = api_client.get("/api/v1/auth/google/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is True
        assert data["valid"] is True
        assert data["reauthorization_required"] is False
        assert data["user"]["emailAddress"] == "student@srmist.edu.in"
