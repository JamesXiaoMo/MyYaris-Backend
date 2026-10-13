import io
import pytest
from app.config import settings
from app.repositories.database import SessionLocal
from app.repositories.models import (
    Favorite,
    MusicCommand,
    User,
    UserDeviceBinding,
    UserSession,
)
from app.services.auth_service import hash_password, verify_password


@pytest.fixture
def clean_auth_db():
    from app.services.control_service import command_queue_manager
    command_queue_manager._queues.clear()
    db = SessionLocal()
    db.query(UserSession).delete()
    db.query(UserDeviceBinding).delete()
    db.query(Favorite).delete()
    db.query(MusicCommand).delete()
    db.query(User).delete()
    db.commit()
    yield db
    db.query(UserSession).delete()
    db.query(UserDeviceBinding).delete()
    db.query(Favorite).delete()
    db.query(MusicCommand).delete()
    db.query(User).delete()
    db.commit()
    command_queue_manager._queues.clear()
    db.close()


def test_argon2id_hashing_and_verification():
    pwd = "CorrectHorseBatteryStaple123"
    hashed = hash_password(pwd)
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, pwd) is True
    assert verify_password(hashed, "WrongPassword") is False


def test_bootstrap_first_admin(client, clean_auth_db):
    payload = {
        "username": "sysadmin",
        "password": "AdminPassword123!",
        "nickname": "Lead Admin",
    }
    # 1. First bootstrap succeeds
    resp = client.post("/api/v1/auth/bootstrap-admin", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "sysadmin"
    assert data["role"] == "admin"
    assert data["has_device_control"] is True
    assert "password" not in data
    assert "password_hash" not in data

    # 2. Second bootstrap attempt is rejected
    resp2 = client.post("/api/v1/auth/bootstrap-admin", json=payload)
    assert resp2.status_code == 403
    assert "Bootstrap is closed" in resp2.json()["detail"]


def test_user_registration_defaults_to_user_role(client, clean_auth_db):
    payload = {
        "username": "driver_alice",
        "password": "AliceSecretPassword123",
        "nickname": "Alice",
    }
    resp = client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "driver_alice"
    assert data["role"] == "user"
    assert data["has_device_control"] is False  # Zero vehicle control by default

    # Duplicate registration rejected
    resp_dup = client.post("/api/v1/auth/register", json=payload)
    assert resp_dup.status_code == 400
    assert "already registered" in resp_dup.json()["detail"]

    # Short password rejected by Pydantic validation
    resp_short = client.post(
        "/api/v1/auth/register",
        json={"username": "driver_bob", "password": "123"},
    )
    assert resp_short.status_code == 422


def test_login_success_sets_session_cookie_and_token(client, clean_auth_db):
    # Register user
    client.post(
        "/api/v1/auth/register",
        json={"username": "driver_alice", "password": "AliceSecretPassword123"},
    )

    # Login
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_alice", "password": "AliceSecretPassword123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == 200
    assert "session_id" in data
    assert data["user"]["username"] == "driver_alice"
    assert data["user"]["has_device_control"] is False

    # Check session cookie in response headers
    set_cookie = resp.headers.get("set-cookie", "")
    assert settings.SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower()


def test_login_failure_generic_message_and_lockout(client, clean_auth_db):
    # 1. Non-existent user returns generic 401
    resp_none = client.post(
        "/api/v1/auth/login",
        json={"username": "non_existent", "password": "any_password123"},
    )
    assert resp_none.status_code == 401
    assert resp_none.json()["detail"] == "Invalid username or password."

    # 2. Existing user with wrong password
    client.post(
        "/api/v1/auth/register",
        json={"username": "driver_bob", "password": "BobSecretPassword123"},
    )
    for _ in range(settings.MAX_FAILED_LOGIN_ATTEMPTS - 1):
        r = client.post(
            "/api/v1/auth/login",
            json={"username": "driver_bob", "password": "wrong_password"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid username or password."

    # 5th attempt triggers lockout
    client.post(
        "/api/v1/auth/login",
        json={"username": "driver_bob", "password": "wrong_password"},
    )

    # 6th attempt is locked out (429)
    resp_lock = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_bob", "password": "BobSecretPassword123"},
    )
    assert resp_lock.status_code == 429
    assert "temporarily locked" in resp_lock.json()["detail"]


def test_session_cookie_and_bearer_auth_on_me(client, clean_auth_db):
    client.post(
        "/api/v1/auth/register",
        json={"username": "driver_charlie", "password": "CharliePassword123"},
    )
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_charlie", "password": "CharliePassword123"},
    )
    session_id = login_resp.json()["session_id"]

    # 1. Access with Bearer token
    resp_bearer = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {session_id}"})
    assert resp_bearer.status_code == 200
    assert resp_bearer.json()["username"] == "driver_charlie"

    # 2. Access with Cookie
    resp_cookie = client.get("/api/v1/auth/me", cookies={settings.SESSION_COOKIE_NAME: session_id})
    assert resp_cookie.status_code == 200
    assert resp_cookie.json()["username"] == "driver_charlie"


def test_logout_revokes_session(client, clean_auth_db):
    client.post(
        "/api/v1/auth/register",
        json={"username": "driver_dan", "password": "DanPassword123!"},
    )
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_dan", "password": "DanPassword123!"},
    )
    session_id = login_resp.json()["session_id"]

    # Logout
    logout_resp = client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {session_id}"})
    assert logout_resp.status_code == 200

    # Subsequent /me call with revoked session fails
    resp_after = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {session_id}"})
    assert resp_after.status_code == 401


