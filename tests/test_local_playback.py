"""iPhone-local playback scope and phone<->car handoff queue transfer.

Isolated SQLite, mocked NetEase upstream, real HTTP routes. No vehicle command is ever
executed by a car; nothing is played.
"""
import asyncio
import contextlib
import itertools
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.repositories.models import CarDeviceStatus, MusicCommand, QueueVersionSequence
from app.services.control_service import music_queue_snapshot_manager as manager
from app.services.netease_service import netease_service

SESSION = "phone-session-abcdef0123456789"
OTHER_SESSION = "phone-session-zyxwvu9876543210"
CAR = settings.DEFAULT_CAR_DEVICE_ID
FAV_INFO = {"playlist_id": 1, "track_count": 5, "name": "Fav", "user_id": 7}


@pytest.fixture(autouse=True)
def clean_queue_state(db_session):
    """The test database is shared by the session: leave no vehicle row behind for other modules."""
    def wipe():
        manager.clear_all()
        db_session.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == CAR).delete()
        db_session.query(MusicCommand).filter(MusicCommand.command_id.like("ho_%")).delete(synchronize_session=False)
        db_session.query(QueueVersionSequence).delete()
        db_session.commit()
    wipe()
    yield
    wipe()


def songs(count, start=1000):
    return [
        {"id": start + i, "title": f"S{start + i}", "artist": "A", "album": "B", "duration_ms": 1000, "cover_url": None, "fee": 0, "playable": None}
        for i in range(count)
    ]


def fav_patches(n=5):
    return (
        patch.object(netease_service, "get_favorite_playlist_info", new_callable=AsyncMock, return_value={**FAV_INFO, "track_count": n}),
        patch.object(netease_service, "get_favorite_playlist_tracks", new_callable=AsyncMock, return_value={"songs": songs(n), "has_more": False, "total": n}),
    )


@contextlib.contextmanager
def heart(batch):
    """Mocked authentic upstream; yields the get_intelligence mock."""
    with patch.object(netease_service.cookie_store, "load", return_value="x=1"), \
         patch.object(netease_service, "is_song_in_favorite_playlist", new_callable=AsyncMock, return_value=True), \
         patch.object(netease_service, "get_intelligence", new_callable=AsyncMock, return_value=("intelligence", batch)) as upstream:
        yield upstream


_seq = itertools.count(int(time.time() * 1000) * 1000)


def local_intel(client, headers, rid, seed, batch_index, count=5, session=SESSION):
    return client.post(
        "/api/v1/music/local/intelligence",
        json={"session_id": session, "request_id": rid, "seed_song_id": seed, "batch_index": batch_index, "count": count},
        headers=headers,
    )


def seed_car_state(client, car_headers):
    """A car that is really playing a favorite queue: status row + active snapshot."""
    a, b = fav_patches(5)
    with a, b:
        queue = asyncio.run(manager.init_favorite_queue(CAR, activate=True))
    r = client.post(
        "/api/v1/music/control/status",
        json={
            "device_id": CAR, "playback_state": "playing", "position_ms": 4321, "status_seq": next(_seq),
            "current_track_id": "netease_1002", "queue_id": queue.queue_id, "queue_version": queue.queue_version,
            "queue_source": "netease_favorite", "current_index": 2, "applied_mode": "shuffle",
            "capabilities": ["queue_v2", "handoff_v1"],
        },
        headers=car_headers,
    )
    assert r.status_code == 200
    return queue


def car_fingerprint(client, phone_headers):
    status = client.get(f"/api/v1/music/control/status?device_id={CAR}", headers=phone_headers).json()
    active = manager.get_active_queue(CAR)
    snaps = sorted(k for k in manager._snapshots if k[0] == CAR)
    return (
        status["queue_id"], status["queue_version"], status["current_index"], status["position_ms"],
        status["playback_state"], status["applied_mode"], status["status_seq"],
        (active.queue_id, active.queue_version) if active else None, snaps,
    )


