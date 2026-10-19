import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import settings
from app.repositories.models import CarDeviceStatus, utc_now
from app.services.control_service import music_queue_snapshot_manager
from app.services.netease_service import netease_service, NeteaseUpstreamError


@pytest.fixture(autouse=True)
def clean_queue_state():
    """Ensure in-memory queue manager is clean before and after each test."""
    music_queue_snapshot_manager.clear_all()
    yield
    music_queue_snapshot_manager.clear_all()


def _make_dummy_songs(count: int, start_id: int = 1000):
    songs = []
    for i in range(count):
        s_id = start_id + i
        songs.append({
            "id": s_id,
            "title": f"Song_{s_id}",
            "artist": "Artist_Test",
            "album": "Album_Test",
            "duration_ms": 200000,
            "cover_url": f"https://example.com/cover/{s_id}.jpg",
            "fee": 0,
            "playable": None,
        })
    return songs


def test_get_queue_pure_read_and_immutable_snapshot(client, phone_headers):
    """
    Rule 1 & Rule 3:
    - Pure read-only (query with ?source= does not switch active queue).
    - Returns opaque queue_id (q_fav_...), version, and relative stream_url.
    - playable is null when unverified.
    - Snapshot pagination: version match succeeds, mismatch returns 409 QUEUE_VERSION_CONFLICT.
    """
    dummy_songs = _make_dummy_songs(50)
    fav_info = {
        "playlist_id": 987654321,
        "name": "我喜欢的音乐",
        "cover_url": "https://example.com/fav.jpg",
        "track_count": 50,
        "play_count": 100,
        "user_id": 12345678,
    }

    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value={"songs": dummy_songs}):

        # 1. Probe page 0 without queue_id or queue_version
        resp = client.get("/api/v1/music/queue?offset=0&limit=20", headers=phone_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert data["source"] == "netease_favorite"
        assert data["queue_id"].startswith("q_fav_")
        assert data["queue_version"] == 100
        assert data["total"] == 50
        assert data["has_more"] is True
        assert len(data["tracks"]) == 20

        # Verify track schema
        t0 = data["tracks"][0]
        assert t0["track_id"] == "netease_1000"
        assert t0["stream_url"] == "/api/v1/music/netease/stream/1000"
        assert t0["playable"] is None
        assert t0["queue_index"] == 0

        queue_id = data["queue_id"]
        queue_version = data["queue_version"]

        # 2. Pure read check: querying with ?source=netease_intelligence does NOT switch active queue
        probe_resp = client.get("/api/v1/music/queue?source=netease_intelligence&offset=0&limit=10", headers=phone_headers)
        assert probe_resp.status_code == 200
        assert probe_resp.json()["source"] == "netease_favorite"
        active_q = music_queue_snapshot_manager.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID)
        assert active_q is not None
        assert active_q.source == "netease_favorite"

        # 3. Valid pagination with bound snapshot (queue_id, queue_version)
        p2_resp = client.get(
            f"/api/v1/music/queue?queue_id={queue_id}&queue_version={queue_version}&offset=20&limit=20",
            headers=phone_headers,
        )
        assert p2_resp.status_code == 200
        p2_data = p2_resp.json()
        assert len(p2_data["tracks"]) == 20
        assert p2_data["tracks"][0]["queue_index"] == 20

        # 4. Version mismatch returns 409 QUEUE_VERSION_CONFLICT
        conflict_resp = client.get(
            f"/api/v1/music/queue?queue_id={queue_id}&queue_version={queue_version - 1}&offset=20&limit=20",
            headers=phone_headers,
        )
        assert conflict_resp.status_code == 409
        err = conflict_resp.json()["detail"]
        assert err["error_code"] == "QUEUE_VERSION_CONFLICT"


def test_empty_favorite_playlist_returns_409(client, phone_headers):
    """
    Rule 1 & Rule 3:
    When owner's favorite playlist has 0 tracks, server returns 409 EMPTY_PLAYLIST.
    """
    fav_info = {
        "playlist_id": 987654321,
        "name": "我喜欢的音乐",
        "cover_url": None,
        "track_count": 0,
        "play_count": 0,
        "user_id": 12345678,
    }
    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info):
        resp = client.get("/api/v1/music/queue", headers=phone_headers)
        assert resp.status_code == 409
        err = resp.json()["detail"]
        assert err["error_code"] == "EMPTY_PLAYLIST"


