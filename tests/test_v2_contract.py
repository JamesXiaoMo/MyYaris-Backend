import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.config import settings
from app.repositories.models import ParseRequest, Task


def test_resolve_and_dispatch_separation(client: TestClient, phone_headers: dict):
    # 1. Pure resolution via POST /api/v1/destinations/resolve with coordinate input
    resolve_resp = client.post(
        "/api/v1/destinations/resolve",
        json={"raw_input": "35.6812, 139.7671", "source": "ios_share"},
        headers=phone_headers,
    )
    assert resolve_resp.status_code == 200
    res_data = resolve_resp.json()
    assert res_data["code"] == 200
    dest_id = res_data["destination_id"]
    assert dest_id is not None
    assert abs(res_data["latitude"] - 35.6812) < 1e-4
    assert abs(res_data["longitude"] - 139.7671) < 1e-4

    # Verify NO task was created eagerly during resolution
    query_resp = client.get(f"/destinations/{dest_id}", headers=phone_headers)
    assert query_resp.status_code == 200
    assert query_resp.json()["task_id"] is None

    # 2. Formal dispatch via POST /api/v1/tasks/destination with alias target_device_id
    dispatch_resp = client.post(
        "/api/v1/tasks/destination",
        json={
            "destination_id": dest_id,
            "target_device_id": "ottocast_p3_pro",
            "expires_in_seconds": 3600,
        },
        headers=phone_headers,
    )
    assert dispatch_resp.status_code == 200
    task_data = dispatch_resp.json()
    assert task_data["task_id"] is not None
    # Alias 'ottocast_p3_pro' must be normalized to canonical 'car_p3pro'
    assert task_data["device_id"] == "car_p3pro"
    assert task_data["status"] == "queued"

    # Now verify request has task_id
    query_after = client.get(f"/destinations/{dest_id}", headers=phone_headers)
    assert query_after.json()["task_id"] == task_data["task_id"]


def test_resolve_share_link_no_eager_task(client: TestClient, phone_headers: dict):
    # Pure resolution of a Google Maps pin URL should not create task eagerly
    url = "https://maps.google.com/?q=35.6585,139.7454"
    resolve_resp = client.post(
        "/api/v1/destinations/resolve",
        json={"raw_input": url, "source": "ios_share"},
        headers=phone_headers,
    )
    assert resolve_resp.status_code == 200
    dest_id = resolve_resp.json()["destination_id"]

    query_resp = client.get(f"/destinations/{dest_id}", headers=phone_headers)
    assert query_resp.status_code == 200
    assert query_resp.json()["task_id"] is None


