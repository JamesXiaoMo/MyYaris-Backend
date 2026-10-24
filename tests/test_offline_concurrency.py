"""Independent SQLite connections and synchronized workers; fake receipts only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateColumn

from app.repositories.database import Base, setup_sqlite_pragmas
from app.repositories.migrations import run_db_migrations
from app.repositories.models import NetworkAuthorization, OfflineItem, OfflineJob, OfflineTrack
from app.services import offline_service as svc

OWNER, ACCOUNT, DEVICE, EPOCH = "owner-a", "account-a", "car-a", "epoch-a"


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fake.db'}", connect_args={"check_same_thread": False})
    setup_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


def authorize(factory, *, owner=OWNER, account=ACCOUNT, device=DEVICE, batches=100):
    with factory() as db:
        return svc.issue_network_authorization(db, owner_key=owner, account_id=account, device_id=device,
            network_epoch=EPOCH, batch_total=batches, expires_in_minutes=1000)[1]


def create(db, token, key, ids=(1,), *, owner=OWNER, account=ACCOUNT, device=DEVICE, quality="320k"):
    return svc.create_job(db, owner_key=owner, account_id=account, device_id=device,
        idempotency_key=key, song_ids=list(ids), quality=quality, network_epoch=EPOCH, wifi_confirm_token=token)[0].job_id


def job(factory, token, key, ids=(1,), **kwargs):
    with factory() as db:
        return create(db, token, key, ids, **kwargs)


def claiming(db, jid, *, owner=OWNER, device=DEVICE, limit=100):
    return [svc.claim_payload(i) for i in svc.claim_batch(db, owner_key=owner, device_id=device,
        job_id=jid, limit=limit, network_epoch=EPOCH)]


def claim(factory, jid, **kwargs):
    with factory() as db:
        return claiming(db, jid, **kwargs)


def load(db, payload):
    return svc.get_item(db, payload["item_id"], device_id=payload["device_id"], owner_key=payload["owner_key"])


def receipt(db, item, payload):
    return svc.submit_receipt(db, item, version=payload["version"], lease_token=payload["lease_token"],
        network_epoch=EPOCH, size_bytes=100, sha256="a" * 64, openable=True, card_volume_id="card-a", format="mp3")


def save(factory, payload):
    with factory() as db:
        return svc.item_payload(receipt(db, load(db, payload), payload))


def race(factory, operations):
    """Preload stale identity maps before releasing independent Sessions together."""
    barrier = Barrier(len(operations))
    def worker(operation):
        with factory() as db:
            action = operation(db)
            barrier.wait(timeout=10)
            try:
                return "ok", action()
            except svc.OfflineConflict as e:
                return "conflict", e.code
    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(worker, operations))


def counters(factory):
    with factory() as db:
        for j in db.query(OfflineJob).all():
            items = db.query(OfflineItem).filter_by(job_id=j.job_id).all()
            assert j.total == len(items)
            assert j.done == sum(i.status == "saved" for i in items)
            assert j.failed == sum(i.status in {"failed", "revoked"} for i in items)
            assert j.skipped == sum(i.status == "saved" and i.attempts == 0 for i in items)


def test_concurrent_claims_same_job(factory):
    token = authorize(factory)
    jid = job(factory, token, "many", range(1, 41))
    results = race(factory, [lambda db: lambda: claiming(db, jid, limit=10)] * 4)
    assert all(r[0] == "ok" for r in results), results
    ids = [i["item_id"] for _, batch in results for i in batch]
    assert len(ids) == len(set(ids)) == 40
    counters(factory)


def test_concurrent_claims_across_jobs(factory):
    token = authorize(factory)
    a, b = [job(factory, token, key, range(1, 21)) for key in ("a", "b")]
    results = race(factory, [lambda db: lambda: claiming(db, a), lambda db: lambda: claiming(db, b)])
    assert all(r[0] == "ok" for r in results), results
    ids = [i["song_id"] for _, batch in results for i in batch]
    assert len(ids) == len(set(ids)) == 20


def test_concurrent_create_last_batch(factory):
    token = authorize(factory, batches=1)
    results = race(factory, [lambda db: lambda: create(db, token, "a"), lambda db: lambda: create(db, token, "b")])
    assert sum(r[0] == "ok" for r in results) == 1, results
    assert ("conflict", "NETWORK_BATCH_EXHAUSTED") in results
    with factory() as db:
        assert db.query(OfflineJob).count() == 1
        assert db.query(NetworkAuthorization).one().batch_remaining == 0


def test_concurrent_idempotent_create(factory):
    token = authorize(factory, batches=1)
    results = race(factory, [lambda db: lambda: create(db, token, "same", [1, 1, 2])] * 3)
    assert all(r[0] == "ok" for r in results), results
    assert len({r[1] for r in results}) == 1
    with factory() as db:
        assert db.query(OfflineItem).count() == 2
        assert db.query(NetworkAuthorization).one().batch_remaining == 0


def test_receipt_cas_with_preloaded_sessions(factory):
    token = authorize(factory)
    payload = claim(factory, job(factory, token, "a"))[0]
    def operation(db):
        item = load(db, payload)
        return lambda: svc.item_payload(receipt(db, item, payload))
    results = race(factory, [operation] * 3)
    assert sum(r[0] == "ok" for r in results) == 1, results
    assert results.count(("conflict", "VERSION_CONFLICT")) == 2
    with factory() as db:
        assert db.query(OfflineTrack).count() == 1
    counters(factory)


def test_pause_vs_claim_is_serializable(factory):
    token = authorize(factory)
    jid = job(factory, token, "a")
    with factory() as db:
        revision = db.get(OfflineJob, jid).revision
    def pause(db):
        item = db.get(OfflineJob, jid)
        return lambda: svc.set_job_state(db, item, svc.PAUSED, revision=revision).state
    results = race(factory, [pause, lambda db: lambda: claiming(db, jid)])
    if results[0][0] == "conflict":
        assert results[0][1] == "REVISION_CONFLICT", results
        with factory() as db:
            item = db.get(OfflineJob, jid)
            svc.set_job_state(db, item, svc.PAUSED, revision=item.revision)
    with factory() as db:
        assert db.get(OfflineJob, jid).state == "paused"
        assert db.query(OfflineItem).filter_by(status="claimed").count() == 0
    counters(factory)


def test_heartbeat_vs_expired_reclaim(factory, monkeypatch):
    token = authorize(factory)
    jid = job(factory, token, "a")
    old = claim(factory, jid)[0]
    now = svc._now() + timedelta(minutes=31)
    monkeypatch.setattr(svc, "_now", lambda: now)
    def heartbeat(db):
        item = load(db, old)
        return lambda: svc.heartbeat(db, item, version=old["version"], lease_token=old["lease_token"], network_epoch=EPOCH).version
    results = race(factory, [heartbeat, lambda db: lambda: claiming(db, jid, limit=1)])
    assert results[0][0] == "conflict" and results[0][1] in {"LEASE_EXPIRED", "VERSION_CONFLICT"}, results
    assert results[1][0] == "ok" and len(results[1][1]) == 1
    fresh = results[1][1][0]
    assert fresh["lease_token"] != old["lease_token"]
    assert save(factory, fresh)["status"] == "saved"
    with pytest.raises(svc.OfflineConflict, match="VERSION_CONFLICT"):
        save(factory, old)


def test_revoke_vs_reverification_cannot_resurrect(factory):
    token = authorize(factory)
    a = job(factory, token, "a")
    save(factory, claim(factory, a)[0])
    b = job(factory, token, "b")
    def revoke(db):
        item = db.query(OfflineItem).filter_by(job_id=b).one()
        version = item.version
        return lambda: svc.revoke_item(db, item, version=version, reason="FILE_MISSING").status
    def verify(db):
        item = db.query(OfflineItem).filter_by(job_id=a).one()
        version = item.version
        return lambda: svc.submit_receipt(db, item, version=version, size_bytes=100, sha256="a" * 64,
            openable=True, card_volume_id="card-a", format="mp3").status
    results = race(factory, [revoke, verify])
    if results[0][0] == "conflict":
        assert results[0][1] == "VERSION_CONFLICT", results
        with factory() as db:
            item = db.query(OfflineItem).filter_by(job_id=b).one()
            svc.revoke_item(db, item, version=item.version, reason="FILE_MISSING")
    with factory() as db:
        assert db.query(OfflineTrack).one().status == "revoked"
        assert {i.status for i in db.query(OfflineItem).all()} == {"revoked"}
    counters(factory)


def test_inventory_restart_and_key_isolation(factory):
    token = authorize(factory)
    a = job(factory, token, "a")
    save(factory, claim(factory, a)[0])
    factory.kw["bind"].dispose()
    run_db_migrations(factory.kw["bind"])
    again = job(factory, token, "b")
    with factory() as db:
        assert db.get(OfflineJob, again).skipped == db.get(OfflineJob, again).done == 1
    for key, scope in [("account", {"account": "account-b"}), ("device", {"device": "car-b"}),
                       ("owner", {"owner": "owner-b", "account": "account-c"}), ("quality", {"quality": "lossless"})]:
        auth_scope = {k: v for k, v in scope.items() if k != "quality"}
        tok = authorize(factory, **auth_scope)
        jid = job(factory, tok, key, **scope)
        execution_scope = {k: v for k, v in scope.items() if k in {"owner", "device"}}
        assert len(claim(factory, jid, **execution_scope)) == 1
    with factory() as db:
        with pytest.raises(svc.OfflineConflict, match="NOT_FOUND"):
            svc.get_job(db, a, owner_key=OWNER, account_id="account-b")
        assert {j.account_id for j in svc.list_jobs(db, owner_key=OWNER, account_id="account-b")} == {"account-b"}
    counters(factory)


def test_network_token_scope_isolation(factory):
    token = authorize(factory)
    for scope in ({"owner": "owner-b"}, {"account": "account-b"}):
        with pytest.raises(svc.OfflineConflict, match="NETWORK_OWNER_MISMATCH"):
            job(factory, token, "bad", **scope)
    with factory() as db:
        assert db.query(OfflineJob).count() == 0
        assert db.query(NetworkAuthorization).one().batch_remaining == 100


def test_card_removal_revokes_other_account_owner_history(factory):
    token = authorize(factory)
    a = job(factory, token, "a")
    save(factory, claim(factory, a)[0])
    token_b = authorize(factory, owner="owner-b", account="account-b")
    b = job(factory, token_b, "b", owner="owner-b", account="account-b")
    save(factory, claim(factory, b, owner="owner-b")[0])
    with factory() as db:
        item = db.query(OfflineItem).filter_by(job_id=b).one()
        svc.revoke_item(db, item, version=item.version, reason="CARD_REMOVED")
    factory.kw["bind"].dispose()
    with factory() as db:
        assert {t.status for t in db.query(OfflineTrack).all()} == {"revoked"}
        assert {i.status for i in db.query(OfflineItem).all()} == {"revoked"}
    token = authorize(factory)
    c = job(factory, token, "c")
    assert claim(factory, c)[0]["work_kind"] == "download"
    counters(factory)


def test_rejected_baseline_migration_is_repeatable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'baseline.db'}")
    additions = {"skipped", "attempts", "lease_token", "work_kind", "device_generation", "inventory_version"}
    with engine.begin() as conn:
        for name in ("offline_jobs", "offline_items", "network_authorizations"):
            table = Base.metadata.tables[name]
            columns = [str(CreateColumn(c).compile(dialect=engine.dialect)) for c in table.columns if c.name not in additions]
            columns.append("PRIMARY KEY (" + list(table.primary_key.columns)[0].name + ")")
            conn.execute(text(f"CREATE TABLE {name} (" + ",".join(columns) + ")"))
        conn.execute(text("INSERT INTO offline_jobs (job_id,owner_key,device_id,idempotency_key,request_fingerprint,state,quality,"
            "total,done,failed,revision,created_at,updated_at) VALUES "
            "('old-job','old-owner','old-car','key','hash','running','320k',2,1,0,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        for sid, state in [(1, "saved"), (2, "claimed")]:
            conn.execute(text("INSERT INTO offline_items (item_id,job_id,owner_key,device_id,song_id,quality,status,version,created_at,updated_at) "
                "VALUES (:iid,'old-job','old-owner','old-car',:sid,'320k',:state,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
                {"iid": f"old-{sid}", "sid": sid, "state": state})
    def interrupt_upgrade(conn, cursor, statement, parameters, context, executemany):
        if "ALTER TABLE offline_items ADD COLUMN lease_token" in statement:
            raise RuntimeError("fake interrupted migration")
    event.listen(engine, "before_cursor_execute", interrupt_upgrade)
    try:
        with pytest.raises(RuntimeError, match="fake interrupted migration"):
            run_db_migrations(engine)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt_upgrade)
    # Even ALTER TABLE additions must roll back, so a retry cannot mistake a
    # partially upgraded DB for a complete upgrade and retain old leases.
    assert "skipped" not in {c["name"] for c in inspect(engine).get_columns("offline_jobs")}
    assert "attempts" not in {c["name"] for c in inspect(engine).get_columns("offline_items")}
    run_db_migrations(engine)
    assert {"lease_token", "inventory_version", "attempts"} <= {c["name"] for c in inspect(engine).get_columns("offline_items")}
    factory = sessionmaker(bind=engine)
    with factory() as db:
        assert {i.status for i in db.query(OfflineItem).all()} == {"revoked"}
        assert {i.account_id for i in db.query(OfflineItem).all()} == {"__legacy__"}
        row = db.get(OfflineJob, "old-job")
        assert row.state == "paused" and row.done == 0 and row.failed == 2
        revision = row.revision
    run_db_migrations(engine)
    with factory() as db:
        assert db.get(OfflineJob, "old-job").revision == revision
    engine.dispose()


def test_cancelled_job_does_not_inherit_later_receipt(factory):
    token = authorize(factory)
    a = job(factory, token, "a")
    b = job(factory, token, "b")
    with factory() as db:
        loaded = db.get(OfflineJob, b)
        svc.set_job_state(db, loaded, svc.CANCELLED, revision=loaded.revision)
    save(factory, claim(factory, a)[0])
    with factory() as db:
        assert db.get(OfflineJob, b).state == "cancelled"
        assert db.get(OfflineJob, b).done == 0
        assert db.query(OfflineItem).filter_by(job_id=b).one().status == "pending"
    counters(factory)


def test_pinned_page_rejects_stale_session_revision(factory):
    token = authorize(factory)
    jid = job(factory, token, "a", range(1, 4))
    with factory() as old_session:
        loaded = svc.get_job(old_session, jid, owner_key=OWNER, account_id=ACCOUNT)
        old_revision = loaded.revision
        claim(factory, jid)
        with pytest.raises(svc.OfflineConflict, match="REVISION_CONFLICT"):
            svc.list_items(old_session, job=loaded, offset=0, limit=2, revision=old_revision)


def test_card_removal_does_not_invalidate_other_vehicle(factory):
    token = authorize(factory)
    a = job(factory, token, "a")
    save(factory, claim(factory, a)[0])
    token_b = authorize(factory, device="car-b")
    b = job(factory, token_b, "b", device="car-b")
    save(factory, claim(factory, b, device="car-b")[0])
    with factory() as db:
        item = db.query(OfflineItem).filter_by(job_id=a).one()
        svc.revoke_item(db, item, version=item.version, reason="CARD_REMOVED")
    with factory() as db:
        assert db.get(OfflineJob, b).done == 1
        assert db.query(OfflineTrack).filter_by(device_id="car-b").one().status == "saved"
    counters(factory)


def test_receipt_inventory_failure_rolls_back_entire_unit(factory, monkeypatch):
    token = authorize(factory)
    jid = job(factory, token, "atomic")
    payload = claim(factory, jid)[0]
    original = svc._save_track
    def interrupted(*args, **kwargs):
        raise RuntimeError("fake storage failure")
    monkeypatch.setattr(svc, "_save_track", interrupted)
    with pytest.raises(RuntimeError, match="fake storage failure"):
        save(factory, payload)
    with factory() as db:
        item = load(db, payload)
        assert item.status == "claimed" and item.version == payload["version"]
        assert db.query(OfflineTrack).count() == 0
        assert db.get(OfflineJob, jid).done == 0
    monkeypatch.setattr(svc, "_save_track", original)
    assert save(factory, payload)["status"] == "saved"
    counters(factory)


def test_confirmation_rotation_vs_claim_cannot_inherit_new_grant(factory):
    token = authorize(factory)
    jid = job(factory, token, "rotate")
    def rotate(db):
        return lambda: svc.issue_network_authorization(db, owner_key=OWNER, account_id=ACCOUNT,
            device_id=DEVICE, network_epoch=EPOCH, batch_total=1, expires_in_minutes=60)[1]
    results = race(factory, [rotate, lambda db: lambda: claiming(db, jid)])
    assert results[0][0] == "ok", results
    if results[1][0] == "ok":
        item = results[1][1][0]
        with factory() as db:
            with pytest.raises(svc.OfflineConflict, match="NETWORK_TOKEN_MISMATCH"):
                svc.heartbeat(db, load(db, item), version=item["version"],
                    lease_token=item["lease_token"], network_epoch=EPOCH)
    else:
        assert results[1] == ("conflict", "NETWORK_TOKEN_MISMATCH"), results
    with factory() as db:
        assert db.get(OfflineJob, jid).state == "paused"
        assert db.query(OfflineItem).filter_by(status="claimed").count() == 0
    counters(factory)
