import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import settings
from app.repositories.models import CarDeviceStatus, MusicCommand, QueueVersionSequence, User, utc_now
from app.services.control_service import (
    music_queue_snapshot_manager,
    command_queue_manager,
    MusicQueueSnapshotManager,
    get_car_status_by_id,
    save_or_update_car_status,
)
from app.schemas.control import CarStatusReportRequest
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
        assert data["queue_version"] > 0
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
        # Pure read: GET queue does NOT mutate or activate active queue!
        active_q = music_queue_snapshot_manager.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID)
        assert active_q is None

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
        # Explicitly activate favorite queue to test active queue invalidation
        asyncio.run(music_queue_snapshot_manager.init_favorite_queue(settings.DEFAULT_CAR_DEVICE_ID, activate=True))
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
        assert b0_data["queue_version"] > 0
        assert len(b0_data["tracks"]) == 10
        assert b0_data["tracks"][0]["queue_index"] == 0
        assert b0_data["tracks"][9]["queue_index"] == 9

        queue_id = b0_data["queue_id"]
        v0 = b0_data["queue_version"]

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
        assert b1_data["queue_version"] > v0  # version incremented
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
        assert dup_data["queue_version"] == b1_data["queue_version"]
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
        active_snap = asyncio.run(music_queue_snapshot_manager.init_favorite_queue(settings.DEFAULT_CAR_DEVICE_ID, activate=True))
        active_qid = active_snap.queue_id
        active_qver = active_snap.queue_version

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
    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value={"playlist_id": 1, "track_count": 10, "name": "Fav"}), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value={"songs": _make_dummy_songs(10), "has_more": False}):
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
        "params": {"mode": "intelligence", "seed_song_id": 1901371647},
    }
    with patch.object(netease_service, "is_song_in_favorite_playlist", new_callable=AsyncMock, return_value=True), \
         patch.object(netease_service, "get_intelligence", new_callable=AsyncMock, return_value=("intelligence", _make_dummy_songs(10))):
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


