import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
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
    verify_csrf,
    verify_token_string,
)
from app.config import settings
from app.repositories.database import get_db
from app.repositories.models import utc_now
from app.schemas.auth import AuthenticatedUser
from app.schemas.control import (
    CarStatusReportRequest,
    CarStatusResponse,
    MusicCommandAckRequest,
    MusicCommandAckResponse,
    MusicCommandDetailResponse,
    MusicCommandRequest,
    MusicCommandResponse,
)
from app.schemas.netease import MusicQueueResponse
from app.services.control_service import (
    car_status_to_response,
    command_queue_manager,
    create_or_get_music_command,
    get_car_status_by_id,
    get_music_command_by_id,
    music_command_to_detail,
    music_command_to_response,
    music_queue_snapshot_manager,
    record_command_ack,
    record_command_sent,
    save_or_update_car_status,
    ws_manager,
    NeteaseUpstreamError,
)
from app.services.device_service import is_same_device, resolve_car_device_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Music Control & State Sync"])

VALID_ACTIONS = {
    "play_track",
    "pause",
    "resume",
    "stop",
    "next",
    "prev",
    "seek",
    "set_loop",
    "set_shuffle",
    "set_volume",
    "set_playback_mode",
    "switch_queue",
}


@router.post("/music/control/command", response_model=MusicCommandResponse)
async def send_music_command(
    cmd: MusicCommandRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    verify_csrf(request, user)
    """
    iPhone client dispatches an immediate playback control command to the vehicle.
    Persisted to SQLite, checked for idempotency/conflict, pushed to online WS car clients,
    and enqueued in memory for reliable fallback polling until car execution confirmation.
    """
    if cmd.action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid action '{cmd.action}'. Supported actions: {sorted(list(VALID_ACTIONS))}",
        )

    target_device = resolve_car_device_id(cmd.device_id)

    # Capability gate check for new queue V2 actions
    if cmd.action in ("set_playback_mode", "switch_queue"):
        car_status = get_car_status_by_id(db, target_device)
        caps = []
        if car_status and car_status.capabilities_json:
            try:
                caps = json.loads(car_status.capabilities_json)
            except Exception:
                caps = []
        if "queue_v2" not in caps:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": 422,
                    "error_code": "CAPABILITY_NOT_SUPPORTED",
                    "error_detail": f"Target car device '{target_device}' does not support queue_v2 capabilities.",
                    "message": "车机版本过低，不支持新版播放队列与心动模式",
                },
            )

    # Precondition assertion on expected_queue_id and expected_queue_version
    if cmd.params:
        active_q = music_queue_snapshot_manager.get_active_queue(target_device)
        if cmd.params.expected_queue_id:
            if not active_q or active_q.queue_id != cmd.params.expected_queue_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": 409,
                        "error_code": "QUEUE_PRECONDITION_FAILED",
                        "error_detail": f"expected_queue_id '{cmd.params.expected_queue_id}' does not match active queue '{active_q.queue_id if active_q else None}'",
                        "message": "播放队列前置条件不匹配",
                    },
                )
        if cmd.params.expected_queue_version is not None:
            if not active_q or active_q.queue_version != cmd.params.expected_queue_version:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": 409,
                        "error_code": "QUEUE_PRECONDITION_FAILED",
                        "error_detail": f"expected_queue_version {cmd.params.expected_queue_version} does not match active queue version {active_q.queue_version if active_q else None}",
                        "message": "播放队列版本前置条件不匹配",
                    },
                )

    # 1. Idempotency & conflict check with SQLite persistence
    cmd_row, is_new = create_or_get_music_command(
        db=db,
        cmd_req=cmd,
        target_device=target_device,
        sender_role=user.role,
    )

    # Update requested_mode or queue_source if applicable
    if is_new and cmd.params:
        car_status_row = get_car_status_by_id(db, target_device)
        if car_status_row:
            if cmd.action == "set_playback_mode" and cmd.params.mode:
                car_status_row.requested_mode = cmd.params.mode
                db.commit()
            elif cmd.action == "switch_queue" and cmd.params.source:
                car_status_row.queue_source = cmd.params.source
                db.commit()

    # If this command ID was already processed, return existing record
    if not is_new:
        return music_command_to_response(cmd_row, dispatched_to_ws=(cmd_row.dispatched_channel == "ws"))

    params_dict = json.loads(cmd_row.params_json) if cmd_row.params_json else {}
    command_payload = {
        "command_id": cmd_row.command_id,
        "device_id": target_device,
        "action": cmd_row.action,
        "params": params_dict,
        "timestamp": cmd_row.created_at.isoformat(),
        "expires_at": cmd_row.expires_at.isoformat() if cmd_row.expires_at else None,
        "sender_role": user.role,
    }

    # 2. Real-time instant push to WebSocket-connected car clients
    delivered_count = await ws_manager.dispatch_command(command_payload, target_device)

    if delivered_count > 0:
        record_command_sent(db, cmd_row.command_id, channel="ws")
        command_payload["ws_dispatched_at"] = utc_now()
    else:
        command_payload["ws_dispatched_at"] = None

    # 3. Always enqueue to command_queue_manager for HTTP long-poll fallback and recovery
    await command_queue_manager.enqueue_command(target_device, command_payload)

    return music_command_to_response(cmd_row, dispatched_to_ws=(delivered_count > 0))


