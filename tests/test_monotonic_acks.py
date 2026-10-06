def test_monotonic_acknowledgements(client, phone_headers, car_headers):
    # 1. Create a task
    create_resp = client.post(
        "/destinations",
        json={
            "input_type": "coords",
            "coordinate": {"latitude": 35.658581, "longitude": 139.745433},
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    task_id = create_resp.json()["task_id"]

    # 2. Car reports 'received'
    ack1 = client.post(
        f"/tasks/{task_id}/ack",
        json={"phase": "received", "device_timestamp": "2026-10-06T10:30:00Z"},
        headers=car_headers,
    )
    assert ack1.status_code == 200
    assert ack1.json()["status"] == "received"

    # 3. Car reports 'launch_requested' (CAR NAVITIME intent triggered)
    ack2 = client.post(
        f"/tasks/{task_id}/ack",
        json={"phase": "launch_requested", "device_timestamp": "2026-10-06T10:30:02Z"},
        headers=car_headers,
    )
    assert ack2.status_code == 200
    assert ack2.json()["status"] == "launch_requested"

    # 4. Delayed duplicate 'received' ack arrives
    ack_delayed = client.post(
        f"/tasks/{task_id}/ack",
        json={"phase": "received", "device_timestamp": "2026-10-06T10:29:59Z"},
        headers=car_headers,
    )
    assert ack_delayed.status_code == 200
    # Must remain launch_requested without regressing
    assert ack_delayed.json()["status"] == "launch_requested"
    assert ack_delayed.json()["accepted"] is False

    # 5. Check task status via GET /tasks/{id}
    task_resp = client.get(f"/tasks/{task_id}", headers=car_headers)
    assert task_resp.status_code == 200
    assert task_resp.json()["status"] == "launch_requested"


def test_terminal_failed_non_regression(client, phone_headers, car_headers):
    # 1. Create a task
    create_resp = client.post(
        "/destinations",
        json={
            "input_type": "coords",
            "coordinate": {"latitude": 35.658581, "longitude": 139.745433},
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    task_id = create_resp.json()["task_id"]

    # 2. Report failure
    ack_fail = client.post(
        f"/tasks/{task_id}/ack",
        json={"phase": "failed", "error_code": "NAVITIME_NOT_INSTALLED", "error_detail": "App not found"},
        headers=car_headers,
    )
    assert ack_fail.status_code == 200
    assert ack_fail.json()["status"] == "failed"

    # 3. Delayed 'received' ack should NOT regress status from 'failed'
    ack_late = client.post(
        f"/tasks/{task_id}/ack",
        json={"phase": "received", "device_timestamp": "2026-10-06T10:31:00Z"},
        headers=car_headers,
    )
    assert ack_late.status_code == 200
    assert ack_late.json()["status"] == "failed"
    assert ack_late.json()["accepted"] is False


def test_device_ownership_isolation(client, phone_headers, car_headers, admin_headers):
    # Create task targeted for 'car_other'
    create_resp = client.post(
        "/destinations",
        json={
            "input_type": "coords",
            "coordinate": {"latitude": 35.658581, "longitude": 139.745433},
            "target_device_id": "car_other",
        },
        headers=phone_headers,
    )
    task_id = create_resp.json()["task_id"]

    # car_headers is configured for 'car_p3pro'; attempting to ack 'car_other' task -> 403
    ack_resp = client.post(
        f"/tasks/{task_id}/ack",
        json={"phase": "received"},
        headers=car_headers,
    )
    assert ack_resp.status_code == 403

    # car_headers attempting to view 'car_other' task -> 403
    view_resp = client.get(f"/tasks/{task_id}", headers=car_headers)
    assert view_resp.status_code == 403

    # admin_headers can view and ack
    admin_view = client.get(f"/tasks/{task_id}", headers=admin_headers)
    assert admin_view.status_code == 200