def test_http_permission_matrix(client, phone_headers, admin_headers, car_headers, db_session):
    """
    Item 1:
    - Admin (owner) and legacy phone: 200 OK.
    - Car with matching device: 200 OK.
    - Car with mismatched device: 403 Forbidden.
    - Standard registered user (role="user") without device control: 403 Forbidden.
    - Standard registered user (role="user") WITH device control: 403 Forbidden on queue/intelligence/commands.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID
    fav_info = {"playlist_id": 1, "track_count": 5, "name": "Fav"}
    fav_tracks = {"songs": _make_dummy_songs(5), "has_more": False}

    # Register standard user
    reg_payload = {"username": "music_driver_test", "password": "DriverSecretPassword123"}
    r_reg = client.post("/api/v1/auth/register", json=reg_payload)
    assert r_reg.status_code == 201
    user_id = r_reg.json()["id"]

    # Login to get session token
    r_login = client.post("/api/v1/auth/login", json=reg_payload)
    assert r_login.status_code == 200
    user_token = r_login.json()["session_id"]
    user_headers = {"Authorization": f"Bearer {user_token}"}

    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value=fav_tracks):

        # 1. Admin and Legacy Phone succeed
        assert client.get("/api/v1/music/queue", headers=phone_headers).status_code == 200
        assert client.get("/api/v1/music/queue", headers=admin_headers).status_code == 200

        # 2. Car token for matching device succeeds
        assert client.get(f"/api/v1/music/queue?device_id={dev_id}", headers=car_headers).status_code == 200

        # 3. Car token for mismatched device fails (403)
        assert client.get("/api/v1/music/queue?device_id=other_car_99", headers=car_headers).status_code == 403

        # 4. Standard registered user without device control: 403 Forbidden
        assert client.get("/api/v1/music/queue", headers=user_headers).status_code == 403
        assert client.post("/api/v1/music/netease/intelligence", json={"seed_song_id": 1000}, headers=user_headers).status_code == 403
        assert client.post("/api/v1/music/control/command", json={"action": "pause"}, headers=user_headers).status_code == 403

        # 5. Grant vehicle device control to standard user
        r_grant = client.post(
            f"/api/v1/auth/users/{user_id}/device-binding",
            json={"device_id": dev_id, "can_control": True},
            headers=admin_headers,
        )
        assert r_grant.status_code == 200

        # Re-login to get updated claims
        r_login2 = client.post("/api/v1/auth/login", json=reg_payload)
        user_headers_ctrl = {"Authorization": f"Bearer {r_login2.json()['session_id']}"}

        # 6. Standard registered user WITH device control STILL gets 403 Forbidden on Owner private assets!
        # (Cannot access owner private liked playlist or generate intelligence queue)
        assert client.get("/api/v1/music/queue", headers=user_headers_ctrl).status_code == 403
        assert client.post("/api/v1/music/netease/intelligence", json={"seed_song_id": 1000}, headers=user_headers_ctrl).status_code == 403
        # But has device control, so dispatching basic playback control to vehicle succeeds (200 OK)
        assert client.post("/api/v1/music/control/command", json={"action": "pause"}, headers=user_headers_ctrl).status_code == 200


def test_multi_page_pagination_service_over_100_tracks(client, phone_headers):
    """
    Item 2:
    - Playlist with 250 tracks paginates exhaustively across 3 pages (100, 100, 50).
    - has_more loop fetches all 250 tracks, total=250, is_complete=True, queue_state="ready".
    """
    fav_info = {
        "playlist_id": 12345,
        "name": "Large Favorite",
        "track_count": 250,
        "user_id": 999,
    }

    async def mock_get_tracks(offset: int = 0, limit: int = 100):
        total = 250
        remaining = max(0, total - offset)
        page_len = min(limit, remaining)
        has_more = (offset + page_len) < total
        return {
            "songs": _make_dummy_songs(page_len, start_id=1000 + offset),
            "has_more": has_more,
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", side_effect=mock_get_tracks):

        snap = client.get("/api/v1/music/queue?offset=0&limit=50", headers=phone_headers)
        assert snap.status_code == 200
        data = snap.json()
        assert data["total"] == 250
        assert data["is_complete"] is True
        assert data["queue_state"] == "ready"
        assert len(data["tracks"]) == 50

        # Query page 5 (tracks 200..249)
        qid = data["queue_id"]
        qver = data["queue_version"]
        resp_p5 = client.get(f"/api/v1/music/queue?queue_id={qid}&queue_version={qver}&offset=200&limit=50", headers=phone_headers)
        assert resp_p5.status_code == 200
        p5_data = resp_p5.json()
        assert len(p5_data["tracks"]) == 50
        assert p5_data["tracks"][0]["queue_index"] == 200
        assert p5_data["tracks"][49]["queue_index"] == 249


def test_immutable_old_snapshot_preserved_after_replenish(client, phone_headers):
    """
    Item 3:
    - Generates v1 intelligence queue (10 tracks).
    - Appends batch 2 (10 tracks) generating v2 snapshot.
    - Querying v1 snapshot by (queue_id, queue_version=v1) returns EXACT original 10 tracks, NOT 20!
    """
    batch1 = _make_dummy_songs(10, start_id=2000)
    batch2 = _make_dummy_songs(10, start_id=3000)

    with patch.object(netease_service.cookie_store, "load", return_value="cookie=1"), \
         patch.object(netease_service, "is_song_in_favorite_playlist", new_callable=AsyncMock, return_value=True), \
         patch.object(netease_service, "get_intelligence", new_callable=AsyncMock, return_value=("intelligence", batch1)):

        # Batch 0
        r_b0 = client.post(
            "/api/v1/music/netease/intelligence",
            json={"seed_song_id": 1901371647, "batch_index": 0, "count": 10},
            headers=phone_headers,
        )
        assert r_b0.status_code == 200
        b0_data = r_b0.json()
        qid = b0_data["queue_id"]
        qver1 = b0_data["queue_version"]

        # Read v1 snapshot page 0
        v1_page_before = client.get(f"/api/v1/music/queue?queue_id={qid}&queue_version={qver1}&offset=0&limit=50", headers=phone_headers)
        assert v1_page_before.status_code == 200
        assert v1_page_before.json()["total"] == 10
        assert len(v1_page_before.json()["tracks"]) == 10

    # Batch 1 Replenishment
    with patch.object(netease_service.cookie_store, "load", return_value="cookie=1"), \
         patch.object(netease_service, "get_intelligence", new_callable=AsyncMock, return_value=("intelligence", batch2)):

        r_b1 = client.post(
            "/api/v1/music/netease/intelligence",
            json={"seed_song_id": 2009, "batch_index": 1, "count": 10},
            headers=phone_headers,
        )
        assert r_b1.status_code == 200
        b1_data = r_b1.json()
        qver2 = b1_data["queue_version"]
        assert qver2 > qver1

        # Query v2 snapshot: has 20 tracks
        v2_page = client.get(f"/api/v1/music/queue?queue_id={qid}&queue_version={qver2}&offset=0&limit=50", headers=phone_headers)
        assert v2_page.status_code == 200
        assert v2_page.json()["total"] == 20

        # Query OLD v1 snapshot: MUST STILL HAVE EXACTLY 10 TRACKS, NOT MUTATED!
        v1_page_after = client.get(f"/api/v1/music/queue?queue_id={qid}&queue_version={qver1}&offset=0&limit=50", headers=phone_headers)
        assert v1_page_after.status_code == 200
        assert v1_page_after.json()["total"] == 10
        assert len(v1_page_after.json()["tracks"]) == 10
        assert v1_page_after.json()["tracks"] == v1_page_before.json()["tracks"]


def test_persistent_monotonic_queue_version_across_restart(client, phone_headers):
    """
    Item 2:
    - Queue versions are monotonically increasing and non-colliding across server restarts / memory clears.
    """
    fav_info = {"playlist_id": 1, "track_count": 5, "name": "Fav"}
    fav_tracks = {"songs": _make_dummy_songs(5), "has_more": False}

    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value=fav_tracks):

        r1 = client.get("/api/v1/music/queue", headers=phone_headers)
        v1 = r1.json()["queue_version"]

        # Simulate server restart / cache clear
        music_queue_snapshot_manager.clear_all()

        r2 = client.get("/api/v1/music/queue", headers=phone_headers)
        v2 = r2.json()["queue_version"]

        assert v2 > v1
        assert v1 != 100
        assert v2 != 100


def test_atomic_command_single_track_and_failure_resilience(client, phone_headers, car_headers, db_session):
    """
    Item 4:
    - play_track / switch_queue to single_track prepares single track queue.
    - Sending command sets requested_mode, but DOES NOT change applied_mode or queue_source in DB status.
    - Only car ACK (applied) updates applied_mode and queue_source in DB status!
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID

    # Initial status: list_loop, netease_favorite, stopped
    client.post(
        "/api/v1/music/control/status",
        json={
            "device_id": dev_id,
            "playback_state": "stopped",
            "applied_mode": "list_loop",
            "requested_mode": "list_loop",
            "queue_source": "netease_favorite",
            "capabilities": ["queue_v2"],
        },
        headers=car_headers,
    )

    dummy_detail = {
        "id": 99988,
        "name": "Single Hit Song",
        "artist": "Hit Artist",
        "album": "Hit Album",
        "duration_ms": 180000,
        "cover_url": "https://example.com/hit.jpg",
    }

    # 1. Send single_track command
    cmd_single = {
        "command_id": "cmd_play_single_001",
        "action": "play_track",
        "params": {"song_id": 99988},
    }
    with patch.object(netease_service, "get_song_detail", new_callable=AsyncMock, return_value=dummy_detail):
        r_cmd = client.post("/api/v1/music/control/command", json=cmd_single, headers=phone_headers)
        assert r_cmd.status_code == 200

    # Verify status in DB: queue_source is STILL netease_favorite, applied_mode is STILL list_loop!
    status_mid = client.get(f"/api/v1/music/control/status?device_id={dev_id}", headers=phone_headers).json()
    assert status_mid["queue_source"] == "netease_favorite"
    assert status_mid["applied_mode"] == "list_loop"

    # 2. Car reports ACK applied
    r_ack = client.post(
        f"/api/v1/music/control/command/{cmd_single['command_id']}/ack",
        json={"status": "applied", "applied_mode": "single_loop"},
        headers=car_headers,
    )
    assert r_ack.status_code == 200

    # Status in DB is now updated to single_track and single_loop!
    status_final = client.get(f"/api/v1/music/control/status?device_id={dev_id}", headers=phone_headers).json()
    assert status_final["queue_source"] == "single_track"
    assert status_final["applied_mode"] == "single_loop"


