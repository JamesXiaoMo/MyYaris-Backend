from datetime import timedelta
from tests.conftest import TestingSessionLocal
from app.repositories.models import Task, utc_now


def test_task_superseding(client, phone_headers, car_headers):
    # 1. Create task 1
    resp1 = client.post(
        "/destinations",
        json={
            "input_type": "coords",
            "coordinate": {"latitude": 35.658581, "longitude": 139.745433},
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    task1_id = resp1.json()["task_id"]

    # 2. Create task 2 before task 1 is executed
    resp2 = client.post(
        "/destinations",
        json={
            "input_type": "coords",
            "coordinate": {"latitude": 35.771986, "longitude": 140.392850},
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    task2_id = resp2.json()["task_id"]

    # Task 1 should be superseded by Task 2
    task1_data = client.get(f"/tasks/{task1_id}", headers=car_headers).json()
    assert task1_data["status"] == "superseded"

    # Task 2 should be queued
    task2_data = client.get(f"/tasks/{task2_id}", headers=car_headers).json()
    assert task2_data["status"] == "queued"

    # Polling receives task 2, not task 1
    poll_resp = client.get("/devices/car_p3pro/tasks/next?timeout=1", headers=car_headers)
    assert poll_resp.status_code == 200
    assert poll_resp.json()["task_id"] == task2_id


def test_idempotency_key_behavior(client, phone_headers):
    key = "idem-test-key-999"
    payload = {
        "input_type": "coords",
        "coordinate": {"latitude": 35.658581, "longitude": 139.745433},
        "idempotency_key": key,
        "target_device_id": "car_p3pro",
    }

    # First request
    resp1 = client.post("/destinations", json=payload, headers=phone_headers)
    assert resp1.status_code == 200
    req_id1 = resp1.json()["request_id"]

    # Second request with identical payload and same key
    resp2 = client.post("/destinations", json=payload, headers=phone_headers)
    assert resp2.status_code == 200
    assert resp2.json()["request_id"] == req_id1

    # Third request with same key but different payload -> 409 Conflict
    conflicting_payload = {
        "input_type": "coords",
        "coordinate": {"latitude": 34.0, "longitude": 135.0},
        "idempotency_key": key,
        "target_device_id": "car_p3pro",
    }
    resp3 = client.post("/destinations", json=conflicting_payload, headers=phone_headers)
    assert resp3.status_code == 409


def test_task_expiration(client, phone_headers, car_headers):
    # Create task
    resp = client.post(
        "/destinations",
        json={
            "input_type": "coords",
            "coordinate": {"latitude": 35.658581, "longitude": 139.745433},
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    task_id = resp.json()["task_id"]

    # Force task expiration in db
    db = TestingSessionLocal()
    task = db.query(Task).filter(Task.task_id == task_id).first()
    task.expires_at = utc_now() - timedelta(minutes=10)
    db.commit()
    db.close()

    # Polling should find 0 valid tasks (returns 204)
    poll_resp = client.get("/devices/car_p3pro/tasks/next?timeout=1", headers=car_headers)
    assert poll_resp.status_code == 204

    # Direct query shows status expired
    task_data = client.get(f"/tasks/{task_id}", headers=car_headers).json()
    assert task_data["status"] == "expired"
