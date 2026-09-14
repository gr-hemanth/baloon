"""Domain models for Google Drive file storage and OAuth 2.0 tokens."""

import time
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class DriveFileMetadata(BaseModel):
    """Structured metadata returned following a Drive upload and permission setup."""
    file_id: str
    filename: str
    mime_type: str
    web_url: str  # Direct viewable share URL
    download_url: Optional[str] = None
    permission_status: str = "PENDING"
    is_public: bool = False
    size_bytes: Optional[int] = None
    created_time: Optional[str] = None
    folder_id: Optional[str] = None
    raw_response: Dict[str, Any] = Field(default_factory=dict)

    def is_shareable(self) -> bool:
        """Check if the document is publicly viewable via web URL."""
        return self.is_public and bool(self.web_url)


class OAuthTokens(BaseModel):
    """Secure model for OAuth 2.0 tokens with strict redaction safeguards."""
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: int = 3600
    token_type: str = "Bearer"
    scope: Optional[str] = None
    created_at: float = Field(default_factory=time.time)

    def is_expired(self, skew_seconds: int = 60) -> bool:
        """Check if access token has expired or is nearing expiry."""
        now = time.time()
        return (now - self.created_at) >= (self.expires_in - skew_seconds)

    def __repr__(self) -> str:
        """Safe representation ensuring tokens are NEVER exposed in logs."""
        has_refresh = bool(self.refresh_token)
        return (
            f"OAuthTokens(token_type='{self.token_type}', "
            f"access_token='[REDACTED]', "
            f"has_refresh_token={has_refresh}, "
            f"expires_in={self.expires_in})"
        )

    def __str__(self) -> str:
        return self.__repr__()
