"""Offline SD download: isolation, idempotency, trusted receipt, revocation, pagination.

All fake: no production credentials, no upstream audio URLs, no real download.
"""
import pytest

from app.config import settings
from app.repositories.database import SessionLocal as AppSessionLocal
from app.repositories.migrations import run_db_migrations
from app.repositories.models import OfflineItem, OfflineJob
from app.services import offline_service as svc
from app.services.favorite_catalog import CatalogSnapshot


@pytest.fixture
def offline_owner(monkeypatch):
    from app.services.netease_service import favorite_catalog

    snap = CatalogSnapshot(
        revision=1,
        etag="e" * 16,
        owner_key="o" * 16,
        playlist_id=12345,
        name="liked",
        cover_url=None,
        tracks=(),
        skipped_ids=(),
        updated_at=0.0,
    )
    monkeypatch.setattr(favorite_catalog, "peek", lambda: snap)
    return snap


def _confirm_network(client, admin_headers, epoch="epoch-a", batch_total=10):
    resp = client.post(
        "/api/v1/music/offline/network",
        headers=admin_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "network_epoch": epoch, "batch_total": batch_total},
    )
    assert resp.status_code == 200
    return resp.json()


def _create_job(client, admin_headers, idem, song_ids, epoch="epoch-a", token=None, quality="320k"):
    if token is None:
        token = _confirm_network(client, admin_headers, epoch=epoch)["wifi_confirm_token"]
    return client.post(
        "/api/v1/music/offline/jobs",
        headers=admin_headers,
        json={
            "idempotency_key": idem,
            "device_id": settings.DEFAULT_CAR_DEVICE_ID,
            "song_ids": song_ids,
            "quality": quality,
            "network_epoch": epoch,
            "wifi_confirm_token": token,
        },
    )


# ---------------------------------------------------------------------------
# Idempotency + network authorization
# ---------------------------------------------------------------------------

def test_network_confirm_and_job_idempotent(client, admin_headers, offline_owner):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    r1 = _create_job(client, admin_headers, "idem-1", [101, 102], token=token)
    assert r1.status_code == 200
    job1 = r1.json()

    # Same idempotency key + same payload -> same job, no new batch consumed.
    r2 = _create_job(client, admin_headers, "idem-1", [101, 102], token=token)
    assert r2.status_code == 200
    assert r2.json()["job_id"] == job1["job_id"]
    assert r2.json()["total"] == 2


def test_job_idempotency_conflict_same_key_different_payload(client, admin_headers, offline_owner):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    assert _create_job(client, admin_headers, "idem-2", [101], token=token).status_code == 200
    # Same key, different song list -> 409 IDEMPOTENCY_CONFLICT.
    r = _create_job(client, admin_headers, "idem-2", [101, 999], token=token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_network_authorization_gates(client, admin_headers, offline_owner):
    # Wrong epoch -> 403 NETWORK_NOT_AUTHORIZED
    r = _create_job(client, admin_headers, "idem-3", [1], epoch="unconfirmed-epoch", token="dummy")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "NETWORK_NOT_AUTHORIZED"

    # Wrong token -> 403 NETWORK_TOKEN_MISMATCH
    _confirm_network(client, admin_headers, epoch="epoch-b", batch_total=1)
    r = _create_job(client, admin_headers, "idem-4", [1], epoch="epoch-b", token="wrong-token")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "NETWORK_TOKEN_MISMATCH"

    # Batch exhausted -> 403 NETWORK_BATCH_EXHAUSTED
    token = _confirm_network(client, admin_headers, epoch="epoch-c", batch_total=1)["wifi_confirm_token"]
    assert _create_job(client, admin_headers, "idem-5", [1], epoch="epoch-c", token=token).status_code == 200
    r = _create_job(client, admin_headers, "idem-6", [2], epoch="epoch-c", token=token)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "NETWORK_BATCH_EXHAUSTED"


# ---------------------------------------------------------------------------
# Auth boundary: role isolation
# ---------------------------------------------------------------------------

def test_normal_user_and_car_and_phone_cannot_create_job(client, admin_headers, phone_headers, car_headers, offline_owner):
    # Normal registered user (role=user) is forbidden from creating/reading jobs.
    client.post(
        "/api/v1/auth/register",
        json={"username": "offline_driver_a", "password": "DriverPass123!", "nickname": "Driver A"},
    )
    login = client.post(
        "/api/v1/auth/login", json={"username": "offline_driver_a", "password": "DriverPass123!"}
    ).json()
    user_headers = {"Authorization": f"Bearer {login['session_id']}"}

    r = client.post(
        "/api/v1/music/offline/jobs",
        headers=user_headers,
        json={"idempotency_key": "k", "song_ids": [1], "network_epoch": "e", "wifi_confirm_token": "t"},
    )
    assert r.status_code == 403

    r = client.get("/api/v1/music/offline/jobs", headers=user_headers)
    assert r.status_code == 403

    # Car (role=car) cannot create jobs either (admin-only).
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    r = client.post(
        "/api/v1/music/offline/jobs",
        headers=car_headers,
        json={"idempotency_key": "k2", "song_ids": [1], "network_epoch": "epoch-a", "wifi_confirm_token": token},
    )
    assert r.status_code == 403

    # Phone (legacy) cannot create jobs (admin-only); can read.
    r = client.post(
        "/api/v1/music/offline/jobs",
        headers=phone_headers,
        json={"idempotency_key": "k3", "song_ids": [1], "network_epoch": "epoch-a", "wifi_confirm_token": token},
    )
    assert r.status_code == 403


def test_phone_cannot_submit_receipt(client, admin_headers, phone_headers, car_headers, offline_owner):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    job = _create_job(client, admin_headers, "idem-7", [201], token=token).json()
    claim = client.post(
        "/api/v1/music/offline/work/claim",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "job_id": job["job_id"], "limit": 10},
    ).json()
    item = claim["items"][0]

    body = {
        "job_id": job["job_id"],
        "version": item["version"],
        "size_bytes": 100,
        "sha256": "a" * 64,
        "openable": True,
        "card_volume_id": "vol-1",
    }
    # Phone cannot submit a receipt (must be a real bound car).
    r = client.post(f"/api/v1/music/offline/items/{item['item_id']}/receipt", headers=phone_headers, json=body)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "NOT_CAR"


