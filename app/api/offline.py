"""Offline SD download API (V3.0).

Auth boundary (reuses existing dependencies, no new bypass):
- Job/network creation + pause/resume/cancel: require_admin (Owner admin).
- Job/item reads: require_admin_or_legacy_phone.
- Claim/heartbeat/receipt/revoke: bound vehicle device only (role=car).
"""
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.api.auth import get_current_user, require_admin, require_admin_or_legacy_phone, verify_csrf
from app.repositories.database import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.offline import (
    ClaimRequest,
    FailRequest,
    JobStateRequest,
    ClaimResponse,
    CreateJobRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    ItemListResponse,
    JobListResponse,
    JobSummary,
    NetworkConfirmRequest,
    NetworkConfirmResponse,
    OfflineItemResponse,
    ReceiptRequest,
    RevokeRequest,
)
from app.services import offline_service as svc
from app.services.device_service import resolve_car_device_id, is_local_scope
from app.services.netease_service import favorite_catalog

class OfflineRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                # Default 422 errors echo the rejected input, which could contain a
                # local path or credentials. Keep this module's errors structural.
                raise HTTPException(422, detail={"code": "INVALID_OFFLINE_REQUEST", "message": "Invalid offline request fields"}) from None

        return handler


router = APIRouter(prefix="/api/v1/music/offline", tags=["Offline SD Music"], route_class=OfflineRoute)


async def _owner_scope(user: AuthenticatedUser) -> Tuple[str, Optional[str]]:
    """Use the existing session-verifying catalog contract, never an unverified peek."""
    try:
        snap, _meta = await favorite_catalog.view()
    except Exception:
        raise HTTPException(status_code=409, detail={"code": "OWNER_NOT_VERIFIED", "message": "Sync the current Owner favorite playlist first"}) from None
    if not snap.owner_key:
        raise HTTPException(status_code=409, detail={"code": "OWNER_NOT_VERIFIED", "message": "Owner verification required"})
    return snap.owner_key, user.user_id


def _car_device(user, requested):
    if not user.device_id:
        raise HTTPException(403, detail={"code": "DEVICE_MISMATCH", "message": "Bound vehicle identity required"})
    bound = resolve_car_device_id(user.device_id)
    if requested is not None and resolve_car_device_id(requested) != bound:
        raise HTTPException(403, detail={"code": "DEVICE_MISMATCH", "message": "Vehicle identity mismatch"})
    return bound


def _target_device(requested):
    if is_local_scope(requested):
        raise HTTPException(422, detail={"code": "DEVICE_MISMATCH", "message": "A vehicle device is required"})
    return resolve_car_device_id(requested)


async def _write_admin(request: Request, user: AuthenticatedUser = Depends(require_admin)):
    verify_csrf(request, user)
    return user


def require_bound_car(request: Request, user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """Offline execution (claim/heartbeat/receipt/revoke) is car-only."""
    if user.role != "car":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "NOT_CAR", "message": "Only the bound vehicle device can execute offline work"},
        )
    verify_csrf(request, user)
    return user


def _raise(e: svc.OfflineConflict) -> None:
    if e.code == "NOT_FOUND":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": e.code, "message": e.message})
    if e.code.startswith("NETWORK_"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": e.code, "message": e.message})
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": e.code, "message": e.message})


def _job_summary(job) -> JobSummary:
    return JobSummary(
        code=200,
        job_id=job.job_id,
        state=job.state,
        quality=job.quality,
        device_id=job.device_id,
        total=job.total,
        done=job.done,
        failed=job.failed,
        skipped=job.skipped,
        revision=job.revision,
        network_epoch=job.network_epoch,
        created_at=svc._aware(job.created_at).isoformat() if job.created_at else "",
        updated_at=svc._aware(job.updated_at).isoformat() if job.updated_at else "",
    )


@router.post("/network", response_model=NetworkConfirmResponse)
async def confirm_network(
    body: NetworkConfirmRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_write_admin),
):
    owner_key, account_id = await _owner_scope(user)
    device_id = _target_device(body.device_id)
    try:
        na, token = svc.issue_network_authorization(
            db,
            owner_key=owner_key,
            account_id=account_id,
            device_id=device_id,
            network_epoch=body.network_epoch,
            batch_total=body.batch_total,
            expires_in_minutes=body.expires_in_minutes,
        )
    except svc.OfflineConflict as e:
        _raise(e)
    return NetworkConfirmResponse(
        code=200,
        device_id=device_id,
        network_epoch=body.network_epoch,
        wifi_confirm_token=token,
        batch_total=na.batch_total,
        batch_remaining=na.batch_remaining,
        expires_at=svc._aware(na.expires_at).isoformat(),
    )


@router.post("/jobs", response_model=JobSummary)
async def create_job(
    body: CreateJobRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_write_admin),
):
    owner_key, account_id = await _owner_scope(user)
    device_id = _target_device(body.device_id)
    try:
        snap, _meta = await favorite_catalog.view(revision=body.favorite_revision)
    except Exception:
        raise HTTPException(409, detail={"code": "FAVORITE_REVISION_CHANGED", "message": "Refresh the complete favorite membership"}) from None
    if snap.owner_key != owner_key:
        raise HTTPException(409, detail={"code": "OWNER_NOT_VERIFIED", "message": "Owner changed"})
    song_ids = sorted({int(t["id"]) for t in snap.tracks} | set(snap.skipped_ids))
    if body.song_ids is not None and set(body.song_ids) != set(song_ids):
        raise HTTPException(409, detail={"code": "INCOMPLETE_FAVORITES", "message": "Use the complete pinned favorite membership"})
    try:
        job, _created = svc.create_job(
            db,
            owner_key=owner_key,
            account_id=account_id,
            device_id=device_id,
            idempotency_key=body.idempotency_key,
            song_ids=song_ids,
            quality=body.quality,
            network_epoch=body.network_epoch,
            wifi_confirm_token=body.wifi_confirm_token,
        )
    except svc.OfflineConflict as e:
        _raise(e)
    return _job_summary(job)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    device_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    owner_key, account_id = await _owner_scope(user)
    jobs = svc.list_jobs(db, owner_key=owner_key, account_id=account_id, device_id=resolve_car_device_id(device_id) if device_id else None)
    return JobListResponse(code=200, jobs=[_job_summary(j) for j in jobs], total=len(jobs))


