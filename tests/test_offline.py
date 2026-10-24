"""Fake-only API regression for SD download bookkeeping; no upstream or vehicle I/O."""
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.repositories.models import NetworkAuthorization, OfflineDevice, OfflineItem, OfflineJob, OfflineTrack
from app.services import offline_service as svc
from app.services.favorite_catalog import CatalogSnapshot

BASE = "/api/v1/music/offline"
DEVICE = settings.DEFAULT_CAR_DEVICE_ID
OWNER = "o" * 16


@pytest.fixture
def offline_owner(monkeypatch, db_session):
    for model in (OfflineItem, OfflineJob, OfflineTrack, NetworkAuthorization, OfflineDevice):
        db_session.query(model).delete()
    db_session.commit()
    from app.services.netease_service import favorite_catalog
    state = {"snapshot": CatalogSnapshot(revision=1, etag="e" * 16, owner_key=OWNER, playlist_id=12345,
        name="liked", cover_url=None, tracks=(), skipped_ids=(), updated_at=0.0)}

    async def view(revision=None, **kwargs):
        if state.get("error"):
            raise PermissionError("fake session unavailable")
        if revision is not None and revision != state["snapshot"].revision:
            raise ValueError("fake revision expired")
        return state["snapshot"], {}

    monkeypatch.setattr(favorite_catalog, "view", view)
    # Poison peek: the offline API must never trust it as identity proof.
    monkeypatch.setattr(favorite_catalog, "peek", lambda: None)
    return state


def confirm(client, headers, epoch="epoch-a", **kwargs):
    r = client.post(BASE + "/network", headers=headers, json={"device_id": DEVICE,
        "network_epoch": epoch, "unlimited_wifi_confirmed": True, "batch_total": 20, **kwargs})
    assert r.status_code == 200, r.text
    return r.json()["wifi_confirm_token"]


def create(client, headers, state, ids=(101, 102), *, token=None, epoch="epoch-a", idem="job", **kwargs):
    state["snapshot"] = replace(state["snapshot"], tracks=tuple({"id": i} for i in ids), skipped_ids=())
    if token is None:
        token = confirm(client, headers, epoch)
    return client.post(BASE + "/jobs", headers=headers, json={"idempotency_key": idem,
        "device_id": DEVICE, "song_ids": list(ids), "favorite_revision": 1,
        "network_epoch": epoch, "wifi_confirm_token": token, **kwargs})


def claim(client, headers, job, **kwargs):
    return client.post(BASE + "/work/claim", headers=headers, json={"job_id": job["job_id"],
        "device_id": DEVICE, "network_epoch": "epoch-a", **kwargs})


def receipt_body(item, **kwargs):
    return {"job_id": item["job_id"], "version": item["version"], "lease_token": item["lease_token"],
        "network_epoch": "epoch-a", "size_bytes": 100, "sha256": "a" * 64,
        "card_volume_id": "card-a", "format": "mp3", "openable": True, **kwargs}


def save(client, headers, item, **kwargs):
    return client.post(BASE + f"/items/{item['item_id']}/receipt", headers=headers, json=receipt_body(item, **kwargs))


def lease_body(item, **kwargs):
    return {"version": item["version"], "lease_token": item["lease_token"], "network_epoch": "epoch-a", **kwargs}


def summary(client, headers, job):
    return client.get(BASE + f"/jobs/{job['job_id']}", headers=headers).json()


def code(response, status, error):
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == error


def test_explicit_confirmation_and_full_favorites(client, admin_headers, offline_owner):
    for data in ({}, {"unlimited_wifi_confirmed": False}):
        r = client.post(BASE + "/network", headers=admin_headers, json={"network_epoch": "epoch-a", **data})
        assert r.status_code == 422
    token = confirm(client, admin_headers)
    offline_owner["snapshot"] = replace(offline_owner["snapshot"], tracks=({"id": 1}, {"id": 2}), skipped_ids=(3,))
    body = {"idempotency_key": "full", "favorite_revision": 1, "network_epoch": "epoch-a", "wifi_confirm_token": token}
    code(client.post(BASE + "/jobs", headers=admin_headers, json={**body, "song_ids": [1]}), 409, "INCOMPLETE_FAVORITES")
    r = client.post(BASE + "/jobs", headers=admin_headers, json=body)
    assert r.status_code == 200 and r.json()["total"] == 3
    code(client.post(BASE + "/jobs", headers=admin_headers, json={**body, "favorite_revision": 2}), 409, "FAVORITE_REVISION_CHANGED")