def test_local_favorite_read_never_touches_car_actual(client, phone_headers, car_headers):
    car_queue = seed_car_state(client, car_headers)
    before = car_fingerprint(client, phone_headers)
    a, b = fav_patches(5)
    with a, b:
        first = client.get(f"/api/v1/music/local/queue?session_id={SESSION}&source=netease_favorite&limit=2", headers=phone_headers)
        assert first.status_code == 200
        body = first.json()
        assert body["source"] == "netease_favorite" and body["total"] == 5 and body["has_more"] is True
        assert body["tracks"][0]["stream_url"] == "/api/v1/music/netease/stream/1000"
        rest = client.get(
            f"/api/v1/music/local/queue?session_id={SESSION}&queue_id={body['queue_id']}&queue_version={body['queue_version']}&offset=2&limit=100",
            headers=phone_headers,
        )
        assert rest.status_code == 200 and len(rest.json()["tracks"]) == 3
    assert car_fingerprint(client, phone_headers) == before
    assert manager.get_active_queue(CAR).queue_version == car_queue.queue_version
    # the car scope has no local snapshot and the local scope has no active queue
    assert all(k[0] == CAR or k[0].startswith("local~") for k in manager._snapshots)


def test_local_heart_continues_in_local_scope_only(client, phone_headers, car_headers):
    seed_car_state(client, car_headers)
    before = car_fingerprint(client, phone_headers)
    with heart(songs(5, 2000)) as upstream:
        b0 = local_intel(client, phone_headers, "req_local_b0", 1002, 0)
    assert b0.status_code == 200
    body0 = b0.json()
    assert body0["queue_id"].startswith("q_intel_") and len(body0["tracks"]) == 5
    with heart(songs(5, 3000)) as upstream:
        b1 = local_intel(client, phone_headers, "req_local_b1", 2004, 1)
    assert b1.status_code == 200
    body1 = b1.json()
    assert body1["queue_id"] == body0["queue_id"] and body1["queue_version"] > body0["queue_version"]
    assert len(body1["tracks"]) == 10 and body1["tracks"][5]["queue_index"] == 5
    # replay of the same request id is idempotent and does not call upstream again
    with heart(songs(5, 4000)) as upstream:
        replay = local_intel(client, phone_headers, "req_local_b1", 2004, 1)
        assert replay.json()["queue_version"] == body1["queue_version"]
        assert upstream.call_count == 0
    # the car's actual queue, cursor, mode and sequence are exactly as they were
    assert car_fingerprint(client, phone_headers) == before
    # the continuation read comes from the local scope
    page = client.get(
        f"/api/v1/music/local/queue?session_id={SESSION}&queue_id={body1['queue_id']}&queue_version={body1['queue_version']}&limit=100",
        headers=phone_headers,
    )
    assert page.status_code == 200 and page.json()["total"] == 10


def test_local_heart_rejects_bad_seed_and_stale_batch(client, phone_headers):
    with heart(songs(5, 2000)) as upstream:
        assert local_intel(client, phone_headers, "r0", 1, 0).status_code == 200
    with heart(songs(5, 3000)) as upstream:
        bad_seed = local_intel(client, phone_headers, "r1", 999999, 1)
        assert bad_seed.status_code == 422 and bad_seed.json()["detail"]["error_code"] == "INVALID_SEED_SONG"
        skipped = local_intel(client, phone_headers, "r2", 2004, 3)
        assert skipped.status_code == 409
    # a different phone session has no such heart queue to continue
    with heart(songs(5, 3000)) as upstream:
        other = local_intel(client, phone_headers, "r3", 2004, 1, session=OTHER_SESSION)
        assert other.status_code == 409


def test_sessions_and_accounts_are_isolated(client, phone_headers, admin_headers):
    with heart(songs(5, 2000)) as upstream:
        body = local_intel(client, phone_headers, "r0", 1, 0).json()
    ref = f"queue_id={body['queue_id']}&queue_version={body['queue_version']}"
    assert client.get(f"/api/v1/music/local/queue?session_id={SESSION}&{ref}", headers=phone_headers).status_code == 200
    assert client.get(f"/api/v1/music/local/queue?session_id={OTHER_SESSION}&{ref}", headers=phone_headers).status_code == 409
    assert client.get(f"/api/v1/music/local/queue?session_id={SESSION}&{ref}", headers=admin_headers).status_code == 409