@router.get("/music/control/command/next")
async def get_next_music_command(
    device_id: Optional[str] = Query(None, description="Vehicle device ID"),
    timeout: Optional[int] = Query(default=None, ge=1, le=60, description="Long poll timeout in seconds"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
):
    """
    Car client polls for the next pending music playback command.
    Returns 204 No Content if timeout expires with no unacknowledged commands.
    """
    target_device = resolve_car_device_id(device_id, user.device_id)

    if user.role == "car" and user.device_id:
        if not is_same_device(user.device_id, target_device):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Car token cannot poll commands for device {target_device}",
            )

    cmd = await command_queue_manager.get_next_command(target_device, timeout_seconds=timeout, db=db)
    if not cmd:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Record HTTP delivery attempt
    record_command_sent(db, cmd["command_id"], channel="http")
    return cmd


@router.post("/music/control/command/{command_id}/ack", response_model=MusicCommandAckResponse)
async def acknowledge_music_command(
    command_id: str,
    ack: MusicCommandAckRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
):
    """
    Car client acknowledges command execution result (received / applied / failed).
    Updates command lifecycle state and broadcasts execution confirmation to phone clients.
    """
    car_device = resolve_car_device_id(user.device_id)
    cmd = record_command_ack(
        db=db,
        command_id=command_id,
        ack_status=ack.status,
        car_device_id=car_device,
        error_code=ack.error_code,
        error_detail=ack.error_detail,
        applied_mode=ack.applied_mode,
        queue_version=ack.queue_version,
        current_index=ack.current_index,
    )
    if not cmd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found.")

    # Broadcast execution result to connected phones
    await ws_manager.broadcast_command_status({
        "command_id": cmd.command_id,
        "status": cmd.status,
        "action": cmd.action,
        "applied_mode": ack.applied_mode,
        "queue_version": ack.queue_version,
        "current_index": ack.current_index,
        "error_code": cmd.error_code,
        "error_detail": cmd.error_detail,
    }, device_id=cmd.device_id)

    return MusicCommandAckResponse(
        command_id=cmd.command_id,
        status=cmd.status,
        accepted=True,
        message=f"Command acknowledged as {cmd.status}",
    )