def test_idempotency_and_atomic_batch_consumption(client, admin_headers, offline_owner, db_session):
    token = confirm(client, admin_headers, batch_total=1)
    a = create(client, admin_headers, offline_owner, token=token).json()
    b = create(client, admin_headers, offline_owner, token=token).json()
    assert a["job_id"] == b["job_id"]
    code(create(client, admin_headers, offline_owner, ids=[9], token=token), 409, "IDEMPOTENCY_CONFLICT")
    code(create(client, admin_headers, offline_owner, ids=[9], token=token, idem="other"), 403, "NETWORK_BATCH_EXHAUSTED")
    assert db_session.query(NetworkAuthorization).filter_by(device_id=DEVICE, network_epoch="epoch-a").one().batch_remaining == 0


@pytest.mark.parametrize("bad", [{"network_epoch": "epoch-b"}, {"wifi_confirm_token": "wrong"}])
def test_network_creation_rejects_wrong_confirmation(client, admin_headers, offline_owner, bad):
    token = confirm(client, admin_headers)
    body = {"idempotency_key": "wrong", "favorite_revision": 1, "network_epoch": "epoch-a", "wifi_confirm_token": token, **bad}
    offline_owner["snapshot"] = replace(offline_owner["snapshot"], tracks=({"id": 1},))
    assert client.post(BASE + "/jobs", headers=admin_headers, json=body).status_code == 403


def test_current_owner_required_for_all_reads_and_car_writes(client, admin_headers, car_headers, offline_owner):
    job = create(client, admin_headers, offline_owner).json()
    item = claim(client, car_headers, job).json()["items"][0]
    offline_owner["snapshot"] = replace(offline_owner["snapshot"], owner_key="b" * 16)
    assert client.get(BASE + "/jobs", headers=admin_headers).json()["jobs"] == []
    code(claim(client, car_headers, job), 404, "NOT_FOUND")
    for operation, body in [("heartbeat", lease_body(item)), ("fail", lease_body(item, error_code="DOWNLOAD_FAILED"))]:
        code(client.post(BASE + f"/work/{item['item_id']}/{operation}", headers=car_headers, json=body), 404, "NOT_FOUND")
    code(save(client, car_headers, item), 404, "NOT_FOUND")
    code(client.post(BASE + f"/items/{item['item_id']}/revoke", headers=car_headers,
                     json={"version": item["version"]}), 404, "NOT_FOUND")
    offline_owner["error"] = True
    code(client.get(BASE + "/jobs", headers=admin_headers), 409, "OWNER_NOT_VERIFIED")
    code(claim(client, car_headers, job), 409, "OWNER_NOT_VERIFIED")


def test_device_spoof_rejected_and_alias_accepted(client, admin_headers, car_headers, offline_owner):
    job = create(client, admin_headers, offline_owner).json()
    code(claim(client, car_headers, job, device_id="different-car"), 403, "DEVICE_MISMATCH")
    r = claim(client, car_headers, job, device_id="ottocast_p3_pro")
    assert r.status_code == 200 and len(r.json()["items"]) == 2
    item = r.json()["items"][0]
    code(save(client, car_headers, item, device_id="different-car"), 403, "DEVICE_MISMATCH")