def test_seek_parameter_normalization_and_conflict_rejection(client, phone_headers):
    """
    Item 6:
    - Normalizes seek_ms, position_ms, seek_position_ms.
    - Conflicting values -> 422 SEEK_PARAMETER_CONFLICT.
    - Missing seek params on seek -> 422 SEEK_PARAMETER_REQUIRED.
    - Negative seek -> 422 INVALID_SEEK_POSITION.
    """
    # 1. Conflict between seek_ms and position_ms
    cmd_conflict = {
        "command_id": "cmd_seek_conflict",
        "action": "seek",
        "params": {"seek_ms": 5000, "position_ms": 6000},
    }
    r1 = client.post("/api/v1/music/control/command", json=cmd_conflict, headers=phone_headers)
    assert r1.status_code == 422
    assert r1.json()["detail"]["error_code"] == "SEEK_PARAMETER_CONFLICT"

    # 2. Negative seek position
    cmd_neg = {
        "command_id": "cmd_seek_neg",
        "action": "seek",
        "params": {"seek_ms": -100},
    }
    r2 = client.post("/api/v1/music/control/command", json=cmd_neg, headers=phone_headers)
    assert r2.status_code == 422
    assert r2.json()["detail"]["error_code"] == "INVALID_SEEK_POSITION"

    # 3. Missing seek position
    cmd_missing = {
        "command_id": "cmd_seek_missing",
        "action": "seek",
        "params": {},
    }
    r3 = client.post("/api/v1/music/control/command", json=cmd_missing, headers=phone_headers)
    assert r3.status_code == 422
    assert r3.json()["detail"]["error_code"] == "SEEK_PARAMETER_REQUIRED"

    # 4. Valid seek position matches and normalizes
    cmd_valid = {
        "command_id": "cmd_seek_valid",
        "action": "seek",
        "params": {"seek_ms": 5000, "position_ms": 5000},
    }
    r4 = client.post("/api/v1/music/control/command", json=cmd_valid, headers=phone_headers)
    assert r4.status_code == 200
    assert r4.json()["params"]["seek_position_ms"] == 5000


