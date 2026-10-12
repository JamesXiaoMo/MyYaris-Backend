import asyncio
import collections
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from fastapi import WebSocket
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.models import CarDeviceStatus, utc_now
from app.schemas.control import CarStatusReportRequest, CarStatusResponse
from app.services.device_service import is_same_device, resolve_car_device_id

logger = logging.getLogger(__name__)


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

            # Dual envelope: top-level fields for flat parsers, plus nested "data" for envelope parsers
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

        if delivered_count > 0 and command_dict.get("command_id"):
            command_queue_manager.mark_ws_delivered(command_dict["command_id"])

        return delivered_count


class MusicCommandQueueManager:
    """In-memory async command queues per device for HTTP long-polling and fallback with deduplication."""

    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}
        self._ws_delivered_command_ids: collections.OrderedDict = collections.OrderedDict()
        self._dispatched_command_ids: collections.OrderedDict = collections.OrderedDict()
        self._lock = asyncio.Lock()

    def mark_ws_delivered(self, command_id: str):
        if not command_id:
            return
        self._ws_delivered_command_ids[command_id] = True
        while len(self._ws_delivered_command_ids) > 1000:
            self._ws_delivered_command_ids.popitem(last=False)

    def is_ws_delivered(self, command_id: str) -> bool:
        return bool(command_id and command_id in self._ws_delivered_command_ids)

    def mark_dispatched(self, command_id: str):
        if not command_id:
            return
        self._dispatched_command_ids[command_id] = True
        while len(self._dispatched_command_ids) > 1000:
            self._dispatched_command_ids.popitem(last=False)

    def is_duplicate(self, command_id: str) -> bool:
        return bool(command_id and (command_id in self._dispatched_command_ids or command_id in self._ws_delivered_command_ids))

    async def _get_or_create_queue(self, device_id: str) -> asyncio.Queue:
        norm_dev = resolve_car_device_id(device_id)
        async with self._lock:
            if norm_dev not in self._queues:
                self._queues[norm_dev] = asyncio.Queue(maxsize=100)
            return self._queues[norm_dev]

    async def enqueue_command(self, device_id: str, command: dict):
        cmd_id = command.get("command_id")
        if cmd_id and self.is_ws_delivered(cmd_id):
            return
        if cmd_id:
            self.mark_dispatched(cmd_id)
        queue = await self._get_or_create_queue(device_id)
        if queue.full():
            try:
                queue.get_nowait()  # Evict oldest unconsumed command
            except asyncio.QueueEmpty:
                pass
        await queue.put(command)

    async def get_next_command(self, device_id: str, timeout_seconds: Optional[int] = None) -> Optional[dict]:
        queue = await self._get_or_create_queue(device_id)
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
            if cmd_id and self.is_ws_delivered(cmd_id):
                logger.info(f"Skipping command {cmd_id} for HTTP polling as it was already delivered via WebSocket")
                if timeout_seconds and timeout_seconds > 0:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    remaining = float(timeout_seconds) - elapsed
                    if remaining <= 0:
                        return None
                    continue
                else:
                    continue

            if cmd_id:
                self.mark_dispatched(cmd_id)
            return cmd


# Global singletons
ws_manager = WebSocketConnectionManager()
command_queue_manager = MusicCommandQueueManager()


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
