"""SD work bookkeeping only; never fetches audio or touches the vehicle filesystem.

SQLite BEGIN IMMEDIATE serializes the complete gate/read/CAS/inventory/counter
transaction across independent Sessions/processes. Item versions and opaque leases
fence late workers; durable inventory tombstones and physical card generations
fence old jobs. API authentication supplies the verified Owner and bound device.
"""
import hashlib
import json
import re
import secrets
from datetime import timedelta, timezone
from functools import wraps

from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.models import NetworkAuthorization, OfflineDevice, OfflineItem, OfflineJob, OfflineTrack, utc_now

PENDING, CLAIMED, SAVED, FAILED, REVOKED = "pending", "claimed", "saved", "failed", "revoked"
RUNNING, PAUSED, DONE, CANCELLED = "running", "paused", "done", "cancelled"
LEGACY_ACCOUNT = "__legacy__"
ERROR_CODES = {"SOURCE_UNAVAILABLE", "DOWNLOAD_FAILED", "NETWORK_LOST", "STORAGE_FULL", "HASH_MISMATCH", "NOT_OPENABLE", "FILE_MISSING"}
REASONS = {"CARD_REMOVED", "CARD_CHANGED", "FILE_MISSING", "NOT_OPENABLE", "HASH_MISMATCH"}


