"""Regression tests for the Owner favorite metadata catalog (persistence + diff sync)."""
import asyncio
import json
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.services.favorite_catalog import (
    FavoriteCatalog,
    FavoriteRevisionGone,
    metadata_hash,
    owner_key_for,
)
from app.services.netease_service import NeteaseUpstreamError, favorite_catalog, netease_service


class FakeCookieStore:
    def __init__(self):
        self.cookie = "MUSIC_U=secret-cookie-value"
        self.profile = {"user_id": 1}

    def load(self):
        return self.cookie

    def get_info(self):
        return {"logged_in": bool(self.cookie), "profile": self.profile}


def song(i, title=None, cover=None, fee=0, playable=True):
    return {
        "id": i,
        "title": title or f"T{i}",
        "artist": f"A{i}",
        "album": f"AL{i}",
        "duration_ms": 1000 + i,
        "cover_url": cover or f"https://p1.music.126.net/c{i}.jpg",
        "fee": fee,
        "playable": playable,
    }


class FakeService:
    """Stands in for NeteaseService; counts every upstream-facing call."""

    def __init__(self, n=5):
        self.cookie_store = FakeCookieStore()
        self.uid = 1
        self.pid = 10
        self.name = "Fav"
        self.songs = [song(i) for i in range(1, n + 1)]
        self.version = 1
        self.delay = 0.0
        self.calls = Counter()
        self.info_error = None
        self.detail_gate = None  # asyncio.Event to hold the full fetch open

    def set_songs(self, songs):
        self.songs = songs
        self.version += 1

    async def get_favorite_playlist_info(self, force_refresh=False, max_age=None):
        self.calls["info"] += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.info_error:
            raise self.info_error
        return {
            "playlist_id": self.pid,
            "name": self.name,
            "cover_url": None,
            "track_count": len(self.songs),
            "play_count": 7,
            "user_id": self.uid,
            "signals": [self.version, self.version, len(self.songs)],
        }

    async def get_like_ids(self, uid):
        self.calls["likelist"] += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return [s["id"] for s in self.songs]

    async def fetch_favorite_playlist_detail(self, pid):
        self.calls["detail"] += 1
        if self.detail_gate is not None:
            await self.detail_gate.wait()
        elif self.delay:
            await asyncio.sleep(self.delay)
        return {
            "playlist_id": pid,
            "name": self.name,
            "cover_url": None,
            "track_count": len(self.songs),
            "entries": [dict(s) for s in self.songs],
            "skipped_ids": [],
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "NETEASE_FAV_CHECK_TTL_SECONDS", 0.0)
    monkeypatch.setattr(settings, "NETEASE_FAV_FULL_RECHECK_SECONDS", 3600.0)
    monkeypatch.setattr(settings, "NETEASE_FAV_FORCE_MIN_INTERVAL_SECONDS", 0.0)
    svc = FakeService()
    path = tmp_path / "catalog.json"
    return svc, FavoriteCatalog(svc, path), path


# ---------------------------------------------------------------- first use
@pytest.mark.asyncio
async def test_first_use_without_cache_does_one_full_fetch(env):
    svc, cat, path = env
    out = await cat.sync()
    assert out.status == "initialized" and out.upstream_mode == "full"
    assert [t["id"] for t in out.snapshot.tracks] == [1, 2, 3, 4, 5]
    assert svc.calls == Counter(info=1, detail=1)
    assert path.exists()


@pytest.mark.asyncio
async def test_first_use_upstream_failure_is_error_not_empty(env):
    svc, cat, _ = env
    svc.info_error = NeteaseUpstreamError("boom", 504)
    with pytest.raises(NeteaseUpstreamError):
        await cat.view()
    assert cat.peek() is None


# ----------------------------------------------- unchanged + concurrency/TTL
@pytest.mark.asyncio
async def test_unchanged_concurrent_checks_share_one_gate_and_skip_detail(env):
    svc, cat, _ = env
    first = await cat.sync()
    svc.calls.clear()
    svc.delay = 0.02
    results = await asyncio.gather(*(cat.sync() for _ in range(12)))
    assert {r.snapshot.revision for r in results} == {first.snapshot.revision}
    assert all(r.status == "unchanged" for r in results)
    # one Owner verification + one like-id set; the heavy detail is NOT re-downloaded
    assert svc.calls == Counter(info=1, likelist=1)