def test_change_password_and_other_session_revocation(client, clean_auth_db):
    client.post(
        "/api/v1/auth/register",
        json={"username": "driver_eve", "password": "EvePasswordOld123"},
    )
    # Login session 1
    s1 = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_eve", "password": "EvePasswordOld123"},
    ).json()["session_id"]

    # Login session 2
    s2 = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_eve", "password": "EvePasswordOld123"},
    ).json()["session_id"]

    # Change password from session 1
    resp_change = client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {s1}"},
        json={"old_password": "EvePasswordOld123", "new_password": "EvePasswordNew456!"},
    )
    assert resp_change.status_code == 200

    # Session 2 was revoked
    resp_s2 = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {s2}"})
    assert resp_s2.status_code == 401

    # Session 1 remains active
    resp_s1 = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {s1}"})
    assert resp_s1.status_code == 200

    # Login with new password works
    resp_new_login = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_eve", "password": "EvePasswordNew456!"},
    )
    assert resp_new_login.status_code == 200


def test_profile_update_and_ssrf_prevention(client, clean_auth_db):
    client.post(
        "/api/v1/auth/register",
        json={"username": "driver_frank", "password": "FrankPassword123!"},
    )
    session_id = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_frank", "password": "FrankPassword123!"},
    ).json()["session_id"]

    headers = {"Authorization": f"Bearer {session_id}"}

    # SSRF Attack 1: Loopback URL rejected
    r1 = client.patch("/api/v1/auth/profile", headers=headers, json={"avatar_url": "http://127.0.0.1/evil.png"})
    assert r1.status_code == 400
    assert "SSRF" in r1.json()["detail"]

    # SSRF Attack 2: Private network IP rejected
    r2 = client.patch("/api/v1/auth/profile", headers=headers, json={"avatar_url": "http://192.0.2.20/secret.jpg"})
    assert r2.status_code == 400

    # SSRF Attack 3: Localhost domain rejected
    r3 = client.patch("/api/v1/auth/profile", headers=headers, json={"avatar_url": "http://localhost:8000/avatar.jpg"})
    assert r3.status_code == 400

    # Valid external avatar URL allowed
    r4 = client.patch(
        "/api/v1/auth/profile",
        headers=headers,
        json={"nickname": "Frank Sinatra", "avatar_url": "https://example.com/avatar.jpg"},
    )
    assert r4.status_code == 200
    assert r4.json()["nickname"] == "Frank Sinatra"
    assert r4.json()["avatar_url"] == "https://example.com/avatar.jpg"


def test_avatar_upload_and_magic_bytes(client, clean_auth_db):
    client.post(
        "/api/v1/auth/register",
        json={"username": "driver_grace", "password": "GracePassword123!"},
    )
    session_id = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_grace", "password": "GracePassword123!"},
    ).json()["session_id"]
    headers = {"Authorization": f"Bearer {session_id}"}

    # 1. Invalid fake PNG (HTML masquerade)
    fake_png = io.BytesIO(b"<html><body>Not an image</body></html>")
    r_fake = client.post(
        "/api/v1/auth/avatar",
        headers=headers,
        files={"file": ("avatar.png", fake_png, "image/png")},
    )
    assert r_fake.status_code == 400
    assert "magic bytes" in r_fake.json()["detail"]

    # 2. Valid PNG with proper magic bytes
    valid_png = io.BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00")
    r_valid = client.post(
        "/api/v1/auth/avatar",
        headers=headers,
        files={"file": ("profile.png", valid_png, "image/png")},
    )
    assert r_valid.status_code == 200
    avatar_url = r_valid.json()["avatar_url"]
    assert avatar_url.startswith("/api/v1/auth/avatar/")

    # 3. Serve avatar image
    r_serve = client.get(avatar_url)
    assert r_serve.status_code == 200
    assert r_serve.headers["content-type"] == "image/png"

    # 4. Path traversal attempt rejected
    r_traversal = client.get("/api/v1/auth/avatar/../config.py")
    assert r_traversal.status_code in (400, 404)