# ---------------------------------------------------------------------------
# Claim / heartbeat / receipt / revoke
# ---------------------------------------------------------------------------

def test_car_claim_heartbeat_receipt_saved(client, admin_headers, car_headers, offline_owner):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    job = _create_job(client, admin_headers, "idem-8", [301, 302], token=token).json()

    claim = client.post(
        "/api/v1/music/offline/work/claim",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "job_id": job["job_id"], "limit": 10},
    ).json()
    assert len(claim["items"]) == 2
    item = claim["items"][0]
    assert item["status"] == "claimed"

    # Heartbeat extends claim (version still matches).
    hb = client.post(
        f"/api/v1/music/offline/work/{item['item_id']}/heartbeat",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "version": item["version"]},
    )
    assert hb.status_code == 200

    # Receipt -> saved (single unified ready state).
    rec = client.post(
        f"/api/v1/music/offline/items/{item['item_id']}/receipt",
        headers=car_headers,
        json={
            "device_id": settings.DEFAULT_CAR_DEVICE_ID,
            "job_id": job["job_id"],
            "version": item["version"],
            "size_bytes": 987654,
            "sha256": "b" * 64,
            "openable": True,
            "card_volume_id": "vol-a",
            "format": "mp3",
        },
    )
    assert rec.status_code == 200
    saved = rec.json()
    assert saved["status"] == "saved"
    assert saved["size_bytes"] == 987654
    assert saved["card_volume_id"] == "vol-a"
    assert saved["format"] == "mp3"

    # Job counters reflect the saved item.
    summary = client.get(f"/api/v1/music/offline/jobs/{job['job_id']}", headers=admin_headers).json()
    assert summary["done"] == 1


def test_receipt_stale_version_rejected(client, admin_headers, car_headers, offline_owner):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    job = _create_job(client, admin_headers, "idem-9", [401], token=token).json()
    item = client.post(
        "/api/v1/music/offline/work/claim",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "job_id": job["job_id"], "limit": 10},
    ).json()["items"][0]

    body = {
        "job_id": job["job_id"],
        "version": item["version"] - 1,  # stale
        "size_bytes": 100,
        "sha256": "c" * 64,
        "openable": True,
        "card_volume_id": "vol-x",
    }
    r = client.post(f"/api/v1/music/offline/items/{item['item_id']}/receipt", headers=car_headers, json=body)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"


def test_revocation_sd_removal(client, admin_headers, car_headers, offline_owner):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    job = _create_job(client, admin_headers, "idem-10", [501], token=token).json()
    item = client.post(
        "/api/v1/music/offline/work/claim",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "job_id": job["job_id"], "limit": 10},
    ).json()["items"][0]

    client.post(
        f"/api/v1/music/offline/items/{item['item_id']}/receipt",
        headers=car_headers,
        json={
            "job_id": job["job_id"],
            "version": item["version"],
            "size_bytes": 100,
            "sha256": "d" * 64,
            "openable": True,
            "card_volume_id": "vol-a",
        },
    )
    # SD removal -> revoke (no longer "still saved").
    rev = client.post(
        f"/api/v1/music/offline/items/{item['item_id']}/revoke",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "version": item["version"] + 1, "reason": "CARD_REMOVED"},
    )
    assert rev.status_code == 200
    assert rev.json()["status"] == "revoked"
    assert rev.json()["error_code"] == "CARD_REMOVED"


