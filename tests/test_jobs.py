from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from packages.shared.models.job import Job, JobStatus


def test_job_status_enum_completeness():
    """Ensure all required job states are defined exactly as specified."""
    expected_states = {
        "PENDING",
        "RUNNING",
        "WAITING_FOR_CAPTCHA",
        "DOWNLOADING",
        "PROCESSING",
        "UPLOADING",
        "SUBMITTING",
        "VERIFYING",
        "COMPLETED",
        "FAILED",
    }
    actual_states = {status.value for status in JobStatus}
    assert expected_states == actual_states, f"Mismatch in JobStatus values: {expected_states ^ actual_states}"


def test_create_job_endpoint(client: TestClient):
    """Verify POST /jobs creates a job and returns 201 with initial state PENDING."""
    with patch("apps.api.routes.jobs.process_job.delay") as mock_delay:
        payload = {
            "user_id": "test_student_1",
            "course_id": "CSE101",
            "semester_id": "sem-5",
            "subject_id": "sub-101",
            "worksheet_id": "ws-55",
            "transport_mode": "auto",
        }
        response = client.post("/jobs", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert "id" in data
        assert data["status"] == "PENDING"
        assert data["user_id"] == "test_student_1"
        assert data["worksheet_id"] == "ws-55"
        assert data["transport_mode"] == "auto"

        # Verify Celery background worker was invoked
        mock_delay.assert_called_once()
        assert mock_delay.call_args[1]["job_id"] == data["id"]


def test_get_job_endpoint(client: TestClient):
    """Verify GET /jobs/{job_id} returns an existing job."""
    with patch("apps.api.routes.jobs.process_job.delay"):
        create_res = client.post("/jobs", json={"user_id": "user_abc", "worksheet_id": "ws_1"})
        assert create_res.status_code == 201
        job_id = create_res.json()["id"]

        get_res = client.get(f"/jobs/{job_id}")
        assert get_res.status_code == 200
        data = get_res.json()
        assert data["id"] == job_id
        assert data["status"] == "PENDING"
        assert data["user_id"] == "user_abc"


def test_get_nonexistent_job(client: TestClient):
    """Verify GET /jobs/{job_id} returns 404 for unknown job IDs."""
    response = client.get("/jobs/non-existent-uuid-12345")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_job_lifecycle_state_persistence(db_session: Session):
    """Verify all required job states can be persisted and transitioned cleanly in the database."""
    all_states = [
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.WAITING_FOR_CAPTCHA,
        JobStatus.DOWNLOADING,
        JobStatus.PROCESSING,
        JobStatus.UPLOADING,
        JobStatus.SUBMITTING,
        JobStatus.VERIFYING,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
    ]

    job = Job(user_id="user_lifecycle", worksheet_id="ws_life")
    db_session.add(job)
    db_session.commit()

    for target_state in all_states:
        job.status = target_state
        job.current_step = f"step_{target_state.value.lower()}"
        db_session.commit()
        db_session.refresh(job)
        assert job.status == target_state


def test_captcha_flow_endpoint(client: TestClient, db_session: Session):
    """Verify submitting CAPTCHA solution transitions job out of WAITING_FOR_CAPTCHA."""
    with patch("apps.api.routes.jobs.process_job.delay") as mock_delay:
        # Create job directly in DB in WAITING_FOR_CAPTCHA state
        job = Job(
            user_id="user_captcha",
            worksheet_id="ws_captcha",
            status=JobStatus.WAITING_FOR_CAPTCHA,
            captcha_challenge={"type": "image", "selector": "#captcha"},
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Submit captcha solution
        response = client.post(
            f"/jobs/{job.id}/captcha",
            json={"solution": "ABCD8"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "PENDING"
        
        # Verify job in DB updated and Celery called
        db_session.refresh(job)
        assert job.captcha_solution == "ABCD8"
        mock_delay.assert_called_once()
