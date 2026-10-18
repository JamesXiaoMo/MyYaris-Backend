import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.migrations import run_db_migrations
from app.repositories.models import Favorite, FavoriteFolder, User, UserSession


@pytest.fixture(autouse=True)
def clean_favorites(db_session: Session):
    db_session.query(Favorite).delete()
    db_session.query(FavoriteFolder).delete()
    db_session.commit()
    yield
    db_session.query(Favorite).delete()
    db_session.query(FavoriteFolder).delete()
    db_session.commit()



def test_create_favorite_success(client: TestClient, phone_headers: dict):
    # 1. Create favorite with all fields
    payload = {
        "name": "公司",
        "address": "北京市海淀区中关村南大街1号",
        "latitude": 39.9567,
        "longitude": 116.3212,
        "coord_system": "wgs84",
    }
    resp = client.post("/api/v1/favorites", json=payload, headers=phone_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"].startswith("fav_")
    assert data["name"] == "公司"
    assert data["address"] == "北京市海淀区中关村南大街1号"
    assert abs(data["latitude"] - 39.9567) < 1e-4
    assert abs(data["longitude"] - 116.3212) < 1e-4
    assert data["coord_system"] == "wgs84"
    assert "created_at" in data
    assert "updated_at" in data

    # 2. Create favorite without name (optional)
    payload_no_name = {
        "address": "上海市浦东新区张江高科技园区",
        "latitude": 31.2015,
        "longitude": 121.5876,
    }
    resp2 = client.post("/api/v1/favorites", json=payload_no_name, headers=phone_headers)
    assert resp2.status_code == 201
    data2 = resp2.json()
    assert data2["name"] is None
    assert data2["coord_system"] == "wgs84"


def test_list_favorites_pagination_and_search(client: TestClient, phone_headers: dict):
    # Seed 3 items
    client.post("/api/v1/favorites", json={"name": "家", "address": "北京市朝阳区北苑路", "latitude": 40.038, "longitude": 116.425}, headers=phone_headers)
    client.post("/api/v1/favorites", json={"name": "公司", "address": "北京市海淀区中关村", "latitude": 39.956, "longitude": 116.321}, headers=phone_headers)
    client.post("/api/v1/favorites", json={"name": "充电站", "address": "北京市西城区西直门", "latitude": 39.941, "longitude": 116.353}, headers=phone_headers)

    # List all
    list_resp = client.get("/api/v1/favorites", headers=phone_headers)
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["total"] == 3
    assert len(list_data["items"]) == 3

    # Pagination: limit 2, offset 0
    p1 = client.get("/api/v1/favorites?limit=2&offset=0", headers=phone_headers).json()
    assert len(p1["items"]) == 2
    assert p1["total"] == 3

    p2 = client.get("/api/v1/favorites?limit=2&offset=2", headers=phone_headers).json()
    assert len(p2["items"]) == 1

    # Search keyword
    s1 = client.get("/api/v1/favorites?search=充电", headers=phone_headers).json()
    assert s1["total"] == 1
    assert s1["items"][0]["name"] == "充电站"

    s2 = client.get("/api/v1/favorites?search=中关村", headers=phone_headers).json()
    assert s2["total"] == 1
    assert s2["items"][0]["name"] == "公司"


def test_get_and_update_favorite(client: TestClient, phone_headers: dict):
    # Create
    create_resp = client.post(
        "/api/v1/favorites",
        json={"name": "旧地址", "address": "北京市朝阳区旧宫", "latitude": 39.812, "longitude": 116.456},
        headers=phone_headers,
    )
    fav_id = create_resp.json()["id"]

    # Get detail
    get_resp = client.get(f"/api/v1/favorites/{fav_id}", headers=phone_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "旧地址"

    # Update via PATCH
    patch_resp = client.patch(
        f"/api/v1/favorites/{fav_id}",
        json={"name": "新地址", "latitude": 39.820},
        headers=phone_headers,
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()
    assert updated["name"] == "新地址"
    assert abs(updated["latitude"] - 39.820) < 1e-4
    assert updated["address"] == "北京市朝阳区旧宫"  # untouched

    # Update via PUT
    put_resp = client.put(
        f"/api/v1/favorites/{fav_id}",
        json={"address": "北京市大兴区亦庄开发区"},
        headers=phone_headers,
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["address"] == "北京市大兴区亦庄开发区"


def test_delete_favorite(client: TestClient, phone_headers: dict):
    create_resp = client.post(
        "/api/v1/favorites",
        json={"name": "准备删除", "address": "北京市东城区王府井", "latitude": 39.914, "longitude": 116.411},
        headers=phone_headers,
    )
    fav_id = create_resp.json()["id"]

    # Delete
    del_resp = client.delete(f"/api/v1/favorites/{fav_id}", headers=phone_headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "ok"
    assert del_resp.json()["deleted_id"] == fav_id

    # Verify 404
    get_after = client.get(f"/api/v1/favorites/{fav_id}", headers=phone_headers)
    assert get_after.status_code == 404

    # Delete non-existent
    del_404 = client.delete(f"/api/v1/favorites/{fav_id}", headers=phone_headers)
    assert del_404.status_code == 404


def test_coordinate_and_address_validation_strict(client: TestClient, phone_headers: dict):
    # 1. Empty address
    r1 = client.post("/api/v1/favorites", json={"address": "", "latitude": 39.9, "longitude": 116.4}, headers=phone_headers)
    assert r1.status_code == 422

    # 2. Whitespace-only address
    r2 = client.post("/api/v1/favorites", json={"address": "   \n\t  ", "latitude": 39.9, "longitude": 116.4}, headers=phone_headers)
    assert r2.status_code == 422

    # 3. Latitude out of range (> 90.0 or < -90.0)
    r3 = client.post("/api/v1/favorites", json={"address": "地址", "latitude": 90.1, "longitude": 116.4}, headers=phone_headers)
    assert r3.status_code == 422
    r4 = client.post("/api/v1/favorites", json={"address": "地址", "latitude": -90.5, "longitude": 116.4}, headers=phone_headers)
    assert r4.status_code == 422

    # 4. Longitude out of range (> 180.0 or < -180.0)
    r5 = client.post("/api/v1/favorites", json={"address": "地址", "latitude": 39.9, "longitude": 180.5}, headers=phone_headers)
    assert r5.status_code == 422
    r6 = client.post("/api/v1/favorites", json={"address": "地址", "latitude": 39.9, "longitude": -181.0}, headers=phone_headers)
    assert r6.status_code == 422

    # 5. Boundary valid coordinates (-90, 90, -180, 180) must be accepted
    r_boundary = client.post(
        "/api/v1/favorites",
        json={"address": "极点地址", "latitude": -90.0, "longitude": 180.0},
        headers=phone_headers,
    )
    assert r_boundary.status_code == 201

    # 6. Non-wgs84 coordinate systems (gcj02, bd09, mars) must be strictly rejected (422)
    r_bad_coord = client.post(
        "/api/v1/favorites",
        json={"address": "地址", "latitude": 39.9, "longitude": 116.4, "coord_system": "mars_coordinates"},
        headers=phone_headers,
    )
    assert r_bad_coord.status_code == 422

    r_gcj = client.post(
        "/api/v1/favorites",
        json={"address": "地址", "latitude": 39.9, "longitude": 116.4, "coord_system": "gcj02"},
        headers=phone_headers,
    )
    assert r_gcj.status_code == 422

    r_bd = client.post(
        "/api/v1/favorites",
        json={"address": "地址", "latitude": 39.9, "longitude": 116.4, "coord_system": "bd09"},
        headers=phone_headers,
    )
    assert r_bd.status_code == 422

    # 7. Update with invalid latitude or non-wgs84 coord_system
    fav_id = r_boundary.json()["id"]
    r_bad_patch = client.patch(
        f"/api/v1/favorites/{fav_id}",
        json={"latitude": 105.0},
        headers=phone_headers,
    )
    assert r_bad_patch.status_code == 422

    r_bad_coord_patch = client.patch(
        f"/api/v1/favorites/{fav_id}",
        json={"coord_system": "gcj02"},
        headers=phone_headers,
    )
    assert r_bad_coord_patch.status_code == 422


def test_auth_boundaries_and_forbidden_roles(client: TestClient, car_headers: dict, admin_headers: dict):
    # 1. Anonymous access rejected (401)
    anon_resp = client.get("/api/v1/favorites")
    assert anon_resp.status_code == 401

    anon_post = client.post("/api/v1/favorites", json={"address": "地址", "latitude": 39.9, "longitude": 116.4})
    assert anon_post.status_code == 401

    # 2. Car client token rejected (403 Forbidden)
    car_get = client.get("/api/v1/favorites", headers=car_headers)
    assert car_get.status_code == 403

    car_post = client.post(
        "/api/v1/favorites",
        json={"address": "地址", "latitude": 39.9, "longitude": 116.4},
        headers=car_headers,
    )
    assert car_post.status_code == 403

    # 3. Admin client token accepted (200 / 201)
    admin_post = client.post(
        "/api/v1/favorites",
        json={"name": "管理员预设", "address": "总装车间", "latitude": 35.123, "longitude": 139.456},
        headers=admin_headers,
    )
    assert admin_post.status_code == 201


def test_restart_persistence_sqlite(client: TestClient, phone_headers: dict, db_session: Session):
    # Tests that favorite records are reliably committed to disk in SQLite and verifiable via an independent fresh DB session
    # 1. Create favorite via HTTP
    post_resp = client.post(
        "/api/v1/favorites",
        json={"name": "持久化测试", "address": "苏州市工业园区金鸡湖大道99号", "latitude": 31.312, "longitude": 120.678},
        headers=phone_headers,
    )
    assert post_resp.status_code == 201
    fav_id = post_resp.json()["id"]

    # 2. Query directly from fresh database session (verifying SQLite disk persistence across connection boundaries)
    from app.repositories.database import SessionLocal
    with SessionLocal() as fresh_db:
        row = fresh_db.query(Favorite).filter(Favorite.id == fav_id).first()
        assert row is not None
        assert row.name == "持久化测试"
        assert row.address == "苏州市工业园区金鸡湖大道99号"
        assert abs(row.latitude - 31.312) < 1e-4
        assert abs(row.longitude - 120.678) < 1e-4


def test_folder_crud_and_validation(client: TestClient, phone_headers: dict):
    # 1. Create folder with leading/trailing spaces -> trimmed
    r1 = client.post("/api/v1/favorite-folders", json={"name": "  日常通勤  "}, headers=phone_headers)
    assert r1.status_code == 201
    f1 = r1.json()
    assert f1["id"].startswith("fld_")
    assert f1["name"] == "日常通勤"
    assert f1["favorite_count"] == 0

    # 2. Duplicate folder name in same scope -> 409 Conflict (case-insensitive & trimmed)
    r_dup = client.post("/api/v1/favorite-folders", json={"name": "日常通勤"}, headers=phone_headers)
    assert r_dup.status_code == 409

    r_dup_case = client.post("/api/v1/favorite-folders", json={"name": "  Work Places "}, headers=phone_headers)
    assert r_dup_case.status_code == 201
    r_dup_case2 = client.post("/api/v1/favorite-folders", json={"name": "work places"}, headers=phone_headers)
    assert r_dup_case2.status_code == 409

    # 3. Empty / whitespace-only / too long (>64 chars) -> 422
    assert client.post("/api/v1/favorite-folders", json={"name": ""}, headers=phone_headers).status_code == 422
    assert client.post("/api/v1/favorite-folders", json={"name": "   \t\n "}, headers=phone_headers).status_code == 422
    assert client.post("/api/v1/favorite-folders", json={"name": "a" * 65}, headers=phone_headers).status_code == 422

    # 4. List folders (both /api/v1/favorite-folders and /api/v1/favorites/folders alias)
    list_resp = client.get("/api/v1/favorite-folders?limit=1&offset=0", headers=phone_headers)
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["total"] == 2
    assert len(list_data["items"]) == 1
    assert list_data["has_more"] is True

    alias_resp = client.get("/api/v1/favorites/folders", headers=phone_headers)
    assert alias_resp.status_code == 200
    assert alias_resp.json()["total"] == 2

    # 5. Rename folder (PATCH & PUT) + duplicate check on rename
    r_rename_conflict = client.patch(
        f"/api/v1/favorite-folders/{f1['id']}",
        json={"name": "Work Places"},
        headers=phone_headers,
    )
    assert r_rename_conflict.status_code == 409

    r_rename_ok = client.patch(
        f"/api/v1/favorite-folders/{f1['id']}",
        json={"name": "  周末出游  "},
        headers=phone_headers,
    )
    assert r_rename_ok.status_code == 200
    assert r_rename_ok.json()["name"] == "周末出游"


def test_favorite_categorization_move_and_non_empty_folder_delete(client: TestClient, phone_headers: dict):
    # Create two folders
    f_commute = client.post("/api/v1/favorite-folders", json={"name": "通勤"}, headers=phone_headers).json()
    f_travel = client.post("/api/v1/favorite-folders", json={"name": "旅行"}, headers=phone_headers).json()

    # Create 2 favorites in f_commute and 1 uncategorized favorite
    fav1 = client.post(
        "/api/v1/favorites",
        json={"name": "公司", "address": "千代田区丸之内", "latitude": 35.6812, "longitude": 139.7671, "folder_id": f_commute["id"]},
        headers=phone_headers,
    ).json()
    assert fav1["folder_id"] == f_commute["id"]
    assert fav1["folder_name"] == "通勤"

    fav2 = client.post(
        "/api/v1/favorites",
        json={"name": "分部", "address": "港区六本木", "latitude": 35.6628, "longitude": 139.7314, "folder_id": f_commute["id"]},
        headers=phone_headers,
    ).json()

    fav_uncat = client.post(
        "/api/v1/favorites",
        json={"name": "超市", "address": "新宿区歌舞伎町", "latitude": 35.6938, "longitude": 139.7034},
        headers=phone_headers,
    ).json()
    assert fav_uncat["folder_id"] is None
    assert fav_uncat["folder_name"] is None

    # Check folder list counts and uncategorized_count
    folders_data = client.get("/api/v1/favorite-folders", headers=phone_headers).json()
    assert folders_data["uncategorized_count"] == 1
    counts = {item["id"]: item["favorite_count"] for item in folders_data["items"]}
    assert counts[f_commute["id"]] == 2
    assert counts[f_travel["id"]] == 0

    # Filter favorites: all vs specific folder vs uncategorized
    all_favs = client.get("/api/v1/favorites", headers=phone_headers).json()
    assert all_favs["total"] == 3
    assert all_favs["has_more"] is False

    commute_favs = client.get(f"/api/v1/favorites?folder_id={f_commute['id']}", headers=phone_headers).json()
    assert commute_favs["total"] == 2
    assert {i["id"] for i in commute_favs["items"]} == {fav1["id"], fav2["id"]}

    uncat_favs = client.get("/api/v1/favorites?uncategorized=true", headers=phone_headers).json()
    assert uncat_favs["total"] == 1
    assert uncat_favs["items"][0]["id"] == fav_uncat["id"]

    uncat_sentinel = client.get("/api/v1/favorites?folder_id=uncategorized", headers=phone_headers).json()
    assert uncat_sentinel["total"] == 1
    assert uncat_sentinel["items"][0]["id"] == fav_uncat["id"]

    # PATCH fav1 without folder_id in body -> keeps existing folder_id
    patch_keep = client.patch(
        f"/api/v1/favorites/{fav1['id']}",
        json={"name": "总部大楼"},
        headers=phone_headers,
    ).json()
    assert patch_keep["name"] == "总部大楼"
    assert patch_keep["folder_id"] == f_commute["id"]

    # PATCH fav1 to move to f_travel
    patch_move = client.patch(
        f"/api/v1/favorites/{fav1['id']}",
        json={"folder_id": f_travel["id"]},
        headers=phone_headers,
    ).json()
    assert patch_move["folder_id"] == f_travel["id"]
    assert patch_move["folder_name"] == "旅行"

    # PATCH fav1 with folder_id: null -> moves back to uncategorized
    patch_clear = client.patch(
        f"/api/v1/favorites/{fav1['id']}",
        json={"folder_id": None},
        headers=phone_headers,
    ).json()
    assert patch_clear["folder_id"] is None
    assert patch_clear["folder_name"] is None

    # Move fav1 back into f_commute so f_commute has 2 items again, then delete non-empty f_commute
    client.patch(f"/api/v1/favorites/{fav1['id']}", json={"folder_id": f_commute["id"]}, headers=phone_headers)

    del_folder_resp = client.delete(f"/api/v1/favorite-folders/{f_commute['id']}", headers=phone_headers)
    assert del_folder_resp.status_code == 200
    del_data = del_folder_resp.json()
    assert del_data["status"] == "ok"
    assert del_data["deleted_id"] == f_commute["id"]
    assert del_data["moved_to_uncategorized_count"] == 2

    # Verify all 3 favorites STILL exist and fav1, fav2 are now uncategorized (folder_id is None)
    all_after_del = client.get("/api/v1/favorites", headers=phone_headers).json()
    assert all_after_del["total"] == 3
    for item in all_after_del["items"]:
        assert item["folder_id"] is None
        assert item["folder_name"] is None

    uncat_after_del = client.get("/api/v1/favorites?uncategorized=true", headers=phone_headers).json()
    assert uncat_after_del["total"] == 3


def test_cross_account_and_admin_folder_isolation(client: TestClient, db_session: Session, phone_headers: dict):
    # Clean users first to register fresh users
    db_session.query(UserSession).delete()
    db_session.query(User).delete()
    db_session.commit()

    # Register & login User A, User B, and an Admin User
    client.post("/api/v1/auth/register", json={"username": "folder_user_a", "password": "PasswordA123!"})
    s_a = client.post("/api/v1/auth/login", json={"username": "folder_user_a", "password": "PasswordA123!"}).json()["session_id"]
    h_a = {"Authorization": f"Bearer {s_a}"}

    client.post("/api/v1/auth/register", json={"username": "folder_user_b", "password": "PasswordB123!"})
    s_b = client.post("/api/v1/auth/login", json={"username": "folder_user_b", "password": "PasswordB123!"}).json()["session_id"]
    h_b = {"Authorization": f"Bearer {s_b}"}

    client.post("/api/v1/auth/register", json={"username": "folder_admin", "password": "PasswordAdmin123!"})
    admin_user = db_session.query(User).filter(User.username == "folder_admin").first()
    admin_user.role = "admin"
    db_session.commit()
    s_adm = client.post("/api/v1/auth/login", json={"username": "folder_admin", "password": "PasswordAdmin123!"}).json()["session_id"]
    h_adm = {"Authorization": f"Bearer {s_adm}"}

    # Both User A and User B can create a folder with the same name "常用地点"
    fld_a = client.post("/api/v1/favorite-folders", json={"name": "常用地点"}, headers=h_a).json()
    fld_b = client.post("/api/v1/favorite-folders", json={"name": "常用地点"}, headers=h_b).json()
    fld_adm = client.post("/api/v1/favorite-folders", json={"name": "管理员文件夹"}, headers=h_adm).json()
    fld_legacy = client.post("/api/v1/favorite-folders", json={"name": "常用地点"}, headers=phone_headers).json()

    fav_a = client.post(
        "/api/v1/favorites",
        json={"name": "A的家", "address": "东京站", "latitude": 35.6812, "longitude": 139.7671, "folder_id": fld_a["id"]},
        headers=h_a,
    ).json()

    # 1. User B cannot view, rename, delete, or filter by User A's folder
    assert client.get(f"/api/v1/favorite-folders/{fld_a['id']}", headers=h_b).status_code == 404
    assert client.patch(f"/api/v1/favorite-folders/{fld_a['id']}", json={"name": "篡改"}, headers=h_b).status_code == 404
    assert client.delete(f"/api/v1/favorite-folders/{fld_a['id']}", headers=h_b).status_code == 404
    assert client.get(f"/api/v1/favorites?folder_id={fld_a['id']}", headers=h_b).status_code == 404

    # 2. User B cannot create a favorite in User A's folder or move B's favorite into A's folder
    r_cross_create = client.post(
        "/api/v1/favorites",
        json={"name": "越权", "address": "涩谷", "latitude": 35.6580, "longitude": 139.7016, "folder_id": fld_a["id"]},
        headers=h_b,
    )
    assert r_cross_create.status_code == 404

    fav_b = client.post(
        "/api/v1/favorites",
        json={"name": "B的家", "address": "品川", "latitude": 35.6284, "longitude": 139.7387},
        headers=h_b,
    ).json()
    r_cross_move = client.patch(
        f"/api/v1/favorites/{fav_b['id']}",
        json={"folder_id": fld_a["id"]},
        headers=h_b,
    )
    assert r_cross_move.status_code == 404

    # 3. Admin account cannot cross-assign User A's favorite or folder across accounts
    assert client.post(
        "/api/v1/favorites",
        json={"name": "Admin放入A文件夹", "address": "秋叶原", "latitude": 35.6983, "longitude": 139.7730, "folder_id": fld_a["id"]},
        headers=h_adm,
    ).status_code == 404
    assert client.patch(
        f"/api/v1/favorites/{fav_a['id']}",
        json={"folder_id": fld_adm["id"]},
        headers=h_adm,
    ).status_code == 404
    assert client.patch(
        f"/api/v1/favorites/{fav_a['id']}",
        json={"folder_id": fld_b["id"]},
        headers=h_adm,
    ).status_code == 404

    # 4. Legacy token caller and logged-in user are mutually isolated on folders
    assert client.post(
        "/api/v1/favorites",
        json={"name": "Legacy越权", "address": "银座", "latitude": 35.6719, "longitude": 139.7640, "folder_id": fld_a["id"]},
        headers=phone_headers,
    ).status_code == 404
    assert client.post(
        "/api/v1/favorites",
        json={"name": "User越权Legacy", "address": "银座", "latitude": 35.6719, "longitude": 139.7640, "folder_id": fld_legacy["id"]},
        headers=h_a,
    ).status_code == 404


def test_migrations_idempotent_on_legacy_sqlite_schema(tmp_path):
    db_path = tmp_path / "legacy_migration_test.db"
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    # Simulate an older favorites table before folder_id existed
    with eng.connect() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE favorites (
                    id VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(128),
                    address VARCHAR(512) NOT NULL,
                    latitude FLOAT NOT NULL,
                    longitude FLOAT NOT NULL,
                    coord_system VARCHAR(32) NOT NULL DEFAULT 'wgs84',
                    created_by VARCHAR(64),
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO favorites (id, name, address, latitude, longitude, coord_system, created_at, updated_at)
                VALUES ('fav_old_1', '老家', '旧地址1号', 35.0, 139.0, 'wgs84', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """
            )
        )
        conn.commit()

    # Run migration twice to verify idempotency
    run_db_migrations(eng)
    run_db_migrations(eng)

    with eng.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(favorites)")).fetchall()]
        assert "user_id" in cols
        assert "is_legacy" in cols
        assert "folder_id" in cols

        folder_table = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='favorite_folders'")
        ).fetchone()
        assert folder_table is not None

        row = conn.execute(
            text("SELECT id, name, is_legacy, folder_id FROM favorites WHERE id='fav_old_1'")
        ).fetchone()
        assert row is not None
        assert row[0] == "fav_old_1"
        assert row[1] == "老家"
        assert row[2] == 1
        assert row[3] is None

        folder_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(favorite_folders)")).fetchall()]
        assert "owner_scope" in folder_cols
        assert "name_norm" in folder_cols


def test_folder_db_unique_constraint_and_concurrent_race_409(
    client: TestClient,
    db_session: Session,
    phone_headers: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    from sqlalchemy.exc import IntegrityError
    import app.api.favorites as fav_api

    # 1. Seed initial folders
    f1 = client.post("/api/v1/favorite-folders", json={"name": "并发测试夹"}, headers=phone_headers).json()
    f2 = client.post("/api/v1/favorite-folders", json={"name": "待改名文件夹"}, headers=phone_headers).json()

    # 2. Simulate concurrent race where check-then-insert is bypassed by two simultaneous requests:
    # SQLite's UniqueConstraint on (owner_scope, name_norm) must catch the race at commit time and map to 409 Conflict!
    monkeypatch.setattr(fav_api, "_ensure_unique_folder_name", lambda *args, **kwargs: None)

    r_race_create = client.post(
        "/api/v1/favorite-folders",
        json={"name": "  并发测试夹  "},
        headers=phone_headers,
    )
    assert r_race_create.status_code == 409

    # Also test case-insensitive collision at DB constraint level when check-then-rename is bypassed
    r_race_rename = client.patch(
        f"/api/v1/favorite-folders/{f2['id']}",
        json={"name": "并发测试夹"},
        headers=phone_headers,
    )
    assert r_race_rename.status_code == 409

    # Verify session was cleanly rolled back and subsequent valid operations still succeed
    r_after = client.get("/api/v1/favorite-folders", headers=phone_headers)
    assert r_after.status_code == 200
    assert r_after.json()["total"] == 2

    # 3. Direct DB insert of duplicate (owner_scope, name_norm) must raise IntegrityError
    dup_model = FavoriteFolder(
        id="fld_direct_dup",
        user_id=None,
        is_legacy=True,
        owner_scope="__legacy__",
        name="并发测试夹",
        name_norm="并发测试夹",
    )
    db_session.add(dup_model)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_pagination_tiebreaker_conflicting_filters_and_repeated_delete(
    client: TestClient,
    db_session: Session,
    phone_headers: dict,
):
    from datetime import datetime, timezone

    # 1. Deterministic tie-breaker when timestamps are identical
    fixed_time = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    for fid, fname in [("fld_001", "FolderB"), ("fld_002", "FolderA"), ("fld_003", "FolderC")]:
        db_session.add(
            FavoriteFolder(
                id=fid,
                user_id=None,
                is_legacy=True,
                owner_scope="__legacy__",
                name=fname,
                name_norm=fname.lower(),
                created_at=fixed_time,
                updated_at=fixed_time,
            )
        )
    db_session.commit()

    # Ascending by created_at (all equal) -> secondary sort by id asc: fld_001, fld_002, fld_003
    p_asc_1 = client.get("/api/v1/favorite-folders?sort_by=created_at&order=asc&limit=2&offset=0", headers=phone_headers).json()
    assert [x["id"] for x in p_asc_1["items"]] == ["fld_001", "fld_002"]
    assert p_asc_1["has_more"] is True

    p_asc_2 = client.get("/api/v1/favorite-folders?sort_by=created_at&order=asc&limit=2&offset=2", headers=phone_headers).json()
    assert [x["id"] for x in p_asc_2["items"]] == ["fld_003"]
    assert p_asc_2["has_more"] is False

    # Out-of-range offset -> empty items, has_more=False
    p_oob = client.get("/api/v1/favorite-folders?limit=10&offset=99", headers=phone_headers).json()
    assert p_oob["items"] == []
    assert p_oob["total"] == 3
    assert p_oob["has_more"] is False

    # Descending by created_at -> secondary sort by id desc: fld_003, fld_002, fld_001
    p_desc = client.get("/api/v1/favorite-folders?sort_by=created_at&order=desc", headers=phone_headers).json()
    assert [x["id"] for x in p_desc["items"]] == ["fld_003", "fld_002", "fld_001"]

    # 2. Conflicting filter: uncategorized=true + concrete folder_id -> 422
    r_conflict = client.get("/api/v1/favorites?uncategorized=true&folder_id=fld_001", headers=phone_headers)
    assert r_conflict.status_code == 422

    # Non-existent folder_id filter -> 404
    r_missing_fld = client.get("/api/v1/favorites?folder_id=fld_does_not_exist", headers=phone_headers)
    assert r_missing_fld.status_code == 404

    # 3. Repeated DELETE on folder returns 200 first, then 404 on retry
    d1 = client.delete("/api/v1/favorite-folders/fld_003", headers=phone_headers)
    assert d1.status_code == 200
    d2 = client.delete("/api/v1/favorite-folders/fld_003", headers=phone_headers)
    assert d2.status_code == 404

    # 4. Explicit regression: PUT/PATCH on categorized favorite with omitted folder_id vs explicit "" / "uncategorized" / null
    fav = client.post(
        "/api/v1/favorites",
        json={"name": "初始归类点", "address": "东京塔", "latitude": 35.6586, "longitude": 139.7454, "folder_id": "fld_001"},
        headers=phone_headers,
    ).json()
    assert fav["folder_id"] == "fld_001"

    # PUT without folder_id keeps fld_001
    put_keep = client.put(
        f"/api/v1/favorites/{fav['id']}",
        json={"name": "PUT未传folder_id保持归属"},
        headers=phone_headers,
    ).json()
    assert put_keep["folder_id"] == "fld_001"

    # PATCH with empty string "" clears folder_id to None
    patch_empty = client.patch(
        f"/api/v1/favorites/{fav['id']}",
        json={"folder_id": "   "},
        headers=phone_headers,
    ).json()
    assert patch_empty["folder_id"] is None

    # Re-assign to fld_002, then clear via "uncategorized" sentinel
    client.patch(f"/api/v1/favorites/{fav['id']}", json={"folder_id": "fld_002"}, headers=phone_headers)
    patch_sentinel = client.patch(
        f"/api/v1/favorites/{fav['id']}",
        json={"folder_id": "uncategorized"},
        headers=phone_headers,
    ).json()
    assert patch_sentinel["folder_id"] is None