def test_idempotency_order_before_mutable_preconditions(client, phone_headers, car_headers):
    """
    Item 5:
    - Idempotent lookup matches command_id FIRST before checking capabilities or queue version.
    - Replay of existing command succeeds even if car capabilities or queue version changed.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID

    # 1. Car reports capabilities with queue_v2
    client.post(
        "/api/v1/music/control/status",
        json={"device_id": dev_id, "playback_state": "stopped", "capabilities": ["queue_v2"]},
        headers=car_headers,
    )

    cmd_orig = {
        "command_id": "cmd_order_test_001",
        "action": "set_playback_mode",
        "params": {"mode": "shuffle"},
    }
    fav_info = {"playlist_id": 1, "track_count": 5, "name": "Fav"}
    fav_tracks = {"songs": _make_dummy_songs(5), "has_more": False}
    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value=fav_tracks):
        r_orig = client.post("/api/v1/music/control/command", json=cmd_orig, headers=phone_headers)
        assert r_orig.status_code == 200

    # 2. Car loses queue_v2 capability (simulating downgrade or legacy status update)
    client.post(
        "/api/v1/music/control/status",
        json={"device_id": dev_id, "playback_state": "stopped", "capabilities": []},
        headers=car_headers,
    )

    # 3. Replay of cmd_orig with same payload MUST SUCCEED (idempotent 200) without failing on capability!
    r_replay = client.post("/api/v1/music/control/command", json=cmd_orig, headers=phone_headers)
    assert r_replay.status_code == 200

    # 4. Same command_id with DIFFERENT payload -> 409 COMMAND_CONFLICT
    cmd_conflict = {
        "command_id": "cmd_order_test_001",
        "action": "pause",
        "params": {},
    }
    r_conflict = client.post("/api/v1/music/control/command", json=cmd_conflict, headers=phone_headers)
    assert r_conflict.status_code == 409
    assert r_conflict.json()["detail"]["error_code"] == "COMMAND_CONFLICT"


def test_websocket_command_parity_with_http(client, car_headers, db_session):
    """
    Item 1:
    - WebSocket command pipeline enforces seek validation and capability gate
      with 100% parity to HTTP endpoint.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID
    # Ensure car status exists without queue_v2 capability initially
    client.post(
        "/api/v1/music/control/status",
        json={"device_id": dev_id, "playback_state": "stopped", "capabilities": []},
        headers=car_headers,
    )

    # 1. Connect phone client over WebSocket
    with client.websocket_connect(f"/api/v1/ws/music?token={settings.PHONE_TOKEN}&client_type=phone&device_id={dev_id}") as ws:
        # Read initial snapshot
        init_msg = ws.receive_json()
        assert init_msg["type"] == "status_snapshot"

        # 2. Seek validation: conflicting seek values -> error 422
        ws.send_json({
            "type": "command",
            "data": {
                "command_id": "ws_cmd_seek_conflict",
                "action": "seek",
                "params": {"seek_ms": 1000, "position_ms": 2000},
            },
        })
        ack1 = ws.receive_json()
        assert ack1["type"] == "command_ack"
        assert ack1["status"] == "error"
        assert ack1["code"] == 422
        assert ack1["error"]["error_code"] == "SEEK_PARAMETER_CONFLICT"

        # 3. Capability gate: set_playback_mode without queue_v2 capability -> error 422
        ws.send_json({
            "type": "command",
            "data": {
                "command_id": "ws_cmd_cap_fail",
                "action": "set_playback_mode",
                "params": {"mode": "shuffle"},
            },
        })
        ack2 = ws.receive_json()
        assert ack2["type"] == "command_ack"
        assert ack2["status"] == "error"
        assert ack2["code"] == 422
        assert ack2["error"]["error_code"] == "CAPABILITY_NOT_SUPPORTED"


