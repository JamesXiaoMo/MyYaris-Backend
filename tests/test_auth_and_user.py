import io
from PIL import Image
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


def test_bootstrap_admin_requires_admin_token_and_subsequent_lockout(client, admin_headers, phone_headers, clean_auth_db):
    payload = {
        "username": "sysadmin",
        "password": "AdminPassword123!",
        "nickname": "Lead Admin",
    }
    # 1. Unauthenticated request without ADMIN_TOKEN is rejected (401)
    resp_no_auth = client.post("/api/v1/auth/bootstrap-admin", json=payload)
    assert resp_no_auth.status_code == 401

    # 2. Request with phone token is rejected (cannot impersonate admin)
    resp_phone = client.post("/api/v1/auth/bootstrap-admin", headers=phone_headers, json=payload)
    assert resp_phone.status_code == 401

    # 3. Legitimate request with ADMIN_TOKEN succeeds
    resp = client.post("/api/v1/auth/bootstrap-admin", headers=admin_headers, json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "sysadmin"
    assert data["role"] == "admin"
    assert data["has_device_control"] is True
    assert "password" not in data
    assert "password_hash" not in data

    # 4. Subsequent bootstrap attempt with ADMIN_TOKEN is permanently closed (403)
    resp2 = client.post("/api/v1/auth/bootstrap-admin", headers=admin_headers, json=payload)
    assert resp2.status_code == 403
    assert "Bootstrap is closed" in resp2.json()["detail"]


def test_bootstrap_admin_when_ordinary_user_exists(client, admin_headers, clean_auth_db):
    # 1. Ordinary user registers first
    client.post(
        "/api/v1/auth/register",
        json={"username": "sysadmin", "password": "UserAlicePassword123", "nickname": "Alice"},
    )
    # Ensure role is 'user'
    login_alice = client.post(
        "/api/v1/auth/login",
        json={"username": "sysadmin", "password": "UserAlicePassword123"},
    ).json()
    assert login_alice["user"]["role"] == "user"
    assert login_alice["user"]["has_device_control"] is False

    # 2. Legitimate admin with ADMIN_TOKEN bootstraps 'sysadmin'
    admin_payload = {
        "username": "sysadmin",
        "password": "RealAdminPassword123!",
        "nickname": "System Administrator",
    }
    resp_boot = client.post("/api/v1/auth/bootstrap-admin", headers=admin_headers, json=admin_payload)
    assert resp_boot.status_code == 201
    assert resp_boot.json()["role"] == "admin"
    assert resp_boot.json()["has_device_control"] is True

    # 3. Old user session was revoked
    old_session = login_alice["session_id"]
    r_old = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_session}"})
    assert r_old.status_code == 401

    # 4. Login with new admin password succeeds
    login_admin = client.post(
        "/api/v1/auth/login",
        json={"username": "sysadmin", "password": "RealAdminPassword123!"},
    )
    assert login_admin.status_code == 200
    assert login_admin.json()["user"]["role"] == "admin"


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

    # Check session cookie and CSRF cookie in response headers
    set_cookie = resp.headers.get("set-cookie", "")
    assert settings.SESSION_COOKIE_NAME in set_cookie
    assert settings.CSRF_COOKIE_NAME in set_cookie
    assert "session_id" in data
    assert "csrf_token" in data
    assert data["csrf_token"] is not None


