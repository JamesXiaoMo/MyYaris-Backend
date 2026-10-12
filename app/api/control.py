import asyncio
import uuid
from typing import Any, Dict, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy.orm import Session

from app.api.auth import (
    get_current_user,
    require_car_or_admin,
    require_phone_or_admin,
    verify_token_string,
)
from app.config import settings
from app.repositories.database import get_db
from app.repositories.models import utc_now
from app.schemas.auth import AuthenticatedUser
from app.schemas.control import (
    CarStatusReportRequest,
    CarStatusResponse,
    MusicCommandRequest,
    MusicCommandResponse,
)
from app.services.control_service import (
    car_status_to_response,
    command_queue_manager,
    get_car_status_by_id,
    save_or_update_car_status,
    ws_manager,
)

router = APIRouter(prefix="/api/v1", tags=["Music Control & State Sync"])

VALID_ACTIONS = {
    "play_track",
    "pause",
    "resume",
    "next",
    "prev",
    "seek",
    "set_loop",
    "set_shuffle",
    "set_volume",
}


def resolve_car_device_id(input_id: Optional[str], user_device_id: Optional[str] = None) -> str:
    chosen = input_id or user_device_id or settings.DEFAULT_CAR_DEVICE_ID
    if chosen in ("car_p3pro", "ottocast_p3_pro"):
        return settings.DEFAULT_CAR_DEVICE_ID
    return chosen


@router.post("/music/control/command", response_model=MusicCommandResponse)
async def send_music_command(
    cmd: MusicCommandRequest,
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    """
    iPhone client dispatches an immediate playback control command to the vehicle.
    Dispatched instantly via WebSocket to online car clients, and enqueued for HTTP long-poll fallback.
    """
    if cmd.action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid action '{cmd.action}'. Supported actions: {sorted(list(VALID_ACTIONS))}",
        )

    target_device = resolve_car_device_id(cmd.device_id)
    cmd_id = cmd.command_id or f"cmd_music_{uuid.uuid4().hex[:12]}"

    command_payload = {
        "command_id": cmd_id,
        "device_id": target_device,
        "action": cmd.action,
        "params": cmd.params.model_dump(exclude_unset=True) if cmd.params else {},
        "timestamp": utc_now().isoformat(),
        "sender_role": user.role,
    }

    # 1. Enqueue for HTTP long-polling car clients
    await command_queue_manager.enqueue_command(target_device, command_payload)

    # 2. Real-time instant push to WebSocket-connected car clients
    delivered_count = await ws_manager.dispatch_command(command_payload, target_device)

    return MusicCommandResponse(
        status="ok",
        command_id=cmd_id,
        device_id=target_device,
        action=cmd.action,
        params=command_payload["params"],
        queued_at=command_payload["timestamp"],
        dispatched_to_ws=(delivered_count > 0),
    )


@router.get("/music/control/command/next")
async def get_next_music_command(
    device_id: Optional[str] = Query(None, description="Vehicle device ID"),
    timeout: Optional[int] = Query(default=None, ge=1, le=60, description="Long poll timeout in seconds"),
    user: AuthenticatedUser = Depends(require_car_or_admin),
):
    """
    Car client polls for the next pending music playback command.
    Returns 204 No Content if timeout expires with no new commands.
    """
    target_device = resolve_car_device_id(device_id, user.device_id)

    if user.role == "car" and user.device_id:
        user_dev = resolve_car_device_id(user.device_id)
        if user_dev != target_device:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Car token cannot poll commands for device {target_device}",
            )

    cmd = await command_queue_manager.get_next_command(target_device, timeout_seconds=timeout)
    if not cmd:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return cmd


@router.post("/music/control/status")
async def report_car_playback_status(
    report: CarStatusReportRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
):
    """
    Car client reports real-time playback heartbeat status (playing/paused/position/volume).
    Persists snapshot to database and immediately broadcasts to all connected iPhone WebSockets.
    """
    target_device = resolve_car_device_id(report.device_id, user.device_id)
    if user.role == "car" and user.device_id:
        user_dev = resolve_car_device_id(user.device_id)
        if user_dev != target_device:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Car token cannot report status for device {target_device}",
            )

    # Persist in DB
    status_row = save_or_update_car_status(db, report, default_device_id=target_device)
    resp = car_status_to_response(status_row)

    # Real-time WebSocket broadcast to iPhone clients
    await ws_manager.broadcast_status(resp.model_dump(), device_id=target_device)

    return {
        "status": "ok",
        "device_id": status_row.device_id,
        "playback_state": status_row.playback_state,
        "position_ms": status_row.position_ms,
        "updated_at": status_row.updated_at.isoformat(),
    }


