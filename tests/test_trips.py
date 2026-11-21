from datetime import datetime, timedelta, timezone


def test_trip_upload_list_detail_and_delete(client, car_headers, admin_headers):
    t0 = datetime(2026, 11, 22, 0, 30, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=10)
    t2 = t0 + timedelta(seconds=20)

    # Car uploads first two points
    r1 = client.post(
        "/api/v1/trips/points",
        headers=car_headers,
        json={
            "device_id": "car_p3pro",
            "trip_id": "trip_test_001",
            "points": [
                {
                    "latitude": 35.681236,
                    "longitude": 139.767125,
                    "speed_kmh": 36.0,
                    "accuracy_m": 6.0,
                    "recorded_at": t0.isoformat(),
                },
                {
                    "latitude": 35.682236,
                    "longitude": 139.767125,
                    "speed_kmh": 40.0,
                    "accuracy_m": 5.0,
                    "recorded_at": t1.isoformat(),
                },
            ],
        },
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["trip_id"] == "trip_test_001"
    assert body1["accepted"] == 2
    assert body1["point_count"] == 2
    assert body1["distance_km"] > 0.1

    # Car uploads third point to same trip
    r2 = client.post(
        "/api/v1/trips/points",
        headers=car_headers,
        json={
            "device_id": "car_p3pro",
            "trip_id": "trip_test_001",
            "points": [
                {
                    "latitude": 35.683236,
                    "longitude": 139.768125,
                    "speed_kmh": 45.0,
                    "accuracy_m": 5.0,
                    "recorded_at": t2.isoformat(),
                },
            ],
        },
    )
    assert r2.status_code == 200
    assert r2.json()["point_count"] == 3
    assert r2.json()["distance_km"] > body1["distance_km"]

    # Admin/user lists trips
    r_list = client.get("/api/v1/trips", headers=admin_headers)
    assert r_list.status_code == 200
    trips = r_list.json()["trips"]
    assert len(trips) >= 1
    assert trips[0]["trip_id"] == "trip_test_001"
    assert trips[0]["point_count"] == 3

    # Detail returns points in chronological order
    r_detail = client.get("/api/v1/trips/trip_test_001", headers=admin_headers)
    assert r_detail.status_code == 200
    detail = r_detail.json()
    assert len(detail["points"]) == 3
    assert detail["points"][0]["latitude"] == 35.681236
    assert detail["points"][2]["latitude"] == 35.683236

    # Delete trip
    r_del = client.delete("/api/v1/trips/trip_test_001", headers=admin_headers)
    assert r_del.status_code == 200
    assert r_del.json()["deleted"] is True

    r_missing = client.get("/api/v1/trips/trip_test_001", headers=admin_headers)
    assert r_missing.status_code == 404


def test_trip_upload_rejects_other_device_for_car_token(client, car_headers):
    r = client.post(
        "/api/v1/trips/points",
        headers=car_headers,
        json={
            "device_id": "other_car_999",
            "trip_id": "trip_forbidden",
            "points": [{"latitude": 35.68, "longitude": 139.76}],
        },
    )
    assert r.status_code == 403