def test_csrf_protection_on_cookie_write_operations(client, clean_auth_db):
    client.post(
        "/api/v1/auth/register",
        json={"username": "driver_csrf_user", "password": "CsrfUserPassword123!"},
    )
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_csrf_user", "password": "CsrfUserPassword123!"},
    )
    login_data = login_resp.json()
    session_id = login_data["session_id"]
    csrf_token = login_data["csrf_token"]

    cookies = {
        settings.SESSION_COOKIE_NAME: session_id,
        settings.CSRF_COOKIE_NAME: csrf_token,
    }

    # 1. State-changing request with cookie but MISSING X-CSRF-Token is rejected (403)
    resp_no_csrf = client.post(
        "/api/v1/favorites",
        cookies=cookies,
        json={"name": "Attacked Favorite", "address": "Tokyo", "latitude": 35.68, "longitude": 139.76},
    )
    assert resp_no_csrf.status_code == 403
    assert "CSRF check failed" in resp_no_csrf.json()["detail"]

    # 2. State-changing request with cookie and INVALID X-CSRF-Token is rejected (403)
    resp_bad_csrf = client.post(
        "/api/v1/favorites",
        cookies=cookies,
        headers={"X-CSRF-Token": "invalid_forged_token_123"},
        json={"name": "Attacked Favorite", "address": "Tokyo", "latitude": 35.68, "longitude": 139.76},
    )
    assert resp_bad_csrf.status_code == 403
    assert "Invalid X-CSRF-Token" in resp_bad_csrf.json()["detail"]

    # 3. State-changing request with cookie and VALID X-CSRF-Token succeeds (201)
    resp_good_csrf = client.post(
        "/api/v1/favorites",
        cookies=cookies,
        headers={"X-CSRF-Token": csrf_token},
        json={"name": "Legit Favorite", "address": "Tokyo", "latitude": 35.68, "longitude": 139.76},
    )
    assert resp_good_csrf.status_code == 201
    assert resp_good_csrf.json()["name"] == "Legit Favorite"

    # 4. Bearer token request in Authorization header does NOT require CSRF header
    resp_bearer = client.post(
        "/api/v1/favorites",
        headers={"Authorization": f"Bearer {session_id}"},
        json={"name": "Bearer Favorite", "address": "Tokyo", "latitude": 35.68, "longitude": 139.76},
    )
    assert resp_bearer.status_code == 201


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

    # SSRF Attack 1: Non-https URL rejected
    r1 = client.patch("/api/v1/auth/profile", headers=headers, json={"avatar_url": "http://example.com/evil.png"})
    assert r1.status_code == 400
    assert "https" in r1.json()["detail"]

    # SSRF Attack 2: Direct IP address rejected
    r2 = client.patch("/api/v1/auth/profile", headers=headers, json={"avatar_url": "https://192.0.2.20/secret.jpg"})
    assert r2.status_code == 400
    assert "Direct IP address" in r2.json()["detail"]

    # SSRF Attack 3: Localhost domain rejected
    r3 = client.patch("/api/v1/auth/profile", headers=headers, json={"avatar_url": "https://localhost:8000/avatar.jpg"})
    assert r3.status_code == 400
    assert "not allowed" in r3.json()["detail"]

    # Valid external avatar URL allowed
    r4 = client.patch(
        "/api/v1/auth/profile",
        headers=headers,
        json={"nickname": "Frank Sinatra", "avatar_url": "https://example.com/avatar.jpg"},
    )
    assert r4.status_code == 200
    assert r4.json()["nickname"] == "Frank Sinatra"
    assert r4.json()["avatar_url"] == "https://example.com/avatar.jpg"


