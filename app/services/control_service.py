import asyncio
import collections
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid
from fastapi import HTTPException, WebSocket, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.models import CarDeviceStatus, MusicCommand, QueueVersionSequence, utc_now
from app.schemas.control import (
    CarStatusReportRequest,
    CarStatusResponse,
    MusicCommandAckRequest,
    MusicCommandAckResponse,
    MusicCommandDetailResponse,
    MusicCommandRequest,
    MusicCommandResponse,
)
from app.schemas.netease import MusicQueueResponse, NeteaseQueueTrackItem
from app.services.device_service import is_same_device, resolve_car_device_id
from app.services.netease_service import netease_service, NeteaseUpstreamError

logger = logging.getLogger(__name__)


def _is_expired(dt: Optional[datetime]) -> bool:
    """Checks if a datetime has passed, handling both offset-naive and offset-aware datetimes."""
    if not dt:
        return False
    now = utc_now()
    if dt.tzinfo is None:
        return dt < now.replace(tzinfo=None)
    return dt < now


class WebSocketConnectionManager:
    """Manages active WebSockets for iPhone clients and Car (Ottocast) clients."""

    def __init__(self):
        self.active_connections: Dict[WebSocket, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, client_type: str, device_id: str):
        await websocket.accept()
        norm_device = resolve_car_device_id(device_id)
        async with self._lock:
            self.active_connections[websocket] = {
                "client_type": client_type,
                "device_id": norm_device,
                "raw_device_id": device_id,
                "connected_at": utc_now().isoformat(),
            }
        logger.info(f"WebSocket client connected: type={client_type}, device={device_id} (norm={norm_device}). Total: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            meta = self.active_connections.pop(websocket, None)
        if meta:
            logger.info(f"WebSocket client disconnected: type={meta.get('client_type')}, device={meta.get('raw_device_id')}. Total: {len(self.active_connections)}")

    def has_car_client(self, device_id: str) -> bool:
        """Returns True if there is an active car WebSocket connection for device_id."""
        norm_target = resolve_car_device_id(device_id)
        for meta in self.active_connections.values():
            if meta.get("client_type") == "car" and is_same_device(meta.get("device_id"), norm_target):
                return True
        return False

    async def broadcast_status(self, status_dict: dict, device_id: Optional[str] = None):
        """Broadcasts playback state updates to all connected iPhone/client WebSockets using dual envelope."""
        norm_device = resolve_car_device_id(device_id) if device_id else None
        stale_sockets = []

        async with self._lock:
            targets = list(self.active_connections.items())

        for ws, meta in targets:
            # Broadcast playback status to phone and admin clients only
            if meta.get("client_type") not in ("phone", "admin"):
                continue

            ws_dev = meta.get("device_id")
            if norm_device and ws_dev and not is_same_device(norm_device, ws_dev):
                continue

            client_dev = meta.get("raw_device_id") or status_dict.get("device_id")
            conn_data = {**status_dict}
            if client_dev:
                conn_data["device_id"] = client_dev

            payload = {
                "type": "status_update",
                "timestamp": utc_now().isoformat(),
                **conn_data,
                "data": conn_data,
            }
            try:
                await ws.send_json(payload)
            except Exception as e:
                logger.warning(f"Failed to send status update to WebSocket client: {e}")
                stale_sockets.append(ws)

        if stale_sockets:
            async with self._lock:
                for ws in stale_sockets:
                    self.active_connections.pop(ws, None)

    async def broadcast_command_status(self, status_dict: dict, device_id: str):
        """Broadcasts command execution status (applied/failed) to connected phone clients."""
        norm_device = resolve_car_device_id(device_id)
        stale_sockets = []

        async with self._lock:
            targets = list(self.active_connections.items())

        for ws, meta in targets:
            # Only broadcast command execution updates to phone or admin clients
            if meta.get("client_type") not in ("phone", "admin"):
                continue

            ws_dev = meta.get("device_id")
            if ws_dev and not is_same_device(norm_device, ws_dev):
                continue

            client_dev = meta.get("raw_device_id") or device_id
            conn_data = {**status_dict, "device_id": client_dev}

            payload = {
                "type": "command_status",
                "timestamp": utc_now().isoformat(),
                **conn_data,
                "data": conn_data,
            }
            try:
                await ws.send_json(payload)
            except Exception as e:
                logger.warning(f"Failed to send command status to WebSocket client: {e}")
                stale_sockets.append(ws)

        if stale_sockets:
            async with self._lock:
                for ws in stale_sockets:
                    self.active_connections.pop(ws, None)

    async def dispatch_command(self, command_dict: dict, target_device_id: str) -> int:
        """Dispatches an immediate music command to car WebSocket clients matching target_device_id using dual envelope."""
        norm_target = resolve_car_device_id(target_device_id)
        payload = {
            "type": "music_command",
            "timestamp": utc_now().isoformat(),
            "command_id": command_dict.get("command_id"),
            "device_id": command_dict.get("device_id"),
            "action": command_dict.get("action"),
            "command": command_dict.get("action"),
            "params": command_dict.get("params", {}),
            "data": command_dict,
        }
        delivered_count = 0
        stale_sockets = []

        async with self._lock:
            targets = list(self.active_connections.items())

        for ws, meta in targets:
            if is_same_device(meta.get("device_id"), norm_target) and meta.get("client_type") == "car":
                try:
                    await ws.send_json(payload)
                    delivered_count += 1
                except Exception as e:
                    logger.warning(f"Failed to dispatch command to car WebSocket: {e}")
                    stale_sockets.append(ws)

        if stale_sockets:
            async with self._lock:
                for ws in stale_sockets:
                    self.active_connections.pop(ws, None)

        return delivered_count


class MusicCommandQueueManager:
    """
    In-memory async command queues per device for HTTP long-polling and WS-fallback.
    Tracks command acknowledgements and prevents duplicate execution without premature eviction.
    Supports recovering unacknowledged commands from SQLite on server restart or on demand.
    """

    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}
        self._acknowledged_command_ids: collections.OrderedDict = collections.OrderedDict()
        self._enqueued_command_ids: set = set()
        self._recovered_devices: set = set()
        self._lock = asyncio.Lock()

    def mark_acknowledged(self, command_id: str):
        if not command_id:
            return
        self._acknowledged_command_ids[command_id] = True
        self._enqueued_command_ids.discard(command_id)
        while len(self._acknowledged_command_ids) > 1000:
            self._acknowledged_command_ids.popitem(last=False)

    def is_acknowledged(self, command_id: str) -> bool:
        return bool(command_id and command_id in self._acknowledged_command_ids)

    async def _get_or_create_queue(self, device_id: str) -> asyncio.Queue:
        norm_dev = resolve_car_device_id(device_id)
        async with self._lock:
            if norm_dev not in self._queues:
                self._queues[norm_dev] = asyncio.Queue(maxsize=50)
            return self._queues[norm_dev]

    def clear_all(self):
        """Clears all in-memory queues and tracking state (used in testing)."""
        self._queues.clear()
        self._acknowledged_command_ids.clear()
        self._enqueued_command_ids.clear()
        self._recovered_devices.clear()

    async def recover_from_db(self, db: Session, device_id: Optional[str] = None):
        """
        Recovers unacknowledged, unexpired commands from SQLite into memory queues.
        Populates _acknowledged_command_ids from terminal database records to prevent replay.
        """
        async with self._lock:
            # 1. Populate terminal commands into _acknowledged_command_ids
            term_rows = (
                db.query(MusicCommand.command_id)
                .filter(MusicCommand.status.in_(["applied", "failed", "expired"]))
                .order_by(MusicCommand.created_at.desc())
                .limit(1000)
                .all()
            )
            for (cid,) in term_rows:
                if cid:
                    self._acknowledged_command_ids[cid] = True

            # 2. Query unacknowledged, unexpired commands (FIFO order)
            query = (
                db.query(MusicCommand)
                .filter(MusicCommand.status.in_(["queued", "sent", "received"]))
                .order_by(MusicCommand.created_at.asc())
            )
            if device_id:
                norm_dev = resolve_car_device_id(device_id)
                query = query.filter(MusicCommand.device_id == norm_dev)
                self._recovered_devices.add(norm_dev)
            else:
                self._recovered_devices.add("__all__")

            pending_rows = query.all()
            for row in pending_rows:
                if _is_expired(row.expires_at):
                    row.status = "expired"
                    continue
                if row.command_id in self._acknowledged_command_ids:
                    continue
                if row.command_id in self._enqueued_command_ids:
                    continue

                params_dict = json.loads(row.params_json) if row.params_json else {}
                cmd_dict = {
                    "command_id": row.command_id,
                    "device_id": row.device_id,
                    "action": row.action,
                    "params": params_dict,
                    "timestamp": row.created_at.isoformat(),
                    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                    "sender_role": row.sender_role,
                    "ws_dispatched_at": None,
                }

                target_dev = resolve_car_device_id(row.device_id)
                if target_dev not in self._queues:
                    self._queues[target_dev] = asyncio.Queue(maxsize=50)
                q = self._queues[target_dev]
                if not q.full():
                    await q.put(cmd_dict)
                    self._enqueued_command_ids.add(row.command_id)

            db.commit()

    async def enqueue_command(self, device_id: str, command: dict):
        """Enqueues command for car long-poll delivery."""
        cmd_id = command.get("command_id")
        if cmd_id:
            if self.is_acknowledged(cmd_id):
                return
            if cmd_id in self._enqueued_command_ids:
                return

        queue = await self._get_or_create_queue(device_id)
        if queue.full():
            try:
                evicted = queue.get_nowait()  # Evict oldest unconsumed command to maintain bound
                if isinstance(evicted, dict) and evicted.get("command_id"):
                    self._enqueued_command_ids.discard(evicted.get("command_id"))
            except asyncio.QueueEmpty:
                pass
        await queue.put(command)
        if cmd_id:
            self._enqueued_command_ids.add(cmd_id)

    async def get_next_command(
        self,
        device_id: str,
        timeout_seconds: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> Optional[dict]:
        """
        Polls the next pending, unacknowledged, unexpired command.
        Skips commands that have already been acknowledged or expired.
        Automatically recovers from SQLite if this device has not been recovered yet.
        """
        norm_dev = resolve_car_device_id(device_id)
        if db is not None and norm_dev not in self._recovered_devices and "__all__" not in self._recovered_devices:
            await self.recover_from_db(db, device_id=norm_dev)

        queue = await self._get_or_create_queue(norm_dev)
        start_time = asyncio.get_event_loop().time()
        remaining = float(timeout_seconds) if timeout_seconds and timeout_seconds > 0 else 0.0

        while True:
            if timeout_seconds is None or timeout_seconds <= 0:
                try:
                    cmd = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return None
            else:
                try:
                    cmd = await asyncio.wait_for(queue.get(), timeout=max(0.01, remaining))
                except asyncio.TimeoutError:
                    return None

            cmd_id = cmd.get("command_id")
            if cmd_id:
                self._enqueued_command_ids.discard(cmd_id)

            # 1. Skip if already acknowledged/applied
            if cmd_id and self.is_acknowledged(cmd_id):
                logger.debug(f"Skipping command {cmd_id}: already acknowledged")
                if timeout_seconds and timeout_seconds > 0:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    remaining = float(timeout_seconds) - elapsed
                    if remaining <= 0:
                        return None
                    continue
                else:
                    continue

            # 2. Check expiration
            exp_str = cmd.get("expires_at")
            if exp_str:
                try:
                    exp_dt = datetime.fromisoformat(exp_str)
                    if _is_expired(exp_dt):
                        logger.info(f"Skipping expired command {cmd_id} (expired at {exp_str})")
                        if timeout_seconds and timeout_seconds > 0:
                            elapsed = asyncio.get_event_loop().time() - start_time
                            remaining = float(timeout_seconds) - elapsed
                            if remaining <= 0:
                                return None
                            continue
                        else:
                            continue
                except Exception:
                    pass

            # 3. If command was dispatched via WS very recently (<3s) and WS car is still connected,
            # allow brief grace period for WS ACK before delivering over HTTP
            ws_dispatched_at = cmd.get("ws_dispatched_at")
            if ws_dispatched_at and ws_manager.has_car_client(device_id):
                age = (utc_now() - ws_dispatched_at).total_seconds()
                if age < 3.0:
                    # Car is online via WS and message was just sent; give WS time to ACK
                    logger.debug(f"Command {cmd_id} waiting for WS ACK (age={age:.1f}s)")
                    # Re-enqueue at back of queue
                    await queue.put(cmd)
                    if cmd_id:
                        self._enqueued_command_ids.add(cmd_id)
                    if timeout_seconds and timeout_seconds > 0:
                        await asyncio.sleep(0.5)
                        elapsed = asyncio.get_event_loop().time() - start_time
                        remaining = float(timeout_seconds) - elapsed
                        if remaining <= 0:
                            return None
                        continue
                    else:
                        return None

            return cmd


class QueueSnapshot:
    def __init__(
        self,
        queue_id: str,
        queue_version: int,
        source: str,
        name: str,
        total: int,
        is_complete: bool,
        queue_state: str,
        tracks: List[Dict[str, Any]],
        device_id: str,
        seed_song_id: Optional[int] = None,
        batch_count: int = 1,
    ):
        self.queue_id = queue_id
        self.queue_version = queue_version
        self.source = source
        self.name = name
        self.total = total
        self.is_complete = is_complete
        self.queue_state = queue_state
        self.tracks = tracks
        self.device_id = device_id
        self.seed_song_id = seed_song_id
        self.batch_count = batch_count
        self.created_at = utc_now()
        self.updated_at = utc_now()


class MusicQueueSnapshotManager:
    """
    Manages versioned immutable queue snapshots per vehicle device.
    Supports pure read-only pagination, version conflict detection (409),
    authentic intelligence queue generation and appending, SQLite persistent monotonic versioning,
    and two-phase pending vs applied queue state promotion upon vehicle ACK.
    """

    def __init__(self):
        self._active_queues: Dict[str, QueueSnapshot] = {}
        self._pending_queues: Dict[Tuple[str, str], QueueSnapshot] = {}  # (norm_dev, command_id) -> snapshot
        self._snapshots: Dict[Tuple[str, str, int], QueueSnapshot] = {}  # (norm_dev, queue_id, version) -> snapshot
        self._replenish_cache: Dict[Tuple[str, str], QueueSnapshot] = {}
        self._intelligence_request_payloads = {}
        self._last_version: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    def clear_all(self):
        """Clears all in-memory queue snapshots and replenishment caches."""
        self._active_queues.clear()
        self._pending_queues.clear()
        self._snapshots.clear()
        self._replenish_cache.clear()
        self._intelligence_request_payloads.clear()
        self._last_version.clear()
        logger.info("Cleared all music queue snapshots and caches.")

    def get_next_queue_version(self, device_id: str, db: Optional[Session] = None) -> int:
        """
        Generates the next monotonically increasing queue version for device_id.
        Persists to independent SQLite QueueVersionSequence table to guarantee strictly monotonic
        non-colliding versions across process restarts, memory clears, and system clock rollbacks,
        without mutating CarDeviceStatus.queue_version.
        """
        norm_dev = resolve_car_device_id(device_id)
        close_db = False
        if db is None:
            from app.repositories.database import SessionLocal
            db = SessionLocal()
            close_db = True
        try:
            seq_row = db.query(QueueVersionSequence).filter(QueueVersionSequence.device_id == norm_dev).first()
            if not seq_row:
                seq_row = QueueVersionSequence(
                    device_id=norm_dev,
                    last_allocated_version=0,
                )
                db.add(seq_row)
            current_db = seq_row.last_allocated_version or 0
            in_mem_last = self._last_version.get(norm_dev, 0)
            baseline = max(current_db, in_mem_last)
            epoch_ms = int(time.time() * 1000)
            # Clock rollback protection: if epoch_ms <= baseline, guarantee baseline + 1
            nxt = max(baseline + 1, epoch_ms)
            seq_row.last_allocated_version = nxt
            db.commit()
            self._last_version[norm_dev] = nxt
            return nxt
        finally:
            if close_db:
                db.close()

    def sync_device_queue_version(self, device_id: str, version: int, db: Optional[Session] = None):
        """
        Syncs in-memory and sequence table watermark for queue version.
        Does NOT mutate CarDeviceStatus.queue_version (CarDeviceStatus only updates on applied ACK).
        """
        if version and version > 0:
            norm_dev = resolve_car_device_id(device_id)
            self._last_version[norm_dev] = max(self._last_version.get(norm_dev, 0), version)
            close_db = False
            if db is None:
                from app.repositories.database import SessionLocal
                db = SessionLocal()
                close_db = True
            try:
                seq_row = db.query(QueueVersionSequence).filter(QueueVersionSequence.device_id == norm_dev).first()
                if not seq_row:
                    seq_row = QueueVersionSequence(device_id=norm_dev, last_allocated_version=version)
                    db.add(seq_row)
                    db.commit()
                elif (seq_row.last_allocated_version or 0) < version:
                    seq_row.last_allocated_version = version
                    db.commit()
            finally:
                if close_db:
                    db.close()

    def get_snapshot(
        self,
        device_id: str,
        queue_id: str,
        queue_version: int,
    ) -> Optional[QueueSnapshot]:
        """
        Looks up an immutable snapshot by (device_id, queue_id, queue_version)
        from snapshot cache or pending queues.
        """
        norm_dev = resolve_car_device_id(device_id)
        snap = self._snapshots.get((norm_dev, queue_id, queue_version))
        if not snap:
            for p in self._pending_queues.values():
                if p.device_id == norm_dev and p.queue_id == queue_id and p.queue_version == queue_version:
                    return p
        return snap

    def register_pending_snapshot(
        self,
        device_id: str,
        command_id: str,
        snapshot: QueueSnapshot,
    ):
        """
        Associates an existing immutable snapshot as pending for command_id.
        Does NOT generate a new version, does NOT touch active queue until ACK.
        """
        norm_dev = resolve_car_device_id(device_id)
        self._pending_queues[(norm_dev, command_id)] = snapshot

    def get_active_queue(self, device_id: str) -> Optional[QueueSnapshot]:
        norm_dev = resolve_car_device_id(device_id)
        return self._active_queues.get(norm_dev)

    def set_active_queue(self, device_id: str, snapshot: QueueSnapshot, db: Optional[Session] = None):
        norm_dev = resolve_car_device_id(device_id)
        self._active_queues[norm_dev] = snapshot
        self._snapshots[(norm_dev, snapshot.queue_id, snapshot.queue_version)] = snapshot
        self.sync_device_queue_version(norm_dev, snapshot.queue_version, db=db)

    def apply_command_queue(
        self,
        device_id: str,
        command_id: Optional[str] = None,
        queue_id: Optional[str] = None,
        queue_version: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> Optional[QueueSnapshot]:
        """
        Promotes a pending queue snapshot to active queue upon car ACK ('applied').
        Leaves existing active queue intact if command failed or pending queue not found.
        """
        norm_dev = resolve_car_device_id(device_id)
        snapshot = None
        if command_id and db:
            command = db.query(MusicCommand).filter(MusicCommand.command_id == command_id).first()
            if command and command.status in ("failed", "expired"):
                self.discard_pending_queue(norm_dev, command_id)
                return None
        if command_id:
            snapshot = self._pending_queues.pop((norm_dev, command_id), None)
        if not snapshot and queue_id and queue_version is not None:
            snapshot = self._snapshots.get((norm_dev, queue_id, queue_version))
        if snapshot:
            curr_active = self._active_queues.get(norm_dev)
            if curr_active and curr_active.queue_version > snapshot.queue_version:
                logger.warning(
                    f"Ignored late status report promotion for queue '{snapshot.queue_id}' (v{snapshot.queue_version}): "
                    f"current active queue is newer (v{curr_active.queue_version})"
                )
                return None
            if command_id and curr_active and curr_active.queue_id != snapshot.queue_id and db:
                # Check if this command is older than an already applied command
                status_row = db.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == norm_dev).first()
                if status_row and status_row.last_applied_command_id and status_row.last_applied_command_id != command_id:
                    applied_cmd = db.query(MusicCommand).filter(MusicCommand.command_id == status_row.last_applied_command_id).first()
                    this_cmd = db.query(MusicCommand).filter(MusicCommand.command_id == command_id).first()
                    if applied_cmd and this_cmd and applied_cmd.created_at > this_cmd.created_at:
                        logger.warning(
                            f"Ignored late ACK promotion for older command '{command_id}': "
                            f"newer command '{status_row.last_applied_command_id}' was already applied"
                        )
                        return None
            self._active_queues[norm_dev] = snapshot
            self.sync_device_queue_version(norm_dev, snapshot.queue_version, db=db)
            if db:
                status_row = db.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == norm_dev).first()
                if status_row:
                    status_row.queue_id = snapshot.queue_id
                    status_row.queue_version = snapshot.queue_version
                    if snapshot.source:
                        status_row.queue_source = snapshot.source
                    status_row.updated_at = utc_now()
            logger.info(
                f"Promoted pending queue '{snapshot.queue_id}' (v{snapshot.queue_version}) to active queue for device '{norm_dev}'"
            )
            return snapshot
        return None

    def discard_pending_queue(self, device_id: str, command_id: str):
        """Discards a pending queue snapshot on command failure or timeout without touching active queue."""
        norm_dev = resolve_car_device_id(device_id)
        self._pending_queues.pop((norm_dev, command_id), None)

    def operation_lock(self, device_id):
        # Serialize upstream preparation and continuation for each vehicle.
        if not hasattr(self, "_operation_locks"):
            self._operation_locks = {}
        return self._operation_locks.setdefault(resolve_car_device_id(device_id), asyncio.Lock())

    async def init_favorite_queue(
        self,
        device_id: str,
        force_refresh: bool = False,
        command_id: Optional[str] = None,
        activate: bool = True,
        db: Optional[Session] = None,
    ) -> QueueSnapshot:
        """
        Initializes or refreshes the full favorite playlist queue for device_id.
        Paginates exhaustively until all tracks are loaded, deduplicates, and guards queue readiness.
        """
        norm_dev = resolve_car_device_id(device_id)
        fav_info = await netease_service.get_favorite_playlist_info(force_refresh=force_refresh)
        pid = fav_info["playlist_id"]
        track_count = fav_info["track_count"]

        if track_count == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": 409,
                    "error_code": "EMPTY_PLAYLIST",
                    "error_detail": "Owner favorite playlist is empty (track_count == 0)",
                    "message": "当前登录账号喜欢歌单为空，无法生成播放队列",
                },
            )

        # Full pagination loop with deduplication
        all_songs = []
        seen_ids = set()
        curr_offset = 0
        page_limit = 100
        has_more = True
        while has_more:
            tracks_data = await netease_service.get_favorite_playlist_tracks(offset=curr_offset, limit=page_limit)
            page_songs = tracks_data.get("songs", [])
            if not page_songs:
                break
            for s in page_songs:
                s_id = s["id"]
                if s_id not in seen_ids:
                    seen_ids.add(s_id)
                    all_songs.append(s)
            curr_offset += len(page_songs)
            has_more = curr_offset < track_count
            if len(all_songs) >= track_count:
                break

        if len(all_songs) == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": 409,
                    "error_code": "EMPTY_PLAYLIST",
                    "error_detail": "No tracks could be retrieved for favorite playlist",
                    "message": "喜欢歌单曲目为空",
                },
            )

        is_complete = (len(all_songs) >= track_count)
        queue_state = "ready" if is_complete else "partial"

        formatted_tracks = []
        for idx, s in enumerate(all_songs):
            s_id = s["id"]
            formatted_tracks.append({
                "track_id": f"netease_{s_id}",
                "song_id": s_id,
                "title": s.get("title") or s.get("name") or f"Track {s_id}",
                "artist": s.get("artist") or "Unknown Artist",
                "album": s.get("album"),
                "duration_ms": s.get("duration_ms", 0),
                "cover_url": s.get("cover_url"),
                "stream_url": f"/api/v1/music/netease/stream/{s_id}",
                "fee": s.get("fee", 0),
                "playable": None,  # Rule 3: null when unverified
                "queue_index": idx,
            })

        owner_uid = fav_info.get("user_id") or "owner"
        opaque_hash = hashlib.sha256(f"{owner_uid}:{pid}".encode("utf-8")).hexdigest()[:12]
        queue_id = f"q_fav_{opaque_hash}"
        version = self.get_next_queue_version(norm_dev, db=db)

        snapshot = QueueSnapshot(
            queue_id=queue_id,
            queue_version=version,
            source="netease_favorite",
            name=fav_info.get("name") or "我喜欢的音乐",
            total=len(formatted_tracks),
            is_complete=is_complete,
            queue_state=queue_state,
            tracks=formatted_tracks,
            device_id=norm_dev,
        )

        async with self._lock:
            self._snapshots[(norm_dev, queue_id, version)] = snapshot
            if command_id:
                self._pending_queues[(norm_dev, command_id)] = snapshot
            if activate:
                self._active_queues[norm_dev] = snapshot
                self.sync_device_queue_version(norm_dev, version, db=db)

        return snapshot

    async def prepare_favorite_queue(
        self,
        device_id: str,
        command_id: Optional[str] = None,
        force_refresh: bool = False,
        db: Optional[Session] = None,
    ) -> QueueSnapshot:
        """Prepares favorite queue snapshot without promoting to active queue until car ACK."""
        return await self.init_favorite_queue(
            device_id=device_id,
            force_refresh=force_refresh,
            command_id=command_id,
            activate=False,
            db=db,
        )

    async def create_intelligence_queue(
        self,
        device_id: str,
        seed_song_id: int,
        songs: List[Dict[str, Any]],
        request_id: Optional[str] = None,
        command_id: Optional[str] = None,
        activate: bool = True,
        db: Optional[Session] = None,
    ) -> QueueSnapshot:
        """
        Creates a new intelligence queue for device_id from initial seed.
        """
        norm_dev = resolve_car_device_id(device_id)
        cache_key = (norm_dev, request_id) if request_id else None
        if cache_key and cache_key in self._replenish_cache:
            return self._replenish_cache[cache_key]

        now_ts = int(time.time() * 1000)
        opaque_hash = hashlib.sha256(f"{seed_song_id}:{now_ts}".encode("utf-8")).hexdigest()[:12]
        version = self.get_next_queue_version(norm_dev, db=db)
        opaque_hash = hashlib.sha256(f"{seed_song_id}:{version}:{now_ts}".encode("utf-8")).hexdigest()[:12]
        queue_id = f"q_intel_{opaque_hash}"

        seen_ids = set()
        formatted_tracks = []
        for idx, s in enumerate(songs):
            s_id = s["id"]
            if s_id in seen_ids:
                continue
            seen_ids.add(s_id)
            formatted_tracks.append({
                "track_id": f"netease_{s_id}",
                "song_id": s_id,
                "title": s.get("title") or s.get("name") or f"Track {s_id}",
                "artist": s.get("artist") or "Unknown Artist",
                "album": s.get("album"),
                "duration_ms": s.get("duration_ms", 0),
                "cover_url": s.get("cover_url"),
                "stream_url": f"/api/v1/music/netease/stream/{s_id}",
                "fee": s.get("fee", 0),
                "playable": None,
                "queue_index": len(formatted_tracks),
            })

        snapshot = QueueSnapshot(
            queue_id=queue_id,
            queue_version=version,
            source="netease_intelligence",
            name="心动模式推荐",
            total=len(formatted_tracks),
            is_complete=False,
            queue_state="ready",
            tracks=formatted_tracks,
            device_id=norm_dev,
            seed_song_id=seed_song_id,
            batch_count=1,
        )

        async with self._lock:
            self._snapshots[(norm_dev, queue_id, version)] = snapshot
            if command_id:
                self._pending_queues[(norm_dev, command_id)] = snapshot
            if activate:
                self._active_queues[norm_dev] = snapshot
                self.sync_device_queue_version(norm_dev, version, db=db)
            if cache_key:
                self._replenish_cache[cache_key] = snapshot

        return snapshot

    async def prepare_intelligence_queue(
        self,
        device_id: str,
        seed_song_id: int,
        songs: List[Dict[str, Any]],
        command_id: Optional[str] = None,
        request_id: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> QueueSnapshot:
        """Prepares intelligence queue snapshot without promoting to active queue until car ACK."""
        return await self.create_intelligence_queue(
            device_id=device_id,
            seed_song_id=seed_song_id,
            songs=songs,
            request_id=request_id,
            command_id=command_id,
            activate=False,
            db=db,
        )

    async def append_intelligence_tracks(
        self,
        device_id: str,
        songs: List[Dict[str, Any]],
        request_id: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> QueueSnapshot:
        """
        Appends a replenishment batch of intelligence tracks to active queue.
        Ensures immutable snapshot: creates a new snapshot with monotonic version,
        leaving previous snapshots completely unchanged.
        """
        norm_dev = resolve_car_device_id(device_id)
        cache_key = (norm_dev, request_id) if request_id else None
        if cache_key and cache_key in self._replenish_cache:
            return self._replenish_cache[cache_key]

        active = self._active_queues.get(norm_dev)
        if not active or active.source != "netease_intelligence":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": 409,
                    "error_code": "QUEUE_NOT_READY",
                    "error_detail": "Active queue is not an intelligence queue",
                    "message": "当前播放队列不是心动模式队列，无法续批",
                },
            )

        if active.batch_count >= 5 or len(active.tracks) >= 150:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": 503,
                    "error_code": "INTELLIGENCE_QUEUE_EXHAUSTED",
                    "error_detail": "Intelligence replenishment batch limit reached (max 5 batches / 150 songs)",
                    "message": "心动模式推荐曲目已达上限",
                },
            )

        async with self._lock:
            if cache_key and cache_key in self._replenish_cache:
                return self._replenish_cache[cache_key]

            if active is not self._active_queues.get(norm_dev):
                raise HTTPException(409, detail={"error_code": "QUEUE_VERSION_CONFLICT"})
            # IMMUTABLE SNAPSHOT: shallow copy tracks so old snapshot tracks list is not mutated
            new_tracks = list(active.tracks)
            existing_song_ids = {t["song_id"] for t in new_tracks}
            old_total = len(new_tracks)
            added_count = 0
            for s in songs:
                if len(new_tracks) >= 150:
                    break
                s_id = s["id"]
                if s_id in existing_song_ids:
                    continue
                existing_song_ids.add(s_id)
                new_tracks.append({
                    "track_id": f"netease_{s_id}",
                    "song_id": s_id,
                    "title": s["title"],
                    "artist": s["artist"],
                    "album": s.get("album"),
                    "duration_ms": s.get("duration_ms", 0),
                    "cover_url": s.get("cover_url"),
                    "stream_url": f"/api/v1/music/netease/stream/{s_id}",
                    "fee": s.get("fee", 0),
                    "playable": None,
                    "queue_index": old_total + added_count,
                })
                added_count += 1

            if not added_count:
                raise HTTPException(503, detail={"error_code": "INTELLIGENCE_QUEUE_EXHAUSTED", "message": "心动续批无新增曲目"})
            new_version = self.get_next_queue_version(norm_dev, db=db)
            new_snapshot = QueueSnapshot(
                queue_id=active.queue_id,
                queue_version=new_version,
                source="netease_intelligence",
                name=active.name,
                total=len(new_tracks),
                is_complete=False,
                queue_state="ready",
                tracks=new_tracks,
                device_id=norm_dev,
                seed_song_id=active.seed_song_id,
                batch_count=active.batch_count + 1,
            )

            self._active_queues[norm_dev] = new_snapshot
            self._snapshots[(norm_dev, active.queue_id, new_version)] = new_snapshot
            if cache_key:
                self._replenish_cache[cache_key] = new_snapshot

        return new_snapshot

    async def create_single_track_queue(
        self,
        device_id: str,
        song_id: int,
        title: str,
        artist: str,
        album: Optional[str] = None,
        duration_ms: int = 0,
        cover_url: Optional[str] = None,
        command_id: Optional[str] = None,
        activate: bool = True,
        db: Optional[Session] = None,
    ) -> QueueSnapshot:
        """
        Creates an explicit temporary single-track queue (source="single_track").
        """
        norm_dev = resolve_car_device_id(device_id)
        now_ts = int(time.time() * 1000)
        opaque_hash = hashlib.sha256(f"{song_id}:{now_ts}".encode("utf-8")).hexdigest()[:12]
        queue_id = f"q_single_{opaque_hash}"
        version = self.get_next_queue_version(norm_dev, db=db)

        tracks = [{
            "track_id": f"netease_{song_id}",
            "song_id": song_id,
            "title": title,
            "artist": artist,
            "album": album,
            "duration_ms": duration_ms,
            "cover_url": cover_url,
            "stream_url": f"/api/v1/music/netease/stream/{song_id}",
            "fee": 0,
            "playable": None,
            "queue_index": 0,
        }]

        snapshot = QueueSnapshot(
            queue_id=queue_id,
            queue_version=version,
            source="single_track",
            name=f"单曲: {title}",
            total=1,
            is_complete=True,
            queue_state="ready",
            tracks=tracks,
            device_id=norm_dev,
        )

        async with self._lock:
            self._snapshots[(norm_dev, queue_id, version)] = snapshot
            if command_id:
                self._pending_queues[(norm_dev, command_id)] = snapshot
            if activate:
                self._active_queues[norm_dev] = snapshot
                self.sync_device_queue_version(norm_dev, version, db=db)

        return snapshot

    async def prepare_single_track_queue(
        self,
        device_id: str,
        song_id: int,
        title: str,
        artist: str,
        album: Optional[str] = None,
        duration_ms: int = 0,
        cover_url: Optional[str] = None,
        command_id: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> QueueSnapshot:
        """Prepares single track queue snapshot without promoting to active queue until car ACK."""
        return await self.create_single_track_queue(
            device_id=device_id,
            song_id=song_id,
            title=title,
            artist=artist,
            album=album,
            duration_ms=duration_ms,
            cover_url=cover_url,
            command_id=command_id,
            activate=False,
            db=db,
        )

    async def get_queue_page(
        self,
        device_id: str,
        source: Optional[str] = None,
        queue_id: Optional[str] = None,
        queue_version: Optional[int] = None,
        offset: int = 0,
        limit: int = 50,
        db: Optional[Session] = None,
    ) -> MusicQueueResponse:
        """
        Pure read-only query for queue metadata and paginated tracks.
        Querying with ?source=... NEVER switches the active queue.
        Validates snapshot immutability: returns 409 QUEUE_VERSION_CONFLICT on version mismatch.
        Side-effect free: default GET on cold device does NOT activate any queue into _active_queues.
        Checks snapshot/pending cache first before falling back to NetEase favorite generation.
        """
        norm_dev = resolve_car_device_id(device_id)
        active = self._active_queues.get(norm_dev)
        target = None

        # 1. Explicit snapshot requested with (queue_id, queue_version): check cache FIRST
        if queue_id and queue_version is not None:
            cached = self._snapshots.get((norm_dev, queue_id, queue_version))
            if not cached:
                for snap in self._pending_queues.values():
                    if snap.device_id == norm_dev and snap.queue_id == queue_id and snap.queue_version == queue_version:
                        cached = snap
                        break
            if cached:
                target = cached
            elif active and queue_id == active.queue_id and queue_version != active.queue_version:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": 409,
                        "error_code": "QUEUE_VERSION_CONFLICT",
                        "error_detail": f"Requested queue version {queue_version} does not match current snapshot version {active.queue_version}.",
                        "message": "播放队列版本已变更，请从第一页重新拉取。",
                    },
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": 409,
                        "error_code": "QUEUE_VERSION_CONFLICT",
                        "error_detail": f"Requested queue snapshot ({queue_id}, v{queue_version}) not found.",
                        "message": "播放队列版本已变更，请从第一页重新拉取。",
                    },
                )

        # 2. Paging with queue_id but without queue_version
        elif queue_id is not None:
            if active and active.queue_id == queue_id:
                target = active
            else:
                candidates = [s for (d, q_id, _), s in self._snapshots.items() if d == norm_dev and q_id == queue_id]
                if not candidates:
                    for snap in self._pending_queues.values():
                        if snap.device_id == norm_dev and snap.queue_id == queue_id:
                            candidates.append(snap)
                if candidates:
                    target = max(candidates, key=lambda s: s.queue_version)
                else:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": 409,
                            "error_code": "QUEUE_VERSION_CONFLICT",
                            "error_detail": f"Requested queue_id {queue_id} not found.",
                            "message": "播放队列不存在或已过期，请重新获取最新队列。",
                        },
                    )

        # 3. Generic query without queue_id (e.g. GET /api/v1/music/queue)
        else:
            if active is not None and (not source or active.source == source):
                if queue_version is not None and queue_version != active.queue_version:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": 409,
                            "error_code": "QUEUE_VERSION_CONFLICT",
                            "error_detail": f"Requested queue version {queue_version} does not match current snapshot version {active.queue_version}.",
                            "message": "播放队列版本已变更，请从第一页重新拉取。",
                        },
                    )
                target = active
            elif source == "netease_intelligence":
                intel_candidates = [s for (d, _, _), s in self._snapshots.items() if d == norm_dev and s.source == "netease_intelligence"]
                if intel_candidates:
                    target = max(intel_candidates, key=lambda s: s.queue_version)
                else:
                    raise HTTPException(409, detail={"error_code": "QUEUE_NOT_READY", "message": "尚无真实心动队列"})
            else:
                # Cold device or pure read query for a different source (without activating)
                fav_candidates = [s for (d, _, _), s in self._snapshots.items() if d == norm_dev and s.source == "netease_favorite"]
                if fav_candidates and queue_version is None:
                    fav_snap = max(fav_candidates, key=lambda s: s.queue_version)
                else:
                    fav_snap = await self.prepare_favorite_queue(norm_dev, db=db)
                if queue_version is not None and queue_version != fav_snap.queue_version:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": 409,
                            "error_code": "QUEUE_VERSION_CONFLICT",
                            "error_detail": f"Requested queue version {queue_version} does not match snapshot version {fav_snap.queue_version}.",
                            "message": "播放队列版本已变更，请从第一页重新拉取。",
                        },
                    )
                target = fav_snap

        limit_bounded = max(1, min(100, limit))
        total = target.total
        tracks_slice = target.tracks[offset : offset + limit_bounded]
        has_more = (offset + len(tracks_slice)) < total

        items = [NeteaseQueueTrackItem(**t) for t in tracks_slice]

        return MusicQueueResponse(
            code=200,
            queue_id=target.queue_id,
            queue_version=target.queue_version,
            source=target.source,
            name=target.name,
            total=total,
            is_complete=target.is_complete,
            batch_index=max(0, target.batch_count - 1),
            queue_state=target.queue_state,
            offset=offset,
            limit=limit_bounded,
            has_more=has_more,
            tracks=items,
        )