@pytest.mark.asyncio
async def test_ttl_window_makes_no_upstream_call(env, monkeypatch):
    svc, cat, _ = env
    await cat.sync()
    monkeypatch.setattr(settings, "NETEASE_FAV_CHECK_TTL_SECONDS", 60.0)
    svc.calls.clear()
    out = await cat.sync()
    assert out.upstream_mode == "ttl" and not svc.calls


# -------------------------------------------------- add/remove/reorder/edit
@pytest.mark.asyncio
async def test_add_remove_reorder_and_same_id_change_publish_new_immutable_revisions(env, monkeypatch):
    svc, cat, _ = env
    r1 = (await cat.sync()).snapshot

    # add (new id at the front, like a fresh like) + remove id 3
    svc.set_songs([song(9)] + [s for s in svc.songs if s["id"] != 3])
    o2 = await cat.sync()
    assert o2.status == "updated" and (o2.added, o2.removed) == (1, 1)
    assert o2.upstream_mode == "full"
    r2 = o2.snapshot
    assert [t["id"] for t in r2.tracks] == [9, 1, 2, 4, 5]
    assert r2.revision > r1.revision
    # the older revision object was not modified in place
    assert [t["id"] for t in r1.tracks] == [1, 2, 3, 4, 5]

    # pure reorder (same set): gate sees changed version hint -> full fetch -> reordered flag
    svc.set_songs(list(reversed(svc.songs)))
    o3 = await cat.sync()
    assert o3.status == "updated" and o3.reordered and (o3.added, o3.removed, o3.updated) == (0, 0, 0)

    # same-id metadata edit is invisible to the gate (same ids/version hint) until the bounded recheck
    edited = [dict(s) for s in svc.songs]
    edited[0]["title"] = "Renamed"
    svc.songs = edited  # no version bump: upstream edited the song itself
    svc.calls.clear()
    o4 = await cat.sync()
    assert o4.status == "unchanged" and o4.upstream_mode == "gate" and "detail" not in svc.calls
    monkeypatch.setattr(settings, "NETEASE_FAV_FULL_RECHECK_SECONDS", 0.0)  # bounded expiry elapsed
    o5 = await cat.sync()
    assert o5.status == "updated" and o5.updated == 1
    assert next(t for t in o5.snapshot.tracks if t["title"] == "Renamed")

    # manual force picks up permission/fee change as well
    changed = [dict(s) for s in svc.songs]
    changed[1].update(playable=False, fee=1)
    svc.songs = changed
    o6 = await cat.sync(force=True)
    assert o6.status == "updated" and o6.updated == 1


def test_cover_scheme_and_cdn_shard_flapping_is_not_a_metadata_change():
    a = song(1, cover="http://p1.music.126.net/abc/1.jpg")
    b = song(1, cover="https://p2.music.126.net/abc/1.jpg")
    assert metadata_hash(a) == metadata_hash(b)
    assert metadata_hash(a) != metadata_hash(song(1, cover="https://p1.music.126.net/other/1.jpg"))


@pytest.mark.asyncio
async def test_identical_full_recheck_keeps_revision(env):
    svc, cat, _ = env
    r1 = (await cat.sync()).snapshot
    out = await cat.sync(force=True)
    assert out.status == "unchanged" and out.snapshot.revision == r1.revision


# ------------------------------------------------------- paging / revisions
@pytest.mark.asyncio
async def test_pinned_paging_never_mixes_revisions_and_old_revision_expires(env):
    svc, cat, _ = env
    snap1, _m = await cat.view()
    svc.set_songs([song(50)] + svc.songs)
    await cat.sync()
    pinned, _m = await cat.view(snap1.revision)
    assert pinned.revision == snap1.revision and pinned.total == 5  # still the old consistent list
    latest, _m = await cat.view()
    assert latest.total == 6
    for k in range(4):  # retention is bounded
        svc.set_songs([song(100 + k)] + svc.songs)
        await cat.sync()
    with pytest.raises(FavoriteRevisionGone):
        await cat.view(snap1.revision)


