import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from fastapi import WebSocket
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.models import CarDeviceStatus, utc_now
from app.schemas.control import CarStatusReportRequest, CarStatusResponse

logger = logging.getLogger(__name__)


class WebSocketConnectionManager:
    """Manages active WebSockets for iPhone clients and Car (Ottocast) clients."""

    def __init__(self):
        self.active_connections: Dict[WebSocket, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, client_type: str, device_id: str):
        await websocket.accept()
        async with self._lock:
            self.active_connections[websocket] = {
                "client_type": client_type,
                "device_id": device_id,
                "connected_at": utc_now().isoformat(),
            }
        logger.info(f"WebSocket client connected: type={client_type}, device={device_id}. Total: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            meta = self.active_connections.pop(websocket, None)
        if meta:
            logger.info(f"WebSocket client disconnected: type={meta.get('client_type')}, device={meta.get('device_id')}. Total: {len(self.active_connections)}")

    async def broadcast_status(self, status_dict: dict, device_id: Optional[str] = None):
        """Broadcasts playback state updates to all connected iPhone/client WebSockets."""
        payload = {
            "type": "status_update",
            "timestamp": utc_now().isoformat(),
            "data": status_dict,
        }
        stale_sockets = []

        async with self._lock:
            targets = list(self.active_connections.items())

        for ws, meta in targets:
            # Broadcast to phones, or all listening devices
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
        """Dispatches an immediate music command to car WebSocket clients matching target_device_id."""
        payload = {
            "type": "music_command",
            "timestamp": utc_now().isoformat(),
            "data": command_dict,
        }
        delivered_count = 0
        stale_sockets = []

        async with self._lock:
            targets = list(self.active_connections.items())

        for ws, meta in targets:
            if meta.get("device_id") == target_device_id and meta.get("client_type") == "car":
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
    """In-memory async command queues per device for HTTP long-polling and fallback."""

    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def _get_or_create_queue(self, device_id: str) -> asyncio.Queue:
        async with self._lock:
            if device_id not in self._queues:
                self._queues[device_id] = asyncio.Queue(maxsize=100)
            return self._queues[device_id]

    async def enqueue_command(self, device_id: str, command: dict):
        queue = await self._get_or_create_queue(device_id)
        if queue.full():
            try:
                queue.get_nowait()  # Evict oldest unconsumed command
            except asyncio.QueueEmpty:
                pass
        await queue.put(command)

    async def get_next_command(self, device_id: str, timeout_seconds: Optional[int] = None) -> Optional[dict]:
        queue = await self._get_or_create_queue(device_id)
        if timeout_seconds is None or timeout_seconds <= 0:
            try:
                return queue.get_nowait()
            except asyncio.QueueEmpty:
                return None

        try:
            return await asyncio.wait_for(queue.get(), timeout=float(timeout_seconds))
        except asyncio.TimeoutError:
            return None


# Global singletons
ws_manager = WebSocketConnectionManager()
command_queue_manager = MusicCommandQueueManager()


def resolve_car_device_id(input_id: Optional[str], default_id: Optional[str] = None) -> str:
    chosen = input_id or default_id or settings.DEFAULT_CAR_DEVICE_ID
    if chosen in ("car_p3pro", "ottocast_p3_pro"):
        return settings.DEFAULT_CAR_DEVICE_ID
    return chosen


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