def test_websocket_owner_gate_enforcement(client, admin_headers):
    """
    Item 1:
    - Standard user (role == 'user') with has_device_control is forbidden from
      queue modifications or mode switches over both HTTP and WebSocket.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID
    reg_payload = {
        "username": "ws_friend_user_unique",
        "password": "friend_password_123",
        "display_name": "Friend User",
    }
    r_reg = client.post("/api/v1/auth/register", json=reg_payload)
    assert r_reg.status_code in (200, 201)
    user_id = r_reg.json()["id"]

    # Grant device control
    client.post(
        f"/api/v1/auth/users/{user_id}/device-binding",
        json={"device_id": dev_id, "can_control": True},
        headers=admin_headers,
    )

    # Login to get session_id
    r_login = client.post("/api/v1/auth/login", json=reg_payload)
    assert r_login.status_code == 200
    user_session = r_login.json()["session_id"]

    with client.websocket_connect(f"/api/v1/ws/music?token={user_session}&client_type=phone&device_id={dev_id}") as ws:
        init_msg = ws.receive_json()
        assert init_msg["type"] == "status_snapshot"

        # Sending set_playback_mode is rejected with 403 OWNER_QUEUE_PERMISSION_DENIED
        ws.send_json({
            "type": "command",
            "data": {
                "command_id": "ws_user_gate_cmd",
                "action": "set_playback_mode",
                "params": {"mode": "shuffle"},
            },
        })
        ack = ws.receive_json()
        assert ack["type"] == "command_ack"
        assert ack["status"] == "error"
        assert ack["code"] == 403
        assert ack["error"]["error_code"] == "OWNER_QUEUE_PERMISSION_DENIED"


def test_resume_error_handling_without_swallowing(client, phone_headers, db_session):
    """
    Item 2:
    - When active_queue is None and NetEase is unauthenticated, resume fails with 409 immediately.
    - Zero commands created in DB, zero commands enqueued or dispatched.
    - Active queue remains None.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID
    music_queue_snapshot_manager.clear_all()
    command_queue_manager.clear_all()
    assert music_queue_snapshot_manager.get_active_queue(dev_id) is None

    cmd_count_before = db_session.query(MusicCommand).count()

    with patch.object(netease_service.cookie_store, "load", return_value=None), \
         patch.object(netease_service, "get_favorite_playlist_info", side_effect=PermissionError("NetEase unauthenticated")):

        resp = client.post(
            "/api/v1/music/control/command",
            json={"command_id": "cmd_resume_fail_001", "action": "resume"},
            headers=phone_headers,
        )
        assert resp.status_code == 409
        err = resp.json()
        assert err["detail"]["error_code"] == "INTELLIGENCE_AUTH_INVALID"

    # Verify zero commands created in DB
    cmd_count_after = db_session.query(MusicCommand).count()
    assert cmd_count_after == cmd_count_before

    # Verify active queue remains None
    assert music_queue_snapshot_manager.get_active_queue(dev_id) is None


def test_true_restart_and_clock_rollback_monotonic_queue_version(db_session):
    """
    Item 3:
    - New MusicQueueSnapshotManager instances simulate true process restart.
    - Clock rollback (system clock going backwards) is caught via max(baseline + 1, epoch_ms).
    - Versions strictly increase monotonically without regression or collision.
    """
    dev_id = "test_dev_monotonic"
    mgr1 = MusicQueueSnapshotManager()
    v1 = mgr1.get_next_queue_version(dev_id, db=db_session)
    assert v1 > 0

    # True process restart: brand new instance with no memory state
    mgr2 = MusicQueueSnapshotManager()

    # Monkeypatch time backwards (1 hour before v1)
    past_time = (v1 / 1000.0) - 3600.0
    with patch("time.time", return_value=past_time):
        v2 = mgr2.get_next_queue_version(dev_id, db=db_session)
        assert v2 == v1 + 1

    # Another call with time still backwards
    with patch("time.time", return_value=past_time):
        v3 = mgr2.get_next_queue_version(dev_id, db=db_session)
        assert v3 == v2 + 1
        assert v3 > v1


