from pathlib import Path
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Application Info
    PROJECT_NAME: str = "SRM Automator"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    SECRET_KEY: str = "insecure-dev-secret-key-change-in-production"
    API_V1_STR: str = "/api/v1"

    # Database (PostgreSQL default, sqlite supported for lightweight local test)
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5432/srm_automator"
    DATABASE_ECHO: bool = False

    # Celery & Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"
    CELERY_TASK_ALWAYS_EAGER: bool = False

    # SRM Portal
    SRM_BASE_URL: str = "https://dld.srmist.edu.in"
    SRM_REQUEST_TIMEOUT_SECONDS: int = 30
    SRM_HEADLESS_BROWSER: bool = True
    SRM_PREFER_HTTP: bool = True
    SRM_ARTIFACTS_DIR: str = "artifacts/browser"
    SRM_DOWNLOAD_DIR: str = "artifacts/downloads"

    # Google Drive OAuth (OAuth 2.0 User Authorization)
    GOOGLE_DRIVE_CLIENT_ID: Optional[str] = None
    GOOGLE_DRIVE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_DRIVE_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/google/callback"
    GOOGLE_DRIVE_ACCESS_TOKEN: Optional[str] = None
    GOOGLE_DRIVE_REFRESH_TOKEN: Optional[str] = None
    GOOGLE_DRIVE_FOLDER_ID: Optional[str] = None

    # AI Answer Engine Configuration
    WORKSHEET_ANSWER_PROVIDER: str = "nvidia"  # Primary: "nvidia", "freellm", "rule", "gemini", "openai"
    AI_FALLBACK_PROVIDER: Optional[str] = "freellm"  # Fallback: "freellm", "nvidia", "rule", None
    AI_PROVIDER_ORDER: str = "nvidia,freellm"  # Configurable provider order: e.g. "nvidia,freellm"
    FREELLM_BASE_URL: str = "http://127.0.0.1:31415/v1"
    FREELLM_API_KEY: Optional[str] = None
    FREELLM_MODEL: str = "default"
    FREELLM_TEMPERATURE: float = 0.2
    FREELLM_TIMEOUT_SECONDS: float = 60.0
    FREELLM_MAX_RETRIES: int = 2
    FREELLM_RETRY_BACKOFF_SECONDS: float = 2.0

    # NVIDIA API (Primary Provider)
    NVIDIA_BASE_URL: str = "https://integrate.api.nvidia.com/v1"
    NVIDIA_API_KEY: Optional[str] = None
    NVIDIA_MODEL: str = "nvidia/nemotron-3-super-120b-a12b"
    NVIDIA_TEMPERATURE: float = 0.2
    NVIDIA_TIMEOUT_SECONDS: float = 60.0
    NVIDIA_MAX_RETRIES: int = 1
    NVIDIA_RETRY_BACKOFF_SECONDS: float = 2.0

    # Provider Failover Strategy & Chunking
    WORKSHEET_CHUNK_SIZE: int = 5  # Number of questions per request chunk (0 to disable chunking)

    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-1.5-flash"
    GEMINI_TEMPERATURE: float = 0.2
    GEMINI_MAX_OUTPUT_TOKENS: int = 4096
    GEMINI_TIMEOUT_SECONDS: float = 60.0
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def browser_artifacts_path(self) -> Path:
        p = Path(self.SRM_ARTIFACTS_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def download_path(self) -> Path:
        p = Path(self.SRM_DOWNLOAD_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_provider_order(self) -> List[str]:
        """Return configured provider priority order list."""
        if self.AI_PROVIDER_ORDER:
            order = [p.strip().lower() for p in self.AI_PROVIDER_ORDER.split(",") if p.strip()]
            if order:
                return order
        order = []
        if self.WORKSHEET_ANSWER_PROVIDER:
            order.append(self.WORKSHEET_ANSWER_PROVIDER.lower())
        if self.AI_FALLBACK_PROVIDER and self.AI_FALLBACK_PROVIDER.lower() not in order:
            order.append(self.AI_FALLBACK_PROVIDER.lower())
        return order or ["nvidia", "freellm"]


settings = Settings()