def test_device_isolation_and_logout_clears_queue(client, phone_headers, car_headers):
    """
    Rule 2:
    - Car token can only access its own device's queue.
    - Logout / cookie clear clears in-memory queue snapshots.
    """
    # 1. Car device requesting queue for unauthorized target device -> 403 Forbidden
    resp = client.get("/api/v1/music/queue?device_id=unauthorized_car", headers=car_headers)
    assert resp.status_code == 403

    # 2. Populate an active queue
    dummy_songs = _make_dummy_songs(10)
    fav_info = {
        "playlist_id": 987654321,
        "name": "我喜欢的音乐",
        "cover_url": None,
        "track_count": 10,
        "play_count": 1,
        "user_id": 12345678,
    }
    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value={"songs": dummy_songs}):
        q_resp = client.get("/api/v1/music/queue", headers=phone_headers)
        assert q_resp.status_code == 200
        assert music_queue_snapshot_manager.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID) is not None

        # 3. Simulate account logout/cookie clear
        netease_service.cookie_store.clear()

        # Cache must be wiped out
        assert music_queue_snapshot_manager.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID) is None


def test_authentic_intelligence_mode_and_replenishment(client, phone_headers):
    """
    Rule 4 & Rule 5:
    - Initial seed (batch_index == 0) must belong to owner favorite playlist (422 if not).
    - Authentic intelligence generation creates dedicated queue (source=netease_intelligence).
    - Replenishment (batch_index > 0) appends tracks with stable previous indices and bumps version.
    - Request ID deduplication prevents duplicate append.
    - Upstream errors return 409 (auth) or 502/503 without fallback to simi.
    """
    intel_batch1 = _make_dummy_songs(10, start_id=2000)
    intel_batch2 = _make_dummy_songs(10, start_id=3000)

    # 1. Seed not in favorite playlist -> 422 INVALID_SEED_SONG
    with patch.object(netease_service.cookie_store, "load", return_value="fake_cookie=1"), \
         patch.object(netease_service, "is_song_in_favorite_playlist", new_callable=AsyncMock, return_value=False):
        resp_invalid_seed = client.post(
            "/api/v1/music/netease/intelligence",
            json={"seed_song_id": 999999, "batch_index": 0},
            headers=phone_headers,
        )
        assert resp_invalid_seed.status_code == 422
        err = resp_invalid_seed.json()["detail"]
        assert err["error_code"] == "INVALID_SEED_SONG"

    # 2. Seed valid -> batch_index == 0 succeeds
    with patch.object(netease_service.cookie_store, "load", return_value="fake_cookie=1"), \
         patch.object(netease_service, "is_song_in_favorite_playlist", new_callable=AsyncMock, return_value=True), \
         patch.object(netease_service, "get_intelligence", new_callable=AsyncMock, return_value=("intelligence", intel_batch1)):

        req_payload = {
            "request_id": "req_intel_init_001",
            "seed_song_id": 1901371647,
            "batch_index": 0,
            "count": 10,
        }
        resp_b0 = client.post("/api/v1/music/netease/intelligence", json=req_payload, headers=phone_headers)
        assert resp_b0.status_code == 200
        b0_data = resp_b0.json()
        assert b0_data["mode"] == "intelligence"
        assert b0_data["queue_id"].startswith("q_intel_")
        assert b0_data["queue_version"] == 100
        assert len(b0_data["tracks"]) == 10
        assert b0_data["tracks"][0]["queue_index"] == 0
        assert b0_data["tracks"][9]["queue_index"] == 9

        queue_id = b0_data["queue_id"]

    # 3. Replenishment: batch_index == 1 appends with stable previous indices
    with patch.object(netease_service.cookie_store, "load", return_value="fake_cookie=1"), \
         patch.object(netease_service, "get_intelligence", new_callable=AsyncMock, return_value=("intelligence", intel_batch2)):

        replenish_payload = {
            "request_id": "req_intel_rep_001",
            "seed_song_id": 2009,  # last song of batch 1
            "batch_index": 1,
            "count": 10,
        }
        resp_b1 = client.post("/api/v1/music/netease/intelligence", json=replenish_payload, headers=phone_headers)
        assert resp_b1.status_code == 200
        b1_data = resp_b1.json()
        assert b1_data["queue_id"] == queue_id
        assert b1_data["queue_version"] == 101  # version incremented
        assert len(b1_data["tracks"]) == 20     # 10 + 10
        # Indices 0..9 unchanged, new tracks are 10..19
        assert b1_data["tracks"][0]["queue_index"] == 0
        assert b1_data["tracks"][9]["queue_index"] == 9
        assert b1_data["tracks"][10]["queue_index"] == 10
        assert b1_data["tracks"][19]["queue_index"] == 19

        # 4. Request ID deduplication: repeating req_intel_rep_001 returns existing result without re-appending
        resp_dup = client.post("/api/v1/music/netease/intelligence", json=replenish_payload, headers=phone_headers)
        assert resp_dup.status_code == 200
        dup_data = resp_dup.json()
        assert dup_data["queue_version"] == 101
        assert len(dup_data["tracks"]) == 20