def test_local_endpoints_are_owner_only(client, phone_headers, car_headers):
    url = f"/api/v1/music/local/queue?session_id={SESSION}&source=netease_favorite"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=car_headers).status_code == 403
    for path in ("intelligence",):
        assert client.post(f"/api/v1/music/local/{path}", json={"session_id": SESSION, "seed_song_id": 1}, headers=car_headers).status_code == 403
    for path in ("queue/adopt", "queue/export"):
        assert client.post(f"/api/v1/music/local/{path}", json={"session_id": SESSION, "queue_id": "q", "queue_version": 1}, headers=car_headers).status_code == 403
    assert client.post("/api/v1/music/local/session/close", json={"session_id": SESSION}, headers=car_headers).status_code == 403
    # malformed session ids are refused before any scope is created
    assert client.get("/api/v1/music/local/queue?session_id=short", headers=phone_headers).status_code == 422


def test_registered_user_cannot_use_owner_local_queue(client):
    reg = {"username": "local_user_test", "password": "DriverSecretPassword123"}
    assert client.post("/api/v1/auth/register", json=reg).status_code == 201
    token = client.post("/api/v1/auth/login", json=reg).json()["session_id"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/api/v1/music/local/queue?session_id={SESSION}&source=netease_favorite", headers=headers).status_code == 403
    assert client.post("/api/v1/music/local/intelligence", json={"session_id": SESSION, "seed_song_id": 1}, headers=headers).status_code == 403


def test_local_scope_ids_cannot_address_car_routes(client, phone_headers, car_headers):
    scope = "local~abcdef0123456789abcd"
    assert client.get(f"/api/v1/music/control/status?device_id={scope}", headers=phone_headers).status_code == 422
    assert client.post("/api/v1/music/control/status", json={"device_id": scope, "playback_state": "paused"}, headers=car_headers).status_code in (403, 422)
    assert client.get(f"/api/v1/music/queue?device_id={scope}", headers=phone_headers).status_code == 422
    assert client.post("/api/v1/music/control/command", json={"device_id": scope, "action": "pause"}, headers=phone_headers).status_code == 422
    assert client.post("/api/v1/music/netease/intelligence", json={"device_id": scope, "seed_song_id": 1}, headers=phone_headers).status_code == 422


def test_adopt_car_queue_copies_without_touching_car(client, phone_headers, car_headers):
    seed_car_state(client, car_headers)
    with heart(songs(5, 2000)) as upstream:
        car_heart = client.post(
            "/api/v1/music/netease/intelligence",
            json={"request_id": "car_heart_b0", "seed_song_id": 1001, "batch_index": 0, "count": 5},
            headers=phone_headers,
        ).json()
    # the vehicle (not the phone) promotes its heart queue
    manager.apply_command_queue(CAR, queue_id=car_heart["queue_id"], queue_version=car_heart["queue_version"])
    before = car_fingerprint(client, phone_headers)
    adopt = client.post(
        "/api/v1/music/local/queue/adopt",
        json={"session_id": SESSION, "queue_id": car_heart["queue_id"], "queue_version": car_heart["queue_version"]},
        headers=phone_headers,
    )
    assert adopt.status_code == 200
    ref = adopt.json()
    assert ref["queue_id"] == car_heart["queue_id"] and ref["source"] == "netease_intelligence" and ref["total"] == 5
    assert ref["queue_version"] != car_heart["queue_version"]
    assert car_fingerprint(client, phone_headers) == before
    # the adopted copy continues locally; the car queue is not extended
    with heart(songs(5, 3000)) as upstream:
        cont = local_intel(client, phone_headers, "adopt_b1", 2004, 1)
    assert cont.status_code == 200 and len(cont.json()["tracks"]) == 10
    assert manager.get_active_queue(CAR).total == 5
    # unknown car snapshot -> conflict, nothing created
    miss = client.post(
        "/api/v1/music/local/queue/adopt",
        json={"session_id": SESSION, "queue_id": "q_intel_nope", "queue_version": 3},
        headers=phone_headers,
    )
    assert miss.status_code == 409


def test_export_heart_queue_then_car_applies_only_after_ack(client, phone_headers, car_headers):
    with heart(songs(5, 2000)) as upstream:
        local_heart = local_intel(client, phone_headers, "export_b0", 1, 0).json()
    export = client.post(
        "/api/v1/music/local/queue/export",
        json={"session_id": SESSION, "queue_id": local_heart["queue_id"], "queue_version": local_heart["queue_version"]},
        headers=phone_headers,
    )
    assert export.status_code == 200
    ref = export.json()
    assert ref["queue_id"] == local_heart["queue_id"] and ref["queue_version"] != local_heart["queue_version"]
    # readable in the car scope but not active
    assert manager.get_active_queue(CAR) is None
    page = client.get(
        f"/api/v1/music/queue?device_id={CAR}&queue_id={ref['queue_id']}&queue_version={ref['queue_version']}",
        headers=phone_headers,
    )
    assert page.status_code == 200 and page.json()["source"] == "netease_intelligence"
    client.post(
        "/api/v1/music/control/status",
        json={"device_id": CAR, "playback_state": "paused", "status_seq": next(_seq), "capabilities": ["queue_v2", "handoff_v1"]},
        headers=car_headers,
    )
    cmd = {
        "command_id": "ho_test_prepare",
        "action": "play_track",
        "params": {
            "source": "netease_intelligence", "queue_id": ref["queue_id"], "queue_version": ref["queue_version"],
            "song_id": 2003, "queue_index": 3, "seek_ms": 61000, "autoplay": False,
        },
    }
    sent = client.post("/api/v1/music/control/command", json=cmd, headers=phone_headers)
    assert sent.status_code == 200, sent.text
    params = sent.json()["params"]
    assert params["source"] == "netease_intelligence" and params["queue_index"] == 3 and params["autoplay"] is False
    assert params["seek_ms"] == 61000 and params["track_id"] == "netease_2003"
    assert manager.get_active_queue(CAR) is None  # pending only
    # idempotent retry with the same id and payload
    again = client.post("/api/v1/music/control/command", json=cmd, headers=phone_headers)
    assert again.status_code == 200 and again.json()["command_id"] == "ho_test_prepare"
    # a mismatching source claim is refused before any command is created
    bad = client.post(
        "/api/v1/music/control/command",
        json={**cmd, "command_id": "ho_bad", "params": {**cmd["params"], "source": "netease_favorite"}},
        headers=phone_headers,
    )
    assert bad.status_code == 422 and bad.json()["detail"]["error_code"] == "QUEUE_SOURCE_MISMATCH"
    ack = client.post("/api/v1/music/control/command/ho_test_prepare/ack", json={"status": "applied", "applied_mode": "intelligence"}, headers=car_headers)
    assert ack.status_code == 200
    active = manager.get_active_queue(CAR)
    assert active is not None and active.queue_version == ref["queue_version"]


def test_registered_user_cannot_play_owner_heart_queue_on_car(client, phone_headers, admin_headers):
    reg = {"username": "ho_driver_test", "password": "DriverSecretPassword123"}
    user_id = client.post("/api/v1/auth/register", json=reg).json()["id"]
    assert client.post(f"/api/v1/auth/users/{user_id}/device-binding", json={"device_id": CAR, "can_control": True}, headers=admin_headers).status_code == 200
    headers = {"Authorization": f"Bearer {client.post('/api/v1/auth/login', json=reg).json()['session_id']}"}
    denied = client.post(
        "/api/v1/music/control/command",
        json={"action": "play_track", "params": {"source": "netease_intelligence", "queue_id": "q", "queue_version": 1, "song_id": 1}},
        headers=headers,
    )
    assert denied.status_code == 403 and denied.json()["detail"]["error_code"] == "OWNER_QUEUE_PERMISSION_DENIED"


def test_close_session_and_lru_bound(client, phone_headers):
    a, b = fav_patches(5)
    with a, b:
        body = client.get(f"/api/v1/music/local/queue?session_id={SESSION}&source=netease_favorite", headers=phone_headers).json()
    ref = f"queue_id={body['queue_id']}&queue_version={body['queue_version']}"
    assert client.post("/api/v1/music/local/session/close", json={"session_id": SESSION}, headers=phone_headers).json() == {"status": "closed"}
    assert client.get(f"/api/v1/music/local/queue?session_id={SESSION}&{ref}", headers=phone_headers).status_code == 409
    # idempotent
    assert client.post("/api/v1/music/local/session/close", json={"session_id": SESSION}, headers=phone_headers).status_code == 200
    a, b = fav_patches(5)
    with a, b:
        for i in range(manager._LOCAL_SCOPES_KEPT + 3):
            sid = f"lru-session-{i:016d}"
            assert client.get(f"/api/v1/music/local/queue?session_id={sid}&source=netease_favorite", headers=phone_headers).status_code == 200
    scopes = {k[0] for k in manager._snapshots if k[0].startswith("local~")}
    assert len(scopes) <= manager._LOCAL_SCOPES_KEPT


def test_transfer_replays_same_snapshot_without_allocating_another_version(client, phone_headers, car_headers):
    queue = seed_car_state(client, car_headers)
    payload = {"session_id": SESSION, "queue_id": queue.queue_id, "queue_version": queue.queue_version}
    first = client.post('/api/v1/music/local/queue/adopt', json=payload, headers=phone_headers)
    replay = client.post('/api/v1/music/local/queue/adopt', json=payload, headers=phone_headers)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    local = first.json()
    out = {"session_id": SESSION, "queue_id": local['queue_id'], "queue_version": local['queue_version']}
    before = car_fingerprint(client, phone_headers)
    exported = client.post('/api/v1/music/local/queue/export', json=out, headers=phone_headers)
    again = client.post('/api/v1/music/local/queue/export', json=out, headers=phone_headers)
    assert exported.status_code == again.status_code == 200
    assert exported.json() == again.json()
    after = car_fingerprint(client, phone_headers)
    assert after[:-1] == before[:-1]  # only a new readable snapshot, no actual/pending change
    assert len(after[-1]) == len(before[-1]) + 1


def test_owner_switch_during_heart_request_cannot_publish_the_old_batch(client, phone_headers):
    async def changed_owner(**kwargs):
        manager.clear_all()  # the same callback invoked by a NetEase credential change
        return 'intelligence', songs(5, 2000)
    with heart(songs(5, 2000)), patch.object(netease_service, 'get_intelligence', side_effect=changed_owner):
        result = local_intel(client, phone_headers, 'switched_owner', 1, 0)
    assert result.status_code == 409
    assert not manager._snapshots
    assert not manager._active_queues


def test_owner_switch_during_favorite_pagination_cannot_publish_the_old_list(client, phone_headers):
    async def changed_owner(*args, **kwargs):
        manager.clear_all()
        return {'songs': songs(5), 'has_more': False, 'total': 5}
    a, _ = fav_patches(5)
    with a, patch.object(netease_service, 'get_favorite_playlist_tracks', side_effect=changed_owner):
        result = client.get(f'/api/v1/music/local/queue?session_id={SESSION}&source=netease_favorite', headers=phone_headers)
    assert result.status_code == 409
    assert not manager._snapshots


def test_silent_prepare_is_rejected_for_an_old_queue_v2_car(client, phone_headers, car_headers):
    queue = seed_car_state(client, car_headers)
    updated = client.post('/api/v1/music/control/status', json={
        'device_id': CAR, 'playback_state': 'paused', 'status_seq': next(_seq), 'capabilities': ['queue_v2'],
    }, headers=car_headers)
    assert updated.status_code == 200
    result = client.post('/api/v1/music/control/command', json={
        'command_id': 'ho_old_car', 'action': 'play_track', 'params': {
            'source': 'netease_favorite', 'queue_id': queue.queue_id, 'queue_version': queue.queue_version,
            'song_id': 1002, 'autoplay': False,
        },
    }, headers=phone_headers)
    assert result.status_code == 422
    assert result.json()['detail']['error_code'] == 'CAPABILITY_NOT_SUPPORTED'
