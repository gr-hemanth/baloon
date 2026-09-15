"""Live controlled Google Drive integration runner.

Performs live end-to-end verification of:
1. Google Drive OAuth 2.0 authorization
2. Secure document upload of completed worksheet (completed_real_1011.docx)
3. Public reader permission configuration ('anyone' -> 'reader')
4. Permission verification via Google Drive API
5. Shareable webViewLink retrieval and verification

Guarantees:
- Zero leakage of access tokens, refresh tokens, client secrets, or private keys.
- Read-only protection of original documents.
- No interaction with SRM portal.
- No modification or deletion of existing Google Drive files.
"""

import asyncio
import io
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

# Ensure UTF-8 output (line_buffering=True ensures prints flush immediately)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.path.insert(0, ".")

from packages.drive.client import GoogleDriveClient
from packages.drive.exceptions import (
    DriveAuthenticationError,
    DriveException,
    DriveFileNotFoundError,
    DrivePermissionError,
    DriveUploadError,
    DriveVerificationError,
)
from packages.shared.config import settings

logger = logging.getLogger("drive_live_test")


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Temporary local HTTP handler to capture OAuth redirect callback."""

    auth_code: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "state" in params:
            OAuthCallbackHandler.state = params["state"][0]

        if "code" in params:
            OAuthCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif;padding:40px;text-align:center;'>"
                b"<h2 style='color:#16a34a;'>Google Drive Authorization Successful!</h2>"
                b"<p>You can close this window and return to your terminal.</p>"
                b"</body></html>"
            )
        else:
            err = params.get("error", ["Unknown OAuth Error"])[0]
            OAuthCallbackHandler.error = err
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"<html><body><h2>Authorization Failed</h2><p>{err}</p></body></html>".encode("utf-8")
            )

    def log_message(self, format, *args):
        # Suppress noisy HTTP server logs to avoid exposing URLs
        pass


def _save_tokens_to_env(refresh_token: str, access_token: Optional[str] = None):
    """Persist acquired refresh token to local .env without overwriting other keys."""
    env_path = Path(".env")
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    updated = {}
    new_lines = []
    for line in lines:
        if line.startswith("GOOGLE_DRIVE_REFRESH_TOKEN="):
            new_lines.append(f"GOOGLE_DRIVE_REFRESH_TOKEN={refresh_token}")
            updated["GOOGLE_DRIVE_REFRESH_TOKEN"] = True
        elif access_token and line.startswith("GOOGLE_DRIVE_ACCESS_TOKEN="):
            new_lines.append(f"GOOGLE_DRIVE_ACCESS_TOKEN={access_token}")
            updated["GOOGLE_DRIVE_ACCESS_TOKEN"] = True
        else:
            new_lines.append(line)

    if "GOOGLE_DRIVE_REFRESH_TOKEN" not in updated:
        new_lines.append(f"GOOGLE_DRIVE_REFRESH_TOKEN={refresh_token}")
    if access_token and "GOOGLE_DRIVE_ACCESS_TOKEN" not in updated:
        new_lines.append(f"GOOGLE_DRIVE_ACCESS_TOKEN={access_token}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


async def run_controlled_drive_test():
    print("=" * 65)
    print("CONTROLLED LIVE GOOGLE DRIVE TEST")
    print("=" * 65)

    client_id = os.getenv("GOOGLE_DRIVE_CLIENT_ID") or settings.GOOGLE_DRIVE_CLIENT_ID
    client_secret = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET") or settings.GOOGLE_DRIVE_CLIENT_SECRET
    redirect_uri = os.getenv("GOOGLE_DRIVE_REDIRECT_URI") or settings.GOOGLE_DRIVE_REDIRECT_URI or "http://localhost:8000/api/v1/auth/google/callback"
    access_token = os.getenv("GOOGLE_DRIVE_ACCESS_TOKEN") or settings.GOOGLE_DRIVE_ACCESS_TOKEN
    refresh_token = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN") or settings.GOOGLE_DRIVE_REFRESH_TOKEN
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID") or settings.GOOGLE_DRIVE_FOLDER_ID

    # 1. Inspect Environment Configuration
    print("\n[Step 1/8] Checking Local Environment Configuration...")
    has_client_id = bool(client_id)
    has_client_secret = bool(client_secret)
    has_access_token = bool(access_token)
    has_refresh_token = bool(refresh_token)

    masked_client_id = (client_id[:8] + "..." + client_id[-12:]) if client_id and len(client_id) > 20 else ("Configured" if has_client_id else "NOT SET")
    print(f"  - GOOGLE_DRIVE_CLIENT_ID     : {masked_client_id}")
    print(f"  - GOOGLE_DRIVE_CLIENT_SECRET : {'Configured (REDACTED)' if has_client_secret else 'NOT SET'}")
    print(f"  - GOOGLE_DRIVE_REDIRECT_URI  : {redirect_uri}")
    print(f"  - GOOGLE_DRIVE_REFRESH_TOKEN : {'Configured (REDACTED)' if has_refresh_token else 'NOT SET'}")
    print(f"  - GOOGLE_DRIVE_ACCESS_TOKEN  : {'Configured (REDACTED)' if has_access_token else 'NOT SET'}")

    if not has_client_id or not has_client_secret:
        print("\n" + "!" * 65)
        print("PREREQUISITE REQUIRED: Google OAuth 2.0 Client Credentials")
        print("!" * 65)
        print("To run the live Google Drive test, provide your OAuth Client credentials:")
        print("1. Open Google Cloud Console -> APIs & Services -> Credentials.")
        print("2. Create or select an OAuth 2.0 Client ID (Web Application or Desktop).")
        print(f"3. Ensure the Authorized Redirect URI matches: {redirect_uri}")
        print("4. Add the following lines to your local .env file:")
        print("     GOOGLE_DRIVE_CLIENT_ID=your-client-id.apps.googleusercontent.com")
        print("     GOOGLE_DRIVE_CLIENT_SECRET=your-client-secret")
        print("!" * 65)
        return {
            "oauth_succeeded": False,
            "upload_succeeded": False,
            "file_id": None,
            "sharing_verification": "NOT_RUN",
            "web_view_link": None,
            "error": "Missing GOOGLE_DRIVE_CLIENT_ID or GOOGLE_DRIVE_CLIENT_SECRET in .env/environment.",
        }

    # Initialize Drive Client
    client = GoogleDriveClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        access_token=access_token,
        refresh_token=refresh_token,
    )

    # 2. Authorization Flow
    print("\n[Step 2/8] Authorizing Google Drive Access...")
    oauth_succeeded = False
    try:
        if has_refresh_token or has_access_token:
            print("  - Using existing configured tokens...")
            active_token = await client._ensure_access_token()
            oauth_succeeded = bool(active_token)
            print(f"  -> Authentication verified! Token active (len={len(active_token)}, secret redacted).")
        else:
            print("  - Initiating interactive OAuth 2.0 authorization flow...")
            auth_url = client.get_authorization_url()
            print("\n" + "-" * 65)
            print("ACTION REQUIRED: Authorize Google Drive in your browser:")
            print(f"\n{auth_url}\n")
            print("-" * 65)

            # Reset handler state before listening
            OAuthCallbackHandler.auth_code = None
            OAuthCallbackHandler.state = None
            OAuthCallbackHandler.error = None

            # Start local callback receiver if redirect_uri points to localhost
            parsed_redirect = urlparse(redirect_uri)
            port = parsed_redirect.port or (80 if parsed_redirect.scheme == "http" else 443)
            host = parsed_redirect.hostname or "localhost"

            print(f"  Waiting for authorization callback on http://{host}:{port} ...")
            print("  (If the browser doesn't automatically redirect, you can also paste the authorization code)")

            auth_code = None
            try:
                server = HTTPServer((host, port), OAuthCallbackHandler)
                server.timeout = 120.0  # 2 minute timeout for user consent
                while auth_code is None:
                    server.handle_request()
                    auth_code = OAuthCallbackHandler.auth_code
                    if OAuthCallbackHandler.error:
                        raise DriveAuthenticationError(f"OAuth error returned: {OAuthCallbackHandler.error}")
            except Exception as srv_err:
                logger.warning("Local callback server exception: %s", srv_err)

            if not auth_code:
                raise DriveAuthenticationError("Timed out waiting for Google OAuth authorization code.")

            # Validate OAuth CSRF state if returned by the provider
            if OAuthCallbackHandler.state and client.state:
                if OAuthCallbackHandler.state != client.state:
                    raise DriveAuthenticationError(
                        f"OAuth state mismatch (CSRF token verification failed): {OAuthCallbackHandler.state} != {client.state}"
                    )

            print("  -> Authorization code received. Exchanging for tokens...")
            tokens = await client.exchange_code(auth_code)
            oauth_succeeded = True
            if tokens.refresh_token:
                _save_tokens_to_env(tokens.refresh_token, tokens.access_token)
                print("  -> Refresh token securely persisted to .env for future sessions.")

    except Exception as auth_err:
        print(f"  -> OAuth Authorization failed: {auth_err}")
        await client.close()
        return {
            "oauth_succeeded": False,
            "upload_succeeded": False,
            "file_id": None,
            "sharing_verification": "FAILED",
            "web_view_link": None,
            "error": str(auth_err),
        }

    # 3. Locate Completed Worksheet (completed_real_1011.docx)
    print("\n[Step 3/8] Locating Completed Worksheet Document...")
    worksheet_path = Path("artifacts/live_test_output/completed_real_1011.docx")
    if not worksheet_path.exists():
        # Check alternative common locations
        alt_paths = [
            Path("artifacts/completed_real_1011.docx"),
            Path("artifacts/downloads/completed_real_1011.docx"),
        ]
        for alt in alt_paths:
            if alt.exists():
                worksheet_path = alt
                break

    if not worksheet_path.exists():
        err_msg = f"Completed worksheet file not found at {worksheet_path}."
        print(f"  -> ERROR: {err_msg}")
        await client.close()
        return {
            "oauth_succeeded": oauth_succeeded,
            "upload_succeeded": False,
            "file_id": None,
            "sharing_verification": "NOT_RUN",
            "web_view_link": None,
            "error": err_msg,
        }

    print(f"  -> Target File Found: {worksheet_path}")
    print(f"  -> File Size        : {worksheet_path.stat().st_size:,} bytes")
    print(f"  -> Filename         : {worksheet_path.name}")

    # 4. Upload File to Google Drive
    print("\n[Step 4/8] Uploading Completed Worksheet to Google Drive...")
    upload_succeeded = False
    metadata = None
    try:
        t0 = time.time()
        metadata = await client.upload_file(
            local_path=worksheet_path,
            filename="completed_real_1011.docx",
            folder_id=folder_id,
            allow_original=False,  # Enforces safety guard ensuring original is never uploaded
        )
        upload_time = time.time() - t0
        upload_succeeded = True
        print(f"  -> Upload SUCCESSFUL in {upload_time:.2f}s!")
        print(f"  -> Drive File ID    : {metadata.file_id}")
        print(f"  -> Stored Name      : {metadata.filename}")
        print(f"  -> MIME Type        : {metadata.mime_type}")
    except Exception as up_err:
        print(f"  -> Upload failed: {up_err}")
        await client.close()
        return {
            "oauth_succeeded": oauth_succeeded,
            "upload_succeeded": False,
            "file_id": None,
            "sharing_verification": "FAILED",
            "web_view_link": None,
            "error": str(up_err),
        }

    # 5. Sharing & Permission Configuration
    print("\n[Step 5/8] Setting Public Sharing Permission ('anyone' -> 'reader')...")
    try:
        await client.set_public_permission(metadata.file_id, role="reader")
        print("  -> Permission applied: role=reader, type=anyone.")
    except Exception as perm_err:
        print(f"  -> Failed to set public permission: {perm_err}")

    # 6. Permission Verification via Drive API
    print("\n[Step 6/8] Verifying Permission via Google Drive API...")
    is_verified = False
    try:
        is_verified = await client.verify_public_permission(metadata.file_id)
        if is_verified:
            print("  -> VERIFICATION CONFIRMED: File is publicly viewable by anyone with the link.")
        else:
            print("  -> WARNING: Public reader permission was not verified on the Drive file.")
    except Exception as v_err:
        print(f"  -> Permission verification check encountered error: {v_err}")

    # 7. Retrieve & Verify webViewLink
    print("\n[Step 7/8] Retrieving and Verifying Shareable webViewLink...")
    web_view_link = metadata.web_url
    print(f"  -> webViewLink: {web_view_link}")

    # 8. Confirm Accessibility via HTTP Request
    print("\n[Step 8/8] Testing Link Accessibility...")
    http_accessible = False
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http_tester:
            test_resp = await http_tester.get(web_view_link)
            if test_resp.status_code in (200, 302, 303):
                http_accessible = True
                print(f"  -> HTTP Accessibility Confirmed: Status {test_resp.status_code} OK")
            else:
                print(f"  -> HTTP check returned status: {test_resp.status_code}")
    except Exception as http_err:
        print(f"  -> HTTP accessibility check error: {http_err}")

    await client.close()

    print("\n" + "=" * 65)
    print("CONTROLLED LIVE GOOGLE DRIVE TEST SUMMARY")
    print("=" * 65)
    print(f"OAuth Succeeded       : {oauth_succeeded}")
    print(f"Upload Succeeded      : {upload_succeeded}")
    print(f"File ID               : {metadata.file_id if metadata else None}")
    print(f"Sharing Verified      : {'PASS (VERIFIED_PUBLIC_READER)' if is_verified else 'FAIL'}")
    print(f"Shareable webViewLink : {web_view_link}")
    print(f"Web Accessible        : {http_accessible}")
    print("=" * 65)

    return {
        "oauth_succeeded": oauth_succeeded,
        "upload_succeeded": upload_succeeded,
        "file_id": metadata.file_id if metadata else None,
        "sharing_verification": "VERIFIED_PUBLIC_READER" if is_verified else "UNVERIFIED",
        "web_view_link": web_view_link,
        "error": None,
    }


if __name__ == "__main__":
    asyncio.run(run_controlled_drive_test())