def test_claim_timeout_reclaim(client, admin_headers, car_headers, offline_owner, monkeypatch):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    job = _create_job(client, admin_headers, "idem-11", [601], token=token).json()
    item = client.post(
        "/api/v1/music/offline/work/claim",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "job_id": job["job_id"], "limit": 10},
    ).json()["items"][0]

    # Advance the service clock past the claim timeout; a new claim reclaims it.
    real_now = svc._now
    monkeypatch.setattr(
        svc, "_now", lambda: real_now() + svc.timedelta(minutes=settings.OFFLINE_CLAIM_TIMEOUT_MINUTES + 1)
    )

    claim2 = client.post(
        "/api/v1/music/offline/work/claim",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "job_id": job["job_id"], "limit": 10},
    ).json()
    assert len(claim2["items"]) == 1
    assert claim2["items"][0]["item_id"] == item["item_id"]
    assert claim2["items"][0]["status"] == "claimed"


def test_pagination_revision_conflict(client, admin_headers, car_headers, offline_owner):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    job = _create_job(client, admin_headers, "idem-12", [701, 702, 703], token=token).json()

    page = client.get(f"/api/v1/music/offline/jobs/{job['job_id']}/items?limit=2", headers=admin_headers).json()
    rev = page["revision"]
    assert page["has_more"] is True

    # Mutate (car claims one item) -> job revision changes.
    client.post(
        "/api/v1/music/offline/work/claim",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "job_id": job["job_id"], "limit": 1},
    )
    # Pinning the old revision now conflicts (optimistic consistency, not immutable snapshots).
    r = client.get(f"/api/v1/music/offline/jobs/{job['job_id']}/items?limit=2&revision={rev}", headers=admin_headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "REVISION_CONFLICT"


# ---------------------------------------------------------------------------
# Manifest hygiene + owner isolation + persistence
# ---------------------------------------------------------------------------

def test_manifest_has_no_path_or_credential(client, admin_headers, car_headers, offline_owner):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    job = _create_job(client, admin_headers, "idem-13", [801], token=token).json()
    item = client.post(
        "/api/v1/music/offline/work/claim",
        headers=car_headers,
        json={"device_id": settings.DEFAULT_CAR_DEVICE_ID, "job_id": job["job_id"], "limit": 10},
    ).json()["items"][0]
    client.post(
        f"/api/v1/music/offline/items/{item['item_id']}/receipt",
        headers=car_headers,
        json={
            "job_id": job["job_id"],
            "version": item["version"],
            "size_bytes": 100,
            "sha256": "e" * 64,
            "openable": True,
            "card_volume_id": "vol-a",
        },
    )
    listing = client.get(f"/api/v1/music/offline/jobs/{job['job_id']}/items", headers=admin_headers).json()
    for it in listing["items"]:
        for forbidden in ("path", "uri", "url", "token", "cookie", "saf"):
            assert forbidden not in it, f"leaked {forbidden}: {it}"


def test_owner_isolation_at_service_level(client, admin_headers, offline_owner):
    db = AppSessionLocal()
    try:
        token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
        job_a, _ = svc.create_job(
            db,
            owner_key="owner-aaaaaaaa",
            account_id="acct-a",
            device_id="car_p3pro",
            idempotency_key="iso-1",
            song_ids=[901],
            quality="320k",
            network_epoch="epoch-a",
            wifi_confirm_token=token,
        )
        # Different owner must not see owner A's job.
        try:
            svc.get_job(db, job_a.job_id, owner_key="owner-bbbbbbbb")
            assert False, "owner B should not see owner A's job"
        except svc.OfflineConflict as e:
            assert e.code == "NOT_FOUND"

        jobs_b = svc.list_jobs(db, owner_key="owner-bbbbbbbb")
        assert all(j.owner_key == "owner-bbbbbbbb" for j in jobs_b)
    finally:
        db.close()


def test_persistence_across_restart(client, admin_headers, offline_owner):
    token = _confirm_network(client, admin_headers)["wifi_confirm_token"]
    job = _create_job(client, admin_headers, "idem-14", [1001, 1002], token=token).json()

    # Simulate app restart: re-run migrations on the same DB and re-read from a fresh session.
    from app.repositories.database import engine as app_engine

    run_db_migrations(app_engine)

    db = AppSessionLocal()
    try:
        persisted = db.query(OfflineJob).filter(OfflineJob.job_id == job["job_id"]).first()
        assert persisted is not None
        assert persisted.total == 2
        items = db.query(OfflineItem).filter(OfflineItem.job_id == job["job_id"]).all()
        assert len(items) == 2
    finally:
        db.close()