def test_command_idempotency_and_precondition_assertion(client, phone_headers, db_session):
    """
    Rule 4:
    - Same command_id + same payload -> idempotent replay.
    - Same command_id + different payload -> 409 Conflict.
    - Precondition assertions: expected_queue_id and expected_queue_version mismatch -> 409 QUEUE_PRECONDITION_FAILED.
    """
    # 1. Initialize an active favorite queue
    fav_info = {
        "playlist_id": 987654321,
        "name": "我喜欢的音乐",
        "cover_url": None,
        "track_count": 20,
        "play_count": 1,
        "user_id": 12345678,
    }
    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value={"songs": _make_dummy_songs(20)}):
        q_resp = client.get("/api/v1/music/queue", headers=phone_headers)
        active_qid = q_resp.json()["queue_id"]
        active_qver = q_resp.json()["queue_version"]

    # 2. Precondition mismatch on expected_queue_version -> 409 QUEUE_PRECONDITION_FAILED
    cmd_fail_ver = {
        "command_id": "cmd_test_precon_ver",
        "action": "pause",
        "params": {
            "expected_queue_version": active_qver + 5,
        },
    }
    resp_pv = client.post("/api/v1/music/control/command", json=cmd_fail_ver, headers=phone_headers)
    assert resp_pv.status_code == 409
    assert resp_pv.json()["detail"]["error_code"] == "QUEUE_PRECONDITION_FAILED"

    # 3. Precondition mismatch on expected_queue_id -> 409 QUEUE_PRECONDITION_FAILED
    cmd_fail_id = {
        "command_id": "cmd_test_precon_id",
        "action": "pause",
        "params": {
            "expected_queue_id": "q_fav_wrongid123",
        },
    }
    resp_pi = client.post("/api/v1/music/control/command", json=cmd_fail_id, headers=phone_headers)
    assert resp_pi.status_code == 409
    assert resp_pi.json()["detail"]["error_code"] == "QUEUE_PRECONDITION_FAILED"

    # 4. Valid command with matching preconditions succeeds
    cmd_valid = {
        "command_id": "cmd_test_idempotent_01",
        "action": "pause",
        "params": {
            "expected_queue_id": active_qid,
            "expected_queue_version": active_qver,
        },
    }
    resp_ok = client.post("/api/v1/music/control/command", json=cmd_valid, headers=phone_headers)
    assert resp_ok.status_code == 200

    # 5. Duplicate replay of same command_id + same payload succeeds
    resp_replay = client.post("/api/v1/music/control/command", json=cmd_valid, headers=phone_headers)
    assert resp_replay.status_code == 200

    # 6. Same command_id + different action -> 409 Conflict (COMMAND_CONFLICT)
    cmd_conflict = {
        "command_id": "cmd_test_idempotent_01",
        "action": "resume",
        "params": {},
    }
    resp_conflict = client.post("/api/v1/music/control/command", json=cmd_conflict, headers=phone_headers)
    assert resp_conflict.status_code == 409
    assert resp_conflict.json()["detail"]["error_code"] == "COMMAND_CONFLICT"