def test_legacy_destinations_still_auto_creates_task(client: TestClient, phone_headers: dict):
    # Legacy POST /destinations (used by Siri Shortcuts) must still auto-create task
    resp = client.post(
        "/destinations",
        json={
            "input_type": "auto",
            "text": "35.6812, 139.7671",
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved"
    assert data["task_id"] is not None


def test_device_id_alias_normalization_in_tasks_poll_and_ack(
    client: TestClient, phone_headers: dict, car_headers: dict
):
    # 1. Create a task for canonical car_p3pro
    create_resp = client.post(
        "/destinations",
        json={
            "input_type": "auto",
            "text": "35.6895, 139.6917",
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    task_id = create_resp.json()["task_id"]

    # 2. Car polls using alias 'ottocast_p3_pro' on /devices/{id}/tasks/next
    poll_alias_resp = client.get(
        "/devices/ottocast_p3_pro/tasks/next?timeout=1",
        headers=car_headers,
    )
    assert poll_alias_resp.status_code == 200
    polled_task = poll_alias_resp.json()
    assert polled_task["task_id"] == task_id
    assert polled_task["device_id"] == "car_p3pro"

    # 3. Car acknowledges using /api/v1/tasks/{id}/ack
    ack_resp = client.post(
        f"/api/v1/tasks/{task_id}/ack",
        json={"phase": "received"},
        headers=car_headers,
    )
    assert ack_resp.status_code == 200
    assert ack_resp.json()["accepted"] is True
    assert ack_resp.json()["status"] == "received"

    # 4. Car acknowledges phase launch_requested using legacy /tasks/{id}/ack
    ack2_resp = client.post(
        f"/tasks/{task_id}/ack",
        json={"phase": "launch_requested"},
        headers=car_headers,
    )
    assert ack2_resp.status_code == 200
    assert ack2_resp.json()["status"] == "launch_requested"

    # 5. Car views task using GET /tasks/{id}
    get_task_resp = client.get(f"/tasks/{task_id}", headers=car_headers)
    assert get_task_resp.status_code == 200
    assert get_task_resp.json()["task_id"] == task_id


def test_websocket_dual_envelope_status_validation_and_broadcast(client: TestClient):
    # Test phone WebSocket connection and car flat status update handling
    with client.websocket_connect(
        f"/api/v1/ws/music?token={settings.PHONE_TOKEN}&client_type=phone&device_id=car_p3pro"
    ) as phone_ws:
        # Initial snapshot received by phone
        snapshot = phone_ws.receive_json()
        assert snapshot["type"] == "status_snapshot"
        assert "data" in snapshot
        assert snapshot["data"]["device_id"] == "car_p3pro"
        assert "playback_state" in snapshot  # root compatibility field

        # Now car connects and sends flat status update (how car_app actually formats it)
        with client.websocket_connect(
            f"/api/v1/ws/music?token={settings.CAR_TOKEN}&client_type=car&device_id=ottocast_p3_pro"
        ) as car_ws:
            car_ws.receive_json()  # discard car's initial snapshot

            # Flat payload sent by car_app
            car_status_msg = {
                "type": "status_update",
                "device_id": "ottocast_p3_pro",
                "playback_state": "playing",
                "current_track_id": "track_test_123",
                "title": "Dual Envelope Song",
                "artist": "Artist Test",
                "position_ms": 45000,
                "duration_ms": 180000,
                "software_volume": 0.8,
                "loop_mode": "all",
                "shuffle": False,
            }
            car_ws.send_json(car_status_msg)

            # Phone receives broadcast with dual envelope
            update = phone_ws.receive_json()
            assert update["type"] == "status_update"
            assert "data" in update
            assert update["data"]["playback_state"] == "playing"
            assert update["data"]["current_track_id"] == "track_test_123"
            assert update["data"]["position_ms"] == 45000
            # Root fields also populated for backward compatibility
            assert update["playback_state"] == "playing"

            # Safety test: Car sends empty/invalid status update
            car_ws.send_json({"type": "status_update", "data": {}})
            # Existing database record must NOT be wiped to stopped/0!
            # Query status via HTTP:
            http_status = client.get(
                "/api/v1/music/control/status?device_id=car_p3pro",
                headers={"Authorization": f"Bearer {settings.PHONE_TOKEN}"},
            )
            assert http_status.status_code == 200
            assert http_status.json()["playback_state"] == "playing"
            assert http_status.json()["current_track_id"] == "track_test_123"
            assert http_status.json()["position_ms"] == 45000


def test_command_deduplication_between_ws_and_http_channels(
    client: TestClient, phone_headers: dict, car_headers: dict
):
    # When car is connected over WebSocket, commands delivered via WS are not duplicated to HTTP poll
    with client.websocket_connect(
        f"/api/v1/ws/music?token={settings.CAR_TOKEN}&client_type=car&device_id=car_p3pro"
    ) as car_ws:
        car_ws.receive_json()  # snapshot

        # Phone sends command via HTTP POST with explicit command_id
        cmd_id = "cmd_unique_dedup_001"
        cmd_resp = client.post(
            "/api/v1/music/control/command",
            json={
                "command_id": cmd_id,
                "device_id": "car_p3pro",
                "action": "pause",
            },
            headers=phone_headers,
        )
        assert cmd_resp.status_code == 200
        assert cmd_resp.json()["dispatched_to_ws"] is True

        # Car receives command over WebSocket
        car_msg = car_ws.receive_json()
        assert car_msg["type"] == "music_command"
        assert car_msg["action"] == "pause"
        assert car_msg["command_id"] == cmd_id
        assert "data" in car_msg  # dual envelope

        # Now car attempts to poll HTTP /api/v1/music/control/command/next
        # It must NOT receive the already WS-delivered command
        http_poll = client.get(
            "/api/v1/music/control/command/next?timeout=1",
            headers=car_headers,
        )
        assert http_poll.status_code == 204