# Global singletons
ws_manager = WebSocketConnectionManager()
command_queue_manager = MusicCommandQueueManager()
music_queue_snapshot_manager = MusicQueueSnapshotManager()
netease_service.cookie_store.on_change_callbacks.append(music_queue_snapshot_manager.clear_all)


# --- Database Operations for Command Lifecycle ---

def create_or_get_music_command(
    db: Session,
    cmd_req: MusicCommandRequest,
    target_device: str,
    sender_role: str = "phone",
) -> Tuple[MusicCommand, bool]:
    """
    Creates a new command or returns an existing one for idempotent calls.
    Raises HTTPException 409 Conflict if command_id exists for a different device or with different payload.
    """
    cmd_id = cmd_req.command_id or f"cmd_music_{uuid.uuid4().hex[:12]}"
    norm_dev = resolve_car_device_id(target_device)
    incoming_params = cmd_req.params.model_dump(exclude_unset=True) if cmd_req.params else {}

    existing = db.query(MusicCommand).filter(MusicCommand.command_id == cmd_id).first()
    if existing:
        # Check cross-device collision
        if not is_same_device(existing.device_id, norm_dev):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": 409,
                    "error_code": "COMMAND_CONFLICT",
                    "error_detail": f"Command ID '{cmd_id}' already exists for another device '{existing.device_id}'.",
                    "message": "指令ID冲突：相同指令ID已绑定不同设备",
                },
            )
        # Check payload mismatch
        existing_params = json.loads(existing.params_json) if existing.params_json else {}
        if existing.action != cmd_req.action or existing_params != incoming_params:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": 409,
                    "error_code": "COMMAND_CONFLICT",
                    "error_detail": f"Command ID '{cmd_id}' already exists with different action or parameters.",
                    "message": "指令ID冲突：相同指令ID已存在且携带不同参数",
                },
            )
        return existing, False

    # Calculate TTL based on action type
    if cmd_req.expires_in_seconds:
        ttl = cmd_req.expires_in_seconds
    elif cmd_req.action in ("seek", "set_volume"):
        ttl = 20  # Fast expiry for transient seek / volume adjustments
    else:
        ttl = 60  # Default 60 seconds

    now = utc_now()
    expires_at = now + timedelta(seconds=ttl)

    row = MusicCommand(
        command_id=cmd_id,
        device_id=norm_dev,
        action=cmd_req.action,
        params_json=json.dumps(incoming_params),
        status="queued",
        sender_role=sender_role,
        created_at=now,
        expires_at=expires_at,
        delivery_attempts=0,
    )
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
        return row, True
    except IntegrityError:
        db.rollback()
        # Handle concurrent insertion of the same command_id
        existing = db.query(MusicCommand).filter(MusicCommand.command_id == cmd_id).first()
        if existing:
            if not is_same_device(existing.device_id, norm_dev):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": 409,
                        "error_code": "COMMAND_CONFLICT",
                        "error_detail": f"Command ID '{cmd_id}' already exists for another device '{existing.device_id}'.",
                        "message": "指令ID冲突：相同指令ID已绑定不同设备",
                    },
                )
            existing_params = json.loads(existing.params_json) if existing.params_json else {}
            if existing.action != cmd_req.action or existing_params != incoming_params:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": 409,
                        "error_code": "COMMAND_CONFLICT",
                        "error_detail": f"Command ID '{cmd_id}' already exists with different action or parameters.",
                        "message": "指令ID冲突：相同指令ID已存在且携带不同参数",
                    },
                )
            return existing, False
        raise


