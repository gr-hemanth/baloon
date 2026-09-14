from pathlib import Path
from typing import Optional
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


settings = Settings()
