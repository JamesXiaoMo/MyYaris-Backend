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


def test_share_link_with_pinpoint(client, phone_headers):
    # Pinpoint containing !3d35.6585805!4d139.7454329
    url = "https://www.google.com/maps/place/Tokyo+Tower/@35.6000,139.7000,12z/data=!4m6!3m5!1s0x60188bbd9009ec09:0x481a93f0d2a409dd!8m2!3d35.6585805!4d139.7454329"
    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": url, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved"
    assert data["task_id"] is not None
    assert abs(data["candidates"][0]["latitude"] - 35.6585805) < 1e-4
    assert abs(data["candidates"][0]["longitude"] - 139.7454329) < 1e-4


def test_share_link_viewport_only_rejected_as_clarification(client, phone_headers):
    # Viewport camera position only (no destination pin)
    url = "https://www.google.com/maps/@35.658581,139.745433,17z"
    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": url, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    # Must NOT create a task or generate fake coordinates
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert len(data["candidates"]) == 0
    assert "视野中心" in data["clarification_question"]


def test_unknown_text_does_not_fabricate_coordinates(client, phone_headers):
    # Completely unknown location query
    resp = client.post(
        "/destinations",
        json={"input_type": "text", "text": "完全虚构不存在的地点12345XYZ", "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_clarification"
    assert len(data["candidates"]) == 0
    assert data["task_id"] is None