@router.get("/music/control/command/{command_id}", response_model=MusicCommandDetailResponse)
def get_music_command_status(
    command_id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Query detailed lifecycle and delivery status for a specific command.
    """
    if user.role == "user" and not user.has_device_control:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: User account lacks vehicle device control permission.",
        )

    cmd = get_music_command_by_id(db, command_id)
    if not cmd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found.")

    if user.role == "car" and user.device_id and not is_same_device(user.device_id, cmd.device_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Car token cannot view commands for other devices.",
        )

    return music_command_to_detail(cmd)


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
        if not is_same_device(user.device_id, target_device):
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
    if user.role == "user" and not user.has_device_control:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: User account lacks vehicle device control permission.",
        )

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


@router.get("/music/queue", response_model=MusicQueueResponse)
async def get_music_queue(
    device_id: Optional[str] = Query(None, description="Target vehicle device ID"),
    source: Optional[str] = Query(None, description="Source filter (netease_favorite, etc.)"),
    queue_id: Optional[str] = Query(None, description="Queue ID for version snapshot check"),
    queue_version: Optional[int] = Query(None, description="Queue version for version snapshot check"),
    offset: int = Query(0, ge=0, description="Track offset"),
    limit: int = Query(50, ge=1, le=100, description="Page limit"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Pure read-only endpoint for music queue metadata and paginated tracks.
    Querying with ?source=... NEVER switches the active playback queue.
    Enforces immutable versioned snapshots. Version mismatch returns 409 QUEUE_VERSION_CONFLICT.
    """
    target_device = resolve_car_device_id(device_id, user.device_id)

    # Account/device isolation
    if user.role == "car" and user.device_id and not is_same_device(user.device_id, target_device):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Car token cannot access music queue of other devices.",
        )
    if user.role == "user" and not user.has_device_control:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: User account lacks vehicle device control permission.",
        )

    try:
        return await music_queue_snapshot_manager.get_queue_page(
            device_id=target_device,
            source=source,
            queue_id=queue_id,
            queue_version=queue_version,
            offset=offset,
            limit=limit,
        )
    except HTTPException:
        raise
    except PermissionError as pe:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": 409,
                "error_code": "INTELLIGENCE_AUTH_INVALID",
                "error_detail": str(pe),
                "message": "网易云登录凭证失效或未登录，无法拉取播放队列",
            },
        )
    except NeteaseUpstreamError as nue:
        raise HTTPException(
            status_code=nue.status_code,
            detail={
                "code": nue.status_code,
                "error_code": "INTELLIGENCE_UPSTREAM_FAILED",
                "error_detail": nue.message,
                "message": "上游网易云服务请求异常",
            },
        )


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
    - iPhone clients receive instant playback status updates from vehicle via dual envelope.
    - Car clients receive immediate playback commands dispatched by iPhone with top-level actions.
    - Car clients acknowledge execution via WS command_ack, which broadcasts command_status to phone.
    """
    # 1. Authenticate token from query param, cookies, or headers
    auth_token = token
    if not auth_token:
        cookie_session = websocket.cookies.get(settings.SESSION_COOKIE_NAME)
        if cookie_session:
            auth_token = cookie_session
        else:
            auth_header = websocket.headers.get("authorization")
            if auth_header:
                auth_token = auth_header
            else:
                dev_header = websocket.headers.get("x-device-token")
                if dev_header:
                    auth_token = dev_header

    try:
        user = verify_token_string(auth_token, db=db)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # User role without vehicle device control cannot connect to vehicle control WS
    if user.role == "user" and not user.has_device_control:
        logger.warning(f"User '{user.identity}' denied WS access: no vehicle control permission")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Determine client identity and normalized target device
    c_type = client_type if client_type in ["car", "phone"] else ("car" if user.role == "car" else "phone")
    raw_dev = device_id or user.device_id or settings.DEFAULT_CAR_DEVICE_ID
    norm_target = resolve_car_device_id(raw_dev)

    await ws_manager.connect(websocket, client_type=c_type, device_id=raw_dev)

    # Send initial status snapshot upon connection in dual envelope
    status_row = get_car_status_by_id(db, norm_target)
    if status_row:
        initial_data = car_status_to_response(status_row).model_dump()
    else:
        now_str = utc_now().isoformat()
        initial_data = {
            "device_id": raw_dev,
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

    # Ensure device_id in snapshot reflects what client requested
    if raw_dev:
        initial_data["device_id"] = raw_dev

    snapshot_payload = {
        "type": "status_snapshot",
        "timestamp": utc_now().isoformat(),
        **initial_data,
        "data": initial_data,
    }

    try:
        await websocket.send_json(snapshot_payload)
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
                report_data = msg.get("data")
                if not isinstance(report_data, dict) or not report_data:
                    # Fallback to root payload for flat car implementations
                    report_data = {k: v for k, v in msg.items() if k not in ("type", "timestamp")}

                # Safety check: if payload has no valid status fields, DO NOT wipe existing status!
                valid_status_keys = {
                    "playback_state",
                    "current_track_id",
                    "position_ms",
                    "duration_ms",
                    "software_volume",
                    "loop_mode",
                    "shuffle",
                }
                if not any(k in report_data for k in valid_status_keys):
                    logger.warning("Ignoring invalid status_update message without status fields: %s", msg)
                    continue

                # If playback_state or position_ms are missing from partial update, preserve existing values
                existing_row = get_car_status_by_id(db, norm_target)
                if "playback_state" not in report_data and existing_row:
                    report_data["playback_state"] = existing_row.playback_state
                if "position_ms" not in report_data and existing_row:
                    report_data["position_ms"] = existing_row.position_ms

                req = CarStatusReportRequest(**report_data)
                saved_row = save_or_update_car_status(db, req, default_device_id=norm_target)
                resp = car_status_to_response(saved_row)
                await ws_manager.broadcast_status(resp.model_dump(), device_id=norm_target)

            elif msg_type == "command" and c_type in ["phone", "admin"]:
                # Phone dispatches command via WebSocket
                cmd_data = msg.get("data")
                if not isinstance(cmd_data, dict) or not cmd_data:
                    cmd_data = {k: v for k, v in msg.items() if k not in ("type", "timestamp")}
                cmd_req = MusicCommandRequest(**cmd_data)
                if cmd_req.action in VALID_ACTIONS:
                    cmd_target = resolve_car_device_id(cmd_req.device_id or norm_target)
                    try:
                        cmd_row, is_new = create_or_get_music_command(
                            db=db,
                            cmd_req=cmd_req,
                            target_device=cmd_target,
                            sender_role=user.role,
                        )
                    except HTTPException as he:
                        await websocket.send_json({
                            "type": "command_ack",
                            "command_id": cmd_req.command_id or "",
                            "status": "conflict" if he.status_code == status.HTTP_409_CONFLICT else "error",
                            "error": he.detail,
                        })
                        continue

                    # If duplicate command ID: DO NOT re-dispatch or re-enqueue!
                    if not is_new:
                        await websocket.send_json({
                            "type": "command_ack",
                            "command_id": cmd_row.command_id,
                            "status": "duplicate" if cmd_row.status in ("queued", "sent") else cmd_row.status,
                            "lifecycle_status": cmd_row.status,
                            "duplicate": True,
                        })
                        continue

                    params_dict = json.loads(cmd_row.params_json) if cmd_row.params_json else {}
                    command_payload = {
                        "command_id": cmd_row.command_id,
                        "device_id": cmd_target,
                        "action": cmd_row.action,
                        "params": params_dict,
                        "timestamp": cmd_row.created_at.isoformat(),
                        "expires_at": cmd_row.expires_at.isoformat() if cmd_row.expires_at else None,
                        "sender_role": user.role,
                    }
                    delivered = await ws_manager.dispatch_command(command_payload, cmd_target)
                    if delivered > 0:
                        record_command_sent(db, cmd_row.command_id, channel="ws")
                        command_payload["ws_dispatched_at"] = utc_now()
                    else:
                        command_payload["ws_dispatched_at"] = None

                    await command_queue_manager.enqueue_command(cmd_target, command_payload)

                    # Handshake acknowledgment back to sender phone
                    await websocket.send_json({
                        "type": "command_ack",
                        "command_id": cmd_row.command_id,
                        "status": "dispatched",
                    })

            elif msg_type == "command_ack" and c_type == "car":
                # Car reports execution acknowledgement over WebSocket
                cmd_id = msg.get("command_id")
                ack_status = msg.get("status", "applied")
                err_code = msg.get("error_code")
                err_detail = msg.get("error_detail") or msg.get("error")
                if cmd_id:
                    try:
                        cmd = record_command_ack(
                            db=db,
                            command_id=cmd_id,
                            ack_status=ack_status,
                            car_device_id=norm_target,
                            error_code=err_code,
                            error_detail=err_detail,
                        )
                        if cmd:
                            # Broadcast execution status to phones
                            await ws_manager.broadcast_command_status({
                                "command_id": cmd.command_id,
                                "status": cmd.status,
                                "action": cmd.action,
                                "error_code": cmd.error_code,
                                "error_detail": cmd.error_detail,
                            }, device_id=cmd.device_id)
                    except Exception as e:
                        logger.warning(f"Failed to process car command_ack {cmd_id}: {e}")

    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"WebSocket session error: {e}")
        await ws_manager.disconnect(websocket)
