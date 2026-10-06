def test_submit_valid_coordinates(client, phone_headers):
    payload = {
        "input_type": "coords",
        "coordinate": {
            "latitude": 35.658581,
            "longitude": 139.745433,
            "coord_system": "wgs84",
        },
        "target_device_id": "car_p3pro",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "resolved"
    assert data["task_id"] is not None
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["latitude"] == 35.658581
    assert data["candidates"][0]["longitude"] == 139.745433


def test_invalid_coordinates_range(client, phone_headers):
    # Latitude > 90
    bad_payload = {
        "input_type": "coords",
        "coordinate": {
            "latitude": 95.0,
            "longitude": 139.745433,
        },
    }
    response = client.post("/destinations", json=bad_payload, headers=phone_headers)
    assert response.status_code == 422  # Pydantic validation error


def test_text_search_and_confirm(client, phone_headers):
    # Step 1: Submit text query
    response = client.post(
        "/destinations",
        json={"input_type": "text", "text": "我想去东京铁塔", "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert response.status_code == 200
    data = response.json()
    request_id = data["request_id"]
    assert len(data["candidates"]) >= 1

    cand_id = data["candidates"][0]["candidate_id"]

    # Step 2: Confirm selected candidate
    confirm_resp = client.post(
        f"/destinations/{request_id}/confirm",
        json={"candidate_id": cand_id, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert confirm_resp.status_code == 200
    task_data = confirm_resp.json()
    assert task_data["status"] == "queued"
    assert task_data["latitude"] == 35.658581
    assert task_data["longitude"] == 139.745433

    # Step 3: Re-confirming is idempotent and returns the same task
    reconfirm_resp = client.post(
        f"/destinations/{request_id}/confirm",
        json={"candidate_id": cand_id, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert reconfirm_resp.status_code == 200
    assert reconfirm_resp.json()["task_id"] == task_data["task_id"]
