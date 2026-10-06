def test_long_poll_timeout_no_tasks(client, car_headers):
    # Timeout 1s with no tasks should return 204 No Content
    response = client.get("/devices/car_p3pro/tasks/next?timeout=1", headers=car_headers)
    assert response.status_code == 204


def test_long_poll_receives_task(client, phone_headers, car_headers):
    # Create task via coordinates
    create_resp = client.post(
        "/destinations",
        json={
            "input_type": "coords",
            "coordinate": {"latitude": 35.771986, "longitude": 140.392850},
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    assert create_resp.status_code == 200
    task_id = create_resp.json()["task_id"]

    # Car polls for task
    poll_resp = client.get("/devices/car_p3pro/tasks/next?timeout=1", headers=car_headers)
    assert poll_resp.status_code == 200
    poll_data = poll_resp.json()
    assert poll_data["task_id"] == task_id
    assert poll_data["status"] == "queued"
    assert poll_data["delivery_attempts"] >= 1
