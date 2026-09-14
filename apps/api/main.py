from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from packages.shared.config import settings
from packages.shared.database import init_db
from apps.api.routes.health import router as health_router
from apps.api.routes.jobs import router as jobs_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("srm_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler to perform startup and shutdown tasks."""
    logger.info("Initializing database schema...")
    try:
        init_db()
        logger.info("Database schema initialized successfully.")
    except Exception as exc:
        logger.warning("Database initialization deferred (database may still be starting): %s", exc)
    yield
    logger.info("Shutting down API server.")


app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Backend-first SRM eCurricula worksheet automation platform API",
    version="0.1.0",
    lifespan=lifespan,
)

# Enable CORS for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Root-level health check endpoint (as specified: GET /health -> {"status": "ok"})
app.include_router(health_router)

# Jobs routes mounted at root (/jobs) and versioned (/api/v1/jobs)
app.include_router(jobs_router)
app.include_router(jobs_router, prefix=settings.API_V1_STR)


@app.get("/")
def root():
    return {
        "message": "SRM eCurricula Automation Platform API",
        "docs": "/docs",
        "health": "/health",
        "jobs": "/jobs"
    }
