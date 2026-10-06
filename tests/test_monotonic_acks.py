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