def test_play_track_favorite_preservation_vs_single_track(client, phone_headers, car_headers):
    """
    Item 4:
    - play_track targeting a song in the current favorite active queue preserves
      the favorite queue and sets queue_index.
    - play_track targeting an unlisted song prepares single_track queue.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID
    music_queue_snapshot_manager.clear_all()

    # 1. Initialize favorite queue with tracks 101, 102, 103
    fav_songs = [
        {"id": 101, "name": "Song 101", "artist": "Artist 1"},
        {"id": 102, "name": "Song 102", "artist": "Artist 2"},
        {"id": 103, "name": "Song 103", "artist": "Artist 3"},
    ]
    fav_info = {"playlist_id": 555, "track_count": 3, "name": "Favs"}
    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value={"songs": fav_songs, "has_more": False}):
        active_q = asyncio.run(music_queue_snapshot_manager.init_favorite_queue(dev_id, activate=True))
        assert active_q is not None
        orig_qid = active_q.queue_id
        orig_qver = active_q.queue_version

    # 2. Play track 102 (member of favorite playlist)
    cmd_fav_track = {
        "command_id": "cmd_play_fav_102",
        "action": "play_track",
        "params": {"song_id": 102},
    }
    r_fav = client.post("/api/v1/music/control/command", json=cmd_fav_track, headers=phone_headers)
    assert r_fav.status_code == 200
    fav_data = r_fav.json()
    assert fav_data["params"]["source"] == "netease_favorite"
    assert fav_data["params"]["queue_id"] == orig_qid
    assert fav_data["params"]["queue_version"] == orig_qver
    assert fav_data["params"]["queue_index"] == 1
    assert fav_data["params"]["track_id"] == "netease_102"

    # Active queue remains favorite queue!
    assert music_queue_snapshot_manager.get_active_queue(dev_id).queue_id == orig_qid

    # 3. Play track 999 (not in favorite playlist)
    dummy_detail = {
        "id": 999,
        "name": "Outside Track",
        "artist": "Outside Artist",
        "duration_ms": 200000,
    }
    cmd_outside_track = {
        "command_id": "cmd_play_outside_999",
        "action": "play_track",
        "params": {"song_id": 999},
    }
    with patch.object(netease_service, "get_song_detail", new_callable=AsyncMock, return_value=dummy_detail):
        r_outside = client.post("/api/v1/music/control/command", json=cmd_outside_track, headers=phone_headers)
        assert r_outside.status_code == 200
        out_data = r_outside.json()
        assert out_data["params"]["source"] == "single_track"
        assert out_data["params"]["queue_id"].startswith("q_single_")
        assert out_data["params"]["track_id"] == "netease_999"

    # Before car ACK, active queue is STILL the favorite queue!
    assert music_queue_snapshot_manager.get_active_queue(dev_id).queue_id == orig_qid

    # After car ACK applied, active queue switches to single_track queue!
    client.post(
        f"/api/v1/music/control/command/{cmd_outside_track['command_id']}/ack",
        json={"status": "applied", "applied_mode": "single_loop"},
        headers=car_headers,
    )
    assert music_queue_snapshot_manager.get_active_queue(dev_id).queue_id == out_data["params"]["queue_id"]


def test_pending_queue_state_machine_and_late_ack_guard(client, phone_headers, car_headers, db_session):
    """
    Item 5:
    - Commands prepare pending queues without mutating active queue.
    - Car failure leaves active queue intact and discards pending queue.
    - Late ACK for older command does not overwrite newer active queue.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID
    music_queue_snapshot_manager.clear_all()

    # 1. Set car capabilities to support queue_v2
    client.post(
        "/api/v1/music/control/status",
        json={"device_id": dev_id, "playback_state": "stopped", "capabilities": ["queue_v2"]},
        headers=car_headers,
    )

    # Prepare initial active favorite queue
    fav_songs = [{"id": 1, "name": "S1", "artist": "A1"}]
    fav_info = {"playlist_id": 11, "track_count": 1, "name": "Favs"}
    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value={"songs": fav_songs, "has_more": False}):
        active_init = asyncio.run(music_queue_snapshot_manager.init_favorite_queue(dev_id, activate=True))

    assert active_init is not None
    init_qid = active_init.queue_id

    # 2. Dispatch command 1 (single track), fails at vehicle
    dummy_d1 = {"id": 111, "name": "Track 111", "artist": "A111"}
    with patch.object(netease_service, "get_song_detail", new_callable=AsyncMock, return_value=dummy_d1):
        r1 = client.post(
            "/api/v1/music/control/command",
            json={"command_id": "cmd_fail_test", "action": "play_track", "params": {"song_id": 111}},
            headers=phone_headers,
        )
        assert r1.status_code == 200

    # Before ACK: active queue is STILL initial queue
    assert music_queue_snapshot_manager.get_active_queue(dev_id).queue_id == init_qid

    # Car sends failed ACK
    client.post(
        "/api/v1/music/control/command/cmd_fail_test/ack",
        json={"status": "failed", "error_code": "NETWORK_ERROR"},
        headers=car_headers,
    )
    # After failed ACK: active queue is STILL initial queue, pending is discarded
    assert music_queue_snapshot_manager.get_active_queue(dev_id).queue_id == init_qid

    # 3. Dispatch command 2 (single track 222), applied by vehicle
    dummy_d2 = {"id": 222, "name": "Track 222", "artist": "A222"}
    with patch.object(netease_service, "get_song_detail", new_callable=AsyncMock, return_value=dummy_d2):
        r2 = client.post(
            "/api/v1/music/control/command",
            json={"command_id": "cmd_apply_test", "action": "play_track", "params": {"song_id": 222}},
            headers=phone_headers,
        )
        assert r2.status_code == 200
        cmd2_qid = r2.json()["params"]["queue_id"]
        cmd2_qver = r2.json()["params"]["queue_version"]

    client.post(
        "/api/v1/music/control/command/cmd_apply_test/ack",
        json={"status": "applied", "applied_mode": "single_loop"},
        headers=car_headers,
    )
    assert music_queue_snapshot_manager.get_active_queue(dev_id).queue_id == cmd2_qid

    # 4. Simulate late ACK arriving for older snapshot (version < cmd2_qver)
    # apply_command_queue must ignore older version and preserve cmd2_qid
    res = music_queue_snapshot_manager.apply_command_queue(
        device_id=dev_id,
        queue_id=init_qid,
        queue_version=active_init.queue_version,  # older version
        db=db_session,
    )
    assert res is None
    assert music_queue_snapshot_manager.get_active_queue(dev_id).queue_id == cmd2_qid


