import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import settings
from app.services.netease_service import netease_service
from scripts.backup_db import backup_database


def test_netease_qr_login_flow(client, phone_headers, tmp_path):
    # Use isolated cookie path for test
    test_cookie_file = tmp_path / "test_cookie.json"
    netease_service.cookie_store.path = test_cookie_file

    # 1. Get QR Key
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"data": {"unikey": "test-unikey-123"}}
        mock_get.return_value = mock_resp

        key_resp = client.get("/api/v1/music/netease/login/qr/key", headers=phone_headers)
        assert key_resp.status_code == 200
        assert key_resp.json()["unikey"] == "test-unikey-123"

    # 2. Create QR image
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"qrurl": "https://music.163.com/login?codekey=123", "qrimg": "data:image/png;base64,abc"},
        }
        mock_get.return_value = mock_resp

        create_resp = client.get("/api/v1/music/netease/login/qr/create?key=test-unikey-123", headers=phone_headers)
        assert create_resp.status_code == 200
        assert create_resp.json()["qrurl"] == "https://music.163.com/login?codekey=123"

    # 3. Check QR - 801 waiting scan
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"code": 801, "message": "等待扫码"}
        mock_get.return_value = mock_resp

        check_resp = client.get("/api/v1/music/netease/login/qr/check?key=test-unikey-123", headers=phone_headers)
        assert check_resp.status_code == 200
        assert check_resp.json()["code"] == 801
        assert check_resp.json()["status"] == "waiting_scan"
        assert check_resp.json()["cookie_saved"] is False

    # 4. Check QR - 803 success
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock(status_code=200)
        # Mock check returning 803 with cookie
        mock_resp.json.side_effect = [
            {"code": 803, "cookie": "MUSIC_U=test_session_token_xyz;"},
            {"code": 200, "data": {"profile": {"userId": 123456, "nickname": "TestUser", "vipType": 11}}},
        ]
        mock_get.return_value = mock_resp

        auth_resp = client.get("/api/v1/music/netease/login/qr/check?key=test-unikey-123", headers=phone_headers)
        assert auth_resp.status_code == 200
        assert auth_resp.json()["code"] == 803
        assert auth_resp.json()["cookie_saved"] is True
        assert auth_resp.json()["nickname"] == "TestUser"
        assert test_cookie_file.exists()

    # 5. Check Login Status
    status_resp = client.get("/api/v1/music/netease/login/status", headers=phone_headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["logged_in"] is True
    assert status_resp.json()["nickname"] == "TestUser"

    # 6. Logout
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock(status_code=200)
        mock_get.return_value = mock_resp

        logout_resp = client.post("/api/v1/music/netease/login/logout", headers=phone_headers)
        assert logout_resp.status_code == 200
        assert logout_resp.json()["logged_out"] is True
        assert not test_cookie_file.exists()

    # 7. Direct Cookie Import Login
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"profile": {"userId": 99999, "nickname": "DirectVipUser", "vipType": 11}},
        }
        mock_get.return_value = mock_resp

        cookie_resp = client.post(
            "/api/v1/music/netease/login/cookie",
            json={"cookie": "MUSIC_U=direct_vip_token;"},
            headers=phone_headers,
        )
        assert cookie_resp.status_code == 200
        assert cookie_resp.json()["logged_in"] is True
        assert cookie_resp.json()["nickname"] == "DirectVipUser"
        assert test_cookie_file.exists()



from app.providers.base import PlaceCandidateItem
from app.providers.google_maps_parser import MapUrlResolutionResult


def test_navigation_api_v1_endpoints(client, phone_headers, car_headers):
    # 1. Resolve destination via POST /api/v1/destinations/resolve
    mock_cand = PlaceCandidateItem(
        title="秋叶原电车站",
        address="Tokyo, Japan",
        latitude=35.698383,
        longitude=139.773072,
        coord_system="wgs84",
        source="google_maps_link",
    )
    resolve_payload = {
        "raw_input": "https://maps.app.goo.gl/EXAMPLE_dest_goal4",
        "source": "ios_share",
    }
    with patch("app.services.destination_service.resolve_and_parse_map_url", new_callable=AsyncMock) as mock_resolve:
        mock_resolve.return_value = MapUrlResolutionResult(candidate=mock_cand, resolved_url="https://maps.google.com/test")
        res = client.post("/api/v1/destinations/resolve", json=resolve_payload, headers=phone_headers)

    assert res.status_code == 200
    res_data = res.json()
    assert res_data["code"] == 200
    assert "destination_id" in res_data
    dest_id = res_data["destination_id"]
    assert res_data["latitude"] == 35.698383
    assert res_data["name"] == "秋叶原电车站"

    # 2. Dispatch task via POST /api/v1/tasks/destination
    task_payload = {
        "destination_id": dest_id,
        "expires_in_seconds": 3600,
        "target_device_id": "car_p3pro",
    }
    task_res = client.post("/api/v1/tasks/destination", json=task_payload, headers=phone_headers)
    assert task_res.status_code == 200
    task_data = task_res.json()
    task_id = task_data["task_id"]
    assert task_data["status"] == "queued"

    # 3. Car polls task via GET /api/v1/tasks/poll
    poll_res = client.get("/api/v1/tasks/poll?device_id=car_p3pro", headers=car_headers)
    assert poll_res.status_code == 200
    polled_task = poll_res.json()
    assert polled_task["task_id"] == task_id

    # 4. Car confirms via POST /api/v1/tasks/{task_id}/ack
    ack_payload = {
        "phase": "launch_requested",
        "device_timestamp": "2026-10-12T12:00:00Z",
    }
    ack_res = client.post(f"/api/v1/tasks/{task_id}/ack", json=ack_payload, headers=car_headers)
    assert ack_res.status_code == 200
    assert ack_res.json()["accepted"] is True
    assert ack_res.json()["status"] == "launch_requested"


def test_sqlite_backup_functionality(tmp_path):
    backup_path_str = backup_database(retention_days=7)
    assert backup_path_str != ""
    backup_path = Path(backup_path_str)
    assert backup_path.exists()
    assert backup_path.stat().st_size > 0