# ------------------------------------------------- offline vs auth failures
@pytest.mark.asyncio
async def test_timeout_serves_stale_same_owner_but_auth_failure_never_does(env):
    svc, cat, _ = env
    await cat.sync()
    svc.info_error = NeteaseUpstreamError("timed out", 504)
    snap, meta = await cat.view(fresh=True)
    assert meta["stale"] is True and meta["stale_reason"] == "upstream_unavailable" and snap.total == 5

    svc.info_error = PermissionError("Netease session credentials expired or invalid")  # 401/403-like
    with pytest.raises(PermissionError):
        await cat.view(fresh=True)

    svc.info_error = None
    svc.cookie_store.cookie = None  # logged out
    with pytest.raises(PermissionError):
        await cat.view()


@pytest.mark.asyncio
async def test_restart_offline_requires_known_owner_before_serving_stale(env, tmp_path):
    svc, cat, path = env
    await cat.sync()
    # "restart": new process, same file, upstream down
    svc.info_error = NeteaseUpstreamError("down", 502)
    reborn = FavoriteCatalog(svc, path)
    snap, meta = await reborn.view()  # profile stored at login proves the Owner
    assert meta["stale"] and snap.total == 5
    svc.cookie_store.profile = {}  # Owner unknowable -> never guess
    with pytest.raises(NeteaseUpstreamError):
        await FavoriteCatalog(svc, path).view()
    svc.cookie_store.profile = {"user_id": 2}  # a different account's profile
    with pytest.raises(NeteaseUpstreamError):
        await FavoriteCatalog(svc, path).view()


@pytest.mark.asyncio
async def test_strict_profile_distinguishes_expired_login_from_offline():
    def resp(status, body):
        m = MagicMock()
        m.status_code = status
        m.json.return_value = body
        return m

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as g:
        g.return_value = resp(200, {"code": 200, "data": {"code": 200, "account": None, "profile": None}})
        with pytest.raises(PermissionError):
            await netease_service.get_profile_by_cookie("MUSIC_U=x", strict=True)
        g.return_value = resp(401, {})
        with pytest.raises(PermissionError):
            await netease_service.get_profile_by_cookie("MUSIC_U=x", strict=True)
        g.return_value = resp(502, {})
        with pytest.raises(NeteaseUpstreamError):
            await netease_service.get_profile_by_cookie("MUSIC_U=x", strict=True)
        g.side_effect = httpx.TimeoutException("t")
        with pytest.raises(NeteaseUpstreamError):
            await netease_service.get_profile_by_cookie("MUSIC_U=x", strict=True)
        assert await netease_service.get_profile_by_cookie("MUSIC_U=x") is None  # lenient mode unchanged


# --------------------------------------------- account/Owner switch races
@pytest.mark.asyncio
async def test_session_change_during_full_fetch_discards_late_result(env):
    svc, cat, path = env
    first = (await cat.sync()).snapshot
    before = path.read_bytes()
    svc.set_songs([song(77)] + svc.songs)
    svc.detail_gate = asyncio.Event()
    task = asyncio.ensure_future(cat.sync(force=True))
    await asyncio.sleep(0.05)
    assert svc.calls["detail"] == 2  # in flight
    cat.invalidate()  # cookie saved: account may have switched
    svc.cookie_store.cookie = "MUSIC_U=other-account"
    svc.detail_gate.set()
    with pytest.raises(PermissionError):
        await task
    assert cat.peek() is first  # zero writes from the late result
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_other_owner_drops_previous_lineage_without_mixing(env):
    svc, cat, path = env
    await cat.sync()
    svc.uid = 2
    svc.cookie_store.cookie = "MUSIC_U=other"
    svc.cookie_store.profile = {"user_id": 2}
    svc.pid = 20
    svc.set_songs([song(i) for i in (31, 32)])
    cat.invalidate()
    out = await cat.sync()
    assert out.status == "initialized"  # fresh lineage
    assert [t["id"] for t in out.snapshot.tracks] == [31, 32]
    assert out.snapshot.owner_key == owner_key_for(2)
    with pytest.raises(FavoriteRevisionGone):  # old owner's pinned page cannot be served any more
        await cat.view(1)