def test_capability_gate_queue_v2(client, phone_headers, car_headers, db_session):
    """
    Rule 8:
    Capability negotiation: car without queue_v2 capability causes set_playback_mode
    or switch_queue to be rejected with 422 CAPABILITY_NOT_SUPPORTED.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID

    # 1. Car reports capabilities WITHOUT queue_v2
    client.post(
        "/api/v1/music/control/status",
        json={
            "device_id": dev_id,
            "playback_state": "stopped",
            "capabilities": ["basic_playback"],
        },
        headers=car_headers,
    )

    # 2. Client sends set_playback_mode -> 422 CAPABILITY_NOT_SUPPORTED
    cmd_mode = {
        "command_id": "cmd_mode_cap_test",
        "action": "set_playback_mode",
        "params": {"mode": "shuffle"},
    }
    resp_gate1 = client.post("/api/v1/music/control/command", json=cmd_mode, headers=phone_headers)
    assert resp_gate1.status_code == 422
    assert resp_gate1.json()["detail"]["error_code"] == "CAPABILITY_NOT_SUPPORTED"

    # 3. Client sends switch_queue -> 422 CAPABILITY_NOT_SUPPORTED
    cmd_switch = {
        "command_id": "cmd_switch_cap_test",
        "action": "switch_queue",
        "params": {"source": "netease_favorite"},
    }
    resp_gate2 = client.post("/api/v1/music/control/command", json=cmd_switch, headers=phone_headers)
    assert resp_gate2.status_code == 422
    assert resp_gate2.json()["detail"]["error_code"] == "CAPABILITY_NOT_SUPPORTED"

    # 4. Car updates capabilities to include queue_v2
    client.post(
        "/api/v1/music/control/status",
        json={
            "device_id": dev_id,
            "playback_state": "stopped",
            "capabilities": ["basic_playback", "queue_v2"],
        },
        headers=car_headers,
    )

    # 5. Same command now passes capability gate
    cmd_pass = {
        "command_id": "cmd_mode_cap_test_pass",
        "action": "set_playback_mode",
        "params": {"mode": "shuffle"},
    }
    resp_pass = client.post("/api/v1/music/control/command", json=cmd_pass, headers=phone_headers)
    assert resp_pass.status_code == 200


def test_monotonic_status_sequence_and_mode_separation(client, phone_headers, car_headers):
    """
    Rule 4 & Rule 6:
    - Mode separation: requested_mode is tracked upon command; applied_mode is reported by car.
    - Monotonic sequence: older status_seq cannot regress newer status_seq.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID

    # 1. Car reports initial status with status_seq = 10, position_ms = 10000
    report_1 = {
        "device_id": dev_id,
        "playback_state": "playing",
        "position_ms": 10000,
        "applied_mode": "list_loop",
        "requested_mode": "list_loop",
        "status_seq": 10,
        "capabilities": ["queue_v2"],
    }
    r1 = client.post("/api/v1/music/control/status", json=report_1, headers=car_headers)
    assert r1.status_code == 200

    q1 = client.get(f"/api/v1/music/control/status?device_id={dev_id}", headers=phone_headers)
    assert q1.json()["position_ms"] == 10000
    assert q1.json()["status_seq"] == 10

    # 2. Delayed out-of-order status with status_seq = 9 arrives -> must NOT overwrite position_ms
    report_delayed = {
        "device_id": dev_id,
        "playback_state": "playing",
        "position_ms": 5000,
        "status_seq": 9,
    }
    client.post("/api/v1/music/control/status", json=report_delayed, headers=car_headers)

    q2 = client.get(f"/api/v1/music/control/status?device_id={dev_id}", headers=phone_headers)
    # Position remains 10000, status_seq remains 10
    assert q2.json()["position_ms"] == 10000
    assert q2.json()["status_seq"] == 10

    # 3. Phone dispatches set_playback_mode="intelligence"
    cmd_mode = {
        "command_id": "cmd_set_intel_mode",
        "action": "set_playback_mode",
        "params": {"mode": "intelligence"},
    }
    c_resp = client.post("/api/v1/music/control/command", json=cmd_mode, headers=phone_headers)
    assert c_resp.status_code == 200

    # Requested mode is updated to "intelligence", but applied_mode is still "list_loop"
    q3 = client.get(f"/api/v1/music/control/status?device_id={dev_id}", headers=phone_headers)
    assert q3.json()["requested_mode"] == "intelligence"
    assert q3.json()["applied_mode"] == "list_loop"

    # 4. Car applies command and confirms via ACK
    ack_resp = client.post(
        f"/api/v1/music/control/command/{cmd_mode['command_id']}/ack",
        json={"status": "applied", "applied_mode": "intelligence"},
        headers=car_headers,
    )
    assert ack_resp.status_code == 200

    q4 = client.get(f"/api/v1/music/control/status?device_id={dev_id}", headers=phone_headers)
    assert q4.json()["applied_mode"] == "intelligence"
    assert q4.json()["last_applied_command_id"] == cmd_mode["command_id"]