def test_avatar_upload_and_pil_sanitization(client, clean_auth_db):
    client.post(
        "/api/v1/auth/register",
        json={"username": "driver_grace", "password": "GracePassword123!"},
    )
    session_id = client.post(
        "/api/v1/auth/login",
        json={"username": "driver_grace", "password": "GracePassword123!"},
    ).json()["session_id"]
    headers = {"Authorization": f"Bearer {session_id}"}

    # 1. Corrupted / non-image data rejected by Pillow
    fake_png = io.BytesIO(b"<html><body>Not an image</body></html>")
    r_fake = client.post(
        "/api/v1/auth/avatar",
        headers=headers,
        files={"file": ("avatar.png", fake_png, "image/png")},
    )
    assert r_fake.status_code == 400
    assert "Invalid or corrupted image" in r_fake.json()["detail"]

    # 2. Dimensions too small (< 16px) rejected
    tiny_img = Image.new("RGB", (8, 8), color="red")
    buf_tiny = io.BytesIO()
    tiny_img.save(buf_tiny, format="JPEG")
    buf_tiny.seek(0)
    r_tiny = client.post(
        "/api/v1/auth/avatar",
        headers=headers,
        files={"file": ("tiny.jpg", buf_tiny, "image/jpeg")},
    )
    assert r_tiny.status_code == 400
    assert "dimensions too small" in r_tiny.json()["detail"]

    # 3. Valid image properly decoded, transcoded, and stripped of metadata
    real_img = Image.new("RGBA", (128, 128), color=(30, 144, 255, 255))
    buf_valid = io.BytesIO()
    real_img.save(buf_valid, format="PNG")
    buf_valid.seek(0)
    r_valid = client.post(
        "/api/v1/auth/avatar",
        headers=headers,
        files={"file": ("profile.png", buf_valid, "image/png")},
    )
    assert r_valid.status_code == 200
    avatar_url = r_valid.json()["avatar_url"]
    assert avatar_url.startswith("/api/v1/auth/avatar/")

    # 4. Serve avatar image back
    r_serve = client.get(avatar_url)
    assert r_serve.status_code == 200
    assert "image" in r_serve.headers["content-type"]

    # 5. Path traversal attempt rejected
    r_traversal = client.get("/api/v1/auth/avatar/../config.py")
    assert r_traversal.status_code in (400, 404)


def test_device_control_permission_and_admin_binding(client, admin_headers, clean_auth_db):
    # 1. Bootstrap Admin using ADMIN_TOKEN
    client.post(
        "/api/v1/auth/bootstrap-admin",
        headers=admin_headers,
        json={"username": "superadmin", "password": "AdminPassword123!"},
    )

    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "superadmin", "password": "AdminPassword123!"},
    ).json()["session_id"]
    admin_session_headers = {"Authorization": f"Bearer {admin_login}"}

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

    # 3. Normal user attempts vehicle commands & status queries -> 403 Forbidden
    cmd_payload = {"command_id": "cmd_test_001", "action": "pause", "device_id": "car_p3pro"}
    r_cmd = client.post("/api/v1/music/control/command", headers=user_headers, json=cmd_payload)
    assert r_cmd.status_code == 403

    r_status = client.get("/api/v1/music/control/status", headers=user_headers)
    assert r_status.status_code == 403

    r_task = client.get("/tasks/task_dummy_999", headers=user_headers)
    assert r_task.status_code == 403

    r_dest = client.get("/destinations/dest_dummy_999", headers=user_headers)
    assert r_dest.status_code == 403

    # 4. Admin grants device binding to user
    r_bind = client.post(
        f"/api/v1/auth/users/{user_id}/device-binding",
        headers=admin_session_headers,
        json={"device_id": "car_p3pro", "can_control": True},
    )
    assert r_bind.status_code == 200
    assert r_bind.json()["can_control"] is True

    # 5. Normal user now can send vehicle commands and query status!
    r_allowed = client.post("/api/v1/music/control/command", headers=user_headers, json=cmd_payload)
    assert r_allowed.status_code == 200

    r_status_ok = client.get("/api/v1/music/control/status", headers=user_headers)
    assert r_status_ok.status_code == 200


def test_registered_admin_cannot_impersonate_car_hardware(client, admin_headers, clean_auth_db):
    # Bootstrap and log in as user admin
    client.post(
        "/api/v1/auth/bootstrap-admin",
        headers=admin_headers,
        json={"username": "admin_bob", "password": "AdminBobPassword123!"},
    )
    admin_session = client.post(
        "/api/v1/auth/login",
        json={"username": "admin_bob", "password": "AdminBobPassword123!"},
    ).json()["session_id"]
    user_admin_headers = {"Authorization": f"Bearer {admin_session}"}

    # User admin cannot poll car tasks or poll car commands directly
    r_tasks = client.get("/devices/car_p3pro/tasks/next", headers=user_admin_headers)
    assert r_tasks.status_code == 403
    assert "Standard user admin sessions cannot execute vehicle operations" in r_tasks.json()["detail"]

    r_next_cmd = client.get("/api/v1/music/control/command/next", headers=user_admin_headers)
    assert r_next_cmd.status_code == 403


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
