import asyncio
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn
from app.config import settings
from app.main import app


async def run_integration_scenario():
    base_url = "http://127.0.0.1:8000"
    headers_phone = {"Authorization": f"Bearer {settings.PHONE_TOKEN}"}
    headers_car = {"Authorization": f"Bearer {settings.CAR_TOKEN}"}

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        # 1. Health check
        print("[1] Testing /health ...")
        res = await client.get("/health")
        assert res.status_code == 200, f"Health failed: {res.text}"
        print("    -> OK:", res.json())

        # 2. Submit explicit coordinates destination
        print("[2] Testing POST /destinations (coords) ...")
        coord_payload = {
            "input_type": "coords",
            "coordinate": {"latitude": 35.658581, "longitude": 139.745433},
            "target_device_id": "car_p3pro",
        }
        res = await client.post("/destinations", json=coord_payload, headers=headers_phone)
        assert res.status_code == 200, f"Coords post failed: {res.text}"
        data = res.json()
        task_id = data["task_id"]
        print(f"    -> OK: request_id={data['request_id']}, task_id={task_id}")

        # 3. Car long-poll next task
        print("[3] Testing GET /devices/car_p3pro/tasks/next ...")
        res = await client.get("/devices/car_p3pro/tasks/next?timeout=5", headers=headers_car)
        assert res.status_code == 200, f"Poll failed: {res.text}"
        task_data = res.json()
        assert task_data["task_id"] == task_id
        assert task_data["status"] == "queued"
        print(f"    -> OK: Received task {task_id}, status={task_data['status']}")

        # 4. Report 'received' ack
        print("[4] Testing POST /tasks/{id}/ack (received) ...")
        ack_res = await client.post(
            f"/tasks/{task_id}/ack",
            json={"phase": "received", "device_timestamp": "2026-10-06T10:30:00Z"},
            headers=headers_car,
        )
        assert ack_res.status_code == 200
        print(f"    -> OK: status={ack_res.json()['status']}")

        # 5. Report 'launch_requested' ack
        print("[5] Testing POST /tasks/{id}/ack (launch_requested) ...")
        ack_res2 = await client.post(
            f"/tasks/{task_id}/ack",
            json={"phase": "launch_requested", "device_timestamp": "2026-10-06T10:30:02Z"},
            headers=headers_car,
        )
        assert ack_res2.status_code == 200
        print(f"    -> OK: status={ack_res2.json()['status']}")

        # 6. Verify non-regression with delayed duplicate ack
        print("[6] Testing delayed duplicate ack non-regression ...")
        ack_res3 = await client.post(
            f"/tasks/{task_id}/ack",
            json={"phase": "received", "device_timestamp": "2026-10-06T10:29:59Z"},
            headers=headers_car,
        )
        assert ack_res3.status_code == 200
        assert ack_res3.json()["status"] == "launch_requested"
        assert ack_res3.json()["accepted"] is False
        print(f"    -> OK: Status correctly preserved as '{ack_res3.json()['status']}'")

        # 7. Test Google Maps pinpoint link
        print("[7] Testing Google Maps pinpoint share link ...")
        pin_url = "https://www.google.com/maps/place/Tokyo+Tower/@35.6000,139.7000,12z/data=!4m6!3m5!1s0x60188bbd9009ec09:0x481a93f0d2a409dd!8m2!3d35.6585805!4d139.7454329"
        pin_res = await client.post(
            "/destinations",
            json={"input_type": "share_link", "url": pin_url, "target_device_id": "car_p3pro"},
            headers=headers_phone,
        )
        assert pin_res.status_code == 200
        assert pin_res.json()["status"] == "resolved"
        assert pin_res.json()["task_id"] is not None
        print(f"    -> OK: Generated task {pin_res.json()['task_id']} from pinpoint link")

        # 8. Test viewport-only link rejection (no fake coordinates)
        print("[8] Testing viewport-only map link rejection ...")
        vp_url = "https://www.google.com/maps/@35.658581,139.745433,17z"
        vp_res = await client.post(
            "/destinations",
            json={"input_type": "share_link", "url": vp_url, "target_device_id": "car_p3pro"},
            headers=headers_phone,
        )
        assert vp_res.status_code == 200
        assert vp_res.json()["status"] == "needs_clarification"
        assert vp_res.json()["task_id"] is None
        print("    -> OK: Viewport-only link correctly required clarification without inventing coordinates")

        # 9. Test car device permission isolation
        print("[9] Testing car device permission isolation ...")
        other_task_res = await client.post(
            "/destinations",
            json={"input_type": "coords", "coordinate": {"latitude": 35.0, "longitude": 139.0}, "target_device_id": "car_other"},
            headers=headers_phone,
        )
        other_task_id = other_task_res.json()["task_id"]
        unauth_ack = await client.post(
            f"/tasks/{other_task_id}/ack",
            json={"phase": "received"},
            headers=headers_car,
        )
        assert unauth_ack.status_code == 403
        print("    -> OK: Car token unauthorized to ack other device's task (403 Forbidden)")

    print("\n>>> All live API scenarios passed successfully! <<<\n")


async def main():
    if not settings.PHONE_TOKEN:
        settings.PHONE_TOKEN = "verify-live-phone-token"
    if not settings.CAR_TOKEN:
        settings.CAR_TOKEN = "verify-live-car-token"
    if not settings.ADMIN_TOKEN:
        settings.ADMIN_TOKEN = "verify-live-admin-token"
    settings.ALLOW_MOCK_FALLBACK = True
    settings.MAPS_PROVIDER = "mock"
    settings.ASR_PROVIDER = "mock"
    settings.NLP_PROVIDER = "mock"

    config = uvicorn.Config(app=app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve())

    # Wait for server to come online
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get("http://127.0.0.1:8000/health")
                if r.status_code == 200:
                    break
        except Exception:
            await asyncio.sleep(0.1)

    try:
        await run_integration_scenario()
    finally:
        server.should_exit = True
        await server_task


if __name__ == "__main__":
    asyncio.run(main())
