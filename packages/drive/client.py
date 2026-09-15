"""Google Drive API v3 client with OAuth 2.0 authorization, upload, and sharing."""

import json
import logging
import mimetypes
import os
import secrets
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from packages.drive.exceptions import (
    DriveAuthenticationError,
    DriveException,
    DriveFileNotFoundError,
    DrivePermissionError,
    DriveTokenExpiredError,
    DriveUploadError,
    DriveVerificationError,
)
from packages.drive.models import DriveFileMetadata, OAuthTokens
from packages.shared.config import settings

logger = logging.getLogger(__name__)

# Standard MIME types
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"


class BaseDriveClient(ABC):
    """Abstract interface for cloud drive storage providers."""

    @abstractmethod
    async def upload_file(
        self,
        local_path: Path,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        folder_id: Optional[str] = None,
        allow_original: bool = False,
    ) -> DriveFileMetadata:
        """Upload a completed worksheet document to cloud storage."""
        pass

    @abstractmethod
    async def set_public_permission(self, file_id: str, role: str = "reader") -> bool:
        """Configure 'anyone with link' sharing permissions."""
        pass

    @abstractmethod
    async def verify_public_permission(self, file_id: str) -> bool:
        """Verify that public viewable access is verified."""
        pass

    @abstractmethod
    async def get_file_metadata(self, file_id: str) -> DriveFileMetadata:
        """Fetch file metadata from the cloud provider."""
        pass