def record_command_sent(
    db: Session,
    command_id: str,
    channel: str,
) -> Optional[MusicCommand]:
    """Records that a command was dispatched via WS or retrieved via HTTP."""
    cmd = db.query(MusicCommand).filter(MusicCommand.command_id == command_id).first()
    if not cmd:
        return None

    if cmd.status == "queued":
        cmd.status = "sent"
    cmd.dispatched_channel = channel
    cmd.delivery_attempts += 1
    cmd.sent_at = utc_now()
    db.commit()
    db.refresh(cmd)
    return cmd


def record_command_ack(
    db: Session,
    command_id: str,
    ack_status: str,
    car_device_id: str,
    error_code: Optional[str] = None,
    error_detail: Optional[str] = None,
    applied_mode: Optional[str] = None,
    queue_version: Optional[int] = None,
    current_index: Optional[int] = None,
    current_track_id: Optional[str] = None,
) -> Optional[MusicCommand]:
    """
    Records vehicle acknowledgement of command execution.
    Supports status: received, applied (executed), failed.
    Prevents cross-device ack.
    Terminal states (applied, failed, expired) are immutable and idempotent.
    """
    cmd = db.query(MusicCommand).filter(MusicCommand.command_id == command_id).first()
    if not cmd:
        return None

    # Device ownership isolation
    if not is_same_device(cmd.device_id, car_device_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: Car token for '{car_device_id}' cannot acknowledge commands for device '{cmd.device_id}'.",
        )

    # Terminal state protection: applied, failed, expired cannot be rolled back or mutated
    if cmd.status in ("applied", "failed", "expired"):
        command_queue_manager.mark_acknowledged(command_id)
        return cmd

    norm_status = ack_status.lower().strip()
    status_row = db.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == cmd.device_id).first()
    previous = (db.query(MusicCommand).filter(MusicCommand.command_id == status_row.last_applied_command_id).first()
                if status_row and status_row.last_applied_command_id else None)
    if _is_expired(cmd.expires_at) or (previous and previous.command_id != command_id and
                                     previous.created_at > cmd.created_at):
        cmd.status = "expired"
        command_queue_manager.mark_acknowledged(command_id)
        music_queue_snapshot_manager.discard_pending_queue(cmd.device_id, command_id)
        db.commit()
        return cmd
    if norm_status in ("applied", "executed", "ok", "success"):
        cmd.status = "applied"
        command_queue_manager.mark_acknowledged(command_id)

        # Update CarDeviceStatus with execution confirmation
        status_row = db.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == cmd.device_id).first()
        if not status_row:
            status_row = CarDeviceStatus(
                device_id=cmd.device_id,
                playback_state="stopped",
            )
            db.add(status_row)
        if status_row:
            status_row.last_applied_command_id = command_id
            cmd_params = json.loads(cmd.params_json) if cmd.params_json else {}
            if applied_mode:
                status_row.applied_mode = applied_mode
            elif cmd.action == "set_playback_mode" and cmd_params.get("mode"):
                status_row.applied_mode = cmd_params["mode"]
            elif cmd.action == "play_track" and cmd_params.get("source") == "netease_favorite":
                if status_row.queue_source != "netease_favorite" or status_row.applied_mode not in ("list_loop", "shuffle"):
                    status_row.applied_mode = "list_loop"

            if cmd.action == "switch_queue" and cmd_params.get("source"):
                status_row.queue_source = cmd_params["source"]
            elif cmd.action == "set_playback_mode" and cmd_params.get("mode") == "intelligence":
                status_row.queue_source = "netease_intelligence"
            elif cmd.action == "set_playback_mode" and cmd_params.get("mode") in ("list_loop", "shuffle"):
                status_row.queue_source = "netease_favorite"
            elif cmd.action == "play_track":
                if cmd_params.get("source"):
                    status_row.queue_source = cmd_params["source"]
                elif cmd_params.get("song_id"):
                    status_row.queue_source = "single_track"

            if cmd_params.get("queue_id"):
                status_row.queue_id = cmd_params["queue_id"]
            if queue_version is not None:
                status_row.queue_version = queue_version
            elif cmd_params.get("queue_version") is not None:
                status_row.queue_version = cmd_params["queue_version"]
            if current_index is not None:
                status_row.current_index = current_index
            elif cmd_params.get("queue_index") is not None:
                status_row.current_index = cmd_params["queue_index"]
            if current_track_id:
                status_row.current_track_id = current_track_id
            elif cmd_params.get("track_id"):
                status_row.current_track_id = cmd_params["track_id"]
            status_row.updated_at = utc_now()

        # Promote pending queue snapshot to active queue upon car execution ACK
        music_queue_snapshot_manager.apply_command_queue(
            device_id=cmd.device_id,
            command_id=command_id,
            queue_id=cmd_params.get("queue_id") if status_row else None,
            queue_version=(queue_version if queue_version is not None else cmd_params.get("queue_version")) if status_row else None,
            db=db,
        )
    elif norm_status in ("failed", "error"):
        cmd.status = "failed"
        cmd.error_code = error_code
        cmd.error_detail = error_detail
        command_queue_manager.mark_acknowledged(command_id)
        music_queue_snapshot_manager.discard_pending_queue(cmd.device_id, command_id)
    elif norm_status == "received":
        cmd.status = "received"
        # DO NOT call mark_acknowledged: received is not execution completion
    else:
        cmd.status = norm_status

    cmd.acknowledged_at = utc_now()
    db.commit()
    db.refresh(cmd)
    return cmd


