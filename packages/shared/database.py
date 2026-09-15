import logging
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from packages.shared.config import settings

logger = logging.getLogger("srm_database")

# Configure engine arguments based on database dialect
engine_kwargs = {"echo": settings.DATABASE_ECHO}
if settings.DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

try:
    engine = create_engine(settings.DATABASE_URL, **engine_kwargs)
    # Validate connection immediately
    with engine.connect() as conn:
        pass
except OperationalError as exc:
    logger.warning(
        "Configured database connection failed at %s (%s). Falling back to local SQLite 'sqlite:///./srm_automator.db'",
        settings.DATABASE_URL.split("@")[-1] if "@" in settings.DATABASE_URL else settings.DATABASE_URL,
        getattr(exc, "orig", exc),
    )
    fallback_url = "sqlite:///./srm_automator.db"
    engine = create_engine(
        fallback_url,
        connect_args={"check_same_thread": False},
        echo=settings.DATABASE_ECHO,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """Dependency for obtaining database sessions in FastAPI routes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)