class GoogleDriveClient(BaseDriveClient):
    """Production-grade Google Drive API v3 client using OAuth 2.0 user authorization."""

    OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
    DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
    DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
    DEFAULT_SCOPE = "https://www.googleapis.com/auth/drive.file"

    _UNSET = object()

    def __init__(
        self,
        client_id: Optional[str] = _UNSET,
        client_secret: Optional[str] = _UNSET,
        redirect_uri: Optional[str] = _UNSET,
        access_token: Optional[str] = _UNSET,
        refresh_token: Optional[str] = _UNSET,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 30.0,
    ):
        self.client_id = (
            (settings.GOOGLE_DRIVE_CLIENT_ID or os.getenv("GOOGLE_DRIVE_CLIENT_ID"))
            if client_id is self._UNSET
            else client_id
        )
        self.client_secret = (
            (settings.GOOGLE_DRIVE_CLIENT_SECRET or os.getenv("GOOGLE_DRIVE_CLIENT_SECRET"))
            if client_secret is self._UNSET
            else client_secret
        )
        self.redirect_uri = (
            (settings.GOOGLE_DRIVE_REDIRECT_URI or "http://localhost:8000/api/v1/auth/google/callback")
            if redirect_uri is self._UNSET
            else (redirect_uri or "http://localhost:8000/api/v1/auth/google/callback")
        )
        self.timeout = timeout

        self._http_client = http_client
        self._tokens: Optional[OAuthTokens] = None
        self._state: Optional[str] = None

        init_access = (
            (settings.GOOGLE_DRIVE_ACCESS_TOKEN or os.getenv("GOOGLE_DRIVE_ACCESS_TOKEN"))
            if access_token is self._UNSET
            else access_token
        )
        init_refresh = (
            (settings.GOOGLE_DRIVE_REFRESH_TOKEN or os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN"))
            if refresh_token is self._UNSET
            else refresh_token
        )

        if init_access:
            self._tokens = OAuthTokens(
                access_token=init_access,
                refresh_token=init_refresh,
            )

    @property
    def state(self) -> Optional[str]:
        """Return the current OAuth state parameter."""
        return self._state

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create shared AsyncClient."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=self.timeout)
        return self._http_client

    def get_authorization_url(
        self,
        state: Optional[str] = None,
        scope: Optional[str] = None,
        response_type: str = "code",
        access_type: str = "offline",
        prompt: str = "consent",
        redirect_uri: Optional[str] = None,
        **extra_params: Any,
    ) -> str:
        """Generate Google OAuth 2.0 authorization URL for user consent."""
        if not self.client_id:
            raise DriveAuthenticationError("Cannot generate authorization URL: GOOGLE_DRIVE_CLIENT_ID is not configured.")

        state_value = state or getattr(self, "_state", None) or secrets.token_urlsafe(32)
        self._state = state_value

        params = {
            "response_type": response_type or "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri or self.redirect_uri,
            "scope": scope or self.DEFAULT_SCOPE,
            "access_type": access_type or "offline",
            "prompt": prompt,
            "state": state_value,
        }
        if extra_params:
            for k, v in extra_params.items():
                if v is not None:
                    params[k] = v

        return f"{self.OAUTH_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, auth_code: str) -> OAuthTokens:
        """Exchange user authorization code for access and refresh tokens."""
        if not self.client_id or not self.client_secret:
            raise DriveAuthenticationError(
                "Cannot exchange code: GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET must be configured."
            )

        client = await self._get_client()
        payload = {
            "code": auth_code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }

        try:
            resp = await client.post(self.OAUTH_TOKEN_URL, data=payload)
            if resp.status_code >= 400:
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                err_code = data.get("error")
                err_msg = data.get("error_description")
                err_desc = f"{err_code}: {err_msg}" if (err_code and err_msg) else (err_msg or err_code or resp.text)
                raise DriveAuthenticationError(f"OAuth code exchange failed ({resp.status_code}): {err_desc}")

            data = resp.json()
            self._tokens = OAuthTokens(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                expires_in=data.get("expires_in", 3600),
                token_type=data.get("token_type", "Bearer"),
                scope=data.get("scope"),
            )
            logger.info("Successfully authenticated with Google Drive via OAuth 2.0.")
            return self._tokens
        except httpx.RequestError as exc:
            raise DriveAuthenticationError(f"Network error during OAuth exchange: {exc}") from exc

    async def refresh_access_token(self) -> OAuthTokens:
        """Obtain a fresh access token using the stored refresh token."""
        if not self._tokens or not self._tokens.refresh_token:
            raise DriveTokenExpiredError("No refresh token available to renew access token.")
        if not self.client_id or not self.client_secret:
            raise DriveAuthenticationError("Client credentials missing for token refresh.")

        client = await self._get_client()
        payload = {
            "refresh_token": self._tokens.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }

        try:
            resp = await client.post(self.OAUTH_TOKEN_URL, data=payload)
            if resp.status_code >= 400:
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                err_desc = data.get("error_description") or resp.text
                raise DriveTokenExpiredError(f"Token refresh failed ({resp.status_code}): {err_desc}")

            data = resp.json()
            self._tokens.access_token = data["access_token"]
            self._tokens.expires_in = data.get("expires_in", 3600)
            self._tokens.created_at = time.time()
            if "refresh_token" in data:
                self._tokens.refresh_token = data["refresh_token"]

            logger.info("Successfully refreshed Google Drive OAuth access token.")
            return self._tokens
        except httpx.RequestError as exc:
            raise DriveAuthenticationError(f"Network error during token refresh: {exc}") from exc

    async def _ensure_access_token(self) -> str:
        """Validate and return active access token, refreshing automatically if needed."""
        if not self._tokens:
            raise DriveAuthenticationError("Google Drive is not authenticated. Obtain OAuth tokens before making API requests.")

        if self._tokens.is_expired():
            if self._tokens.refresh_token:
                await self.refresh_access_token()
            else:
                raise DriveTokenExpiredError("Google Drive access token has expired and no refresh token is present.")

        return self._tokens.access_token

    def _determine_mime_type(self, path: Path) -> str:
        """Determine appropriate MIME type for the document."""
        suffix = path.suffix.lower()
        if suffix == ".docx":
            return DOCX_MIME
        elif suffix == ".pdf":
            return PDF_MIME
        mime, _ = mimetypes.guess_type(str(path))
        return mime or "application/octet-stream"

    async def upload_file(
        self,
        local_path: Path,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        folder_id: Optional[str] = None,
        allow_original: bool = False,
    ) -> DriveFileMetadata:
        """Upload completed worksheet, set public permissions, verify, and return metadata."""
        path = Path(local_path)
        if not path.exists():
            raise DriveFileNotFoundError(f"Local worksheet file not found: {path}")

        # Requirement 4: Safety check ensuring only completed worksheets are uploaded
        if not allow_original:
            fname_lower = path.name.lower()
            is_completed = (
                fname_lower.startswith("completed_")
                or "_completed" in fname_lower
                or "completed" in fname_lower
            )
            if not is_completed:
                raise ValueError(
                    f"Safety violation: '{path.name}' does not appear to be a completed worksheet. "
                    "Only completed worksheets generated by the answering pipeline can be uploaded. "
                    "Set allow_original=True if uploading an uncompleted document is explicitly intended."
                )

        access_token = await self._ensure_access_token()
        client = await self._get_client()

        target_name = filename or path.name
        target_mime = mime_type or self._determine_mime_type(path)
        file_bytes = path.read_bytes()

        # Construct multipart/related request
        boundary = "-------srm_automator_boundary_314159"
        metadata_obj: Dict[str, Any] = {"name": target_name}
        if folder_id:
            metadata_obj["parents"] = [folder_id]

        meta_part = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata_obj)}\r\n"
        ).encode("utf-8")

        media_part = (
            f"--{boundary}\r\n"
            f"Content-Type: {target_mime}\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        body = meta_part + media_part

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
            "Content-Length": str(len(body)),
        }

        # Safe logging: Never log Authorization header or raw tokens
        logger.info("Uploading %s (%d bytes, MIME: %s) to Google Drive...", target_name, len(file_bytes), target_mime)

        try:
            resp = await client.post(
                f"{self.DRIVE_UPLOAD_URL}?uploadType=multipart",
                content=body,
                headers=headers,
            )

            # Check for token expiration during upload
            if resp.status_code == 401 and self._tokens and self._tokens.refresh_token:
                logger.info("Access token expired during upload; refreshing and retrying...")
                await self.refresh_access_token()
                headers["Authorization"] = f"Bearer {self._tokens.access_token}"
                resp = await client.post(
                    f"{self.DRIVE_UPLOAD_URL}?uploadType=multipart",
                    content=body,
                    headers=headers,
                )

            if resp.status_code >= 400:
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                err_msg = data.get("error", {}).get("message") or resp.text
                raise DriveUploadError(f"Drive upload failed ({resp.status_code}): {err_msg}", status_code=resp.status_code, response=data)

            upload_result = resp.json()
            file_id = upload_result["id"]
            logger.info("File uploaded successfully. Drive File ID: %s", file_id)

            # Set public sharing permission: Anyone with link can read
            await self.set_public_permission(file_id, role="reader")

            # Verify public permission
            is_verified = await self.verify_public_permission(file_id)
            if not is_verified:
                raise DriveVerificationError(f"Public sharing permission verification failed for Drive file {file_id}")

            # Retrieve final web shareable metadata
            metadata = await self.get_file_metadata(file_id)
            metadata.permission_status = "VERIFIED_PUBLIC_READER"
            metadata.is_public = True

            logger.info("Worksheet verified and publicly shareable: %s", metadata.web_url)
            return metadata

        except httpx.RequestError as exc:
            raise DriveUploadError(f"Network error while uploading to Google Drive: {exc}") from exc

    async def set_public_permission(self, file_id: str, role: str = "reader") -> bool:
        """Configure 'anyone with link' reader permission on the Drive file."""
        access_token = await self._ensure_access_token()
        client = await self._get_client()

        url = f"{self.DRIVE_FILES_URL}/{file_id}/permissions"
        payload = {
            "role": role,
            "type": "anyone",
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 401 and self._tokens and self._tokens.refresh_token:
                await self.refresh_access_token()
                headers["Authorization"] = f"Bearer {self._tokens.access_token}"
                resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code >= 400:
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                err_msg = data.get("error", {}).get("message") or resp.text
                raise DrivePermissionError(f"Failed to set public permission ({resp.status_code}): {err_msg}")

            logger.info("Public %s permission applied to Drive file %s", role, file_id)
            return True
        except httpx.RequestError as exc:
            raise DrivePermissionError(f"Network error setting permissions: {exc}") from exc

    async def verify_public_permission(self, file_id: str) -> bool:
        """Verify that the file has an active 'anyone' reader permission."""
        access_token = await self._ensure_access_token()
        client = await self._get_client()

        url = f"{self.DRIVE_FILES_URL}/{file_id}/permissions?fields=permissions(id,type,role)"
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                raise DriveVerificationError(f"Failed to fetch permissions list ({resp.status_code})")

            data = resp.json()
            perms: List[Dict[str, Any]] = data.get("permissions", [])
            for p in perms:
                if p.get("type") == "anyone" and p.get("role") in ("reader", "commenter", "writer"):
                    return True

            return False
        except httpx.RequestError as exc:
            raise DriveVerificationError(f"Network error verifying permissions: {exc}") from exc

    async def get_file_metadata(self, file_id: str) -> DriveFileMetadata:
        """Fetch metadata including webViewLink and download link."""
        access_token = await self._ensure_access_token()
        client = await self._get_client()

        fields = "id,name,mimeType,webViewLink,webContentLink,size,createdTime,permissions"
        url = f"{self.DRIVE_FILES_URL}/{file_id}?fields={fields}"
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                raise DriveFileNotFoundError(f"File {file_id} not found on Google Drive")
            if resp.status_code >= 400:
                raise DriveException(f"Failed to fetch file metadata ({resp.status_code}): {resp.text}")

            data = resp.json()
            web_url = data.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
            download_url = data.get("webContentLink") or f"https://drive.google.com/uc?id={file_id}&export=download"

            size_val = None
            if data.get("size"):
                try:
                    size_val = int(data["size"])
                except ValueError:
                    pass

            return DriveFileMetadata(
                file_id=data.get("id", file_id),
                filename=data.get("name", "worksheet.docx"),
                mime_type=data.get("mimeType", DOCX_MIME),
                web_url=web_url,
                download_url=download_url,
                size_bytes=size_val,
                created_time=data.get("createdTime"),
                raw_response=data,
            )
        except httpx.RequestError as exc:
            raise DriveException(f"Network error querying file metadata: {exc}") from exc

    async def close(self) -> None:
        """Close underlying HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None
