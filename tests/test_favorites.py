import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.models import Favorite


@pytest.fixture(autouse=True)
def clean_favorites(db_session: Session):
    db_session.query(Favorite).delete()
    db_session.commit()
    yield
    db_session.query(Favorite).delete()
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