def test_role_boundary(client, admin_headers, car_headers, phone_headers, offline_owner):
    job = create(client, admin_headers, offline_owner).json()
    item = claim(client, car_headers, job).json()["items"][0]
    for headers in (admin_headers, phone_headers):
        code(claim(client, headers, job), 403, "NOT_CAR")
        code(save(client, headers, item), 403, "NOT_CAR")
        code(client.post(BASE + f"/work/{item['item_id']}/heartbeat", headers=headers, json=lease_body(item)), 403, "NOT_CAR")
        code(client.post(BASE + f"/work/{item['item_id']}/fail", headers=headers,
                         json=lease_body(item, error_code="DOWNLOAD_FAILED")), 403, "NOT_CAR")
        code(client.post(BASE + f"/items/{item['item_id']}/revoke", headers=headers, json={"version": item["version"]}), 403, "NOT_CAR")
    for headers in (car_headers, phone_headers):
        assert create(client, headers, offline_owner, token="fake").status_code == 403


def test_normal_registered_user_cannot_read_or_create(client, offline_owner):
    client.post("/api/v1/auth/register", json={"username": "offline_user", "password": "FakeDriverPass123!"})
    login = client.post("/api/v1/auth/login", json={"username": "offline_user", "password": "FakeDriverPass123!"}).json()
    headers = {"Authorization": f"Bearer {login['session_id']}"}
    assert client.get(BASE + "/jobs", headers=headers).status_code == 403
    assert client.post(BASE + "/network", headers=headers, json={"network_epoch": "e", "unlimited_wifi_confirmed": True}).status_code == 403


def test_claim_requires_epoch_and_job(client, admin_headers, car_headers, offline_owner):
    create(client, admin_headers, offline_owner)
    for body in ({}, {"job_id": "j"}, {"network_epoch": "epoch-a"}):
        assert client.post(BASE + "/work/claim", headers=car_headers, json=body).status_code == 422


def test_heartbeat_changes_version_and_receipt_requires_current_lease(client, admin_headers, car_headers, offline_owner):
    job = create(client, admin_headers, offline_owner).json()
    item = claim(client, car_headers, job).json()["items"][0]
    assert item["owner_key"] == OWNER and item["work_kind"] == "download" and item["lease_expires_at"]
    hb = client.post(BASE + f"/work/{item['item_id']}/heartbeat", headers=car_headers, json=lease_body(item))
    assert hb.status_code == 200 and hb.json()["version"] == item["version"] + 1
    code(save(client, car_headers, item), 409, "VERSION_CONFLICT")
    item["version"] = hb.json()["version"]
    code(save(client, car_headers, item, lease_token="f" * 48), 409, "LEASE_MISMATCH")
    r = save(client, car_headers, item)
    assert r.status_code == 200 and r.json()["status"] == "saved"
    assert summary(client, admin_headers, job)["done"] == 1
    code(save(client, car_headers, item), 409, "VERSION_CONFLICT")


@pytest.mark.parametrize("state", ["pause", "cancel"])
def test_pause_cancel_invalidate_inflight_and_block_execution(client, admin_headers, car_headers, offline_owner, state):
    job = create(client, admin_headers, offline_owner).json()
    item = claim(client, car_headers, job).json()["items"][0]
    rev = summary(client, admin_headers, job)["revision"]
    r = client.post(BASE + f"/jobs/{job['job_id']}/{state}", headers=admin_headers, json={"revision": rev})
    assert r.status_code == 200
    code(claim(client, car_headers, job), 409, "JOB_NOT_RUNNING")
    code(save(client, car_headers, item), 409, "VERSION_CONFLICT")
    token = confirm(client, admin_headers)
    resume = client.post(BASE + f"/jobs/{job['job_id']}/resume", headers=admin_headers,
        json={"revision": r.json()["revision"], "network_epoch": "epoch-a", "wifi_confirm_token": token})
    if state == "cancel":
        code(resume, 409, "BAD_STATE")
    else:
        assert resume.status_code == 200
        assert len(claim(client, car_headers, job).json()["items"]) == 2


