"""Exceptions for worksheet parsing, answer generation, and document filling."""

from typing import Any, Dict, Optional


class AnswerEngineError(Exception):
    """Base exception for all answer generation errors."""
    pass


class LLMAuthenticationError(AnswerEngineError):
    """Raised when API key is invalid, unauthorized, or unconfigured when required."""
    pass


class LLMRateLimitError(AnswerEngineError):
    """Raised when the LLM provider returns 429 / quota exceeded."""
    def __init__(self, message: str, status_code: Optional[int] = 429, response: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response or {}


class LLMTimeoutError(AnswerEngineError):
    """Raised when an LLM API request times out."""
    pass


class LLMNetworkError(AnswerEngineError):
    """Raised on connection drop, DNS failure, or transport error communicating with LLM."""
    pass


class LLMResponseError(AnswerEngineError):
    """Raised when the model response is malformed, blocked, or fails schema validation."""
    def __init__(self, message: str, raw_response: Optional[str] = None):
        super().__init__(message)
        self.raw_response = raw_response


class MissingAnswerError(AnswerEngineError):
    """Raised when the LLM response is missing required question answers."""
    pass


class WorksheetFillingError(Exception):
    """Raised when an answer target cannot be resolved or filled into a designated document location."""
    pass

