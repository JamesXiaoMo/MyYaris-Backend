"""iPhone-local playback queue scope.

The phone may play the Owner's NetEase stream on its own speaker. Its queue snapshots, heart
(intelligence) batches and version sequence live in a private scope derived on the server from the
authenticated account and an opaque client session id. Nothing here reads or writes a car device's
active queue, status row, commands or WebSocket state; the catalog (favorite metadata) is shared
read-only. Handoff copies one immutable snapshot between scopes (``adopt`` / ``export``) and never
activates a car queue (only the vehicle's applied ACK does).
"""
import hashlib
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_admin_or_legacy_phone, verify_csrf
from app.api.control import read_queue_page_http
from app.api.netease import _generate_intelligence_queue
from app.repositories.database import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.netease import (
    MusicQueueResponse,
    NeteaseIntelligenceRequest,
    NeteaseIntelligenceResponse,
)
from app.services.control_service import music_queue_snapshot_manager
from app.services.device_service import LOCAL_SCOPE_PREFIX, is_local_scope, resolve_car_device_id

router = APIRouter(prefix="/api/v1/music/local", tags=["iPhone Local Playback"])

SESSION_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"


class LocalSessionBody(BaseModel):
    session_id: str = Field(..., pattern=SESSION_PATTERN, description="Opaque client playback session id")


class LocalIntelligenceRequest(NeteaseIntelligenceRequest):
    session_id: str = Field(..., pattern=SESSION_PATTERN)


class LocalQueueTransferRequest(LocalSessionBody):
    queue_id: str = Field(..., min_length=1, max_length=64)
    queue_version: int = Field(..., ge=1)
    device_id: Optional[str] = Field(None, description="Car device that is the other side of the transfer")


class LocalQueueRef(BaseModel):
    queue_id: str
    queue_version: int
    source: str
    total: int
    is_complete: bool
    batch_index: int = 0


def local_scope(user: AuthenticatedUser, session_id: str) -> str:
    """Server-derived scope: bound to the authenticated account and the client session, never client-chosen."""
    who = f"{user.role}|{user.user_id or user.identity}|{session_id}"
    return LOCAL_SCOPE_PREFIX + hashlib.sha256(who.encode("utf-8")).hexdigest()[:20]


def _car_side(device_id: Optional[str]) -> str:
    if is_local_scope(device_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="device_id must be a car device")
    return resolve_car_device_id(device_id)


def _ref(snapshot) -> LocalQueueRef:
    return LocalQueueRef(
        queue_id=snapshot.queue_id,
        queue_version=snapshot.queue_version,
        source=snapshot.source,
        total=snapshot.total,
        is_complete=snapshot.is_complete,
        batch_index=max(0, snapshot.batch_count - 1),
    )


@router.get("/queue", response_model=MusicQueueResponse)
async def get_local_queue(
    session_id: str = Query(..., pattern=SESSION_PATTERN),
    source: Optional[str] = Query(None),
    queue_id: Optional[str] = Query(None),
    queue_version: Optional[int] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Read-only queue pages for this phone session. Never changes a car's queue or status."""
    scope = local_scope(user, session_id)
    async with music_queue_snapshot_manager.operation_lock("local~operations"):
        music_queue_snapshot_manager.touch_local_scope(scope, db=db)
        return await read_queue_page_http(
            scope,
            source=source,
            queue_id=queue_id,
            queue_version=queue_version,
            offset=offset,
            limit=limit,
            db=db,
        )


@router.post("/intelligence", response_model=NeteaseIntelligenceResponse)
async def generate_local_intelligence(
    req: LocalIntelligenceRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Authentic heart queue for this phone session (same upstream rules and seed checks as the car)."""
    verify_csrf(request, user)
    scope = local_scope(user, req.session_id)
    async with music_queue_snapshot_manager.operation_lock("local~operations"):
        music_queue_snapshot_manager.touch_local_scope(scope, db=db)
        return await _generate_intelligence_queue(req, request, db, user, scope=scope)


@router.post("/queue/adopt", response_model=LocalQueueRef)
async def adopt_car_queue(
    req: LocalQueueTransferRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Car -> phone handoff: copy one immutable car snapshot into this phone scope (car state untouched)."""
    verify_csrf(request, user)
    scope = local_scope(user, req.session_id)
    car = _car_side(req.device_id)
    async with music_queue_snapshot_manager.operation_lock("local~operations"):
        music_queue_snapshot_manager.touch_local_scope(scope, db=db)
        snap = music_queue_snapshot_manager.clone_snapshot(
            car, req.queue_id, req.queue_version, scope, activate=True, db=db
        )
    return _ref(snap)


@router.post("/queue/export", response_model=LocalQueueRef)
async def export_local_queue(
    req: LocalQueueTransferRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Phone -> car handoff preparation: make this phone snapshot readable in the car scope.

    The copy is only a readable immutable snapshot there; it becomes the car's active queue solely
    when the vehicle reports a later play_track as applied.
    """
    verify_csrf(request, user)
    scope = local_scope(user, req.session_id)
    car = _car_side(req.device_id)
    async with music_queue_snapshot_manager.operation_lock("local~operations"), music_queue_snapshot_manager.operation_lock(car):
        snap = music_queue_snapshot_manager.clone_snapshot(
            scope, req.queue_id, req.queue_version, car, activate=False, db=db
        )
    return _ref(snap)


@router.post("/session/close")
async def close_local_session(
    req: LocalSessionBody,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Forget this phone session's snapshots (logout, device switch, app exit). Idempotent."""
    verify_csrf(request, user)
    scope = local_scope(user, req.session_id)
    async with music_queue_snapshot_manager.operation_lock("local~operations"):
        music_queue_snapshot_manager.drop_scope(scope, db=db)
    return {"status": "closed"}
