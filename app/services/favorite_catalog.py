"""Bounded persistent metadata catalog for the verified Owner's NetEase favorite playlist.

Why this exists
---------------
The upstream ``/playlist/track/all`` endpoint re-downloads the *entire* playlist
detail (all tracks, megabytes of JSON) for every single page and then calls
``/song/detail`` again for the page.  A full queue build therefore cost
``pages x (playlist/detail + song/detail)`` serial upstream round trips.

The catalog keeps one ordered, white-listed metadata list per Owner and
synchronises it with a layered check:

1. TTL window: no upstream call at all.
2. Cheap gate: Owner verification + ``/user/playlist`` version hints
   (``trackUpdateTime``/``trackNumberUpdateTime``/``trackCount``) + the
   unordered ``/likelist`` id set.  Identical hints and identical id set means
   "unchanged": the multi-megabyte playlist detail is *not* downloaded again.
3. Full fetch: one ``/playlist/detail`` call yields the ordered id list *and*
   all track metadata/privileges.  The result is diffed against the previous
   revision (added / removed / reordered / metadata changed) and published
   atomically as a new immutable revision.  A full fetch is also forced after
   a bounded interval so title/cover/permission edits on an existing id are
   eventually picked up, and on explicit manual refresh.

Only white-listed song metadata, order, revision and timestamps are stored.
No audio, CDN/stream URL, Cookie or token is ever written.  Cache identity is
the verified Owner (opaque hash) + playlist id + schema version; a different
Owner, a cleared cookie or a corrupt file drops the catalog, never mixes it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_RETAINED_REVISIONS = 3
_TRACK_FIELDS = ("id", "title", "artist", "album", "duration_ms", "cover_url", "fee", "playable")


class FavoriteRevisionGone(Exception):
    """Requested pinned revision is no longer retained; clients must restart from page 1."""

    def __init__(self, requested: int, current: Optional[int]):
        super().__init__(f"favorite catalog revision {requested} is no longer available")
        self.requested = requested
        self.current = current


def owner_key_for(uid: Any) -> str:
    """Opaque, non-reversible-enough Owner scope key (never the raw uid)."""
    return hashlib.sha256(f"myyaris-fav-owner:{uid}".encode("utf-8")).hexdigest()[:16]


def _cover_identity(url: Optional[str]) -> str:
    """Compare covers by path only: the upstream flips scheme/CDN shard between calls."""
    if not url:
        return ""
    rest = url.split("://", 1)[-1]
    return rest.split("?", 1)[0].split("/", 1)[-1] if "/" in rest else rest


def metadata_hash(entry: Dict[str, Any]) -> str:
    payload = json.dumps(
        [
            entry.get("title"),
            entry.get("artist"),
            entry.get("album"),
            entry.get("duration_ms"),
            _cover_identity(entry.get("cover_url")),
            entry.get("fee"),
            entry.get("playable"),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def compute_etag(name: str, cover_url: Optional[str], tracks: Tuple[Dict[str, Any], ...], skipped: Tuple[int, ...]) -> str:
    h = hashlib.sha256()
    h.update(f"{SCHEMA_VERSION}|{name}|{_cover_identity(cover_url)}|".encode("utf-8"))
    for t in tracks:
        h.update(f"{t['id']}:{metadata_hash(t)};".encode("utf-8"))
    h.update(("|" + ",".join(str(i) for i in skipped)).encode("utf-8"))
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class CatalogSnapshot:
    """Immutable published revision. ``tracks`` entries must never be mutated."""

    revision: int
    etag: str
    owner_key: str
    playlist_id: int
    name: str
    cover_url: Optional[str]
    tracks: Tuple[Dict[str, Any], ...]
    skipped_ids: Tuple[int, ...]
    updated_at: float
    member_ids: frozenset = field(default_factory=frozenset, compare=False)

    @property
    def total(self) -> int:
        return len(self.tracks)


@dataclass
class SyncOutcome:
    status: str  # initialized | updated | unchanged
    snapshot: CatalogSnapshot
    checked_at: float
    added: int = 0
    removed: int = 0
    reordered: bool = False
    updated: int = 0
    upstream_mode: str = "ttl"  # ttl | gate | full


class FavoriteCatalog:
    def __init__(self, service: Any, path: Optional[Path] = None):
        self._service = service
        self._path = Path(path) if path else None
        self._snap: Optional[CatalogSnapshot] = None
        self._recent: Dict[int, CatalogSnapshot] = {}
        self._loaded = False
        self._epoch = 0
        self._verified_sig: Optional[str] = None
        self._verified_owner: Optional[str] = None
        self._checked_at = 0.0
        self._full_checked_at = 0.0
        self._signals: Optional[List[Any]] = None
        self._play_count: Optional[int] = None
        self._flights: Dict[Tuple[int, bool], "asyncio.Task[SyncOutcome]"] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._bg_tasks: set = set()
        self._last_force_at = 0.0
        self._last_force_outcome: Optional[SyncOutcome] = None
        self.metrics: Dict[str, int] = {
            "syncs": 0,
            "ttl_hits": 0,
            "gate_checks": 0,
            "full_fetches": 0,
            "publishes": 0,
            "unchanged": 0,
        }

    # ------------------------------------------------------------------ config
    @property
    def path(self) -> Path:
        return self._path or (Path(settings.DATA_DIR) / "netease_favorite_catalog.json")

    @staticmethod
    def _check_ttl() -> float:
        return float(settings.NETEASE_FAV_CHECK_TTL_SECONDS)

    @staticmethod
    def _full_recheck() -> float:
        return float(settings.NETEASE_FAV_FULL_RECHECK_SECONDS)

    @staticmethod
    def _force_min_interval() -> float:
        return float(settings.NETEASE_FAV_FORCE_MIN_INTERVAL_SECONDS)

    @staticmethod
    def _max_tracks() -> int:
        return int(settings.NETEASE_FAV_MAX_TRACKS)

    @staticmethod
    def _max_file_bytes() -> int:
        return int(settings.NETEASE_FAV_MAX_FILE_BYTES)

    # --------------------------------------------------------------- identity
    def _sig(self) -> Optional[str]:
        cookie = self._service.cookie_store.load()
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest() if cookie else None

    def _known_owner_key(self, sig: Optional[str]) -> Optional[str]:
        """Owner already proven for this exact session, or the profile stored at login time."""
        if sig is None:
            return None
        if sig == self._verified_sig and self._verified_owner:
            return self._verified_owner
        try:
            profile = (self._service.cookie_store.get_info() or {}).get("profile") or {}
        except Exception:
            return None
        uid = profile.get("user_id")
        return owner_key_for(uid) if uid else None

    def _guard(self, epoch: int, sig: Optional[str]) -> None:
        if self._epoch != epoch or self._sig() != sig:
            raise PermissionError("Netease session changed during favorite sync; result discarded.")

    # ----------------------------------------------------------- invalidation
    def invalidate(self) -> None:
        """Cookie saved/refreshed: keep stored metadata but require Owner re-verification."""
        self._epoch += 1
        self._verified_sig = None
        self._verified_owner = None
        self._checked_at = 0.0

    def clear(self) -> None:
        """Cookie cleared (NetEase logout): drop in-memory and on-disk metadata."""
        self._epoch += 1
        self._verified_sig = None
        self._verified_owner = None
        self._checked_at = 0.0
        self._drop_snapshot()

    def _drop_snapshot(self) -> None:
        self._snap = None
        self._recent.clear()
        self._signals = None
        self._full_checked_at = 0.0
        self._loaded = True
        self._remove_file()

    def _remove_file(self) -> None:
        for p in (self.path, self.path.with_name(self.path.name + ".tmp")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("Could not remove favorite catalog file: %s", e)

    # ------------------------------------------------------------ persistence
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        p = self.path
        try:
            if not p.exists():
                return
            if p.stat().st_size > self._max_file_bytes():
                raise ValueError("catalog file exceeds size bound")
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            snap, signals, full_checked_at = self._validate(data)
        except Exception as e:  # corrupt / tampered / foreign schema -> safe invalidation
            logger.warning("Favorite catalog file invalid (%s); discarding.", type(e).__name__)
            self._remove_file()
            return
        self._snap = snap
        self._recent[snap.revision] = snap
        self._signals = signals
        self._full_checked_at = full_checked_at

    def _validate(self, data: Any) -> Tuple[CatalogSnapshot, Optional[List[Any]], float]:
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            raise ValueError("schema")
        owner_key = data.get("owner_key")
        playlist_id = data.get("playlist_id")
        revision = data.get("revision")
        name = data.get("name")
        tracks_raw = data.get("tracks")
        skipped_raw = data.get("skipped_ids", [])
        if (
            not isinstance(owner_key, str)
            or len(owner_key) != 16
            or not isinstance(playlist_id, int)
            or isinstance(playlist_id, bool)
            or playlist_id <= 0
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision <= 0
            or not isinstance(name, str)
            or not isinstance(tracks_raw, list)
            or len(tracks_raw) > self._max_tracks()
            or not isinstance(skipped_raw, list)
        ):
            raise ValueError("envelope")
        cover = data.get("cover_url")
        if cover is not None and not isinstance(cover, str):
            raise ValueError("cover")
        seen = set()
        tracks: List[Dict[str, Any]] = []
        for t in tracks_raw:
            if not isinstance(t, dict):
                raise ValueError("track")
            tid = t.get("id")
            if not isinstance(tid, int) or isinstance(tid, bool) or tid <= 0 or tid in seen:
                raise ValueError("track id")
            seen.add(tid)
            if not isinstance(t.get("title"), str) or not isinstance(t.get("artist"), str):
                raise ValueError("track text")
            if t.get("album") is not None and not isinstance(t.get("album"), str):
                raise ValueError("album")
            dur = t.get("duration_ms")
            if not isinstance(dur, int) or isinstance(dur, bool) or dur < 0:
                raise ValueError("duration")
            if t.get("cover_url") is not None and not isinstance(t.get("cover_url"), str):
                raise ValueError("track cover")
            if not isinstance(t.get("fee"), int) or isinstance(t.get("fee"), bool):
                raise ValueError("fee")
            if t.get("playable") is not None and not isinstance(t.get("playable"), bool):
                raise ValueError("playable")
            tracks.append({k: t.get(k) for k in _TRACK_FIELDS})
        if not all(isinstance(i, int) and not isinstance(i, bool) and i > 0 and i not in seen for i in skipped_raw):
            raise ValueError("skipped ids")
        skipped = tuple(skipped_raw)
        etag = compute_etag(name, cover, tuple(tracks), skipped)
        if data.get("etag") != etag:
            raise ValueError("etag mismatch")
        updated_at = data.get("updated_at")
        if not isinstance(updated_at, (int, float)):
            raise ValueError("updated_at")
        signals = data.get("signals")
        if signals is not None and not isinstance(signals, list):
            raise ValueError("signals")
        full_checked_at = data.get("full_checked_at")
        if not isinstance(full_checked_at, (int, float)):
            full_checked_at = 0.0
        snap = CatalogSnapshot(
            revision=revision,
            etag=etag,
            owner_key=owner_key,
            playlist_id=playlist_id,
            name=name,
            cover_url=cover,
            tracks=tuple(tracks),
            skipped_ids=skipped,
            updated_at=float(updated_at),
            member_ids=frozenset(seen | set(skipped)),
        )
        return snap, signals, float(full_checked_at)

    def _persist(self, snap: CatalogSnapshot) -> None:
        if snap.total > self._max_tracks():
            logger.warning("Favorite catalog has %d tracks (> %d): memory-only, not persisted.", snap.total, self._max_tracks())
            self._remove_file()
            return
        payload = {
            "schema": SCHEMA_VERSION,
            "owner_key": snap.owner_key,
            "playlist_id": snap.playlist_id,
            "revision": snap.revision,
            "etag": snap.etag,
            "name": snap.name,
            "cover_url": snap.cover_url,
            "updated_at": snap.updated_at,
            "full_checked_at": self._full_checked_at,
            "signals": self._signals,
            "skipped_ids": list(snap.skipped_ids),
            "tracks": [{k: t.get(k) for k in _TRACK_FIELDS} for t in snap.tracks],
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > self._max_file_bytes():
            logger.warning("Favorite catalog exceeds %d bytes: memory-only, not persisted.", self._max_file_bytes())
            self._remove_file()
            return
        p = self.path
        tmp = p.with_name(p.name + ".tmp")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except OSError as e:
            logger.warning("Could not persist favorite catalog: %s", e)
            try:
                tmp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------- sync
    def _lock_for_loop(self) -> asyncio.Lock:
        key = id(asyncio.get_running_loop())
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def peek(self) -> Optional[CatalogSnapshot]:
        """Memory-only latest revision (no upstream, no verification)."""
        self._ensure_loaded()
        return self._snap

    @property
    def checked_at(self) -> float:
        return self._checked_at

    @property
    def play_count(self) -> Optional[int]:
        return self._play_count

    async def sync(self, *, force: bool = False) -> SyncOutcome:
        """Coalesced check/update. Concurrent callers share one upstream pass."""
        self._ensure_loaded()
        sig = self._sig()
        if sig is None:
            raise PermissionError("Netease Cloud Music account is not logged in on backend.")
        now = time.time()
        snap = self._snap
        if (
            not force
            and snap is not None
            and self._verified_sig == sig
            and (now - self._checked_at) < self._check_ttl()
        ):
            self.metrics["ttl_hits"] += 1
            return SyncOutcome("unchanged", snap, self._checked_at, upstream_mode="ttl")
        if (
            force
            and snap is not None
            and self._verified_sig == sig
            and self._last_force_outcome is not None
            and (now - self._last_force_at) < self._force_min_interval()
            and self._last_force_outcome.snapshot is snap
        ):
            self.metrics["ttl_hits"] += 1
            return SyncOutcome("unchanged", snap, self._checked_at, upstream_mode="ttl")

        loop = asyncio.get_running_loop()
        key = (id(loop), force)
        task = self._flights.get(key)
        if task is None or task.done():
            task = loop.create_task(self._run(force, sig, self._epoch))
            self._flights[key] = task

            def _done(t: "asyncio.Task[SyncOutcome]", k=key) -> None:
                if self._flights.get(k) is t:
                    self._flights.pop(k, None)
                if not t.cancelled():
                    t.exception()  # mark retrieved; waiters still observe it

            task.add_done_callback(_done)
        return await asyncio.shield(task)

    async def _run(self, force: bool, sig0: str, epoch0: int) -> SyncOutcome:
        async with self._lock_for_loop():
            self._ensure_loaded()
            if self._epoch != epoch0:
                raise PermissionError("Netease session changed during favorite sync; result discarded.")
            snap = self._snap
            now = time.time()
            if (
                not force
                and snap is not None
                and self._verified_sig == sig0
                and (now - self._checked_at) < self._check_ttl()
            ):
                self.metrics["ttl_hits"] += 1
                return SyncOutcome("unchanged", snap, self._checked_at, upstream_mode="ttl")
            self.metrics["syncs"] += 1
            svc = self._service
            # Reuse an Owner resolution made a moment ago (e.g. by the queue builder); manual force never does.
            info = await svc.get_favorite_playlist_info(max_age=0.0 if force else 5.0)
            self._guard(epoch0, sig0)
            uid = info.get("user_id")
            owner_key = owner_key_for(uid)
            pid = info["playlist_id"]
            if snap is not None and (snap.owner_key != owner_key or snap.playlist_id != pid):
                logger.info("Favorite catalog owner/playlist changed; discarding previous lineage.")
                self._drop_snapshot()
                snap = None
            self._play_count = info.get("play_count")
            signals = list(info.get("signals") or [])
            had_snapshot = snap is not None
            full_due = force or snap is None or (now - self._full_checked_at) >= self._full_recheck()

            if snap is not None and not full_due:
                self.metrics["gate_checks"] += 1
                like_ids = await svc.get_like_ids(uid)
                self._guard(epoch0, sig0)
                if (
                    signals == (self._signals or [])
                    and (info.get("name") or snap.name) == snap.name
                    and set(like_ids) == snap.member_ids
                ):
                    self._mark_verified(sig0, owner_key)
                    self.metrics["unchanged"] += 1
                    return SyncOutcome("unchanged", snap, self._checked_at, upstream_mode="gate")

            # Full ordered fetch (one upstream call; metadata comes with it).
            self.metrics["full_fetches"] += 1
            detail = await svc.fetch_favorite_playlist_detail(pid)
            self._guard(epoch0, sig0)
            entries: List[Dict[str, Any]] = detail["entries"]
            skipped: Tuple[int, ...] = tuple(detail.get("skipped_ids") or ())
            declared = detail.get("track_count")
            if not entries and not skipped and (declared or info.get("track_count")):
                raise self._service_error("Upstream favorite detail returned no tracks for a non-empty playlist")
            name = detail.get("name") or info.get("name") or "我喜欢的音乐"
            cover = detail.get("cover_url") or info.get("cover_url")

            old_tracks = snap.tracks if snap is not None else ()
            old = {t["id"]: t for t in old_tracks}
            new_map = {e["id"]: e for e in entries}
            added = [i for i in new_map if i not in old]
            removed = [i for i in old if i not in new_map]
            old_common = [t["id"] for t in old_tracks if t["id"] in new_map]
            new_common = [e["id"] for e in entries if e["id"] in old]
            reordered = old_common != new_common
            updated = [i for i in new_common if metadata_hash(old[i]) != metadata_hash(new_map[i])]
            meta_changed = snap is not None and (
                snap.name != name
                or _cover_identity(snap.cover_url) != _cover_identity(cover)
                or tuple(snap.skipped_ids) != skipped
            )
            changed = snap is None or bool(added or removed or reordered or updated or meta_changed)

            self._guard(epoch0, sig0)  # no await below: publish is atomic w.r.t. session changes
            self._signals = signals
            self._full_checked_at = time.time()
            if not changed:
                assert snap is not None
                self._persist(snap)  # refresh signals/full_checked_at bookkeeping
                self._mark_verified(sig0, owner_key)
                self.metrics["unchanged"] += 1
                outcome = SyncOutcome("unchanged", snap, self._checked_at, upstream_mode="full")
            else:
                tracks = tuple({k: e.get(k) for k in _TRACK_FIELDS} for e in entries)
                revision = max((snap.revision + 1) if snap is not None else 1, int(time.time() * 1000))
                new_snap = CatalogSnapshot(
                    revision=revision,
                    etag=compute_etag(name, cover, tracks, skipped),
                    owner_key=owner_key,
                    playlist_id=pid,
                    name=name,
                    cover_url=cover,
                    tracks=tracks,
                    skipped_ids=skipped,
                    updated_at=time.time(),
                    member_ids=frozenset(set(new_map) | set(skipped)),
                )
                self._persist(new_snap)
                self._snap = new_snap
                self._recent[revision] = new_snap
                for r in sorted(self._recent)[:-_RETAINED_REVISIONS]:
                    self._recent.pop(r, None)
                self._mark_verified(sig0, owner_key)
                self.metrics["publishes"] += 1
                outcome = SyncOutcome(
                    "updated" if had_snapshot else "initialized",
                    new_snap,
                    self._checked_at,
                    added=len(added),
                    removed=len(removed),
                    reordered=reordered,
                    updated=len(updated),
                    upstream_mode="full",
                )
            if force:
                self._last_force_at = time.time()
                self._last_force_outcome = outcome
            return outcome

    @staticmethod
    def _service_error(message: str) -> Exception:
        from app.services.netease_service import NeteaseUpstreamError

        return NeteaseUpstreamError(message, status_code=502)

    def _mark_verified(self, sig: str, owner_key: str) -> None:
        self._verified_sig = sig
        self._verified_owner = owner_key
        self._checked_at = time.time()

    # ------------------------------------------------------------------- read
    def _kick_background(self) -> None:
        loop = asyncio.get_running_loop()
        key = (id(loop), False)
        existing = self._flights.get(key)
        if existing is not None and not existing.done():
            return

        async def _bg() -> None:
            try:
                await self.sync()
            except Exception as e:  # background refresh must never surface
                logger.info("Background favorite refresh skipped: %s", type(e).__name__)

        task = loop.create_task(_bg())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def refresh_in_background(self) -> None:
        """Fire-and-forget TTL-aware check; silent when logged out or nothing is cached yet."""
        self._ensure_loaded()
        if self._snap is None or self._sig() is None:
            return
        if (time.time() - self._checked_at) >= self._check_ttl():
            self._kick_background()

    async def view(
        self,
        revision: Optional[int] = None,
        *,
        fresh: bool = False,
    ) -> Tuple[CatalogSnapshot, Dict[str, Any]]:
        """Resolve the snapshot to serve.

        * ``revision`` pins a retained revision (paging never mixes revisions).
        * ``fresh`` checks upstream first (explicit playback); otherwise serve the
          verified catalog immediately and refresh in the background when due.
        * Auth failure (no/expired session, Owner unprovable) raises PermissionError.
        * Upstream unavailability falls back to the catalog only for the Owner
          already known for this session (``stale`` is reported, never hidden).
        """
        self._ensure_loaded()
        sig = self._sig()
        if sig is None:
            raise PermissionError("Netease Cloud Music account is not logged in on backend.")
        meta: Dict[str, Any] = {"stale": False, "stale_reason": None}

        if revision is not None:
            pinned = self._recent.get(revision)
            if pinned is None:
                raise FavoriteRevisionGone(revision, self._snap.revision if self._snap else None)
            if self._verified_sig != sig or self._verified_owner != pinned.owner_key:
                try:
                    await self.sync()
                except Exception as e:
                    from app.services.netease_service import NeteaseUpstreamError

                    if isinstance(e, NeteaseUpstreamError) and self._known_owner_key(sig) == pinned.owner_key:
                        meta.update(stale=True, stale_reason="upstream_unavailable")
                    else:
                        raise
                else:
                    if self._verified_owner != pinned.owner_key:
                        raise FavoriteRevisionGone(revision, self._snap.revision if self._snap else None)
            return pinned, self._decorate(meta)

        snap = self._snap
        if snap is not None and self._verified_sig == sig and not fresh:
            if (time.time() - self._checked_at) >= self._check_ttl():
                self._kick_background()
            return snap, self._decorate(meta)

        try:
            outcome = await self.sync()
        except Exception as e:
            from app.services.netease_service import NeteaseUpstreamError

            snap = self._snap
            if isinstance(e, NeteaseUpstreamError) and snap is not None and self._known_owner_key(sig) == snap.owner_key:
                meta.update(stale=True, stale_reason="upstream_unavailable")
                return snap, self._decorate(meta)
            raise
        return outcome.snapshot, self._decorate(meta)

    def _decorate(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        meta["checked_at"] = self._checked_at
        meta["play_count"] = self._play_count
        return meta


__all__ = [
    "CatalogSnapshot",
    "FavoriteCatalog",
    "FavoriteRevisionGone",
    "SyncOutcome",
    "metadata_hash",
    "owner_key_for",
]