def test_device_control_permission_and_admin_binding(client, clean_auth_db):
    # 1. Bootstrap Admin
    admin_session = client.post(
        "/api/v1/auth/bootstrap-admin",
        json={"username": "superadmin", "password": "AdminPassword123!"},
    ).json()

    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "superadmin", "password": "AdminPassword123!"},
    ).json()["session_id"]
    admin_headers = {"Authorization": f"Bearer {admin_login}"}

    # 2. Register normal user
    user_data = client.post(
        "/api/v1/auth/register",
        json={"username": "driver_helen", "password": "HelenPassword123!"},
    ).json()
    user_id = user_data["id"]

    user_session = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_helen", "password": "HelenPassword123!"},
    ).json()["session_id"]
    user_headers = {"Authorization": f"Bearer {user_session}"}

    # 3. Normal user attempts to dispatch vehicle music command -> 403 Forbidden
    cmd_payload = {"command_id": "cmd_test_001", "action": "pause", "device_id": "car_p3pro"}
    r_forbidden = client.post("/api/v1/music/control/command", headers=user_headers, json=cmd_payload)
    assert r_forbidden.status_code == 403
    assert "vehicle device control permission" in r_forbidden.json()["detail"]

    # 4. Admin grants device binding to user
    r_bind = client.post(
        f"/api/v1/auth/users/{user_id}/device-binding",
        headers=admin_headers,
        json={"device_id": "car_p3pro", "can_control": True},
    )
    assert r_bind.status_code == 200
    assert r_bind.json()["can_control"] is True

    # 5. Normal user now can send vehicle commands!
    r_allowed = client.post("/api/v1/music/control/command", headers=user_headers, json=cmd_payload)
    assert r_allowed.status_code == 200
    assert r_allowed.json()["status"] in ("queued", "sent", "dispatched", "ok")


def test_favorites_user_isolation(client, clean_auth_db):
    # 1. User A login
    client.post("/api/v1/auth/register", json={"username": "user_a", "password": "PasswordUserA123"})
    s_a = client.post("/api/v1/auth/login", json={"username": "user_a", "password": "PasswordUserA123"}).json()["session_id"]
    h_a = {"Authorization": f"Bearer {s_a}"}

    # 2. User B login
    client.post("/api/v1/auth/register", json={"username": "user_b", "password": "PasswordUserB123"})
    s_b = client.post("/api/v1/auth/login", json={"username": "user_b", "password": "PasswordUserB123"}).json()["session_id"]
    h_b = {"Authorization": f"Bearer {s_b}"}

    # User A creates favorite
    fav_a = client.post(
        "/api/v1/favorites",
        headers=h_a,
        json={"name": "Home A", "address": "Tokyo Station", "latitude": 35.6812, "longitude": 139.7671},
    ).json()

    # User B creates favorite
    fav_b = client.post(
        "/api/v1/favorites",
        headers=h_b,
        json={"name": "Home B", "address": "Shinjuku Station", "latitude": 35.6909, "longitude": 139.7003},
    ).json()

    # User A lists favorites: sees ONLY Home A
    list_a = client.get("/api/v1/favorites", headers=h_a).json()
    assert list_a["total"] == 1
    assert list_a["items"][0]["name"] == "Home A"

    # User B lists favorites: sees ONLY Home B
    list_b = client.get("/api/v1/favorites", headers=h_b).json()
    assert list_b["total"] == 1
    assert list_b["items"][0]["name"] == "Home B"

    # User A attempts to delete User B's favorite -> 404 (isolated)
    del_cross = client.delete(f"/api/v1/favorites/{fav_b['id']}", headers=h_a)
    assert del_cross.status_code == 404


def test_netease_account_protection(client, clean_auth_db):
    # Register normal user
    client.post("/api/v1/auth/register", json={"username": "normal_music_user", "password": "MusicPassword123!"})
    user_s = client.post(
        "/api/v1/auth/login",
        json={"username": "normal_music_user", "password": "MusicPassword123!"},
    ).json()["session_id"]
    user_headers = {"Authorization": f"Bearer {user_s}"}

    # Normal user trying to import cookie is rejected with 403
    r_cookie = client.post(
        "/api/v1/music/netease/login/cookie",
        headers=user_headers,
        json={"cookie": "MUSIC_U=test_cookie;"},
    )
    assert r_cookie.status_code == 403

    # Normal user checking login status gets only boolean logged_in, no profile details
    r_status = client.get("/api/v1/music/netease/login/status", headers=user_headers)
    assert r_status.status_code == 200
    data = r_status.json()
    assert "logged_in" in data
    assert data.get("user_id") is None
    assert data.get("nickname") is None


def test_legacy_phone_token_compatibility(client, phone_headers, clean_auth_db):
    # Calling favorites with legacy PHONE_TOKEN works
    fav_legacy = client.post(
        "/api/v1/favorites",
        headers=phone_headers,
        json={"name": "Legacy Shared Fav", "address": "Ginza", "latitude": 35.6719, "longitude": 139.7640},
    )
    assert fav_legacy.status_code == 201

    list_legacy = client.get("/api/v1/favorites", headers=phone_headers)
    assert list_legacy.status_code == 200
    assert list_legacy.json()["total"] >= 1