@pytest.mark.asyncio
async def test_pinned_page_of_previous_owner_is_refused_after_switch(env):
    svc, cat, _ = env
    snap, _m = await cat.view()
    svc.uid = 2
    svc.cookie_store.cookie = "MUSIC_U=other"
    svc.cookie_store.profile = {"user_id": 2}
    svc.pid = 20
    svc.set_songs([song(40)])
    cat.invalidate()
    with pytest.raises(FavoriteRevisionGone):
        await cat.view(snap.revision)


@pytest.mark.asyncio
async def test_netease_logout_clears_memory_and_disk():
    # exercised through the real singleton wiring (cookie_store.clear -> catalog.clear)
    svc = FakeService()
    path = settings.DATA_DIR
    favorite_catalog._service = svc  # noqa: SLF001 - isolate from real cookie store
    try:
        await favorite_catalog.sync()
        assert favorite_catalog.path.exists()
        netease_service.cookie_store.on_clear_callbacks[0]()
        assert favorite_catalog.peek() is None and not favorite_catalog.path.exists()
    finally:
        favorite_catalog._service = netease_service  # noqa: SLF001
        assert path


# --------------------------------------------- persistence / safe failure
@pytest.mark.asyncio
async def test_restart_recovers_catalog_and_gate_uses_persisted_hints(env):
    svc, cat, path = env
    r1 = (await cat.sync()).snapshot
    svc.calls.clear()
    reborn = FavoriteCatalog(svc, path)
    out = await reborn.sync()
    assert out.status == "unchanged" and out.upstream_mode == "gate"
    assert out.snapshot.revision == r1.revision and "detail" not in svc.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["garbage", "truncate", "etag", "schema", "dup_id", "empty"])