@pytest.mark.parametrize("gate", ["epoch", "expiry", "rotation", "owner"])
def test_gate_denial_persists_pause_and_never_auto_resumes(client, admin_headers, car_headers, offline_owner, db_session, gate):
    token = confirm(client, admin_headers)
    job = create(client, admin_headers, offline_owner, token=token).json()
    item = claim(client, car_headers, job).json()["items"][0]
    epoch = "epoch-a"
    if gate == "epoch":
        epoch = "epoch-b"
    elif gate == "expiry":
        na = db_session.query(NetworkAuthorization).filter_by(device_id=DEVICE, network_epoch=epoch).one()
        na.expires_at = svc._now() - timedelta(seconds=1)
        db_session.commit()
    elif gate == "rotation":
        confirm(client, admin_headers)
    else:
        na = db_session.query(NetworkAuthorization).filter_by(device_id=DEVICE, network_epoch=epoch).one()
        na.owner_key = "other-owner"
        db_session.commit()
    r = client.post(BASE + f"/work/{item['item_id']}/heartbeat", headers=car_headers,
                    json=lease_body(item, network_epoch=epoch))
    assert r.status_code == 403
    assert summary(client, admin_headers, job)["state"] == "paused"
    code(claim(client, car_headers, job), 409, "JOB_NOT_RUNNING")
    token = confirm(client, admin_headers, epoch=epoch)
    current = summary(client, admin_headers, job)
    resumed = client.post(BASE + f"/jobs/{job['job_id']}/resume", headers=admin_headers,
        json={"revision": current["revision"], "network_epoch": epoch, "wifi_confirm_token": token})
    assert resumed.status_code == 200
    assert len(claim(client, car_headers, job, network_epoch=epoch).json()["items"]) == 2


def test_job_state_revision_cas(client, admin_headers, car_headers, offline_owner):
    job = create(client, admin_headers, offline_owner).json()
    claim(client, car_headers, job)
    code(client.post(BASE + f"/jobs/{job['job_id']}/pause", headers=admin_headers, json={"revision": job["revision"]}), 409, "REVISION_CONFLICT")


def test_cross_job_dedup_durable_and_revoke_all_old_references(client, admin_headers, car_headers, offline_owner, db_session):
    token = confirm(client, admin_headers)
    a = create(client, admin_headers, offline_owner, ids=[201], token=token, idem="a").json()
    b = create(client, admin_headers, offline_owner, ids=[201], token=token, idem="b").json()
    item = claim(client, car_headers, a).json()["items"][0]
    assert claim(client, car_headers, b).json()["items"] == []
    assert save(client, car_headers, item).status_code == 200
    c = create(client, admin_headers, offline_owner, ids=[201], token=token, idem="c").json()
    assert c["total"] == c["done"] == c["skipped"] == 1 and c["state"] == "done"
    for job in (a, b, c):
        assert summary(client, admin_headers, job)["done"] == 1
    listing = client.get(BASE + f"/jobs/{c['job_id']}/items", headers=admin_headers).json()
    saved = listing["items"][0]
    rev = client.post(BASE + f"/items/{saved['item_id']}/revoke", headers=car_headers,
        json={"version": saved["version"], "reason": "FILE_MISSING"})
    assert rev.status_code == 200
    db_session.expire_all()
    assert db_session.query(OfflineTrack).filter_by(owner_key=OWNER, song_id=201).one().status == "revoked"
    for job in (a, b, c):
        current = summary(client, admin_headers, job)
        assert current["done"] == 0 and current["failed"] == 1
    code(save(client, car_headers, item), 409, "VERSION_CONFLICT")
    d = create(client, admin_headers, offline_owner, ids=[201], token=token, idem="d").json()
    assert claim(client, car_headers, d).json()["items"][0]["work_kind"] == "download"


