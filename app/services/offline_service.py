"""Offline SD download service: jobs, items, trusted car receipts, revocation.

Frozen minimal contract (business rules):
- A single unified "saved" state is used (no separate "ready").
- Receipt/heartbeat/revoke are accepted only from the bound vehicle device (role=car)
  whose canonical device_id matches the item; enforced at the API layer.
- Per-item ``version`` is optimistic concurrency: every mutation bumps it and any
  call carrying a stale version is rejected (VERSION_CONFLICT).
- Job idempotency is (owner_key, idempotency_key) + request fingerprint; a replay
  of the same request returns the same job, a different fingerprint conflicts.
- Network authorization is bound to (device_id, network_epoch), time-limited and
  per-batch. The server never infers "unlimited" from an SSID; the car reports the
  epoch and performs the actual network judgement.
- No absolute path / SAF URI / credential is ever stored or returned.
"""
import hashlib
import json
import secrets
from datetime import timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.models import (
    NetworkAuthorization,
    OfflineItem,
    OfflineJob,
    utc_now,
)

# Item status (single unified saved state)
PENDING = "pending"
CLAIMED = "claimed"
SAVED = "saved"
FAILED = "failed"
REVOKED = "revoked"

# Job states
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
CANCELLED = "cancelled"


class OfflineConflict(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now():
    return utc_now()


def _is_expired(dt) -> bool:
    """Time-zone safe expiry check (SQLite returns offset-naive datetimes)."""
    if dt is None:
        return False
    now = _now()
    if dt.tzinfo is None:
        return dt < now.replace(tzinfo=None)
    return dt < now


def _age_seconds(dt) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (_now() - dt).total_seconds()


# ---------------------------------------------------------------------------
# Network authorization (device + network epoch, time-limited, per-batch)
# ---------------------------------------------------------------------------

def issue_network_authorization(
    db: Session,
    *,
    owner_key: str,
    account_id: Optional[str],
    device_id: str,
    network_epoch: str,
    batch_total: int,
    expires_in_minutes: int,
) -> Tuple[NetworkAuthorization, str]:
    """Creates or refreshes an authorization. Returns (record, plaintext token)."""
    token = secrets.token_urlsafe(32)
    token_hash = _sha256_hex(token)
    now = _now()
    existing = (
        db.query(NetworkAuthorization)
        .filter(
            NetworkAuthorization.device_id == device_id,
            NetworkAuthorization.network_epoch == network_epoch,
        )
        .first()
    )
    if existing is not None:
        existing.owner_key = owner_key
        existing.account_id = account_id
        existing.token_hash = token_hash
        existing.batch_total = batch_total
        existing.batch_remaining = batch_total
        existing.expires_at = now + timedelta(minutes=expires_in_minutes)
        existing.updated_at = now
        na = existing
    else:
        na = NetworkAuthorization(
            id=_new_id("netauth"),
            owner_key=owner_key,
            account_id=account_id,
            device_id=device_id,
            network_epoch=network_epoch,
            token_hash=token_hash,
            batch_total=batch_total,
            batch_remaining=batch_total,
            expires_at=now + timedelta(minutes=expires_in_minutes),
        )
        db.add(na)
    db.commit()
    db.refresh(na)
    return na, token


def _consume_network_batch(
    db: Session,
    *,
    device_id: str,
    network_epoch: str,
    wifi_confirm_token: str,
) -> None:
    na = (
        db.query(NetworkAuthorization)
        .filter(
            NetworkAuthorization.device_id == device_id,
            NetworkAuthorization.network_epoch == network_epoch,
        )
        .first()
    )
    if na is None:
        raise OfflineConflict("NETWORK_NOT_AUTHORIZED", "network epoch not authorized for this device")
    if not secrets.compare_digest(_sha256_hex(wifi_confirm_token), na.token_hash):
        raise OfflineConflict("NETWORK_TOKEN_MISMATCH", "wifi confirmation token mismatch")
    if _is_expired(na.expires_at):
        raise OfflineConflict("NETWORK_AUTH_EXPIRED", "network authorization expired")
    if na.batch_remaining <= 0:
        raise OfflineConflict("NETWORK_BATCH_EXHAUSTED", "network authorization batch exhausted")
    na.batch_remaining -= 1


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _job_fingerprint(song_ids: List[int], quality: str, device_id: str, network_epoch: str) -> str:
    payload = {
        "song_ids": sorted(int(s) for s in song_ids),
        "quality": quality,
        "device_id": device_id,
        "network_epoch": network_epoch,
    }
    return _sha256_hex(json.dumps(payload, sort_keys=True))


def create_job(
    db: Session,
    *,
    owner_key: str,
    account_id: Optional[str],
    device_id: str,
    idempotency_key: str,
    song_ids: List[int],
    quality: str,
    network_epoch: str,
    wifi_confirm_token: str,
) -> Tuple[OfflineJob, bool]:
    """Returns (job, created). Idempotent on (owner_key, idempotency_key)."""
    fingerprint = _job_fingerprint(song_ids, quality, device_id, network_epoch)

    existing = (
        db.query(OfflineJob)
        .filter(OfflineJob.owner_key == owner_key, OfflineJob.idempotency_key == idempotency_key)
        .first()
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise OfflineConflict("IDEMPOTENCY_CONFLICT", "same idempotency_key with a different request")
        return existing, False

    # Consume one network batch only when actually creating a new job.
    _consume_network_batch(
        db, device_id=device_id, network_epoch=network_epoch, wifi_confirm_token=wifi_confirm_token
    )

    job = OfflineJob(
        job_id=_new_id("job"),
        owner_key=owner_key,
        account_id=account_id,
        device_id=device_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        state=RUNNING,
        quality=quality,
        total=len(song_ids),
        done=0,
        failed=0,
        revision=1,
        network_epoch=network_epoch,
        wifi_confirm_token_hash=_sha256_hex(wifi_confirm_token),
    )
    db.add(job)
    for sid in song_ids:
        db.add(
            OfflineItem(
                item_id=_new_id("itm"),
                job_id=job.job_id,
                owner_key=owner_key,
                account_id=account_id,
                device_id=device_id,
                song_id=int(sid),
                quality=quality,
                status=PENDING,
                version=1,
            )
        )
    db.commit()
    db.refresh(job)
    return job, True


def get_job(db: Session, job_id: str, *, owner_key: str) -> OfflineJob:
    job = db.query(OfflineJob).filter(OfflineJob.job_id == job_id, OfflineJob.owner_key == owner_key).first()
    if job is None:
        raise OfflineConflict("NOT_FOUND", "job not found")
    return job


def list_jobs(db: Session, *, owner_key: str, device_id: Optional[str] = None) -> List[OfflineJob]:
    q = db.query(OfflineJob).filter(OfflineJob.owner_key == owner_key)
    if device_id:
        q = q.filter(OfflineJob.device_id == device_id)
    return q.order_by(OfflineJob.created_at.desc()).all()


def set_job_state(db: Session, job: OfflineJob, state: str) -> OfflineJob:
    if state not in (PAUSED, RUNNING, CANCELLED):
        raise OfflineConflict("BAD_STATE", "invalid job state transition")
    job.state = state
    db.commit()
    db.refresh(job)
    return job


def _touch_job(db: Session, job_id: str) -> None:
    db.flush()  # autoflush is disabled; persist pending item mutations before recounting
    job = db.query(OfflineJob).filter(OfflineJob.job_id == job_id).first()
    if job is None:
        return
    items = db.query(OfflineItem).filter(OfflineItem.job_id == job_id).all()
    job.done = sum(1 for i in items if i.status == SAVED)
    job.failed = sum(1 for i in items if i.status in (FAILED, REVOKED))
    job.revision += 1
    if job.state not in (PAUSED, CANCELLED):
        job.state = DONE if not any(i.status in (PENDING, CLAIMED) for i in items) else RUNNING


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def get_item(db: Session, item_id: str, *, device_id: str) -> OfflineItem:
    item = db.query(OfflineItem).filter(OfflineItem.item_id == item_id, OfflineItem.device_id == device_id).first()
    if item is None:
        raise OfflineConflict("NOT_FOUND", "item not found")
    return item


def list_items(
    db: Session,
    *,
    job: OfflineJob,
    offset: int,
    limit: int,
    revision: Optional[int] = None,
) -> Tuple[List[OfflineItem], int, bool]:
    """Optimistic consistency: pin ``revision`` and re-read; 409 on any change."""
    if revision is not None and revision != job.revision:
        raise OfflineConflict("REVISION_CONFLICT", "job changed; re-read items from page 1")

    total = db.query(OfflineItem).filter(OfflineItem.job_id == job.job_id).count()
    items = (
        db.query(OfflineItem)
        .filter(OfflineItem.job_id == job.job_id)
        .order_by(OfflineItem.created_at.asc(), OfflineItem.item_id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    has_more = offset + len(items) < total
    return items, total, has_more


def claim_batch(
    db: Session,
    *,
    device_id: str,
    job_id: Optional[str],
    limit: int,
    network_epoch: Optional[str],
) -> List[OfflineItem]:
    now = _now()

    # Reclaim stale claims whose claim_deadline passed without heartbeat.
    expired = db.query(OfflineItem).filter(OfflineItem.status == CLAIMED, OfflineItem.claim_deadline < now).all()
    expired_job_ids = {it.job_id for it in expired}
    for it in expired:
        it.status = PENDING
        it.claim_deadline = None
        it.version += 1
    if expired:
        db.flush()  # autoflush is disabled; make reclaimed items visible to the pending query

    q = (
        db.query(OfflineItem)
        .join(OfflineJob, OfflineItem.job_id == OfflineJob.job_id)
        .filter(OfflineItem.device_id == device_id, OfflineItem.status == PENDING)
    )
    if job_id:
        q = q.filter(OfflineItem.job_id == job_id)
    if network_epoch is not None:
        q = q.filter(OfflineJob.network_epoch == network_epoch)

    items = q.order_by(OfflineItem.created_at.asc(), OfflineItem.item_id.asc()).limit(limit).all()

    deadline = now + timedelta(minutes=settings.OFFLINE_CLAIM_TIMEOUT_MINUTES)
    touched = set(expired_job_ids)
    claimed: List[OfflineItem] = []
    for it in items:
        it.status = CLAIMED
        it.claim_deadline = deadline
        it.version += 1
        claimed.append(it)
        touched.add(it.job_id)

    for jid in touched:
        _touch_job(db, jid)
    db.commit()
    return claimed


def heartbeat(db: Session, item: OfflineItem, *, version: int) -> OfflineItem:
    if item.version != version:
        raise OfflineConflict("VERSION_CONFLICT", "stale item version")
    if item.status != CLAIMED:
        raise OfflineConflict("NOT_CLAIMED", "item is not claimed")
    item.claim_deadline = _now() + timedelta(minutes=settings.OFFLINE_CLAIM_TIMEOUT_MINUTES)
    db.commit()
    db.refresh(item)
    return item


def submit_receipt(
    db: Session,
    item: OfflineItem,
    *,
    version: int,
    size_bytes: int,
    sha256: str,
    openable: bool,
    card_volume_id: str,
    format: Optional[str],
) -> OfflineItem:
    if item.version != version:
        raise OfflineConflict("VERSION_CONFLICT", "stale item version")
    if item.status not in (CLAIMED, SAVED):
        raise OfflineConflict("BAD_STATE", "item is not in a claimable state")

    now = _now()
    if not openable:
        item.status = FAILED
        item.error_code = "NOT_OPENABLE"
        item.error_detail = "car reported file is not openable"
        item.claim_deadline = None
        item.version += 1
    else:
        item.status = SAVED
        item.size_bytes = size_bytes
        item.sha256 = sha256
        item.card_volume_id = card_volume_id
        item.format = format
        item.error_code = None
        item.error_detail = None
        item.last_receipt_at = now
        item.last_verified_at = now
        item.claim_deadline = None
        item.version += 1

    _touch_job(db, item.job_id)
    db.commit()
    db.refresh(item)
    return item


def revoke_item(db: Session, item: OfflineItem, *, version: int, reason: str) -> OfflineItem:
    if item.version != version:
        raise OfflineConflict("VERSION_CONFLICT", "stale item version")
    if item.status != SAVED:
        raise OfflineConflict("BAD_STATE", "only a saved item can be revoked")

    item.status = REVOKED
    item.error_code = "CARD_REMOVED" if reason == "CARD_REMOVED" else "REVOKED"
    item.error_detail = reason
    item.last_verified_at = None
    item.version += 1

    _touch_job(db, item.job_id)
    db.commit()
    db.refresh(item)
    return item


def item_payload(item: OfflineItem) -> Dict[str, Any]:
    stale = False
    if item.status == SAVED and item.last_verified_at is not None:
        stale = _age_seconds(item.last_verified_at) > settings.OFFLINE_RECHECK_SECONDS
    return {
        "item_id": item.item_id,
        "song_id": item.song_id,
        "quality": item.quality,
        "status": item.status,
        "version": item.version,
        "format": item.format,
        "size_bytes": item.size_bytes,
        "sha256": item.sha256,
        "card_volume_id": item.card_volume_id,
        "error_code": item.error_code,
        "stale": stale,
        "last_verified_at": item.last_verified_at.isoformat() if item.last_verified_at else None,
    }
