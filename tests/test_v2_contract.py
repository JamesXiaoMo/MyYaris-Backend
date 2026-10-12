import asyncio
import datetime
import json
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.config import settings
from app.repositories.models import MusicCommand, ParseRequest, Task, utc_now
from app.services.control_service import command_queue_manager


@pytest.fixture(autouse=True)
def clean_queue_state(db_session):
    command_queue_manager.clear_all()
    db_session.query(MusicCommand).delete()
    db_session.commit()
    yield
    command_queue_manager.clear_all()
    db_session.query(MusicCommand).delete()
    db_session.commit()


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
            http_status = client.get(
                "/api/v1/music/control/status?device_id=car_p3pro",
                headers={"Authorization": f"Bearer {settings.PHONE_TOKEN}"},
            )
            assert http_status.status_code == 200
            assert http_status.json()["playback_state"] == "playing"
            assert http_status.json()["current_track_id"] == "track_test_123"
            assert http_status.json()["position_ms"] == 45000


def test_command_lifecycle_and_car_ack_http(client: TestClient, phone_headers: dict, car_headers: dict):
    # 1. Phone dispatches command via HTTP
    cmd_id = "cmd_lifecycle_001"
    post_resp = client.post(
        "/api/v1/music/control/command",
        json={
            "command_id": cmd_id,
            "device_id": "car_p3pro",
            "action": "pause",
        },
        headers=phone_headers,
    )
    assert post_resp.status_code == 200
    res_data = post_resp.json()
    assert res_data["command_id"] == cmd_id
    assert res_data["lifecycle_status"] == "queued"

    # 2. Query command detail
    detail_resp = client.get(
        f"/api/v1/music/control/command/{cmd_id}",
        headers=phone_headers,
    )
    assert detail_resp.status_code == 200
    detail_data = detail_resp.json()
    assert detail_data["command_id"] == cmd_id
    assert detail_data["action"] == "pause"
    assert detail_data["status"] == "queued"
    assert detail_data["acknowledged_at"] is None

    # 3. Car acknowledges execution via HTTP POST /ack
    ack_resp = client.post(
        f"/api/v1/music/control/command/{cmd_id}/ack",
        json={"status": "applied"},
        headers=car_headers,
    )
    assert ack_resp.status_code == 200
    assert ack_resp.json()["status"] == "applied"

    # 4. Query detail after acknowledgement
    detail_after = client.get(
        f"/api/v1/music/control/command/{cmd_id}",
        headers=phone_headers,
    )
    assert detail_after.status_code == 200
    assert detail_after.json()["status"] == "applied"
    assert detail_after.json()["acknowledged_at"] is not None


def test_command_lifecycle_car_failure_report(client: TestClient, phone_headers: dict, car_headers: dict):
    cmd_id = "cmd_fail_001"
    client.post(
        "/api/v1/music/control/command",
        json={
            "command_id": cmd_id,
            "device_id": "car_p3pro",
            "action": "play_track",
            "params": {"track_id": "invalid_track_id"},
        },
        headers=phone_headers,
    )

    # Car acknowledges execution failure with error details
    ack_resp = client.post(
        f"/api/v1/music/control/command/{cmd_id}/ack",
        json={
            "status": "failed",
            "error_code": "TRACK_DECODE_ERROR",
            "error_detail": "Unsupported codec format",
        },
        headers=car_headers,
    )
    assert ack_resp.status_code == 200
    assert ack_resp.json()["status"] == "failed"

    # Query command status
    detail_resp = client.get(
        f"/api/v1/music/control/command/{cmd_id}",
        headers=phone_headers,
    )
    assert detail_resp.status_code == 200
    data = detail_resp.json()
    assert data["status"] == "failed"
    assert data["error_code"] == "TRACK_DECODE_ERROR"
    assert data["error_detail"] == "Unsupported codec format"


