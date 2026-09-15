"""Google Drive OAuth 2.0 authorization endpoints."""

import logging
import os
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse

from packages.drive.client import GoogleDriveClient
from packages.drive.exceptions import DriveAuthenticationError
from packages.shared.config import settings

logger = logging.getLogger("api_auth")

router = APIRouter(prefix="/auth/google", tags=["Google Drive Auth"])


@router.get("/status")
def get_drive_auth_status() -> Dict[str, Any]:
    """Check current Google Drive OAuth authorization status."""
    refresh_token = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN") or settings.GOOGLE_DRIVE_REFRESH_TOKEN
    access_token = os.getenv("GOOGLE_DRIVE_ACCESS_TOKEN") or settings.GOOGLE_DRIVE_ACCESS_TOKEN
    client_id = os.getenv("GOOGLE_DRIVE_CLIENT_ID") or settings.GOOGLE_DRIVE_CLIENT_ID
    client_secret = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET") or settings.GOOGLE_DRIVE_CLIENT_SECRET

    connected = bool(refresh_token or access_token)
    client_configured = bool(client_id and client_secret)

    return {
        "connected": connected,
        "client_configured": client_configured,
        "has_refresh_token": bool(refresh_token),
        "redirect_uri": settings.GOOGLE_DRIVE_REDIRECT_URI,
    }


@router.get("/url")
def get_drive_authorization_url() -> Dict[str, Any]:
    """Generate Google Drive OAuth consent URL."""
    try:
        client = GoogleDriveClient()
        auth_url = client.get_authorization_url()
        return {
            "authorization_url": auth_url,
            "redirect_uri": client.redirect_uri,
        }
    except DriveAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


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
