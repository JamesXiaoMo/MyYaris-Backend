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
        print(f"    -> OK: Status correctly preserved as '{ack_res3.json()['status']}'")

        # 7. Submit text destination & confirm candidate
        print("[7] Testing text destination & candidate confirmation ...")
        text_res = await client.post(
            "/destinations",
            json={"input_type": "text", "text": "我想去东京铁塔", "target_device_id": "car_p3pro"},
            headers=headers_phone,
        )
        assert text_res.status_code == 200
        req_id = text_res.json()["request_id"]
        cand_id = text_res.json()["candidates"][0]["candidate_id"]

        confirm_res = await client.post(
            f"/destinations/{req_id}/confirm",
            json={"candidate_id": cand_id, "target_device_id": "car_p3pro"},
            headers=headers_phone,
        )
        assert confirm_res.status_code == 200
        print(f"    -> OK: Confirmed task {confirm_res.json()['task_id']} for candidate {cand_id}")

    print("\n>>> All live API scenarios passed successfully! <<<\n")


async def main():
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
