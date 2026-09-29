
# Baloon(SRM eCurricula Automator)

> Personal-use automation project for automating the SRM eCurricula worksheet workflow.

## Introduction

SRM eCurricula Automator is a backend-first automation system that reduces the repetitive work involved in downloading, answering, filling, uploading, and submitting SRM eCurricula worksheets.

The system uses SRM's available HTTP APIs wherever possible and uses browser automation only when required. Worksheet processing and AI answer generation are handled in the background, with Google Drive used for storing completed worksheets.

The workflow also includes a user review step before the final SRM submission.

---

## About

The project is designed around a simple workflow:

- Authenticate with SRM
- Discover courses, sessions, SLOs, and worksheets
- Download the required worksheet
- Parse different worksheet formats
- Generate answers using AI
- Fill the original worksheet structure without modifying the source file
- Verify the completed document
- Upload it to Google Drive
- Verify the public Drive link
- Wait for user approval
- Submit the link to SRM
- Verify the submission

CAPTCHA is handled through a waiting state where the user provides the CAPTCHA and the background workflow continues.

---

## Tech Stack

| Component | Technology |
|---|---|
| Backend | Python |
| API | FastAPI |
| Background Jobs | Celery |
| Message Broker | Redis |
| Database | SQLite |
| Browser Automation | Playwright |
| Document Processing | python-docx, PDF processing libraries |
| Primary AI Provider | NVIDIA API |
| Fallback AI Provider | FreeLLM |
| Cloud Storage | Google Drive API |
| Google Authentication | OAuth 2.0 |
| Frontend | Next.js / React |

---

## Personal Use

This project is built specifically for my personal SRM eCurricula workflow.

It is **not intended to be a general multi-user application, hosted SaaS platform, or public automation service**.

The architecture and implementation are focused on automating my own workflow while keeping user control over the final submission.

---

## Current Project Structure

```text
srm-automator/
│
├── apps/
│   ├── api/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── health.py
│   │       └── jobs.py
│   │
│   └── worker/
│       ├── __init__.py
│       ├── celery_app.py
│       └── tasks.py
│
├── packages/
│   ├── srm/
│   │   ├── client.py
│   │   ├── http_client.py
│   │   ├── browser_client.py
│   │   ├── orchestrator.py
│   │   ├── discovery.py
│   │   ├── exceptions.py
│   │   └── models.py
│   │
│   ├── worksheets/
│   │   ├── __init__.py
│   │   └── processor.py
│   │
│   ├── drive/
│   │   ├── __init__.py
│   │   └── client.py
│   │
│   └── shared/
│       ├── config.py
│       ├── database.py
│       ├── models/
│       │   └── job.py
│       └── schemas/
│           └── job.py
│
├── tests/
├── .env.example
├── README.md
└── ...
````

---

## Working Pipeline

```text
User
  │
  ▼
SRM Authentication
  │
  ▼
Course Discovery
  │
  ▼
Session / SLO Selection
  │
  ▼
Worksheet Discovery
  │
  ▼
Worksheet Download
  │
  ▼
Question Parsing
  │
  ▼
AI Answer Generation
  │
  ├── NVIDIA
  │
  └── FreeLLM Fallback
  │
  ▼
Answer Validation
  │
  ▼
Worksheet Filling
  │
  ▼
Document Verification
  │
  ▼
Google Drive Upload
  │
  ▼
Public Link Verification
  │
  ▼
User Review
  │
  ├── Cancel
  │
  └── Submit
        │
        ▼
   SRM Submission
        │
        ▼
   Submission Verification
        │
        ▼
      Completed
```

### Workflow States

```text
PENDING
   ↓
RUNNING
   ↓
WAITING_FOR_CAPTCHA
   ↓
DOWNLOADING
   ↓
PROCESSING
   ↓
UPLOADING
   ↓
AWAITING_USER_REVIEW
   ↓
SUBMITTING
   ↓
VERIFYING
   ↓
COMPLETED
```

```text
FAILED
```

can occur when a workflow step cannot be completed.

---

## Core Design

The system follows a **direct HTTP first** approach for SRM operations.

```text
                    ┌─────────────────┐
                    │  FastAPI API    │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │ Celery Worker   │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │ SRM Orchestrator│
                    └───────┬─┬───────┘
                            │ │
                HTTP First  │ │  Browser Fallback
                            │ │
                            ▼ ▼
                     SRM HTTP API
                     Playwright
```

Playwright is kept as a fallback for browser-dependent operations such as CAPTCHA handling rather than being used for the entire workflow.