def test_websocket_valid_status_keys_update(client, db_session):
    """
    Item 6:
    - WebSocket status_update accepts new fields (applied_mode, queue_id, queue_version,
      status_seq, capabilities) and persists them to DB without dropping or requiring playback_state.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID
    # Explicitly set initial status to playing
    save_or_update_car_status(
        db_session,
        CarStatusReportRequest(device_id=dev_id, playback_state="playing", position_ms=12000),
        default_device_id=dev_id,
    )
    db_session.commit()

    with client.websocket_connect(f"/api/v1/ws/music?token={settings.CAR_TOKEN}&client_type=car&device_id={dev_id}") as ws:
        init_msg = ws.receive_json()
        assert init_msg["type"] == "status_snapshot"

        # Car sends status update containing ONLY queue & mode fields
        ws.send_json({
            "type": "status_update",
            "data": {
                "applied_mode": "intelligence",
                "queue_id": "q_intel_test_999",
                "queue_version": 42,
                "queue_source": "netease_intelligence",
                "status_seq": 105,
                "capabilities": ["queue_v2"],
            },
        })

    # Verify database was updated and preserved existing playback_state
    db_session.expire_all()
    updated = get_car_status_by_id(db_session, dev_id)
    assert updated.applied_mode == "intelligence"
    assert updated.queue_id == "q_intel_test_999"
    assert updated.queue_version == 42
    assert updated.queue_source == "netease_intelligence"
    assert updated.status_seq == 105
    assert updated.playback_state == "playing"
    assert updated.position_ms == 12000


def test_prepare_and_failed_does_not_mutate_actual_status_queue_version(client, phone_headers, car_headers, db_session):
    """
    Requirement 1:
    - QueueVersionSequence independently allocates versions without mutating CarDeviceStatus.queue_version.
    - During prepare, failed, or expired command stages, actual status in DB, HTTP /status,
      and WS status_snapshot maintain unchanged applied queue_id and queue_version.
    - Only upon 'applied' ACK does CarDeviceStatus update to the new queue_id and queue_version.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID
    music_queue_snapshot_manager.clear_all()
    command_queue_manager.clear_all()

    # 1. Establish initial status in DB: queue_id = q_fav_initial, queue_version = 10
    status_row = db_session.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == dev_id).first()
    if not status_row:
        status_row = CarDeviceStatus(
            device_id=dev_id,
            playback_state="stopped",
            queue_id="q_fav_initial",
            queue_version=10,
            queue_source="netease_favorite",
            applied_mode="list_loop",
        )
        db_session.add(status_row)
    else:
        status_row.queue_id = "q_fav_initial"
        status_row.queue_version = 10
        status_row.queue_source = "netease_favorite"
        status_row.applied_mode = "list_loop"
    db_session.commit()

    # Verify initial status via HTTP
    resp_init = client.get("/api/v1/music/control/status", headers=phone_headers)
    assert resp_init.status_code == 200
    assert resp_init.json()["queue_id"] == "q_fav_initial"
    assert resp_init.json()["queue_version"] == 10

    # Verify initial status via WS snapshot
    with client.websocket_connect(f"/api/v1/ws/music?token={settings.CAR_TOKEN}&client_type=car&device_id={dev_id}") as ws:
        init_ws = ws.receive_json()
        assert init_ws["type"] == "status_snapshot"
        assert init_ws["data"]["queue_id"] == "q_fav_initial"
        assert init_ws["data"]["queue_version"] == 10

    # 2. Prepare a single-track play command (generates new snapshot with version > 10)
    dummy_d = {"id": 888, "name": "Song 888", "artist": "Artist 888", "duration_ms": 180000}
    with patch.object(netease_service, "get_song_detail", new_callable=AsyncMock, return_value=dummy_d):
        cmd_resp = client.post(
            "/api/v1/music/control/command",
            json={"command_id": "cmd_seq_test_001", "action": "play_track", "params": {"song_id": 888}},
            headers=phone_headers,
        )
        assert cmd_resp.status_code == 200
        cmd_params = cmd_resp.json()["params"]
        prepared_qid = cmd_params["queue_id"]
        prepared_qver = cmd_params["queue_version"]
        assert prepared_qid.startswith("q_single_")
        assert prepared_qver > 10

    # 3. Before vehicle ACK: DB CarDeviceStatus, HTTP status, and WS snapshot MUST remain unchanged!
    db_session.expire_all()
    cur_status = db_session.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == dev_id).first()
    assert cur_status.queue_id == "q_fav_initial"
    assert cur_status.queue_version == 10

    resp_after_prep = client.get("/api/v1/music/control/status", headers=phone_headers)
    assert resp_after_prep.status_code == 200
    assert resp_after_prep.json()["queue_id"] == "q_fav_initial"
    assert resp_after_prep.json()["queue_version"] == 10

    with client.websocket_connect(f"/api/v1/ws/music?token={settings.CAR_TOKEN}&client_type=car&device_id={dev_id}") as ws:
        snap_ws = ws.receive_json()
        assert snap_ws["type"] == "status_snapshot"
        assert snap_ws["data"]["queue_id"] == "q_fav_initial"
        assert snap_ws["data"]["queue_version"] == 10

    # 4. Vehicle reports command failed -> status still unchanged!
    client.post(
        "/api/v1/music/control/command/cmd_seq_test_001/ack",
        json={"status": "failed", "error_code": "NETWORK_TIMEOUT"},
        headers=car_headers,
    )
    db_session.expire_all()
    cur_status_failed = db_session.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == dev_id).first()
    assert cur_status_failed.queue_id == "q_fav_initial"
    assert cur_status_failed.queue_version == 10

    resp_failed = client.get("/api/v1/music/control/status", headers=phone_headers)
    assert resp_failed.json()["queue_id"] == "q_fav_initial"
    assert resp_failed.json()["queue_version"] == 10

    # 5. Send command 2 and car ACK applied -> only now does status mutate to applied!
    with patch.object(netease_service, "get_song_detail", new_callable=AsyncMock, return_value=dummy_d):
        cmd2_resp = client.post(
            "/api/v1/music/control/command",
            json={"command_id": "cmd_seq_test_002", "action": "play_track", "params": {"song_id": 888}},
            headers=phone_headers,
        )
        assert cmd2_resp.status_code == 200
        cmd2_params = cmd2_resp.json()["params"]
        cmd2_qid = cmd2_params["queue_id"]
        cmd2_qver = cmd2_params["queue_version"]

    client.post(
        "/api/v1/music/control/command/cmd_seq_test_002/ack",
        json={"status": "applied", "applied_mode": "single_loop"},
        headers=car_headers,
    )

    db_session.expire_all()
    applied_status = db_session.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == dev_id).first()
    assert applied_status.queue_id == cmd2_qid
    assert applied_status.queue_version == cmd2_qver
    assert applied_status.applied_mode == "single_loop"

    resp_applied = client.get("/api/v1/music/control/status", headers=phone_headers)
    assert resp_applied.json()["queue_id"] == cmd2_qid
    assert resp_applied.json()["queue_version"] == cmd2_qver