def get_music_command_by_id(db: Session, command_id: str) -> Optional[MusicCommand]:
    cmd = db.query(MusicCommand).filter(MusicCommand.command_id == command_id).first()
    if cmd and cmd.status in ("queued", "sent") and _is_expired(cmd.expires_at):
        cmd.status = "expired"
        db.commit()
        db.refresh(cmd)
    return cmd


def music_command_to_response(row: MusicCommand, dispatched_to_ws: bool = False) -> MusicCommandResponse:
    params_dict = json.loads(row.params_json) if row.params_json else {}
    return MusicCommandResponse(
        status="ok",
        lifecycle_status=row.status,
        command_id=row.command_id,
        device_id=row.device_id,
        action=row.action,
        params=params_dict,
        queued_at=row.created_at.isoformat(),
        dispatched_to_ws=dispatched_to_ws,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
    )


def music_command_to_detail(row: MusicCommand) -> MusicCommandDetailResponse:
    params_dict = json.loads(row.params_json) if row.params_json else {}
    return MusicCommandDetailResponse(
        command_id=row.command_id,
        device_id=row.device_id,
        action=row.action,
        params=params_dict,
        status=row.status,
        sender_role=row.sender_role,
        dispatched_channel=row.dispatched_channel,
        delivery_attempts=row.delivery_attempts,
        created_at=row.created_at.isoformat(),
        expires_at=row.expires_at.isoformat(),
        sent_at=row.sent_at.isoformat() if row.sent_at else None,
        acknowledged_at=row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        error_code=row.error_code,
        error_detail=row.error_detail,
    )


