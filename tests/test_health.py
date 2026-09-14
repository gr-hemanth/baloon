from fastapi.testclient import TestClient


def test_health_endpoint(client: TestClient):
    """Verify that GET /health returns 200 OK and {'status': 'ok'}."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_endpoint(client: TestClient):
    """Verify that the API root endpoint responds."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "health" in data
    assert "jobs" in data