def test_stale_inventory_requires_local_verify_not_download(client, admin_headers, car_headers, offline_owner, monkeypatch):
    token = confirm(client, admin_headers)
    a = create(client, admin_headers, offline_owner, ids=[301], token=token, idem="a").json()
    item = claim(client, car_headers, a).json()["items"][0]
    saved = save(client, car_headers, item).json()
    now = svc._now()
    monkeypatch.setattr(svc, "_now", lambda: now + timedelta(seconds=settings.OFFLINE_RECHECK_SECONDS + 1))
    token = confirm(client, admin_headers)
    assert client.get(BASE + f"/jobs/{a['job_id']}/items", headers=admin_headers).json()["items"][0]["stale"] is True
    b = create(client, admin_headers, offline_owner, ids=[301], token=token, idem="b").json()
    verify = claim(client, car_headers, b).json()["items"][0]
    assert verify["work_kind"] == "verify" and verify["sha256"] == "a" * 64
    code(save(client, car_headers, verify, sha256="b" * 64), 409, "RECEIPT_MISMATCH")
    assert save(client, car_headers, verify).json()["stale"] is False
    # Exact saved file can be reverified without granting a network/download lease.
    listed = client.get(BASE + f"/jobs/{a['job_id']}/items", headers=admin_headers).json()["items"][0]
    body = receipt_body(item, version=listed["version"], lease_token=None, network_epoch=None)
    assert client.post(BASE + f"/items/{listed['item_id']}/receipt", headers=car_headers, json=body).status_code == 200


def test_not_openable_globally_revokes_inventory(client, admin_headers, car_headers, offline_owner):
    token = confirm(client, admin_headers)
    a = create(client, admin_headers, offline_owner, ids=[401], token=token, idem="a").json()
    item = claim(client, car_headers, a).json()["items"][0]
    saved = save(client, car_headers, item).json()
    b = create(client, admin_headers, offline_owner, ids=[401], token=token, idem="b").json()
    body = receipt_body(item, version=saved["version"], lease_token=None, network_epoch=None, openable=False)
    r = client.post(BASE + f"/items/{item['item_id']}/receipt", headers=car_headers, json=body)
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert summary(client, admin_headers, b)["done"] == 0


def test_card_removal_fences_all_jobs_songs_and_late_receipts(client, admin_headers, car_headers, offline_owner):
    token = confirm(client, admin_headers)
    job = create(client, admin_headers, offline_owner, token=token).json()
    items = claim(client, car_headers, job).json()["items"]
    saved = save(client, car_headers, items[0]).json()
    r = client.post(BASE + f"/items/{saved['item_id']}/revoke", headers=car_headers,
        json={"version": saved["version"], "reason": "CARD_REMOVED"})
    assert r.status_code == 200
    code(save(client, car_headers, items[1]), 409, "VERSION_CONFLICT")
    current = summary(client, admin_headers, job)
    assert current["done"] == 0 and current["failed"] == 2


def test_card_switch_invalidates_old_work(client, admin_headers, car_headers, offline_owner):
    token = confirm(client, admin_headers)
    job = create(client, admin_headers, offline_owner, ids=[501, 502, 503], token=token).json()
    items = claim(client, car_headers, job).json()["items"]
    assert save(client, car_headers, items[0]).status_code == 200
    assert save(client, car_headers, items[1], card_volume_id="card-b").status_code == 200
    code(save(client, car_headers, items[2]), 409, "VERSION_CONFLICT")
    current = summary(client, admin_headers, job)
    assert current["done"] == 1 and current["failed"] == 2


def test_bounded_failures_and_no_raw_diagnostics(client, admin_headers, car_headers, offline_owner):
    job = create(client, admin_headers, offline_owner, ids=[601]).json()
    for attempt in range(settings.OFFLINE_MAX_CLAIM_ATTEMPTS):
        item = claim(client, car_headers, job).json()["items"][0]
        assert item["attempts"] == attempt + 1
        r = client.post(BASE + f"/work/{item['item_id']}/fail", headers=car_headers,
            json=lease_body(item, error_code="DOWNLOAD_FAILED"))
        assert r.status_code == 200
        assert r.json()["status"] == ("failed" if attempt + 1 == settings.OFFLINE_MAX_CLAIM_ATTEMPTS else "pending")
    assert summary(client, admin_headers, job)["failed"] == 1
    code(claim(client, car_headers, job), 409, "JOB_NOT_RUNNING")


