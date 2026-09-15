import os
from typing import Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

# Force testing configuration
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "True"

from packages.shared.database import Base, get_db
from apps.api.main import app
from apps.worker.celery_app import celery_app

# Use in-memory SQLite for automated tests to run isolated and offline
TEST_SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Configure Celery eager mode and hermetic offline answer provider during testing."""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    from packages.shared.config import settings
    orig_provider = settings.WORKSHEET_ANSWER_PROVIDER
    settings.WORKSHEET_ANSWER_PROVIDER = "rule"
    yield
    settings.WORKSHEET_ANSWER_PROVIDER = orig_provider


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    """Provide a clean database session for each test function."""
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """Provide a FastAPI TestClient wired to the test in-memory database."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
