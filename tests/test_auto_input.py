from openlocationcode import openlocationcode as olc


def test_auto_valid_coordinates_comma(client, phone_headers):
    payload = {
        "input_type": "auto",
        "text": "34.4245, 132.7083",
        "target_device_id": "test_device_auto_1",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "resolved"
    assert data["task_id"] is not None
    assert "任务已发送到车机待处理" in data["message"]
    assert "34.424500" in data["message"]
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["latitude"] == 34.4245
    assert data["candidates"][0]["longitude"] == 132.7083

    # Check task assigned to test device, not car_p3pro
    task_resp = client.get(f"/tasks/{data['task_id']}", headers=phone_headers)
    assert task_resp.status_code == 200
    assert task_resp.json()["device_id"] == "test_device_auto_1"


def test_auto_valid_coordinates_chinese_comma(client, phone_headers):
    payload = {
        "input_type": "auto",
        "text": "  34.424500 ， 132.708300  ",
        "target_device_id": "test_device_auto_2",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "resolved"
    assert data["task_id"] is not None
    assert "任务已发送到车机待处理" in data["message"]


def test_auto_valid_coordinates_space_and_newline(client, phone_headers):
    # Space only
    resp1 = client.post(
        "/destinations",
        json={"input_type": "auto", "text": "34.4245 132.7083", "target_device_id": "test_device_auto_3"},
        headers=phone_headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "resolved"

    # Newline
    resp2 = client.post(
        "/destinations",
        json={"input_type": "auto", "text": "34.4245\n132.7083", "target_device_id": "test_device_auto_3"},
        headers=phone_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "resolved"

    # Chinese comma + newline
    resp3 = client.post(
        "/destinations",
        json={"input_type": "auto", "text": "34.4245，\n132.7083", "target_device_id": "test_device_auto_3"},
        headers=phone_headers,
    )
    assert resp3.status_code == 200
    assert resp3.json()["status"] == "resolved"


def test_auto_valid_negative_and_integers(client, phone_headers):
    # Negative coordinates
    resp = client.post(
        "/destinations",
        json={"input_type": "auto", "text": "-33.8688, 151.2093", "target_device_id": "test_device_auto_4"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["candidates"][0]["latitude"] == -33.8688

    # Integers
    resp_int = client.post(
        "/destinations",
        json={"input_type": "auto", "text": "34, 132", "target_device_id": "test_device_auto_4"},
        headers=phone_headers,
    )
    assert resp_int.status_code == 200
    assert resp_int.json()["status"] == "resolved"
    assert resp_int.json()["candidates"][0]["latitude"] == 34.0
    assert resp_int.json()["candidates"][0]["longitude"] == 132.0


def test_auto_swapped_coordinates_not_guessed(client, phone_headers):
    # 132.7083 is invalid as latitude (> 90); must NOT guess swap
    payload = {
        "input_type": "auto",
        "text": "132.7083, 34.4245",
        "target_device_id": "test_device_auto_5",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert "超出有效范围" in data["message"]
    assert "不会自动调换顺序" in data["message"]


def test_auto_coordinates_out_of_range(client, phone_headers):
    # Lon > 180
    payload = {
        "input_type": "auto",
        "text": "34.4245, 190.0",
        "target_device_id": "test_device_auto_6",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert "超出有效范围" in data["message"]


def test_auto_messy_text_and_url_rejected(client, phone_headers):
    # Embedded in messy words (must not arbitrarily extract 2 numbers)
    resp1 = client.post(
        "/destinations",
        json={"input_type": "auto", "text": "今天去 34.4245, 132.7083 吃拉面", "target_device_id": "test_device_auto_7"},
        headers=phone_headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "needs_clarification"
    assert resp1.json()["task_id"] is None

    # URL with coordinates
    resp2 = client.post(
        "/destinations",
        json={"input_type": "auto", "text": "https://maps.google.com/?ll=34.42,132.70", "target_device_id": "test_device_auto_7"},
        headers=phone_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "needs_clarification"
    assert resp2.json()["task_id"] is None


def test_auto_full_plus_code(client, phone_headers):
    payload = {
        "input_type": "auto",
        "text": "8Q6JCPF8+Q7",
        "target_device_id": "test_device_auto_8",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "resolved"
    assert data["task_id"] is not None
    assert "任务已发送到车机待处理" in data["message"]
    assert "8Q6JCPF8+Q7" in data["message"]


def mock_locality(monkeypatch, refs):
    from app.config import settings
    from app.providers import nominatim_provider
    monkeypatch.setattr(settings, "MAPS_PROVIDER", "nominatim")
    class Provider:
        async def search(self, text, *, locality_only=False):
            assert locality_only
            return refs
    monkeypatch.setattr(nominatim_provider, "get_nominatim_provider", lambda: Provider())


def locality_ref(lat=34.42, lon=132.74, address="東広島市, 広島県, 日本"):
    from app.providers.base import PlaceCandidateItem
    return PlaceCandidateItem(title="城市参照", address=address, latitude=lat, longitude=lon)


def test_auto_short_plus_code_with_city_and_prefecture(client, phone_headers, monkeypatch):
    mock_locality(monkeypatch, [locality_ref()])
    payload = {
        "input_type": "auto",
        "text": "CPF8+Q7 东广岛市 广岛县",
        "target_device_id": "test_device_auto_9",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "resolved"
    assert data["task_id"] is not None
    assert "任务已发送到车机待处理" in data["message"]


def test_auto_short_plus_code_missing_prefecture_needs_clarification(client, phone_headers, monkeypatch):
    mock_locality(monkeypatch, [locality_ref()])
    payload = {
        "input_type": "auto",
        "text": "CPF8+Q7 东广岛市",
        "target_device_id": "test_device_auto_10",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert "请确认候选目的地" in data["message"]


def test_auto_coarse_plus_code_needs_clarification(client, phone_headers):
    # Coarse code (length 8 < 10)
    payload = {
        "input_type": "auto",
        "text": "8Q6JCP00+",
        "target_device_id": "test_device_auto_11",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert "该编码覆盖较大区域" in data["message"]


def test_auto_arbitrary_unrecognized_text(client, phone_headers):
    payload = {
        "input_type": "auto",
        "text": "西条町寺家",
        "target_device_id": "test_device_auto_12",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert "未能识别有效目的地" in data["message"]


def test_auto_empty_text_raises_400(client, phone_headers):
    payload = {
        "input_type": "auto",
        "text": "   ",
    }
    response = client.post("/destinations", json=payload, headers=phone_headers)
    assert response.status_code == 400
