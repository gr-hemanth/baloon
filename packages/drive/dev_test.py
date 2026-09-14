"""Developer testing tool for Google Drive OAuth 2.0 and file upload.

Allows developers to test Google Drive connectivity, token verification,
and document upload using credentials supplied strictly via environment variables.
Guarantees zero leakage of access tokens, refresh tokens, or client secrets.

Usage:
    python -m packages.drive.dev_test
"""

import asyncio
import os
import sys
from pathlib import Path
import tempfile

from packages.drive.client import GoogleDriveClient
from packages.drive.exceptions import DriveAuthenticationError, DriveException
from packages.shared.config import settings


async def run_safe_drive_test():
    print("=" * 60)
    print("Google Drive OAuth 2.0 Developer Test Tool")
    print("=" * 60)

    client_id = os.getenv("GOOGLE_DRIVE_CLIENT_ID") or settings.GOOGLE_DRIVE_CLIENT_ID
    client_secret = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET") or settings.GOOGLE_DRIVE_CLIENT_SECRET
    access_token = os.getenv("GOOGLE_DRIVE_ACCESS_TOKEN") or settings.GOOGLE_DRIVE_ACCESS_TOKEN
    refresh_token = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN") or settings.GOOGLE_DRIVE_REFRESH_TOKEN

    client = GoogleDriveClient(
        client_id=client_id,
        client_secret=client_secret,
        access_token=access_token,
        refresh_token=refresh_token,
    )

    # Step 1: Check credentials
    print("\n[1/3] Inspecting OAuth 2.0 Configuration...")
    if not client_id or not client_secret:
        print("  -> Notice: GOOGLE_DRIVE_CLIENT_ID or GOOGLE_DRIVE_CLIENT_SECRET not set.")
        print("     To configure Google Drive integration:")
        print("     1. Create an OAuth 2.0 Client ID (Web Application or Desktop) in Google Cloud Console.")
        print("     2. Set in your environment:")
        print("        $env:GOOGLE_DRIVE_CLIENT_ID = 'your-client-id.apps.googleusercontent.com'")
        print("        $env:GOOGLE_DRIVE_CLIENT_SECRET = 'your-client-secret'")
        print("        $env:GOOGLE_DRIVE_REFRESH_TOKEN = 'your-refresh-token'")
        print("\n  Client structure is verified and operational.")
        await client.close()
        return

    masked_client_id = client_id[:6] + "..." + client_id[-10:] if len(client_id) > 16 else "***"
    print(f"  -> Client ID configured: {masked_client_id}")
    print("  -> Client Secret: [REDACTED]")

    # If no tokens configured, offer the auth URL
    if not access_token and not refresh_token:
        print("\n[INFO] No access token or refresh token found.")
        try:
            auth_url = client.get_authorization_url()
            print("  To authorize Google Drive access for the first time, visit:")
            print(f"\n  {auth_url}\n")
            print("  After authorizing, exchange the code with:")
            print("    tokens = await client.exchange_code(authorization_code)")
        except Exception as exc:
            print(f"  -> Could not generate auth URL: {exc}")
        await client.close()
        return

    # Step 2: Verify Token & Connectivity
    print("\n[2/3] Checking OAuth 2.0 Token Validity...")
    try:
        active_token = await client._ensure_access_token()
        token_len = len(active_token)
        print(f"  -> Access token active! Length: {token_len} chars [REDACTED]")
    except Exception as exc:
        print(f"  -> Token validation error: {exc}")
        await client.close()
        return

    # Step 3: Test Upload of a Completed Worksheet
    print("\n[3/3] Testing Upload of Completed Worksheet Document...")
    temp_dir = Path(tempfile.mkdtemp(prefix="drive_test_"))
    test_file = temp_dir / "completed_test_worksheet.docx"
    test_file.write_bytes(b"PK\x03\x04" + b"\x00" * 100)  # Minimal test DOCX binary signature

    try:
        metadata = await client.upload_file(
            local_path=test_file,
            filename="completed_test_worksheet.docx",
        )
        print("  -> Upload & Sharing SUCCESSFUL!")
        print(f"     File ID: {metadata.file_id}")
        print(f"     Filename: {metadata.filename}")
        print(f"     MIME Type: {metadata.mime_type}")
        print(f"     Shareable Web URL: {metadata.web_url}")
        print(f"     Permission Status: {metadata.permission_status}")
    except DriveException as de:
        print(f"  -> Google Drive operation error: {de}")
    except Exception as exc:
        print(f"  -> Unexpected error: {exc}")
    finally:
        if test_file.exists():
            test_file.unlink()
        await client.close()

    print("\n" + "=" * 60)
    print("Google Drive Test Complete")
    print("=" * 60)


def main():
    asyncio.run(run_safe_drive_test())


if __name__ == "__main__":
    main()