def test_expired_lease_rejected_and_retries_bounded(client, admin_headers, car_headers, offline_owner, monkeypatch):
    token = confirm(client, admin_headers, expires_in_minutes=1000)
    job = create(client, admin_headers, offline_owner, ids=[701], token=token).json()
    now = svc._now()
    for attempt in range(settings.OFFLINE_MAX_CLAIM_ATTEMPTS):
        item = claim(client, car_headers, job).json()["items"][0]
        now += timedelta(minutes=settings.OFFLINE_CLAIM_TIMEOUT_MINUTES + 1)
        monkeypatch.setattr(svc, "_now", lambda: now)
        code(save(client, car_headers, item), 409, "LEASE_EXPIRED")
        code(client.post(BASE + f"/work/{item['item_id']}/heartbeat", headers=car_headers, json=lease_body(item)), 409, "LEASE_EXPIRED")
    r = claim(client, car_headers, job)
    assert r.status_code == 200 and r.json()["items"] == []
    current = summary(client, admin_headers, job)
    assert current["failed"] == 1 and current["state"] == "done"


def test_revision_conflict_after_heartbeat_and_read_hygiene(client, admin_headers, car_headers, offline_owner):
    job = create(client, admin_headers, offline_owner).json()
    item = claim(client, car_headers, job).json()["items"][0]
    page = client.get(BASE + f"/jobs/{job['job_id']}/items?limit=1", headers=admin_headers).json()
    assert page["has_more"]
    hb = client.post(BASE + f"/work/{item['item_id']}/heartbeat", headers=car_headers, json=lease_body(item))
    assert hb.status_code == 200
    code(client.get(BASE + f"/jobs/{job['job_id']}/items?revision={page['revision']}", headers=admin_headers), 409, "REVISION_CONFLICT")
    for row in page["items"]:
        assert not set(row) & {"path", "uri", "url", "token", "cookie", "lease_token", "error_detail"}


@pytest.mark.parametrize("bad", [{"size_bytes": 0}, {"sha256": "z" * 64}, {"sha256": "a" * 16},
    {"card_volume_id": "content://fake/card"}, {"format": "exe"}, {"owner_key": "spoof"}])
def test_receipt_rejects_incomplete_or_private_fields(client, admin_headers, car_headers, offline_owner, bad):
    job = create(client, admin_headers, offline_owner).json()
    item = claim(client, car_headers, job).json()["items"][0]
    rejected = save(client, car_headers, item, **bad)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "INVALID_OFFLINE_REQUEST"
    assert "input" not in rejected.text and "content://" not in rejected.text


def test_cookie_writes_require_existing_csrf_guard(client, admin_headers, offline_owner):
    from app.main import app
    from app.api.auth import get_current_user
    from app.schemas.auth import AuthenticatedUser
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(identity="fake-admin", role="admin", auth_via_cookie=True)
    try:
        r = client.post(BASE + "/network", json={"network_epoch": "e", "unlimited_wifi_confirmed": True})
        assert r.status_code == 403 and "CSRF" in r.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_dynamic_admin_account_isolation_at_api(client, admin_headers, phone_headers, offline_owner):
    from app.main import app
    from app.api.auth import get_current_user
    from app.schemas.auth import AuthenticatedUser
    current = {"id": "account-a"}
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        identity="fake-admin", role="admin", user_id=current["id"], is_legacy_token=False)
    try:
        token = confirm(client, admin_headers)
        a = create(client, admin_headers, offline_owner, ids=[901], token=token).json()
        current["id"] = "account-b"
        assert client.get(BASE + "/jobs", headers=admin_headers).json()["jobs"] == []
        code(client.get(BASE + f"/jobs/{a['job_id']}", headers=admin_headers), 404, "NOT_FOUND")
        code(create(client, admin_headers, offline_owner, ids=[901], token=token, idem="b"), 403, "NETWORK_OWNER_MISMATCH")
        b = create(client, admin_headers, offline_owner, ids=[901], idem="b").json()
        assert b["done"] == 0
        current["id"] = "account-a"
        assert {j["job_id"] for j in client.get(BASE + "/jobs", headers=admin_headers).json()["jobs"]} == {a["job_id"]}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    # The legacy phone has the legacy scope, never another admin's account.
    assert client.get(BASE + "/jobs", headers=phone_headers).json()["jobs"] == []


