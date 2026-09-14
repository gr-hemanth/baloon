# SRM eCurricula Worksheet Automation Platform

A backend-first automation platform for managing and completing SRM eCurricula worksheets with asynchronous worker orchestration, direct HTTP preference, and headless Playwright browser fallback.

---

## Architecture Overview

```
                          ┌───────────────────────┐
                          │  Frontend (Next.js)   │
                          │   (Browser Optional)  │
                          └──────────┬────────────┘
                                     │ HTTP (REST)
                                     ▼
                          ┌───────────────────────┐
                          │  FastAPI Application  │
                          │      (apps/api)       │
                          └──────────┬────────────┘
                                     │ Enqueue Job
                                     ▼
┌──────────────────┐       ┌───────────────────────┐       ┌──────────────────┐
│ PostgreSQL       │◄──────┤ Redis Broker & Celery │──────►│ Celery Worker    │
│ (State & Jobs)   │       │ (Background Tasks)    │       │ (apps/worker)    │
└──────────────────┘       └───────────────────────┘       └─────────┬────────┘
                                                                     │
                                                                     ▼
                                                          ┌───────────────────────┐
                                                          │   SRM Orchestrator    │
                                                          └──────────┬────────────┘
                                            Direct HTTP First        │       Browser Fallback
                                      ┌──────────────────────────────┴──────────────────────────────┐
                                      ▼                                                             ▼
                           ┌─────────────────────┐                                       ┌─────────────────────┐
                           │   SRMHttpClient     │                                       │  SRMBrowserClient   │
                           │  (Direct REST/Form) │                                       │ (Playwright Chrome) │
                           └──────────┬──────────┘                                       └──────────┬──────────┘
                                      │                                                             │
                                      └──────────────────────────────┬──────────────────────────────┘
                                                                     ▼
                                                        ┌─────────────────────────┐
                                                        │   SRM Portal (DLD)      │
                                                        │ https://dld.srmist.edu.in│
                                                        └─────────────────────────┘
```

### Key Architectural Principles

1. **Backend-First Automation**: Long-running jobs run strictly on background server workers (Celery + Redis). The user's browser does **not** need to remain open during execution.
2. **Direct HTTP Priority**: Direct HTTP/REST requests are attempted first to eliminate browser overhead and maximize throughput.
3. **Headless Browser Fallback**: When an SRM operation requires JavaScript execution, DOM evaluation, or session handling unsupported by HTTP, `SRMOrchestrator` automatically falls back to Playwright headless Chromium.
4. **Human-in-the-Loop for CAPTCHAs**: Security and authentication mechanisms are never automatically bypassed. When a CAPTCHA is detected, the job pauses in `WAITING_FOR_CAPTCHA` state and exposes the challenge for the user to solve via the UI or API (`POST /jobs/{id}/captcha`).
5. **Strict Secret Management**: No credentials, API tokens, cookies, or secrets are ever hardcoded or committed to version control. All configuration is loaded from environment variables.

---

## Project Structure

```
srm-automator/
├── apps/
│   ├── api/                      # FastAPI application
│   │   ├── routes/
│   │   │   ├── health.py         # GET /health
│   │   │   └── jobs.py           # POST /jobs, GET /jobs/{job_id}, POST /jobs/{id}/captcha
│   │   └── main.py               # Application factory, lifespan, CORS, routing
│   └── worker/                   # Celery background worker
│       ├── celery_app.py         # Celery instance configuration
│       └── tasks.py              # Background job execution workflow & state machine
├── packages/
│   ├── srm/                      # SRM integration layer
│   │   ├── client.py             # Abstract SRMClient interface contract
│   │   ├── http_client.py        # Direct HTTP/REST transport (SRMHttpClient)
│   │   ├── browser_client.py     # Playwright headless Chromium transport (SRMBrowserClient)
│   │   ├── orchestrator.py       # Adaptive transport fallback orchestrator (SRMOrchestrator)
│   │   ├── discovery.py          # Portal network analysis & endpoint discovery script
│   │   ├── exceptions.py         # SRM exception hierarchy (SRMCaptchaRequired, etc.)
│   │   └── models.py             # Domain models (Course, Semester, Subject, Worksheet)
│   ├── worksheets/               # Document processing scaffold (deferred)
│   │   └── processor.py          # WorksheetProcessor interface
│   ├── drive/                    # Google Drive integration scaffold (deferred)
│   │   └── client.py             # GoogleDriveClient interface
│   └── shared/                   # Cross-cutting concerns
│       ├── config.py             # Pydantic Settings configuration
│       ├── database.py           # SQLAlchemy engine, session factory, Base
│       ├── models/
│       │   └── job.py            # Job model with full 10-state lifecycle
│       └── schemas/
│           └── job.py            # Pydantic validation schemas
├── frontend/                     # Next.js/React frontend scaffold
│   ├── package.json
│   └── README.md
├── tests/
│   ├── conftest.py               # Test fixtures (SQLite in-memory DB, eager Celery)
│   ├── test_health.py            # Health endpoint test suite
│   └── test_jobs.py              # Job creation, lifecycle states, and CAPTCHA flow tests
├── artifacts/                    # Auto-generated runtime artifacts & discovery logs
├── .env.example                  # Environment variable template
├── docker-compose.yml            # PostgreSQL, Redis, API, Worker, and Frontend
├── Dockerfile                    # Container definition with Playwright & dependencies
├── pyproject.toml                # Project packaging & pytest configuration
├── requirements.txt              # Production and development dependencies
├── .gitignore
└── README.md
```