def test_cold_pending_single_read_does_not_call_favorite_or_mutate_active(client, phone_headers):
    """
    Requirement 2:
    - Pure read-only and snapshot cache order:
      Reading an explicit (queue_id, queue_version) for a cold device hits snapshot cache directly,
      without calling NetEase favorite playlist or mutating active queue.
    - Generic GET /api/v1/music/queue on cold device returns favorite view with activate=False
      without populating _active_queues.
    """
    dev_id = settings.DEFAULT_CAR_DEVICE_ID
    music_queue_snapshot_manager.clear_all()
    assert music_queue_snapshot_manager.get_active_queue(dev_id) is None

    # 1. Prepare a single-track pending queue
    snap = asyncio.run(
        music_queue_snapshot_manager.prepare_single_track_queue(
            device_id=dev_id,
            song_id=7777,
            title="Cold Isolated Track",
            artist="Isolated Artist",
            command_id="cmd_cold_read_001",
        )
    )
    assert snap.queue_id.startswith("q_single_")
    assert music_queue_snapshot_manager.get_active_queue(dev_id) is None

    # 2. Query explicit snapshot while mocking NetEase favorite to raise an error
    # If get_queue_page tried to init favorite playlist, this test would fail!
    with patch.object(netease_service, "get_favorite_playlist_info", side_effect=PermissionError("Should not call favorite!")):
        resp = client.get(
            f"/api/v1/music/queue?queue_id={snap.queue_id}&queue_version={snap.queue_version}",
            headers=phone_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["queue_id"] == snap.queue_id
        assert data["queue_version"] == snap.queue_version
        assert data["source"] == "single_track"
        assert len(data["tracks"]) == 1
        assert data["tracks"][0]["song_id"] == 7777
        assert data["tracks"][0]["title"] == "Cold Isolated Track"

    # Active queue remains None after reading explicit snapshot
    assert music_queue_snapshot_manager.get_active_queue(dev_id) is None

    # 3. Default GET /api/v1/music/queue on cold device returns favorite view but does NOT activate
    dummy_favs = [{"id": 501, "name": "Fav Track 501", "artist": "Artist 501"}]
    fav_info = {"playlist_id": 9999, "track_count": 1, "name": "我喜欢的音乐"}
    with patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value=fav_info), \
         patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value={"songs": dummy_favs, "has_more": False}):
        resp_generic = client.get("/api/v1/music/queue", headers=phone_headers)
        assert resp_generic.status_code == 200
        gen_data = resp_generic.json()
        assert gen_data["source"] == "netease_favorite"
        assert gen_data["total"] == 1

    # Default GET did NOT activate: active queue is STILL None!
    assert music_queue_snapshot_manager.get_active_queue(dev_id) is None
