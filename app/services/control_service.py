import asyncio
import collections
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid
from fastapi import HTTPException, WebSocket, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.models import CarDeviceStatus, MusicCommand, utc_now
from app.schemas.control import (
    CarStatusReportRequest,
    CarStatusResponse,
    MusicCommandAckRequest,
    MusicCommandAckResponse,
    MusicCommandDetailResponse,
    MusicCommandRequest,
    MusicCommandResponse,
)
from app.services.device_service import is_same_device, resolve_car_device_id

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


# Global singletons
ws_manager = WebSocketConnectionManager()
command_queue_manager = MusicCommandQueueManager()


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
                detail=f"Command ID '{cmd_id}' already exists for another device '{existing.device_id}'.",
            )
        # Check payload mismatch
        existing_params = json.loads(existing.params_json) if existing.params_json else {}
        if existing.action != cmd_req.action or existing_params != incoming_params:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Command ID '{cmd_id}' already exists with different action or parameters.",
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
                    detail=f"Command ID '{cmd_id}' already exists for another device '{existing.device_id}'.",
                )
            existing_params = json.loads(existing.params_json) if existing.params_json else {}
            if existing.action != cmd_req.action or existing_params != incoming_params:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Command ID '{cmd_id}' already exists with different action or parameters.",
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
    if norm_status in ("applied", "executed", "ok", "success"):
        cmd.status = "applied"
        command_queue_manager.mark_acknowledged(command_id)
    elif norm_status in ("failed", "error"):
        cmd.status = "failed"
        cmd.error_code = error_code
        cmd.error_detail = error_detail
        command_queue_manager.mark_acknowledged(command_id)
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
    if not status_row:
        status_row = CarDeviceStatus(
            device_id=dev_id,
            playback_state=report.playback_state,
            current_track_id=report.current_track_id,
            position_ms=report.position_ms,
            duration_ms=report.duration_ms or 0,
            software_volume=report.software_volume if report.software_volume is not None else 1.0,
            loop_mode=report.loop_mode or "all",
            shuffle_enabled=bool(report.shuffle),
            last_heartbeat_at=now,
            updated_at=now,
        )
        db.add(status_row)
    else:
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
        status_row.last_heartbeat_at = now
        status_row.updated_at = now

    db.commit()
    db.refresh(status_row)
    return status_row


def get_car_status_by_id(db: Session, device_id: Optional[str] = None) -> Optional[CarDeviceStatus]:
    dev_id = resolve_car_device_id(device_id)
    return db.query(CarDeviceStatus).filter(CarDeviceStatus.device_id == dev_id).first()


def car_status_to_response(row: CarDeviceStatus) -> CarStatusResponse:
    return CarStatusResponse(
        device_id=row.device_id,
        playback_state=row.playback_state,
        current_track_id=row.current_track_id,
        position_ms=row.position_ms,
        duration_ms=row.duration_ms,
        software_volume=row.software_volume,
        loop_mode=row.loop_mode,
        shuffle_enabled=row.shuffle_enabled,
        last_heartbeat_at=row.last_heartbeat_at.isoformat(),
        updated_at=row.updated_at.isoformat() if row.updated_at else row.last_heartbeat_at.isoformat(),
    )