def test_command_idempotency_and_collision_conflict(client: TestClient, phone_headers: dict):
    cmd_id = "cmd_idem_test_99"

    # 1. First submission
    resp1 = client.post(
        "/api/v1/music/control/command",
        json={"command_id": cmd_id, "device_id": "car_p3pro", "action": "resume"},
        headers=phone_headers,
    )
    assert resp1.status_code == 200

    # 2. Idempotent re-submission with identical action and params -> succeeds (200 OK)
    resp2 = client.post(
        "/api/v1/music/control/command",
        json={"command_id": cmd_id, "device_id": "car_p3pro", "action": "resume"},
        headers=phone_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["command_id"] == cmd_id

    # 3. Conflict: same command_id with DIFFERENT action -> 409 Conflict
    resp3 = client.post(
        "/api/v1/music/control/command",
        json={"command_id": cmd_id, "device_id": "car_p3pro", "action": "next"},
        headers=phone_headers,
    )
    assert resp3.status_code == 409

    # 4. Conflict: same command_id for DIFFERENT device -> 409 Conflict
    resp4 = client.post(
        "/api/v1/music/control/command",
        json={"command_id": cmd_id, "device_id": "car_other_device", "action": "resume"},
        headers=phone_headers,
    )
    assert resp4.status_code == 409


def test_command_cross_device_ack_forbidden(client: TestClient, phone_headers: dict, monkeypatch):
    cmd_id = "cmd_cross_device_01"
    client.post(
        "/api/v1/music/control/command",
        json={"command_id": cmd_id, "device_id": "car_p3pro", "action": "pause"},
        headers=phone_headers,
    )

    # Attempt to acknowledge using a car token from a different device
    # Emulate an attacker/other car client with device_id="other_car_999"
    from app.schemas.auth import AuthenticatedUser
    from app.api import auth

    def mock_require_car_other():
        return AuthenticatedUser(identity="car_other", role="car", device_id="other_car_999")

    from app.main import app
    app.dependency_overrides[auth.require_car_or_admin] = mock_require_car_other

    try:
        ack_resp = client.post(
            f"/api/v1/music/control/command/{cmd_id}/ack",
            json={"status": "applied"},
        )
        assert ack_resp.status_code == 403
        assert "cannot acknowledge commands for device" in ack_resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(auth.require_car_or_admin, None)


def test_command_ws_send_drop_recovered_by_http(
    client: TestClient, phone_headers: dict, car_headers: dict
):
    # Tests the failure mode: WS send succeeded at transport level, but car disconnected before acknowledging.
    # The command MUST NOT be lost and MUST be recoverable via HTTP long polling!
    with client.websocket_connect(
        f"/api/v1/ws/music?token={settings.CAR_TOKEN}&client_type=car&device_id=car_p3pro"
    ) as car_ws:
        car_ws.receive_json()  # snapshot

        cmd_id = "cmd_ws_drop_recovery_01"
        # Phone sends command while car is connected to WS
        post_resp = client.post(
            "/api/v1/music/control/command",
            json={"command_id": cmd_id, "device_id": "car_p3pro", "action": "next"},
            headers=phone_headers,
        )
        assert post_resp.status_code == 200
        assert post_resp.json()["dispatched_to_ws"] is True

        # Car receives over WS
        ws_msg = car_ws.receive_json()
        assert ws_msg["command_id"] == cmd_id

    # Now the WebSocket has DROPPED and car did NOT send an ACK!
    # Car falls back to HTTP polling:
    http_poll = client.get(
        "/api/v1/music/control/command/next?timeout=2",
        headers=car_headers,
    )
    assert http_poll.status_code == 200
    polled_cmd = http_poll.json()
    assert polled_cmd["command_id"] == cmd_id
    assert polled_cmd["action"] == "next"

    # Car acknowledges via HTTP
    ack_resp = client.post(
        f"/api/v1/music/control/command/{cmd_id}/ack",
        json={"status": "applied"},
        headers=car_headers,
    )
    assert ack_resp.status_code == 200


def test_command_ttl_expiration_no_stale_replay(client: TestClient, phone_headers: dict, car_headers: dict, db_session):
    # Transient seek command with short TTL
    cmd_id = "cmd_stale_seek_001"
    post_resp = client.post(
        "/api/v1/music/control/command",
        json={
            "command_id": cmd_id,
            "device_id": "car_p3pro",
            "action": "seek",
            "params": {"seek_ms": 99000},
            "expires_in_seconds": 5,
        },
        headers=phone_headers,
    )
    assert post_resp.status_code == 200

    # Simulate time elapsed past expires_at in both queue and SQLite DB
    from app.services.control_service import command_queue_manager
    # Pull from queue and expire the timestamp
    queue = asyncio.run(command_queue_manager._get_or_create_queue("car_p3pro"))
    cmd = queue.get_nowait()
    cmd["expires_at"] = (utc_now() - datetime.timedelta(seconds=10)).isoformat()
    queue.put_nowait(cmd)

    db_session.query(MusicCommand).filter(MusicCommand.command_id == cmd_id).update({
        "expires_at": utc_now() - datetime.timedelta(seconds=10)
    })
    db_session.commit()

    # Car polls HTTP: expired seek command must be skipped and NOT returned
    poll_resp = client.get(
        "/api/v1/music/control/command/next?timeout=1",
        headers=car_headers,
    )
    assert poll_resp.status_code == 204


def test_websocket_car_ack_broadcasts_to_phone(client: TestClient, phone_headers: dict):
    with client.websocket_connect(
        f"/api/v1/ws/music?token={settings.PHONE_TOKEN}&client_type=phone&device_id=car_p3pro"
    ) as phone_ws:
        phone_ws.receive_json()  # snapshot

        with client.websocket_connect(
            f"/api/v1/ws/music?token={settings.CAR_TOKEN}&client_type=car&device_id=car_p3pro"
        ) as car_ws:
            car_ws.receive_json()  # snapshot

            cmd_id = "cmd_ws_ack_broadcast_01"
            # Phone sends command via HTTP
            client.post(
                "/api/v1/music/control/command",
                json={"command_id": cmd_id, "device_id": "car_p3pro", "action": "resume"},
                headers=phone_headers,
            )
            # Car gets command
            car_msg = car_ws.receive_json()
            assert car_msg["command_id"] == cmd_id

            # Car sends command_ack over WebSocket
            car_ws.send_json({
                "type": "command_ack",
                "command_id": cmd_id,
                "status": "applied",
            })

            # Phone receives broadcast of command execution!
            phone_status_msg = phone_ws.receive_json()
            assert phone_status_msg["type"] == "command_status"
            assert phone_status_msg["command_id"] == cmd_id
            assert phone_status_msg["status"] == "applied"
            assert "data" in phone_status_msg


def test_ws_and_http_cross_entry_idempotency_and_conflict(client: TestClient, phone_headers: dict):
    # Tests that both WS and HTTP entrypoints perform identical idempotency and conflict checks:
    # 1. WS submit -> car receives once.
    # 2. WS duplicate submit -> duplicate ACK to phone, car DOES NOT receive second dispatch.
    # 3. HTTP submit with same ID & same payload -> 200 OK idempotent return.
    # 4. HTTP submit with same ID & different payload -> 409 Conflict.
    # 5. HTTP new submit -> then WS submit with different payload -> WS receives conflict ACK.
    with client.websocket_connect(
        f"/api/v1/ws/music?token={settings.PHONE_TOKEN}&client_type=phone&device_id=car_p3pro"
    ) as phone_ws:
        phone_ws.receive_json()  # snapshot

        with client.websocket_connect(
            f"/api/v1/ws/music?token={settings.CAR_TOKEN}&client_type=car&device_id=car_p3pro"
        ) as car_ws:
            car_ws.receive_json()  # snapshot

            cmd_ws_id = "cmd_cross_proto_idem_01"
            # 1. WS dispatches command
            phone_ws.send_json({
                "type": "command",
                "data": {
                    "command_id": cmd_ws_id,
                    "device_id": "car_p3pro",
                    "action": "pause",
                },
            })
            # Car receives
            car_msg = car_ws.receive_json()
            assert car_msg["command_id"] == cmd_ws_id
            # Phone receives dispatched ack
            phone_ack1 = phone_ws.receive_json()
            assert phone_ack1["type"] == "command_ack"
            assert phone_ack1["command_id"] == cmd_ws_id
            assert phone_ack1["status"] == "dispatched"

            # 2. Duplicate WS command with identical payload
            phone_ws.send_json({
                "type": "command",
                "data": {
                    "command_id": cmd_ws_id,
                    "device_id": "car_p3pro",
                    "action": "pause",
                },
            })
            phone_ack2 = phone_ws.receive_json()
            assert phone_ack2["type"] == "command_ack"
            assert phone_ack2["command_id"] == cmd_ws_id
            assert phone_ack2.get("duplicate") is True

            # 3. HTTP submit with same ID & same payload -> 200 OK idempotent
            http_dup_resp = client.post(
                "/api/v1/music/control/command",
                json={"command_id": cmd_ws_id, "device_id": "car_p3pro", "action": "pause"},
                headers=phone_headers,
            )
            assert http_dup_resp.status_code == 200
            assert http_dup_resp.json()["command_id"] == cmd_ws_id

            # 4. HTTP submit with same ID & DIFFERENT payload -> 409 Conflict
            http_conflict_resp = client.post(
                "/api/v1/music/control/command",
                json={"command_id": cmd_ws_id, "device_id": "car_p3pro", "action": "next"},
                headers=phone_headers,
            )
            assert http_conflict_resp.status_code == 409

            # 5. HTTP new submit -> then WS duplicate and conflict
            cmd_http_id = "cmd_cross_proto_idem_02"
            http_new_resp = client.post(
                "/api/v1/music/control/command",
                json={"command_id": cmd_http_id, "device_id": "car_p3pro", "action": "resume"},
                headers=phone_headers,
            )
            assert http_new_resp.status_code == 200

            # WS tries with same ID but different payload -> WS conflict error
            phone_ws.send_json({
                "type": "command",
                "data": {
                    "command_id": cmd_http_id,
                    "device_id": "car_p3pro",
                    "action": "stop",
                },
            })
            phone_ack3 = phone_ws.receive_json()
            assert phone_ack3["type"] == "command_ack"
            assert phone_ack3["command_id"] == cmd_http_id
            assert phone_ack3["status"] == "conflict"


def test_server_restart_queue_manager_sqlite_recovery(
    client: TestClient, phone_headers: dict, car_headers: dict
):
    # Tests that when the server restarts (simulated by completely re-instantiating MusicCommandQueueManager),
    # unacknowledged commands in SQLite are NOT lost and are recovered for HTTP consumption.
    cmd_id = "cmd_recovery_restart_01"
    # 1. Enqueue command via HTTP
    post_resp = client.post(
        "/api/v1/music/control/command",
        json={"command_id": cmd_id, "device_id": "car_p3pro", "action": "next"},
        headers=phone_headers,
    )
    assert post_resp.status_code == 200

    # 2. Simulate server crash/reboot: Re-instantiate a clean MusicCommandQueueManager
    from app.services.control_service import MusicCommandQueueManager
    from app.repositories.database import SessionLocal
    fresh_manager = MusicCommandQueueManager()

    # Verify fresh memory state is empty
    assert len(fresh_manager._queues) == 0
    assert not fresh_manager.is_acknowledged(cmd_id)

    # 3. Recover from database
    with SessionLocal() as db:
        asyncio.run(fresh_manager.recover_from_db(db, device_id="car_p3pro"))

        # Verify command is restored into memory queue and can be polled
        recovered_cmd = asyncio.run(fresh_manager.get_next_command("car_p3pro", timeout_seconds=1, db=db))
        assert recovered_cmd is not None
        assert recovered_cmd["command_id"] == cmd_id
        assert recovered_cmd["action"] == "next"

    # 4. Verify HTTP polling on live endpoint also recovers and consumes via get_next_music_command
    # Clear in-memory singleton to simulate server process restart
    command_queue_manager.clear_all()
    poll_resp = client.get(
        "/api/v1/music/control/command/next?timeout=1",
        headers=car_headers,
    )
    assert poll_resp.status_code == 200
    polled_data = poll_resp.json()
    assert polled_data["command_id"] == cmd_id


def test_terminal_ack_state_protection_and_received_non_terminal(
    client: TestClient, phone_headers: dict, car_headers: dict
):
    # Tests:
    # 1. 'received' is an intermediate phase, NOT execution completion. Queue manager does NOT evict/mark it acknowledged.
    # 2. 'applied' and 'failed' are terminal states: immutable and idempotent (cannot rollback to received or toggle).
    cmd_id1 = "cmd_term_test_01"
    client.post(
        "/api/v1/music/control/command",
        json={"command_id": cmd_id1, "device_id": "car_p3pro", "action": "pause"},
        headers=phone_headers,
    )

    # 1. Car sends received
    ack_recv = client.post(
        f"/api/v1/music/control/command/{cmd_id1}/ack",
        json={"status": "received"},
        headers=car_headers,
    )
    assert ack_recv.status_code == 200
    assert ack_recv.json()["status"] == "received"
    # Must NOT be marked as acknowledged
    assert command_queue_manager.is_acknowledged(cmd_id1) is False

    # 2. Car sends applied -> becomes terminal applied
    ack_app = client.post(
        f"/api/v1/music/control/command/{cmd_id1}/ack",
        json={"status": "applied"},
        headers=car_headers,
    )
    assert ack_app.status_code == 200
    assert ack_app.json()["status"] == "applied"
    assert command_queue_manager.is_acknowledged(cmd_id1) is True

    # 3. Retrying ACK with 'received' or 'failed' cannot regress terminal 'applied'
    ack_re_recv = client.post(
        f"/api/v1/music/control/command/{cmd_id1}/ack",
        json={"status": "received"},
        headers=car_headers,
    )
    assert ack_re_recv.status_code == 200
    assert ack_re_recv.json()["status"] == "applied"  # Kept applied!

    ack_re_fail = client.post(
        f"/api/v1/music/control/command/{cmd_id1}/ack",
        json={"status": "failed", "error_code": "LATE_ERR"},
        headers=car_headers,
    )
    assert ack_re_fail.status_code == 200
    assert ack_re_fail.json()["status"] == "applied"  # Kept applied!

    # 4. Reverse terminal test: once failed, cannot regress to applied or received
    cmd_id2 = "cmd_term_test_02"
    client.post(
        "/api/v1/music/control/command",
        json={"command_id": cmd_id2, "device_id": "car_p3pro", "action": "play_track"},
        headers=phone_headers,
    )
    ack_fail = client.post(
        f"/api/v1/music/control/command/{cmd_id2}/ack",
        json={"status": "failed", "error_code": "ERR_PLAY"},
        headers=car_headers,
    )
    assert ack_fail.status_code == 200
    assert ack_fail.json()["status"] == "failed"

    ack_fail_to_app = client.post(
        f"/api/v1/music/control/command/{cmd_id2}/ack",
        json={"status": "applied"},
        headers=car_headers,
    )
    assert ack_fail_to_app.status_code == 200
    assert ack_fail_to_app.json()["status"] == "failed"  # Kept failed!