class OfflineConflict(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")


def _new_id(prefix):
    return f"{prefix}_{secrets.token_hex(12)}"


def _sha256_hex(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _now():
    return utc_now()


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def _is_expired(dt):
    return dt is None or _aware(dt) <= _now()


def _stale(row):
    return row.last_verified_at is None or (_now() - _aware(row.last_verified_at)).total_seconds() >= settings.OFFLINE_RECHECK_SECONDS


def account_scope(account_id):
    return account_id or LEGACY_ACCOUNT


def _transaction(fn):
    @wraps(fn)
    def wrapped(db: Session, *args, **kwargs):
        # Authentication and object lookup may have opened a read transaction. There
        # must be no caller-owned writes to discard; this service owns its unit of work.
        if db.new or db.dirty or db.deleted:
            raise OfflineConflict("TRANSACTION_DIRTY", "offline operation requires a clean session")
        db.rollback()
        db.expire_all()
        previous_expiration = db.expire_on_commit
        db.expire_on_commit = False
        try:
            if db.bind.dialect.name != "sqlite":
                raise OfflineConflict("UNSUPPORTED_DATABASE", "offline work requires SQLite transaction locking")
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            result = fn(db, *args, **kwargs)
            db.commit()
            return result
        except OperationalError:
            db.rollback()
            raise OfflineConflict("DATABASE_BUSY", "offline transaction unavailable; retry explicitly") from None
        except Exception:
            db.rollback()
            raise
        finally:
            db.expire_on_commit = previous_expiration
    return wrapped


def _device(db, device_id):
    row = db.get(OfflineDevice, device_id)
    if row is None:
        row = OfflineDevice(device_id=device_id, generation=1)
        db.add(row)
        db.flush()
    return row


def _scope(row):
    return (OfflineTrack.owner_key == row.owner_key, OfflineTrack.account_id == row.account_id,
            OfflineTrack.device_id == row.device_id, OfflineTrack.song_id == row.song_id, OfflineTrack.quality == row.quality)


def _track(db, row):
    return db.query(OfflineTrack).filter(*_scope(row)).first()


def _track_items(db, row):
    return db.query(OfflineItem).filter(OfflineItem.owner_key == row.owner_key, OfflineItem.account_id == row.account_id,
        OfflineItem.device_id == row.device_id, OfflineItem.song_id == row.song_id, OfflineItem.quality == row.quality)


def _network(db, *, owner_key, account_id, device_id, network_epoch, token_hash=None):
    na = db.query(NetworkAuthorization).filter_by(device_id=device_id, network_epoch=network_epoch).first()
    if na is None:
        raise OfflineConflict("NETWORK_NOT_AUTHORIZED", "network confirmation required")
    if na.owner_key != owner_key or na.account_id != account_scope(account_id):
        raise OfflineConflict("NETWORK_OWNER_MISMATCH", "network confirmation scope changed")
    if token_hash is not None and not secrets.compare_digest(token_hash, na.token_hash):
        raise OfflineConflict("NETWORK_TOKEN_MISMATCH", "network confirmation changed")
    if _is_expired(na.expires_at):
        raise OfflineConflict("NETWORK_AUTH_EXPIRED", "network confirmation expired")
    return na


@_transaction
def issue_network_authorization(db, *, owner_key, account_id, device_id, network_epoch, batch_total, expires_in_minutes):
    if not 1 <= batch_total <= 1000 or not 1 <= expires_in_minutes <= 10080:
        raise OfflineConflict("BAD_CONFIRMATION", "invalid authorization bounds")
    token = secrets.token_urlsafe(32)
    na = db.query(NetworkAuthorization).filter_by(device_id=device_id, network_epoch=network_epoch).first()
    if na is None:
        na = NetworkAuthorization(id=_new_id("netauth"), device_id=device_id, network_epoch=network_epoch)
        db.add(na)
    na.owner_key, na.account_id = owner_key, account_scope(account_id)
    na.token_hash, na.batch_total, na.batch_remaining = _sha256_hex(token), batch_total, batch_total
    na.expires_at = _now() + timedelta(minutes=expires_in_minutes)
    db.flush()
    return na, token


@_transaction
def create_job(db, *, owner_key, account_id, device_id, idempotency_key, song_ids, quality, network_epoch, wifi_confirm_token):
    account_id = account_scope(account_id)
    # Canonical full membership, never truncate or count duplicate song IDs twice.
    song_ids = sorted(set(song_ids))
    if not song_ids or any(type(s) is not int or s <= 0 for s in song_ids):
        raise OfflineConflict("BAD_SONG_IDS", "positive song identifiers required")
    fingerprint = _sha256_hex(json.dumps([song_ids, quality, device_id, network_epoch, account_id]))
    scoped_key = _sha256_hex(json.dumps([account_id, idempotency_key]))
    job = db.query(OfflineJob).filter_by(owner_key=owner_key, idempotency_key=scoped_key).first()
    if job:
        if job.request_fingerprint != fingerprint:
            raise OfflineConflict("IDEMPOTENCY_CONFLICT", "same key with different request")
        return job, False
    na = _network(db, owner_key=owner_key, account_id=account_id, device_id=device_id,
                  network_epoch=network_epoch, token_hash=_sha256_hex(wifi_confirm_token))
    res = db.execute(update(NetworkAuthorization).where(NetworkAuthorization.id == na.id,
        NetworkAuthorization.batch_remaining > 0).values(batch_remaining=NetworkAuthorization.batch_remaining - 1))
    if res.rowcount != 1:
        raise OfflineConflict("NETWORK_BATCH_EXHAUSTED", "network confirmation batch exhausted")
    device = _device(db, device_id)
    job = OfflineJob(job_id=_new_id("job"), owner_key=owner_key, account_id=account_id, device_id=device_id,
        idempotency_key=scoped_key, request_fingerprint=fingerprint, state=RUNNING, quality=quality,
        total=len(song_ids), done=0, failed=0, skipped=0, revision=1, network_epoch=network_epoch,
        wifi_confirm_token_hash=na.token_hash)
    db.add(job)
    for sid in song_ids:
        item = OfflineItem(item_id=_new_id("itm"), job_id=job.job_id, owner_key=owner_key, account_id=account_id,
            device_id=device_id, song_id=sid, quality=quality, status=PENDING, version=1, attempts=0,
            device_generation=device.generation, inventory_version=0, work_kind="download")
        track = _track(db, item)
        if track:
            item.inventory_version = track.version
            if track.status == SAVED and track.card_volume_id == device.card_volume_id:
                _copy_saved(item, track)
                if _stale(track):
                    item.status = PENDING
        db.add(item)
    _touch_job(db, job.job_id)
    return job, True


def get_job(db, job_id, *, owner_key, account_id=None, device_id=None, car=False):
    q = db.query(OfflineJob).populate_existing().filter_by(job_id=job_id, owner_key=owner_key)
    q = q.filter_by(device_id=device_id) if car else q.filter_by(account_id=account_scope(account_id))
    job = q.first()
    if job is None:
        raise OfflineConflict("NOT_FOUND", "job not found")
    return job


def list_jobs(db, *, owner_key, account_id=None, device_id=None):
    q = db.query(OfflineJob).populate_existing().filter_by(owner_key=owner_key, account_id=account_scope(account_id))
    if device_id:
        q = q.filter_by(device_id=device_id)
    return q.order_by(OfflineJob.created_at.desc()).all()


def get_item(db, item_id, *, device_id, owner_key):
    item = db.query(OfflineItem).populate_existing().filter_by(item_id=item_id, device_id=device_id, owner_key=owner_key).first()
    if item is None:
        raise OfflineConflict("NOT_FOUND", "item not found")
    return item


def _touch_job(db, job_id):
    db.flush()
    job = db.get(OfflineJob, job_id)
    items = db.query(OfflineItem).filter_by(job_id=job_id).all()
    job.done = sum(i.status == SAVED for i in items)
    job.failed = sum(i.status in (FAILED, REVOKED) for i in items)
    job.skipped = sum(i.status == SAVED and i.attempts == 0 for i in items)
    job.revision += 1
    job.updated_at = _now()
    if job.state not in (PAUSED, CANCELLED):
        job.state = RUNNING if any(i.status in (PENDING, CLAIMED) for i in items) else DONE
    db.flush()


def _invalidate_claim(item):
    item.status = PENDING
    item.version += 1
    item.claim_deadline = item.lease_token = None


def _pause(db, job):
    job.state = PAUSED
    for item in db.query(OfflineItem).filter_by(job_id=job.job_id, status=CLAIMED).all():
        _invalidate_claim(item)
    _touch_job(db, job.job_id)


def _gate(db, job, network_epoch):
    if job.state != RUNNING:
        raise OfflineConflict("JOB_NOT_RUNNING", "job requires explicit resume or new job")
    try:
        if not network_epoch or network_epoch != job.network_epoch:
            raise OfflineConflict("NETWORK_EPOCH_MISMATCH", "network changed; explicit confirmation and resume required")
        return _network(db, owner_key=job.owner_key, account_id=job.account_id, device_id=job.device_id,
                        network_epoch=network_epoch, token_hash=job.wifi_confirm_token_hash)
    except OfflineConflict:
        # This intentional committed denial must survive the HTTP error rollback.
        _pause(db, job)
        db.commit()
        raise


@_transaction
def set_job_state(db, job, state, *, revision, network_epoch=None, wifi_confirm_token=None):
    if job.revision != revision:
        raise OfflineConflict("REVISION_CONFLICT", "stale job revision")
    transitions = {RUNNING: {PAUSED, CANCELLED}, PAUSED: {RUNNING, CANCELLED}, DONE: set(), CANCELLED: set()}
    if state not in transitions.get(job.state, set()):
        raise OfflineConflict("BAD_STATE", "invalid job transition")
    if state == RUNNING:
        na = _network(db, owner_key=job.owner_key, account_id=job.account_id, device_id=job.device_id,
            network_epoch=network_epoch, token_hash=_sha256_hex(wifi_confirm_token or ""))
        # Explicit new confirmation may replace an expired or changed-network grant.
        if na.token_hash != job.wifi_confirm_token_hash or network_epoch != job.network_epoch:
            if na.batch_remaining <= 0:
                raise OfflineConflict("NETWORK_BATCH_EXHAUSTED", "network confirmation batch exhausted")
            na.batch_remaining -= 1
        job.network_epoch, job.wifi_confirm_token_hash = network_epoch, na.token_hash
        job.state = RUNNING
        _touch_job(db, job.job_id)
    else:
        _pause(db, job)
        job.state = state
    return job


@_transaction
def list_items(db, *, job, offset, limit, revision=None):
    if revision is not None and job.revision != revision:
        raise OfflineConflict("REVISION_CONFLICT", "job changed; reread page one")
    q = db.query(OfflineItem).filter_by(job_id=job.job_id)
    total = q.count()
    items = q.order_by(OfflineItem.created_at, OfflineItem.item_id).offset(offset).limit(limit).all()
    return items, total, offset + len(items) < total


def _copy_saved(item, track):
    item.status, item.work_kind = SAVED, "verify"
    for key in ("size_bytes", "sha256", "card_volume_id", "format", "last_verified_at"):
        setattr(item, key, getattr(track, key))
    item.inventory_version = track.version
    item.claim_deadline = item.lease_token = None
    item.error_code = item.error_detail = None


def _expire_claims(db, device_id):
    jobs = set()
    for item in db.query(OfflineItem).filter(OfflineItem.device_id == device_id,
            OfflineItem.status == CLAIMED, OfflineItem.claim_deadline <= _now()).all():
        _invalidate_claim(item)
        if item.attempts >= settings.OFFLINE_MAX_CLAIM_ATTEMPTS:
            item.status, item.error_code = FAILED, "CLAIM_LIMIT_EXCEEDED"
        jobs.add(item.job_id)
    for jid in jobs:
        _touch_job(db, jid)


@_transaction
def claim_batch(db, *, owner_key, device_id, job_id, limit, network_epoch):
    job = get_job(db, job_id, owner_key=owner_key, device_id=device_id, car=True)
    na = _gate(db, job, network_epoch)
    _expire_claims(db, device_id)
    device = _device(db, device_id)
    items = db.query(OfflineItem).filter_by(job_id=job_id, status=PENDING).order_by(OfflineItem.created_at, OfflineItem.item_id).all()
    claimed = []
    changed = False
    for item in items:
        track = _track(db, item)
        if track and track.status == SAVED and track.card_volume_id == device.card_volume_id and not _stale(track):
            _copy_saved(item, track)
            item.version += 1
            changed = True
            continue
        active = _track_items(db, item).filter(OfflineItem.status == CLAIMED).first()
        if active:
            continue
        if item.attempts >= settings.OFFLINE_MAX_CLAIM_ATTEMPTS:
            item.status, item.error_code = FAILED, "CLAIM_LIMIT_EXCEEDED"
            item.version += 1
            changed = True
            continue
        if len(claimed) >= limit:
            continue
        if track and track.status == SAVED:
            for key in ("size_bytes", "sha256", "card_volume_id", "format", "last_verified_at"):
                setattr(item, key, getattr(track, key))
            db.flush()
        version = item.version
        lease = secrets.token_hex(24)
        kind = "verify" if track and track.status == SAVED else "download"
        deadline = min(_aware(na.expires_at), _now() + timedelta(minutes=settings.OFFLINE_CLAIM_TIMEOUT_MINUTES))
        res = db.execute(update(OfflineItem).where(OfflineItem.item_id == item.item_id,
            OfflineItem.version == version, OfflineItem.status == PENDING).values(status=CLAIMED,
            version=version + 1, attempts=OfflineItem.attempts + 1, lease_token=lease, claim_deadline=deadline,
            inventory_version=track.version if track else 0, device_generation=device.generation,
            work_kind=kind, error_code=None, error_detail=None).execution_options(synchronize_session=False))
        if res.rowcount != 1:
            raise OfflineConflict("VERSION_CONFLICT", "item changed during claim")
        db.refresh(item)
        claimed.append(item)
        changed = True
    if changed:
        _touch_job(db, job_id)
    return claimed


def _cas(db, item, version, **values):
    result = db.execute(update(OfflineItem).where(OfflineItem.item_id == item.item_id,
        OfflineItem.version == version).values(version=version + 1, **values).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise OfflineConflict("VERSION_CONFLICT", "stale item version")
    db.refresh(item)


def _check_version(item, version):
    if item.version != version:
        raise OfflineConflict("VERSION_CONFLICT", "stale item version")


def _lease(db, item, *, version, lease_token, network_epoch):
    _check_version(item, version)
    if item.status != CLAIMED:
        raise OfflineConflict("NOT_CLAIMED", "active claim required")
    job = db.get(OfflineJob, item.job_id)
    na = _gate(db, job, network_epoch)
    if not lease_token or not secrets.compare_digest(item.lease_token or "", lease_token):
        raise OfflineConflict("LEASE_MISMATCH", "claim lease mismatch")
    if _is_expired(item.claim_deadline):
        raise OfflineConflict("LEASE_EXPIRED", "claim lease expired")
    device = _device(db, item.device_id)
    track = _track(db, item)
    if item.device_generation != device.generation or item.inventory_version != (track.version if track else 0):
        raise OfflineConflict("INVENTORY_CHANGED", "card or inventory changed; claim again")
    return na


@_transaction
def heartbeat(db, item, *, version, lease_token, network_epoch):
    na = _lease(db, item, version=version, lease_token=lease_token, network_epoch=network_epoch)
    deadline = min(_aware(na.expires_at), _now() + timedelta(minutes=settings.OFFLINE_CLAIM_TIMEOUT_MINUTES))
    _cas(db, item, version, claim_deadline=deadline, updated_at=_now())
    _touch_job(db, item.job_id)
    return item


def _invalidate_inventory(db, *, device_id, reason, target=None, exclude=None):
    tracks = db.query(OfflineTrack).filter_by(device_id=device_id)
    items = db.query(OfflineItem).filter_by(device_id=device_id)
    if target is not None:
        tracks = tracks.filter(*_scope(target))
        items = _track_items(db, target)
    for track in tracks.all():
        track.status, track.last_verified_at = REVOKED, None
        track.version += 1
    jobs = set()
    for item in items.all():
        if item.item_id == exclude:
            continue
        # Failed and revoked entries also lose their token/version; no late job can resurrect.
        item.status, item.error_code, item.error_detail = REVOKED, reason, None
        item.last_verified_at = item.claim_deadline = item.lease_token = None
        item.version += 1
        jobs.add(item.job_id)
    for jid in jobs:
        _touch_job(db, jid)


def _save_track(db, item):
    track = _track(db, item)
    if track is None:
        track = OfflineTrack(id=_new_id("trk"), owner_key=item.owner_key, account_id=item.account_id,
            device_id=item.device_id, song_id=item.song_id, quality=item.quality, version=0)
        db.add(track)
    track.status = SAVED
    track.version += 1
    for key in ("size_bytes", "sha256", "card_volume_id", "format", "last_verified_at"):
        setattr(track, key, getattr(item, key))
    db.flush()
    jobs = {item.job_id}
    for other in _track_items(db, item).all():
        # Only waiting/saved entries can inherit this fresh authoritative receipt.
        # Cancelled/failed/revoked jobs retain their explicit outcome.
        if other.item_id != item.item_id and db.get(OfflineJob, other.job_id).state == CANCELLED:
            continue
        if other.status in (PENDING, SAVED) or other.item_id == item.item_id:
            _copy_saved(other, track)
            if other.item_id != item.item_id:
                other.version += 1
            jobs.add(other.job_id)
    for jid in jobs:
        _touch_job(db, jid)


@_transaction
def submit_receipt(db, item, *, version, size_bytes, sha256, openable, card_volume_id, format=None,
                   lease_token=None, network_epoch=None):
    _check_version(item, version)
    sha256 = sha256.lower()
    device = _device(db, item.device_id)
    if item.status == SAVED:
        # Offline local re-verification is allowed for the exact existing file/card,
        # including a completed/paused job; it grants no download permission.
        track = _track(db, item)
        if not track or track.status != SAVED or item.device_generation != device.generation or item.inventory_version != track.version:
            raise OfflineConflict("INVENTORY_CHANGED", "inventory changed")
        if (card_volume_id, sha256, size_bytes, format) != (track.card_volume_id, track.sha256, track.size_bytes, track.format):
            raise OfflineConflict("RECEIPT_MISMATCH", "reverification must describe the existing file")
    else:
        _lease(db, item, version=version, lease_token=lease_token, network_epoch=network_epoch)
        if item.work_kind == "verify":
            track = _track(db, item)
            if (card_volume_id, sha256, size_bytes, format) != (track.card_volume_id, track.sha256, track.size_bytes, track.format):
                raise OfflineConflict("RECEIPT_MISMATCH", "verification must describe the existing file")
    if not openable:
        _invalidate_inventory(db, device_id=item.device_id, reason="NOT_OPENABLE", target=item)
        return item
    if size_bytes <= 0 or not re.fullmatch(r"[a-fA-F0-9]{64}", sha256) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", card_volume_id) or format not in {"mp3", "flac", "m4a", "aac", "ogg", "wav"}:
        raise OfflineConflict("BAD_RECEIPT", "complete file verification required")
    if device.card_volume_id and device.card_volume_id != card_volume_id:
        # First receipt for a replacement card fences all other old work atomically.
        _invalidate_inventory(db, device_id=item.device_id, reason="CARD_CHANGED", exclude=item.item_id)
        device.generation += 1
    device.card_volume_id = card_volume_id
    _cas(db, item, version, status=SAVED, size_bytes=size_bytes, sha256=sha256.lower(), card_volume_id=card_volume_id,
         format=format, error_code=None, error_detail=None, claim_deadline=None, lease_token=None,
         last_receipt_at=_now(), last_verified_at=_now(), device_generation=device.generation, updated_at=_now())
    _save_track(db, item)
    return item


@_transaction
def revoke_item(db, item, *, version, reason):
    _check_version(item, version)
    if reason not in REASONS:
        raise OfflineConflict("BAD_REASON", "unsupported revocation reason")
    device = _device(db, item.device_id)
    if reason in {"CARD_REMOVED", "CARD_CHANGED"}:
        device.generation += 1
        device.card_volume_id = None
        _invalidate_inventory(db, device_id=item.device_id, reason=reason)
    else:
        _invalidate_inventory(db, device_id=item.device_id, reason=reason, target=item)
    return item


@_transaction
def fail_item(db, item, *, version, lease_token, network_epoch, error_code, error_detail=None):
    _lease(db, item, version=version, lease_token=lease_token, network_epoch=network_epoch)
    if error_code not in ERROR_CODES:
        raise OfflineConflict("BAD_ERROR_CODE", "unsupported failure code")
    if item.work_kind == "verify" and error_code in {"FILE_MISSING", "HASH_MISMATCH", "NOT_OPENABLE"}:
        _invalidate_inventory(db, device_id=item.device_id, reason=error_code, target=item)
        return item
    retry = error_code in {"SOURCE_UNAVAILABLE", "DOWNLOAD_FAILED", "NETWORK_LOST"} and item.attempts < settings.OFFLINE_MAX_CLAIM_ATTEMPTS
    # Raw diagnostics may contain paths/URLs/credentials: intentionally never persist them.
    _cas(db, item, version, status=PENDING if retry else FAILED, error_code=error_code, error_detail=None,
         claim_deadline=None, lease_token=None, updated_at=_now())
    _touch_job(db, item.job_id)
    if error_code == "NETWORK_LOST":
        _pause(db, db.get(OfflineJob, item.job_id))
    return item


def item_payload(item):
    return {key: getattr(item, key) for key in ("item_id", "song_id", "quality", "status", "version", "format",
            "size_bytes", "sha256", "card_volume_id", "error_code")} | {
        "stale": item.status == SAVED and _stale(item),
        "last_verified_at": _aware(item.last_verified_at).isoformat() if item.last_verified_at else None,
    }


def claim_payload(item):
    return item_payload(item) | {"job_id": item.job_id, "owner_key": item.owner_key, "device_id": item.device_id,
        "lease_token": item.lease_token, "lease_expires_at": _aware(item.claim_deadline).isoformat(),
        "work_kind": item.work_kind, "attempts": item.attempts}