# --- Car Playback Status Management ---

def save_or_update_car_status(
    db: Session,
    report: CarStatusReportRequest,
    default_device_id: Optional[str] = None,
) -> CarDeviceStatus:
    dev_id = resolve_car_device_id(report.device_id, default_device_id)
    status_row = db.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == dev_id).first()

    now = utc_now()
    if status_row and report.last_applied_command_id and status_row.last_applied_command_id != report.last_applied_command_id:
        incoming = db.query(MusicCommand).filter(MusicCommand.command_id == report.last_applied_command_id).first()
        previous = db.query(MusicCommand).filter(MusicCommand.command_id == status_row.last_applied_command_id).first()
        if incoming and (incoming.status in ("failed", "expired") or
                         (previous and previous.created_at > incoming.created_at)):
            return status_row
    if not status_row:
        # Default capabilities must be empty list so legacy car units are not falsely upgraded
        caps_str = json.dumps(report.capabilities) if report.capabilities is not None else json.dumps([])
        status_row = CarDeviceStatus(
            device_id=dev_id,
            playback_state=report.playback_state,
            current_track_id=report.current_track_id,
            position_ms=report.position_ms,
            duration_ms=report.duration_ms or 0,
            software_volume=report.software_volume if report.software_volume is not None else 1.0,
            loop_mode=report.loop_mode,
            shuffle_enabled=bool(report.shuffle),
            applied_mode=report.applied_mode,
            requested_mode=report.requested_mode,
            queue_id=report.queue_id,
            queue_version=report.queue_version,
            queue_source=report.queue_source,
            current_index=report.current_index or 0,
            consecutive_failures=report.consecutive_failures or 0,
            error_code=report.error_code,
            error_detail=report.error_detail,
            capabilities_json=caps_str,
            status_seq=report.status_seq or 0,
            last_applied_command_id=report.last_applied_command_id,
            last_heartbeat_at=now,
            updated_at=now,
        )
        db.add(status_row)
    else:
        # Monotonic status sequence protection: reject out-of-order older status snapshots
        if report.status_seq is not None and status_row.status_seq is not None:
            if report.status_seq <= status_row.status_seq:
                logger.warning(
                    f"Ignored out-of-order status report for device {dev_id}: "
                    f"report.status_seq ({report.status_seq}) < current ({status_row.status_seq})"
                )
                return status_row

        if report.status_seq is not None:
            status_row.status_seq = report.status_seq

        status_row.playback_state = report.playback_state
        if report.current_track_id is not None:
            status_row.current_track_id = report.current_track_id
        status_row.position_ms = report.position_ms
        if report.duration_ms is not None and report.duration_ms > 0:
            status_row.duration_ms = report.duration_ms
        if report.software_volume is not None:
            status_row.software_volume = report.software_volume
        if report.loop_mode is not None:
            status_row.loop_mode = report.loop_mode
        if report.shuffle is not None:
            status_row.shuffle_enabled = bool(report.shuffle)
        if report.applied_mode is not None:
            status_row.applied_mode = report.applied_mode
        if report.requested_mode is not None:
            status_row.requested_mode = report.requested_mode
        if report.queue_id is not None:
            status_row.queue_id = report.queue_id
        if report.queue_version is not None:
            status_row.queue_version = report.queue_version
        if report.queue_source is not None:
            status_row.queue_source = report.queue_source
        if report.current_index is not None:
            status_row.current_index = report.current_index
        if report.consecutive_failures is not None:
            status_row.consecutive_failures = report.consecutive_failures
        status_row.error_code = report.error_code
        status_row.error_detail = report.error_detail
        if report.capabilities is not None:
            status_row.capabilities_json = json.dumps(report.capabilities)
        if report.last_applied_command_id is not None:
            status_row.last_applied_command_id = report.last_applied_command_id

        status_row.last_heartbeat_at = now
        status_row.updated_at = now

    if report.last_applied_command_id:
        record_command_ack(db, report.last_applied_command_id, "applied", dev_id,
                           applied_mode=report.applied_mode, queue_version=report.queue_version,
                           current_index=report.current_index)
        music_queue_snapshot_manager.apply_command_queue(
            device_id=dev_id,
            command_id=report.last_applied_command_id,
            queue_id=report.queue_id,
            queue_version=report.queue_version,
            db=db,
        )

    db.commit()
    db.refresh(status_row)
    return status_row