def test_network_lost_requires_explicit_resume_and_keeps_missing_work(client, admin_headers, car_headers, offline_owner):
    job = create(client, admin_headers, offline_owner).json()
    items = claim(client, car_headers, job).json()["items"]
    r = client.post(BASE + f"/work/{items[0]['item_id']}/fail", headers=car_headers,
        json=lease_body(items[0], error_code="NETWORK_LOST"))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert summary(client, admin_headers, job)["state"] == "paused"
    code(save(client, car_headers, items[1]), 409, "VERSION_CONFLICT")
    token = confirm(client, admin_headers)
    current = summary(client, admin_headers, job)
    r = client.post(BASE + f"/jobs/{job['job_id']}/resume", headers=admin_headers,
        json={"revision": current["revision"], "network_epoch": "epoch-a", "wifi_confirm_token": token})
    assert r.status_code == 200
    assert len(claim(client, car_headers, job).json()["items"]) == 2


def test_verify_missing_file_revokes_instead_of_implicit_download(client, admin_headers, car_headers, offline_owner, monkeypatch):
    token = confirm(client, admin_headers)
    a = create(client, admin_headers, offline_owner, ids=[1001], token=token, idem="a").json()
    assert save(client, car_headers, claim(client, car_headers, a).json()["items"][0]).status_code == 200
    now = svc._now() + timedelta(seconds=settings.OFFLINE_RECHECK_SECONDS + 1)
    monkeypatch.setattr(svc, "_now", lambda: now)
    token = confirm(client, admin_headers)
    b = create(client, admin_headers, offline_owner, ids=[1001], token=token, idem="b").json()
    item = claim(client, car_headers, b).json()["items"][0]
    assert item["work_kind"] == "verify"
    r = client.post(BASE + f"/work/{item['item_id']}/fail", headers=car_headers,
        json=lease_body(item, error_code="FILE_MISSING"))
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert summary(client, admin_headers, a)["done"] == 0
    code(claim(client, car_headers, b), 409, "JOB_NOT_RUNNING")


def test_claim_lease_cannot_outlive_network_confirmation(client, admin_headers, car_headers, offline_owner):
    from datetime import datetime
    token = confirm(client, admin_headers, expires_in_minutes=1)
    job = create(client, admin_headers, offline_owner, token=token).json()
    item = claim(client, car_headers, job).json()["items"][0]
    assert 0 < (datetime.fromisoformat(item["lease_expires_at"]) - svc._now()).total_seconds() <= 60
    code(save(client, car_headers, item, job_id="another-job"), 409, "JOB_MISMATCH")
    r = client.post(BASE + f"/work/{item['item_id']}/fail", headers=car_headers,
        json=lease_body(item, error_code="DOWNLOAD_FAILED", error_detail="fake-private-diagnostic"))
    assert r.status_code == 422


def test_complete_playlist_larger_than_one_work_batch(client, admin_headers, car_headers, offline_owner):
    job = create(client, admin_headers, offline_owner, ids=range(1, 252)).json()
    assert job["total"] == 251
    first = claim(client, car_headers, job, limit=100).json()["items"]
    second = claim(client, car_headers, job, limit=100).json()["items"]
    third = claim(client, car_headers, job, limit=100).json()["items"]
    assert [len(first), len(second), len(third)] == [100, 100, 51]
    assert len({i["song_id"] for i in first + second + third}) == 251
    for offset, expected in [(0, 100), (100, 100), (200, 51)]:
        page = client.get(BASE + f"/jobs/{job['job_id']}/items?offset={offset}&limit=100", headers=admin_headers).json()
        assert page["total"] == 251 and len(page["items"]) == expected
