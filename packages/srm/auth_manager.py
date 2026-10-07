"""Centralized Authentication & Interactive Browser Session Manager.

Manages interactive Playwright browser authentication lifecycles for SRM portal:
1. Enforces single active browser per job or discovery operation.
2. Prevents duplicate launches on rapid user clicks.
3. Tracks state machine: PENDING -> AUTHENTICATING -> OPENING_BROWSER -> WAITING_FOR_CAPTCHA -> AUTHENTICATED / AUTHENTICATION_ERROR.
4. Only marks browser opened once page creation is verified.
5. Captures and persists SRMAuthSession for seamless direct HTTP handoff to background workers.
6. Automatically cleans up browser contexts, pages, and timeouts.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional

from packages.srm.models import SRMAuthSession

logger = logging.getLogger("srm_auth_manager")


class AuthPhase(str, Enum):
    IDLE = "IDLE"
    PENDING = "PENDING"
    AUTHENTICATING = "AUTHENTICATING"
    OPENING_BROWSER = "OPENING_BROWSER"
    WAITING_FOR_CAPTCHA = "WAITING_FOR_CAPTCHA"
    AUTHENTICATED = "AUTHENTICATED"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


@dataclass
class AuthRequest:
    """An active or completed authentication request."""
    request_id: str  # job_id or "discovery:{user_id}"
    user_id: str
    password: str
    phase: AuthPhase = AuthPhase.PENDING
    message: str = "Authentication requested"
    browser_confirmed: bool = False
    auth_session: Optional[SRMAuthSession] = None
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    timeout_seconds: int = 180
    cancel_requested: bool = False
    browser_instance: Optional[Any] = None  # Reference to running browser/page for cleanup

    def is_active(self) -> bool:
        """Return True if this auth request is currently in progress."""
        return self.phase in (
            AuthPhase.PENDING,
            AuthPhase.AUTHENTICATING,
            AuthPhase.OPENING_BROWSER,
            AuthPhase.WAITING_FOR_CAPTCHA,
        )

    def is_expired(self) -> bool:
        """Check if request has timed out."""
        return (time.time() - self.created_at) > self.timeout_seconds


class SRMAuthManager:
    """Thread-safe, process-safe manager for SRM authentication sessions and browser lifecycles."""

    def __init__(self):
        self._requests: Dict[str, AuthRequest] = {}
        self._sessions: Dict[str, SRMAuthSession] = {}  # Key: user_id or job_id
        self._lock = asyncio.Lock()

    async def get_request(self, request_id: str) -> Optional[AuthRequest]:
        """Fetch an authentication request by ID."""
        async with self._lock:
            req = self._requests.get(request_id)
            if req and req.is_expired() and req.is_active():
                req.phase = AuthPhase.TIMED_OUT
                req.message = "Authentication timed out waiting for CAPTCHA solution."
                req.updated_at = time.time()
                await self._cleanup_browser(req)
            return req

    async def create_or_get_request(
        self,
        request_id: str,
        user_id: str,
        password: str,
        timeout_seconds: int = 180,
    ) -> tuple[AuthRequest, bool]:
        """Create a new auth request or return existing active request (duplicate protection).
        
        Returns (request, is_new).
        """
        async with self._lock:
            existing = self._requests.get(request_id)
            if existing and existing.is_active() and not existing.is_expired():
                logger.info("Reusing existing active auth request %s (phase: %s)", request_id, existing.phase)
                return existing, False

            # If existing is dead or expired, clean up old browser if any
            if existing:
                await self._cleanup_browser(existing)

            # Evict stale requests (> 15 minutes)
            now = time.time()
            stale_keys = [k for k, r in self._requests.items() if (now - r.created_at) > 900]
            for k in stale_keys:
                self._requests.pop(k, None)

            new_req = AuthRequest(
                request_id=request_id,
                user_id=user_id,
                password=password,
                phase=AuthPhase.PENDING,
                message="Authentication request initialized",
                timeout_seconds=timeout_seconds,
            )
            self._requests[request_id] = new_req
            logger.info("Created new auth request %s for user %s", request_id, user_id)
            return new_req, True

    async def update_phase(
        self,
        request_id: str,
        phase: AuthPhase,
        message: str,
        browser_confirmed: Optional[bool] = None,
        error_message: Optional[str] = None,
    ) -> Optional[AuthRequest]:
        """Update request state and synchronize progress."""
        async with self._lock:
            req = self._requests.get(request_id)
            if not req:
                return None
            req.phase = phase
            req.message = message
            req.updated_at = time.time()
            if browser_confirmed is not None:
                req.browser_confirmed = browser_confirmed
            if error_message is not None:
                req.error_message = error_message
            logger.info("AuthRequest [%s] -> %s: %s (browser_confirmed=%s)", request_id, phase.value, message, req.browser_confirmed)
            return req

    async def store_session(self, request_id: str, session: SRMAuthSession) -> None:
        """Store captured authenticated session and transition state to AUTHENTICATED."""
        async with self._lock:
            req = self._requests.get(request_id)
            if req:
                req.phase = AuthPhase.AUTHENTICATED
                req.message = "Authentication successful"
                req.auth_session = session
                req.updated_at = time.time()
                await self._cleanup_browser(req)

            # Also cache session by user_id and request_id for fast lookup
            self._sessions[request_id] = session
            if session.user_id:
                self._sessions[session.user_id] = session
            logger.info("Stored authenticated session for request %s (user: %s)", request_id, session.user_id)

    set_session = store_session

    async def get_session(self, identifier: str) -> Optional[SRMAuthSession]:
        """Fetch an authenticated session by job_id or user_id."""
        async with self._lock:
            sess = self._sessions.get(identifier)
            if sess and sess.is_valid:
                return sess
            # Also check if request has it
            req = self._requests.get(identifier)
            if req and req.auth_session and req.auth_session.is_valid:
                return req.auth_session
            return None

    async def cancel_request(self, request_id: str, reason: str = "Cancelled by user") -> None:
        """Cancel an in-progress auth request and close its browser."""
        async with self._lock:
            req = self._requests.get(request_id)
            if req:
                req.phase = AuthPhase.CANCELLED
                req.message = reason
                req.cancel_requested = True
                req.updated_at = time.time()
                await self._cleanup_browser(req)
                logger.info("Cancelled auth request %s: %s", request_id, reason)

    async def _cleanup_browser(self, req: AuthRequest) -> None:
        """Safely close any active browser or context attached to an auth request."""
        if req.browser_instance:
            try:
                browser = req.browser_instance
                if hasattr(browser, "close"):
                    res = browser.close()
                    if asyncio.iscoroutine(res):
                        await res
                logger.info("Cleaned up browser instance for request %s", req.request_id)
            except Exception as exc:
                logger.warning("Error cleaning up browser for %s: %s", req.request_id, exc)
            finally:
                req.browser_instance = None


# Global singleton instance
auth_manager = SRMAuthManager()