@router.get("/music/control/status", response_model=CarStatusResponse)
def get_car_playback_status(
    device_id: Optional[str] = Query(None, description="Vehicle device ID"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Query the latest real-time vehicle playback snapshot.
    Returns current playback state, track ID, seek position, and volume.
    """
    target_device = resolve_car_device_id(device_id)
    status_row = get_car_status_by_id(db, target_device)

    if not status_row:
        # Return initial idle state rather than 404 to avoid breaking client UI
        now_str = utc_now().isoformat()
        return CarStatusResponse(
            device_id=target_device,
            playback_state="stopped",
            current_track_id=None,
            position_ms=0,
            duration_ms=0,
            software_volume=1.0,
            loop_mode="all",
            shuffle_enabled=False,
            last_heartbeat_at=now_str,
            updated_at=now_str,
        )

    return car_status_to_response(status_row)


@router.websocket("/ws/music")
async def music_websocket_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
    client_type: Optional[str] = Query("phone"),
    device_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Full-duplex WebSocket channel for real-time music control and playback state broadcasting.
    - iPhone clients receive instant playback status updates from vehicle.
    - Car clients receive immediate playback commands dispatched by iPhone.
    """
    # 1. Authenticate token from query param or headers
    auth_token = token
    if not auth_token:
        # Check Authorization header from websocket headers
        auth_header = websocket.headers.get("authorization")
        if auth_header:
            auth_token = auth_header
        else:
            dev_header = websocket.headers.get("x-device-token")
            if dev_header:
                auth_token = dev_header

    try:
        user = verify_token_string(auth_token)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Determine client identity
    c_type = client_type if client_type in ["car", "phone"] else ("car" if user.role == "car" else "phone")
    target_dev = device_id or user.device_id or settings.DEFAULT_CAR_DEVICE_ID

    await ws_manager.connect(websocket, client_type=c_type, device_id=target_dev)

    # Send initial status snapshot upon connection
    status_row = get_car_status_by_id(db, target_dev)
    if status_row:
        initial_data = car_status_to_response(status_row).model_dump()
    else:
        now_str = utc_now().isoformat()
        initial_data = {
            "device_id": target_dev,
            "playback_state": "stopped",
            "current_track_id": None,
            "position_ms": 0,
            "duration_ms": 0,
            "software_volume": 1.0,
            "loop_mode": "all",
            "shuffle_enabled": False,
            "last_heartbeat_at": now_str,
            "updated_at": now_str,
        }

    try:
        await websocket.send_json({"type": "status_snapshot", "data": initial_data})
    except Exception:
        await ws_manager.disconnect(websocket)
        return

    # Main WebSocket loop
    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong", "timestamp": utc_now().isoformat()})

            elif msg_type == "status_update" and c_type == "car":
                # Car updates playback status via WebSocket
                report_data = msg.get("data", {})
                req = CarStatusReportRequest(**report_data)
                saved_row = save_or_update_car_status(db, req, default_device_id=target_dev)
                resp = car_status_to_response(saved_row)
                await ws_manager.broadcast_status(resp.model_dump(), device_id=target_dev)

            elif msg_type == "command" and c_type in ["phone", "admin"]:
                # Phone dispatches command via WebSocket
                cmd_data = msg.get("data", {})
                cmd_req = MusicCommandRequest(**cmd_data)
                if cmd_req.action in VALID_ACTIONS:
                    cmd_id = cmd_req.command_id or f"cmd_music_{uuid.uuid4().hex[:12]}"
                    command_payload = {
                        "command_id": cmd_id,
                        "device_id": target_dev,
                        "action": cmd_req.action,
                        "params": cmd_req.params.model_dump(exclude_unset=True) if cmd_req.params else {},
                        "timestamp": utc_now().isoformat(),
                        "sender_role": user.role,
                    }
                    await command_queue_manager.enqueue_command(target_dev, command_payload)
                    await ws_manager.dispatch_command(command_payload, target_dev)
                    await websocket.send_json({
                        "type": "command_ack",
                        "command_id": cmd_id,
                        "status": "dispatched",
                    })

    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception:
        await ws_manager.disconnect(websocket)