---

## Job State Lifecycle

A job progresses through the following sequential states:

| State | Description |
|---|---|
| `PENDING` | Job received and enqueued in Celery broker |
| `RUNNING` | Worker initialized, connecting to SRM portal |
| `WAITING_FOR_CAPTCHA` | Paused: Portal presented a CAPTCHA; awaiting user input |
| `DOWNLOADING` | Downloading worksheet PDF/documents |
| `PROCESSING` | Parsing questions and generating answers |
| `UPLOADING` | Storing artifacts / preparing upload |
| `SUBMITTING` | Uploading and submitting worksheet to SRM |
| `VERIFYING` | Confirming submission acknowledgment on portal |
| `COMPLETED` | Job finished successfully |
| `FAILED` | Job encountered an unrecoverable error |

---

## Quickstart & Commands

### 1. Install Dependencies

```powershell
# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install backend dependencies
pip install -r requirements.txt

# Install Playwright Chromium browser
playwright install chromium
```

### 2. Start PostgreSQL and Redis

Using Docker Compose:
```powershell
docker compose up -d postgres redis
```

Verify services are healthy:
```powershell
docker compose ps
```

### 3. Start FastAPI Backend

```powershell
uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
```
Interactive Swagger documentation available at: `http://localhost:8000/docs`

### 4. Start Celery Background Worker

On Windows:
```powershell
celery -A apps.worker.celery_app.celery_app worker --loglevel=info -P solo
```

On Linux/macOS or Docker:
```bash
celery -A apps.worker.celery_app.celery_app worker --loglevel=info -c 2
```

### 5. Run the SRM Discovery Script

Analyzes `https://dld.srmist.edu.in`, intercepts network traffic, identifies candidate REST/XHR endpoints, and generates sanitized reports:

```powershell
python -m packages.srm.discovery
```
Outputs:
- JSON report: `artifacts/srm_discovery.json`
- Markdown report: `artifacts/srm_discovery_report.md`

### 6. Run the Test Suite

```powershell
pytest -v
```

---

## API Endpoints

### Health Check
- **`GET /health`**  
  Response:
  ```json
  {
    "status": "ok"
  }
  ```

### Create Job
- **`POST /jobs`**  
  Request:
  ```json
  {
    "user_id": "student_101",
    "course_id": "CSE101",
    "semester_id": "sem-5",
    "subject_id": "sub-101",
    "worksheet_id": "ws-01",
    "transport_mode": "auto"
  }
  ```
  Response: `201 Created` with job ID and `status: "PENDING"`.

### Get Job Status
- **`GET /jobs/{job_id}`**  
  Response includes the current status (e.g., `RUNNING`, `DOWNLOADING`, `WAITING_FOR_CAPTCHA`), active step, and results.

### Submit CAPTCHA Solution
- **`POST /jobs/{job_id}/captcha`**  
  Request:
  ```json
  {
    "solution": "AB49C"
  }
  ```
  Resumes the paused job by providing the user's CAPTCHA solution to the background worker.

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/srm_automator` | PostgreSQL connection URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis caching & broker |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery message queue |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` | Celery task result backend |
| `SRM_BASE_URL` | `https://dld.srmist.edu.in` | Target SRM portal URL |
| `SRM_PREFER_HTTP` | `True` | Prioritize direct HTTP over browser |
| `SRM_HEADLESS_BROWSER` | `True` | Headless mode for Playwright |
| `SECRET_KEY` | *(dev secret)* | JWT and session cryptographic secret |
