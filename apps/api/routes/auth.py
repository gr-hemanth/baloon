"""Google Drive OAuth 2.0 authorization endpoints."""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse

from packages.drive.client import (
    GoogleDriveClient,
    clear_drive_tokens_from_env,
    save_drive_tokens_to_env,
)
from packages.drive.exceptions import DriveAuthenticationError
from packages.shared.config import settings

logger = logging.getLogger("api_auth")

router = APIRouter(prefix="/auth/google", tags=["Google Drive Auth"])


@router.get("/status")
async def get_drive_auth_status() -> Dict[str, Any]:
    """Check current Google Drive OAuth authorization status."""
    refresh_token = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN") or settings.GOOGLE_DRIVE_REFRESH_TOKEN
    access_token = os.getenv("GOOGLE_DRIVE_ACCESS_TOKEN") or settings.GOOGLE_DRIVE_ACCESS_TOKEN
    client_id = os.getenv("GOOGLE_DRIVE_CLIENT_ID") or settings.GOOGLE_DRIVE_CLIENT_ID
    client_secret = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET") or settings.GOOGLE_DRIVE_CLIENT_SECRET

    connected = bool(refresh_token or access_token)
    client_configured = bool(client_id and client_secret)

    user_info = None
    if connected and client_configured:
        try:
            client = GoogleDriveClient()
            user_info = await client.get_user_info()
            await client.close()
        except Exception:
            pass

    return {
        "connected": connected,
        "client_configured": client_configured,
        "has_refresh_token": bool(refresh_token),
        "redirect_uri": settings.GOOGLE_DRIVE_REDIRECT_URI,
        "user": user_info,
    }


@router.get("/url")
def get_drive_authorization_url(prompt: str = "select_account consent") -> Dict[str, Any]:
    """Generate Google Drive OAuth consent URL with account selection prompt."""
    try:
        client = GoogleDriveClient()
        auth_url = client.get_authorization_url(prompt=prompt)
        return {
            "authorization_url": auth_url,
            "redirect_uri": client.redirect_uri,
        }
    except DriveAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.post("/disconnect")
async def disconnect_google_drive() -> Dict[str, Any]:
    """Disconnect current Google Drive account, revoking tokens and clearing local storage."""
    refresh_token = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN") or settings.GOOGLE_DRIVE_REFRESH_TOKEN
    access_token = os.getenv("GOOGLE_DRIVE_ACCESS_TOKEN") or settings.GOOGLE_DRIVE_ACCESS_TOKEN

    if refresh_token or access_token:
        try:
            client = GoogleDriveClient()
            await client.revoke_credentials()
            await client.close()
        except Exception as exc:
            logger.warning("Revocation request to Google server completed with note: %s", exc)

    # Clear tokens from in-memory settings and os.environ
    settings.GOOGLE_DRIVE_REFRESH_TOKEN = None
    settings.GOOGLE_DRIVE_ACCESS_TOKEN = None
    os.environ.pop("GOOGLE_DRIVE_REFRESH_TOKEN", None)
    os.environ.pop("GOOGLE_DRIVE_ACCESS_TOKEN", None)

    # Clear tokens from .env file safely
    clear_drive_tokens_from_env()

    logger.info("Google Drive disconnected successfully. Local tokens cleared.")
    return {"status": "disconnected", "connected": False}


@router.post("/test-upload")
async def test_drive_upload() -> Dict[str, Any]:
    """Perform a small, safe test file upload to verify Drive authorization and ownership."""
    refresh_token = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN") or settings.GOOGLE_DRIVE_REFRESH_TOKEN
    access_token = os.getenv("GOOGLE_DRIVE_ACCESS_TOKEN") or settings.GOOGLE_DRIVE_ACCESS_TOKEN
    if not (refresh_token or access_token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Drive is not connected. Authorize an account first.",
        )

    client = GoogleDriveClient()
    test_file = Path("artifacts/test_drive_connectivity.txt")
    try:
        user_info = await client.get_user_info()

        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("SRM Automator Google Drive Authorization Verification Test.\n", encoding="utf-8")

        metadata = await client.upload_file(
            local_path=test_file,
            filename="completed_test_auth_verification.txt",
            mime_type="text/plain",
            allow_original=True,
        )

        return {
            "status": "success",
            "file_id": metadata.file_id,
            "filename": metadata.filename,
            "web_url": metadata.web_url,
            "is_public": metadata.is_public,
            "user": user_info,
        }
    except Exception as exc:
        logger.error("Test upload error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Test upload failed: {exc}",
        )
    finally:
        if test_file.exists():
            try:
                test_file.unlink()
            except Exception:
                pass
        await client.close()


@router.get("/callback")
async def google_oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
) -> HTMLResponse:
    """Handle Google OAuth redirect callback."""
    if error:
        logger.warning("Google OAuth error: %s", error)
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;padding:40px;text-align:center;'>"
            f"<h2 style='color:#dc2626;'>Google Drive Authorization Denied</h2>"
            f"<p>{error}</p>"
            f"<p><a href='/dashboard'>Return to Dashboard</a></p>"
            f"</body></html>",
            status_code=400,
        )

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        client = GoogleDriveClient()
        tokens = await client.exchange_code(code)

        # Save tokens to local environment in memory and settings
        if tokens.refresh_token:
            settings.GOOGLE_DRIVE_REFRESH_TOKEN = tokens.refresh_token
            os.environ["GOOGLE_DRIVE_REFRESH_TOKEN"] = tokens.refresh_token
        if tokens.access_token:
            settings.GOOGLE_DRIVE_ACCESS_TOKEN = tokens.access_token
            os.environ["GOOGLE_DRIVE_ACCESS_TOKEN"] = tokens.access_token

        # Persist tokens to local .env file
        save_drive_tokens_to_env(refresh_token=tokens.refresh_token, access_token=tokens.access_token)

        await client.close()

        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px;text-align:center;'>"
            "<h2 style='color:#16a34a;'>Google Drive Connected Successfully!</h2>"
            "<p>Your account is now linked. You can close this window and return to the dashboard.</p>"
            "<script>window.opener && window.opener.postMessage({type: 'GOOGLE_DRIVE_CONNECTED'}, '*');</script>"
            "<p><a href='/dashboard' style='display:inline-block;margin-top:20px;padding:10px 20px;background:#2563eb;color:white;text-decoration:none;border-radius:6px;'>Return to Dashboard</a></p>"
            "</body></html>"
        )
    except Exception as exc:
        logger.error("OAuth token exchange error: %s", exc)
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;padding:40px;text-align:center;'>"
            f"<h2 style='color:#dc2626;'>Token Exchange Failed</h2>"
            f"<p>{exc}</p>"
            f"<p><a href='/dashboard'>Return to Dashboard</a></p>"
            f"</body></html>",
            status_code=500,
        )
