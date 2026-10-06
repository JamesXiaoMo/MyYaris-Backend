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


def test_integer_coordinates_via_payload(client, phone_headers):
    payload = {
        "input_type": "coords",
        "coordinate": {
            "latitude": 35,
            "longitude": 139,
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
    assert data["candidates"][0]["latitude"] == 35.0
    assert data["candidates"][0]["longitude"] == 139.0


def test_share_link_ll_only_rejected_as_clarification(client, phone_headers):
    # Parameter ll alone represents center, NOT destination
    url = "https://maps.google.com/?ll=35.6812,139.7671&z=15"
    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": url, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert len(data["candidates"]) == 0
    assert "视野中心" in data["clarification_question"]


def test_share_link_ll_with_explicit_q_resolves(client, phone_headers):
    # Parameter ll present as viewport, but q has explicit pinpoint coordinates
    url = "https://maps.google.com/?ll=35.6812,139.7671&q=35.6586,139.7454"
    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": url, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved"
    assert data["task_id"] is not None
    assert abs(data["candidates"][0]["latitude"] - 35.6586) < 1e-4
    assert abs(data["candidates"][0]["longitude"] - 139.7454) < 1e-4

    # Integer coordinates in q
    url_int = "https://maps.google.com/?ll=36,140&q=35,139"
    resp_int = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": url_int, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp_int.status_code == 200
    data_int = resp_int.json()
    assert data_int["status"] == "resolved"
    assert data_int["candidates"][0]["latitude"] == 35.0
    assert data_int["candidates"][0]["longitude"] == 139.0


def test_share_link_route_with_destination_resolves(client, phone_headers):
    # Route link with explicit destination coordinates
    url = "https://www.google.com/maps/dir/?api=1&origin=35.6812,139.7671&destination=35.6586,139.7454"
    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": url, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved"
    assert data["task_id"] is not None
    assert abs(data["candidates"][0]["latitude"] - 35.6586) < 1e-4
    assert abs(data["candidates"][0]["longitude"] - 139.7454) < 1e-4

    # Route with daddr integer coordinates
    url_daddr = "https://maps.google.com/?saddr=35.6812,139.7671&daddr=35,139"
    resp_daddr = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": url_daddr, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp_daddr.status_code == 200
    data_daddr = resp_daddr.json()
    assert data_daddr["status"] == "resolved"
    assert data_daddr["candidates"][0]["latitude"] == 35.0
    assert data_daddr["candidates"][0]["longitude"] == 139.0


def test_share_link_route_without_destination_rejected_as_clarification(client, phone_headers):
    # Route link with coordinates in path/protobuf but lacking explicit destination parameter
    # System MUST NOT pick the origin (first coordinate pair) as destination!
    url = "https://www.google.com/maps/dir/35.6812,139.7671/35.6586,139.7454/data=!4m2!4m1!3e0"
    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": url, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert len(data["candidates"]) == 0
    assert "路线导航链接" in data["clarification_question"]


def test_share_link_multiple_locations_rejected_as_clarification(client, phone_headers):
    # Multiple distinct protobuf coordinates in URL
    url = "https://www.google.com/maps/data=!3d35.1!4d139.1!3d35.2!4d139.2"
    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": url, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert len(data["candidates"]) == 0
    assert "多个地点坐标" in data["clarification_question"]


def test_share_link_exhausted_hops_returns_failed(client, phone_headers, monkeypatch):
    import httpx

    class FakeResponse:
        def __init__(self, status_code, headers, url):
            self.status_code = status_code
            self.headers = headers
            self.url = url
            self.is_redirect = True

    async def fake_head(self, url, headers=None):
        return FakeResponse(302, {"location": "https://maps.google.com/loop_next"}, url)

    monkeypatch.setattr(httpx.AsyncClient, "head", fake_head)

    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": "https://maps.google.com/loop_start", "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert "重定向跳数过多" in data["error_message"]