async def test_corrupt_file_is_discarded_and_rebuilt(env, damage):
    svc, cat, path = env
    await cat.sync()
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if damage == "garbage":
        path.write_bytes(b"\x00\xff not json")
    elif damage == "truncate":
        path.write_text(raw[: len(raw) // 2], encoding="utf-8")
    elif damage == "etag":
        data["tracks"][0]["title"] = "tampered"
        path.write_text(json.dumps(data), encoding="utf-8")
    elif damage == "schema":
        data["schema"] = 99
        path.write_text(json.dumps(data), encoding="utf-8")
    elif damage == "dup_id":
        data["tracks"][1]["id"] = data["tracks"][0]["id"]
        path.write_text(json.dumps(data), encoding="utf-8")
    else:
        path.write_text("", encoding="utf-8")
    reborn = FavoriteCatalog(svc, path)
    assert reborn.peek() is None and not path.exists()
    out = await reborn.sync()
    assert out.status == "initialized" and out.snapshot.total == 5


@pytest.mark.asyncio
async def test_persisted_file_holds_only_whitelisted_metadata(env):
    svc, cat, path = env
    await cat.sync()
    text = path.read_text(encoding="utf-8")
    assert "secret-cookie-value" not in text and "MUSIC_U" not in text
    assert "stream_url" not in text and "token" not in text.lower()
    data = json.loads(text)
    assert set(data["tracks"][0]) == {"id", "title", "artist", "album", "duration_ms", "cover_url", "fee", "playable"}
    assert data["owner_key"] == owner_key_for(1) and "user_id" not in text
    assert (path.stat().st_mode & 0o777) == 0o600


@pytest.mark.asyncio
async def test_capacity_bound_keeps_memory_only(env, monkeypatch):
    svc, cat, path = env
    monkeypatch.setattr(settings, "NETEASE_FAV_MAX_TRACKS", 3)
    out = await cat.sync()
    assert out.snapshot.total == 5 and not path.exists()  # served from memory, never truncated


# ----------------------------------------------------------- HTTP surface
def _patch_catalog_source(svc):
    return (
        patch.object(netease_service, "get_favorite_playlist_info", svc.get_favorite_playlist_info),
        patch.object(netease_service, "get_like_ids", svc.get_like_ids),
        patch.object(netease_service, "fetch_favorite_playlist_detail", svc.fetch_favorite_playlist_detail),
        patch.object(netease_service.cookie_store, "load", return_value="MUSIC_U=api-cookie"),
    )


def test_http_sync_pages_pin_and_conflict(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "NETEASE_FAV_CHECK_TTL_SECONDS", 0.0)
    monkeypatch.setattr(settings, "NETEASE_FAV_FORCE_MIN_INTERVAL_SECONDS", 0.0)
    svc = FakeService(n=7)
    p1, p2, p3, p4 = _patch_catalog_source(svc)
    with p1, p2, p3, p4:
        s1 = client.post("/api/v1/music/netease/playlist/favorite/sync", json={}, headers=admin_headers)
        assert s1.status_code == 200, s1.text
        j1 = s1.json()
        assert j1["status"] == "initialized" and j1["changed"] is True and j1["total"] == 7 and j1["mode"] == "full"
        rev = j1["revision"]

        s2 = client.post(
            "/api/v1/music/netease/playlist/favorite/sync", json={"known_revision": rev}, headers=admin_headers
        )
        assert s2.json()["changed"] is False and s2.json()["status"] == "unchanged" and s2.json()["mode"] == "gate"

        page = client.get(
            f"/api/v1/music/netease/playlist/favorite?offset=0&limit=3&revision={rev}", headers=admin_headers
        ).json()
        assert page["revision"] == rev and page["has_more"] is True and page["total"] == 7
        assert page["songs"] == page["tracks"]

        svc.set_songs([song(99)] + svc.songs)  # library changes between pages
        s3 = client.post(
            "/api/v1/music/netease/playlist/favorite/sync", json={"known_revision": rev}, headers=admin_headers
        ).json()
        assert s3["status"] == "updated" and s3["changed"] is True and s3["added"] == 1
        page2 = client.get(
            f"/api/v1/music/netease/playlist/favorite?offset=3&limit=10&revision={rev}", headers=admin_headers
        ).json()
        assert page2["revision"] == rev and page2["total"] == 7 and [t["id"] for t in page2["songs"]] == [4, 5, 6, 7]

        for k in range(4):
            svc.set_songs([song(200 + k)] + svc.songs)
            client.post("/api/v1/music/netease/playlist/favorite/sync", json={}, headers=admin_headers)
        gone = client.get(f"/api/v1/music/netease/playlist/favorite?revision={rev}", headers=admin_headers)
        assert gone.status_code == 409
        assert gone.json()["detail"]["error_code"] == "FAVORITE_REVISION_CONFLICT"


def test_http_sync_errors_are_honest_and_role_gated(client, admin_headers, car_headers, monkeypatch):
    monkeypatch.setattr(settings, "NETEASE_FAV_CHECK_TTL_SECONDS", 0.0)
    svc = FakeService()
    p1, p2, p3, p4 = _patch_catalog_source(svc)
    with p1, p2, p3, p4:
        assert client.post("/api/v1/music/netease/playlist/favorite/sync", json={}, headers=admin_headers).status_code == 200
        svc.info_error = NeteaseUpstreamError("timed out", 504)
        r = client.post("/api/v1/music/netease/playlist/favorite/sync", json={}, headers=admin_headers)
        assert r.status_code == 504  # sync never pretends success
        stale_page = client.get("/api/v1/music/netease/playlist/favorite", headers=admin_headers).json()
        assert stale_page["stale"] is False  # inside SWR path (verified catalog) -> served, background check fails quietly
        svc.info_error = PermissionError("Netease session credentials expired or invalid")
        r = client.post("/api/v1/music/netease/playlist/favorite/sync", json={}, headers=admin_headers)
        assert r.status_code == 409
    assert client.post("/api/v1/music/netease/playlist/favorite/sync", json={}, headers=car_headers).status_code == 403
    assert client.post("/api/v1/music/netease/playlist/favorite/sync", json={}).status_code == 401


# --------------------------------------------------- queue snapshot reuse
def test_queue_uses_catalog_authoritative_total_one_detail_and_stable_old_snapshot(
    client, admin_headers, phone_headers, monkeypatch
):
    from app.services.control_service import music_queue_snapshot_manager

    monkeypatch.setattr(settings, "NETEASE_FAV_CHECK_TTL_SECONDS", 0.0)
    monkeypatch.setattr(settings, "NETEASE_FAV_FORCE_MIN_INTERVAL_SECONDS", 0.0)
    music_queue_snapshot_manager.clear_all()
    svc = FakeService(n=250)
    real_info = svc.get_favorite_playlist_info

    async def info_with_stale_hint(force_refresh=False, max_age=None):
        data = await real_info(force_refresh, max_age)
        data["track_count"] = len(svc.songs) - 1  # user/playlist trackCount often disagrees with the real list
        return data

    svc.get_favorite_playlist_info = info_with_stale_hint
    p1, p2, p3, p4 = _patch_catalog_source(svc)
    with p1, p2, p3, p4:
        first = client.get("/api/v1/music/queue?offset=0&limit=100", headers=phone_headers)
        assert first.status_code == 200, first.text
        q1 = first.json()
        assert q1["total"] == 250 and q1["is_complete"] is True and q1["queue_state"] == "ready"
        assert svc.calls["detail"] == 1  # whole queue from ONE ordered fetch, not pages x detail

        again = client.get("/api/v1/music/queue?offset=100&limit=100", headers=phone_headers).json()
        assert again["queue_version"] == q1["queue_version"] and svc.calls["detail"] == 1

        # library changes -> catalog publishes -> a *new* snapshot version; the old one stays intact
        svc.set_songs([song(900)] + svc.songs)
        sync = client.post("/api/v1/music/netease/playlist/favorite/sync", json={}, headers=admin_headers).json()
        assert sync["status"] == "updated"
        fresh = client.get("/api/v1/music/queue?offset=0&limit=5", headers=phone_headers).json()
        assert fresh["queue_version"] > q1["queue_version"] and fresh["total"] == 251
        assert fresh["tracks"][0]["song_id"] == 900

        old = client.get(
            f"/api/v1/music/queue?queue_id={q1['queue_id']}&queue_version={q1['queue_version']}&offset=0&limit=5",
            headers=phone_headers,
        ).json()
        assert old["total"] == 250 and old["tracks"][0]["song_id"] == 1  # old snapshot untouched
    music_queue_snapshot_manager.clear_all()


def test_explicit_favorite_read_uses_new_catalog_but_active_queue_is_never_mutated(
    client, admin_headers, phone_headers, monkeypatch
):
    from app.services.control_service import music_queue_snapshot_manager

    monkeypatch.setattr(settings, "NETEASE_FAV_CHECK_TTL_SECONDS", 0.0)
    monkeypatch.setattr(settings, "NETEASE_FAV_FORCE_MIN_INTERVAL_SECONDS", 0.0)
    music_queue_snapshot_manager.clear_all()
    svc = FakeService(n=5)
    p1, p2, p3, p4 = _patch_catalog_source(svc)
    with p1, p2, p3, p4:
        q1 = client.get("/api/v1/music/queue?source=netease_favorite", headers=phone_headers).json()
        snap1 = music_queue_snapshot_manager.get_snapshot("car_p3pro", q1["queue_id"], q1["queue_version"])
        music_queue_snapshot_manager.set_active_queue("car_p3pro", snap1)  # as if the car applied it

        svc.set_songs([song(77)] + svc.songs)
        assert client.post(
            "/api/v1/music/netease/playlist/favorite/sync", json={}, headers=admin_headers
        ).json()["status"] == "updated"

        # browsing the current queue keeps the applied (playing) snapshot
        current = client.get("/api/v1/music/queue", headers=phone_headers).json()
        assert current["queue_version"] == q1["queue_version"] and current["total"] == 5
        # an explicit favorite read (phone resolving a tapped song) gets the new list
        fresh = client.get("/api/v1/music/queue?source=netease_favorite", headers=phone_headers).json()
        assert fresh["queue_version"] > q1["queue_version"] and fresh["total"] == 6
        assert music_queue_snapshot_manager.get_active_queue("car_p3pro") is snap1  # playing snapshot untouched
        assert [t["song_id"] for t in snap1.tracks] == [1, 2, 3, 4, 5]
    music_queue_snapshot_manager.clear_all()
