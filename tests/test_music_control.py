import pytest
from app.config import settings


def test_music_command_send_and_poll(client, phone_headers, car_headers):
    # 1. iPhone issues play_track command
    cmd_payload = {
        "action": "play_track",
        "params": {
            "track_id": "test_track_001",
            "stream_url": "http://127.0.0.1:8000/api/v1/music/stream/test_track_001",
            "title": "Song Title",
            "artist": "Artist Name",
            "seek_ms": 0,
            "loop_mode": "all",
            "shuffle": False,
        },
    }
    resp = client.post("/api/v1/music/control/command", json=cmd_payload, headers=phone_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["action"] == "play_track"
    assert data["command_id"].startswith("cmd_music_")
    assert data["device_id"] == settings.DEFAULT_CAR_DEVICE_ID
    assert data["params"]["title"] == "Song Title"

    # 2. Car pulls the pending command via GET /api/v1/music/control/command/next
    poll_resp = client.get("/api/v1/music/control/command/next", headers=car_headers)
    assert poll_resp.status_code == 200
    polled_cmd = poll_resp.json()
    assert polled_cmd["command_id"] == data["command_id"]
    assert polled_cmd["action"] == "play_track"
    assert polled_cmd["params"]["track_id"] == "test_track_001"

    # 3. Next poll on empty queue without timeout returns 204
    empty_resp = client.get("/api/v1/music/control/command/next", headers=car_headers)
    assert empty_resp.status_code == 204


def test_invalid_music_command(client, phone_headers):
    # Invalid action should be rejected
    resp = client.post(
        "/api/v1/music/control/command",
        json={"action": "invalid_unknown_action"},
        headers=phone_headers,
    )
    assert resp.status_code == 400
    assert "Invalid action" in resp.json()["detail"]


def test_car_status_report_and_query(client, car_headers, phone_headers):
    # 1. Query status before any report - should return default stopped state
    query_resp = client.get(
        f"/api/v1/music/control/status?device_id=new_device_test",
        headers=phone_headers,
    )
    assert query_resp.status_code == 200
    assert query_resp.json()["playback_state"] == "stopped"
    assert query_resp.json()["position_ms"] == 0

    # 2. Car reports active playback status
    status_report = {
        "device_id": settings.DEFAULT_CAR_DEVICE_ID,
        "playback_state": "playing",
        "current_track_id": "track_12345",
        "position_ms": 35200,
        "duration_ms": 210000,
        "software_volume": 0.75,
        "loop_mode": "one",
        "shuffle": True,
    }
    report_resp = client.post(
        "/api/v1/music/control/status",
        json=status_report,
        headers=car_headers,
    )
    assert report_resp.status_code == 200
    rep_data = report_resp.json()
    assert rep_data["status"] == "ok"
    assert rep_data["playback_state"] == "playing"
    assert rep_data["position_ms"] == 35200

    # 3. iPhone queries updated status
    get_resp = client.get(
        f"/api/v1/music/control/status?device_id={settings.DEFAULT_CAR_DEVICE_ID}",
        headers=phone_headers,
    )
    assert get_resp.status_code == 200
    snapshot = get_resp.json()
    assert snapshot["device_id"] == settings.DEFAULT_CAR_DEVICE_ID
    assert snapshot["playback_state"] == "playing"
    assert snapshot["current_track_id"] == "track_12345"
    assert snapshot["position_ms"] == 35200
    assert snapshot["duration_ms"] == 210000
    assert snapshot["software_volume"] == 0.75
    assert snapshot["loop_mode"] == "one"
    assert snapshot["shuffle_enabled"] is True


def test_auth_and_x_device_token(client):
    # 1. Unauthenticated request rejected
    resp = client.get("/api/v1/music/control/status")
    assert resp.status_code == 401

    # 2. X-Device-Token header authentication works
    x_headers = {"X-Device-Token": settings.PHONE_TOKEN}
    resp_x = client.get("/api/v1/music/control/status", headers=x_headers)
    assert resp_x.status_code == 200

    # 3. Bound car controls enter the same HTTP pipeline as the phone
    car_x_headers = {"X-Device-Token": settings.CAR_TOKEN}
    cmd_resp = client.post(
        "/api/v1/music/control/command",
        json={"action": "pause"},
        headers=car_x_headers,
    )
    assert cmd_resp.status_code == 200
    cross_device = client.post("/api/v1/music/control/command",
        json={"action": "pause", "device_id": "other_vehicle"}, headers=car_x_headers)
    assert cross_device.status_code == 403


def test_websocket_broadcast_and_dispatch(client):
    # 1. Test WebSocket rejection with invalid token
    with pytest.raises(Exception):
        with client.websocket_connect("/api/v1/ws/music?token=invalid_token"):
            pass

    # 2. Connect phone WebSocket client
    with client.websocket_connect(f"/api/v1/ws/music?token={settings.PHONE_TOKEN}&client_type=phone") as phone_ws:
        # First message received is the initial status snapshot
        snap = phone_ws.receive_json()
        assert snap["type"] == "status_snapshot"
        assert "data" in snap

        # Test ping / pong over WebSocket
        phone_ws.send_json({"type": "ping"})
        pong = phone_ws.receive_json()
        assert pong["type"] == "pong"

        # Now connect car WebSocket client
        with client.websocket_connect(
            f"/api/v1/ws/music?token={settings.CAR_TOKEN}&client_type=car&device_id={settings.DEFAULT_CAR_DEVICE_ID}"
        ) as car_ws:
            car_snap = car_ws.receive_json()
            assert car_snap["type"] == "status_snapshot"

            # 3. iPhone sends a seek command via HTTP
            cmd_resp = client.post(
                "/api/v1/music/control/command",
                json={
                    "action": "seek",
                    "params": {"seek_ms": 64000},
                },
                headers={"Authorization": f"Bearer {settings.PHONE_TOKEN}"},
            )
            assert cmd_resp.status_code == 200
            # Since car_ws is connected, dispatched_to_ws must be True!
            assert cmd_resp.json()["dispatched_to_ws"] is True

            # Car WebSocket must receive the music_command instantly!
            car_cmd = car_ws.receive_json()
            assert car_cmd["type"] == "music_command"
            assert car_cmd["data"]["action"] == "seek"
            assert car_cmd["data"]["params"]["seek_ms"] == 64000

            # 4. Car updates status via HTTP
            status_update = {
                "device_id": settings.DEFAULT_CAR_DEVICE_ID,
                "playback_state": "paused",
                "position_ms": 64000,
            }
            client.post(
                "/api/v1/music/control/status",
                json=status_update,
                headers={"Authorization": f"Bearer {settings.CAR_TOKEN}"},
            )

            # iPhone WebSocket must receive the status_update broadcast!
            broadcast_msg = phone_ws.receive_json()
            assert broadcast_msg["type"] == "status_update"
            assert broadcast_msg["data"]["playback_state"] == "paused"
            assert broadcast_msg["data"]["position_ms"] == 64000
