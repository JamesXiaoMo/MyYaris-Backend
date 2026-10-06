def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "1.0.0"
    assert "timestamp" in data
    # Ensure no secrets leaked
    assert "token" not in str(data).lower()
    assert "key" not in str(data).lower()


def test_auth_rejection(client):
    # Missing auth header
    response = client.post("/destinations", json={"input_type": "text", "text": "东京铁塔"})
    assert response.status_code == 401

    # Invalid token
    response = client.post(
        "/destinations",
        json={"input_type": "text", "text": "东京铁塔"},
        headers={"Authorization": "Bearer bad-token-123"},
    )
    assert response.status_code == 401

    # Empty token
    response_empty = client.post(
        "/destinations",
        json={"input_type": "text", "text": "东京铁塔"},
        headers={"Authorization": "Bearer "},
    )
    assert response_empty.status_code == 401


def test_role_separation(client, phone_headers, car_headers):
    # Car token attempting to submit destination -> 403 Forbidden
    response = client.post(
        "/destinations",
        json={"input_type": "text", "text": "东京铁塔"},
        headers=car_headers,
    )
    assert response.status_code == 403

    # Phone token attempting to poll car tasks -> 403 Forbidden
    response = client.get(
        "/devices/car_p3pro/tasks/next?timeout=1",
        headers=phone_headers,
    )
    assert response.status_code == 403