def get_car_status_by_id(db: Session, device_id: Optional[str] = None) -> Optional[CarDeviceStatus]:
    dev_id = resolve_car_device_id(device_id)
    return db.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == dev_id).first()


def car_status_to_response(row: CarDeviceStatus) -> CarStatusResponse:
    caps = []
    if row.capabilities_json:
        try:
            caps = json.loads(row.capabilities_json)
        except Exception:
            caps = []
    return CarStatusResponse(
        device_id=row.device_id,
        playback_state=row.playback_state,
        current_track_id=row.current_track_id,
        position_ms=row.position_ms,
        duration_ms=row.duration_ms,
        software_volume=row.software_volume,
        loop_mode=row.loop_mode,
        shuffle_enabled=row.shuffle_enabled,
        applied_mode=row.applied_mode or "list_loop",
        requested_mode=row.requested_mode or "list_loop",
        queue_id=row.queue_id,
        queue_version=row.queue_version if row.queue_version is not None else 1,
        queue_source=row.queue_source or "netease_favorite",
        current_index=row.current_index or 0,
        consecutive_failures=row.consecutive_failures or 0,
        error_code=row.error_code,
        error_detail=row.error_detail,
        capabilities=caps,
        status_seq=row.status_seq or 0,
        last_applied_command_id=row.last_applied_command_id,
        last_heartbeat_at=row.last_heartbeat_at.isoformat(),
        updated_at=row.updated_at.isoformat() if row.updated_at else row.last_heartbeat_at.isoformat(),
    )