@router.get("/jobs/{job_id}", response_model=JobSummary)
async def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    owner_key, account_id = await _owner_scope(user)
    try:
        job = svc.get_job(db, job_id, owner_key=owner_key, account_id=account_id)
    except svc.OfflineConflict as e:
        _raise(e)
    return _job_summary(job)


@router.get("/jobs/{job_id}/items", response_model=ItemListResponse)
async def list_items(
    job_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    revision: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    owner_key, account_id = await _owner_scope(user)
    try:
        job = svc.get_job(db, job_id, owner_key=owner_key, account_id=account_id)
        items, total, has_more = svc.list_items(db, job=job, offset=offset, limit=limit, revision=revision)
    except svc.OfflineConflict as e:
        _raise(e)
    return ItemListResponse(
        code=200,
        job_id=job_id,
        revision=job.revision,
        offset=offset,
        limit=limit,
        total=total,
        has_more=has_more,
        items=[svc.item_payload(i) for i in items],
    )


def _set_job_state(state: str):
    async def handler(job_id: str, body: JobStateRequest, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(_write_admin)):
        owner_key, account_id = await _owner_scope(user)
        try:
            job = svc.get_job(db, job_id, owner_key=owner_key, account_id=account_id)
            job = svc.set_job_state(db, job, state, **body.model_dump())
        except svc.OfflineConflict as e:
            _raise(e)
        return _job_summary(job)

    return handler


router.post("/jobs/{job_id}/pause", response_model=JobSummary)(_set_job_state(svc.PAUSED))
router.post("/jobs/{job_id}/resume", response_model=JobSummary)(_set_job_state(svc.RUNNING))
router.post("/jobs/{job_id}/cancel", response_model=JobSummary)(_set_job_state(svc.CANCELLED))


@router.post("/work/claim", response_model=ClaimResponse)
async def claim_work(
    body: ClaimRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_bound_car),
):
    device_id = _car_device(user, body.device_id)
    owner_key, _ = await _owner_scope(user)
    try:
        items = svc.claim_batch(
            db, owner_key=owner_key, device_id=device_id, job_id=body.job_id, limit=body.limit, network_epoch=body.network_epoch
        )
    except svc.OfflineConflict as e:
        _raise(e)
    return ClaimResponse(code=200, owner_key=owner_key, items=[svc.claim_payload(i) for i in items])


@router.post("/work/{item_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    item_id: str,
    body: HeartbeatRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_bound_car),
):
    device_id = _car_device(user, body.device_id)
    owner_key, _ = await _owner_scope(user)
    try:
        item = svc.get_item(db, item_id, device_id=device_id, owner_key=owner_key)
        item = svc.heartbeat(db, item, version=body.version, lease_token=body.lease_token, network_epoch=body.network_epoch)
    except svc.OfflineConflict as e:
        _raise(e)
    return HeartbeatResponse(code=200, item_id=item.item_id, version=item.version, status=item.status, lease_expires_at=svc._aware(item.claim_deadline).isoformat())


@router.post("/items/{item_id}/receipt", response_model=OfflineItemResponse)
async def submit_receipt(
    item_id: str,
    body: ReceiptRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_bound_car),
):
    device_id = _car_device(user, body.device_id)
    owner_key, _ = await _owner_scope(user)
    try:
        item = svc.get_item(db, item_id, device_id=device_id, owner_key=owner_key)
        if item.job_id != body.job_id:
            raise svc.OfflineConflict("JOB_MISMATCH", "item does not belong to the given job")
        item = svc.submit_receipt(
            db,
            item,
            version=body.version,
            lease_token=body.lease_token,
            network_epoch=body.network_epoch,
            size_bytes=body.size_bytes,
            sha256=body.sha256,
            openable=body.openable,
            card_volume_id=body.card_volume_id,
            format=body.format,
        )
    except svc.OfflineConflict as e:
        _raise(e)
    return svc.item_payload(item)


@router.post("/items/{item_id}/revoke", response_model=OfflineItemResponse)
async def revoke_item(
    item_id: str,
    body: RevokeRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_bound_car),
):
    device_id = _car_device(user, body.device_id)
    owner_key, _ = await _owner_scope(user)
    try:
        item = svc.get_item(db, item_id, device_id=device_id, owner_key=owner_key)
        item = svc.revoke_item(db, item, version=body.version, reason=body.reason)
    except svc.OfflineConflict as e:
        _raise(e)
    return svc.item_payload(item)


@router.post("/work/{item_id}/fail", response_model=OfflineItemResponse)
async def fail_item(item_id: str, body: FailRequest, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(require_bound_car)):
    device_id = _car_device(user, body.device_id)
    owner_key, _ = await _owner_scope(user)
    try:
        item = svc.get_item(db, item_id, device_id=device_id, owner_key=owner_key)
        item = svc.fail_item(db, item, version=body.version, lease_token=body.lease_token,
                            network_epoch=body.network_epoch, error_code=body.error_code)
    except svc.OfflineConflict as e:
        _raise(e)
    return svc.item_payload(item)
