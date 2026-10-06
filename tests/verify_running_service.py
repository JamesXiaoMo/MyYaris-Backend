import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import httpx

# Load production .env without printing secrets
env_path = Path("/path/to/user/桌面/MyYaris/backend/.env")
load_dotenv(env_path)

phone_token = os.getenv("PHONE_TOKEN", "")
car_token = os.getenv("CAR_TOKEN", "")
admin_token = os.getenv("ADMIN_TOKEN", "")

assert phone_token, "PHONE_TOKEN is not configured in .env"
assert car_token, "CAR_TOKEN is not configured in .env"
assert admin_token, "ADMIN_TOKEN is not configured in .env"

base_url = "http://127.0.0.1:8000"

print(">>> Starting Live Service Verification on http://127.0.0.1:8000 ...")

with httpx.Client(base_url=base_url, timeout=10.0) as client:
    # 1. Health check
    print("[1] Verifying /health ...")
    res = client.get("/health")
    assert res.status_code == 200, f"Health check failed: {res.status_code} {res.text}"
    health_data = res.json()
    assert health_data["status"] == "ok"
    print(f"    -> OK: status={health_data['status']}, version={health_data['version']}")

    # 2. Auth rejection (no token)
    print("[2] Verifying Auth rejection on missing token ...")
    res_no_auth = client.post("/destinations", json={"input_type": "text", "text": "Tokyo"})
    assert res_no_auth.status_code == 401, f"Expected 401, got {res_no_auth.status_code}"
    print(f"    -> OK: 401 Unauthorized correctly returned when token is missing")

    # 3. Auth rejection (invalid token)
    print("[3] Verifying Auth rejection on invalid token ...")
    res_bad_auth = client.post(
        "/destinations",
        json={"input_type": "text", "text": "Tokyo"},
        headers={"Authorization": "Bearer completely_invalid_random_token"},
    )
    assert res_bad_auth.status_code == 401, f"Expected 401, got {res_bad_auth.status_code}"
    print(f"    -> OK: 401 Unauthorized correctly returned on invalid token")

    # 4. Role separation (car token attempting phone endpoint)
    print("[4] Verifying Role separation (Car token on /destinations) ...")
    res_wrong_role = client.post(
        "/destinations",
        json={"input_type": "text", "text": "Tokyo"},
        headers={"Authorization": f"Bearer {car_token}"},
    )
    assert res_wrong_role.status_code == 403, f"Expected 403, got {res_wrong_role.status_code}"
    print(f"    -> OK: 403 Forbidden correctly returned when Car token calls Phone endpoint")

    # 5. Coords submission for isolated TEST device
    print("[5] Submitting coordinates for isolated test device ('car_test_probe') ...")
    test_device = "car_test_probe"
    coord_payload = {
        "input_type": "coords",
        "coordinate": {
            "latitude": 35.658581,
            "longitude": 139.745433,
            "coord_system": "wgs84",
        },
        "target_device_id": test_device,
    }
    res_dest = client.post(
        "/destinations",
        json=coord_payload,
        headers={"Authorization": f"Bearer {phone_token}"},
    )
    assert res_dest.status_code == 200, f"Dest post failed: {res_dest.status_code} {res_dest.text}"
    dest_data = res_dest.json()
    assert dest_data["status"] == "resolved"
    assert dest_data["task_id"] is not None
    created_task_id = dest_data["task_id"]
    print(f"    -> OK: status=resolved, task_id={created_task_id} queued for {test_device}")

    # 6. Polling next task for test device (Admin token used as proxy reader)
    print(f"[6] Polling next task for {test_device} ...")
    res_poll = client.get(
        f"/devices/{test_device}/tasks/next?timeout=5",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_poll.status_code == 200, f"Poll failed: {res_poll.status_code} {res_poll.text}"
    poll_data = res_poll.json()
    assert poll_data["task_id"] == created_task_id
    assert poll_data["status"] == "queued"
    assert poll_data["latitude"] == 35.658581
    assert poll_data["longitude"] == 139.745433
    print(f"    -> OK: Retrieved task {created_task_id} with status='{poll_data['status']}'")
    print("    -> Note: Intentionally NOT issuing ack, preserving task without fake confirmation.")

print("\n>>> All live service verifications completed successfully! <<<\n")
