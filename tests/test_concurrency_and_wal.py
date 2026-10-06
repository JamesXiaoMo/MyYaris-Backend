import asyncio
import pytest
from sqlalchemy import text
from app.repositories.database import engine
from app.services.queue_notifier import queue_notifier
from app.services.task_service import create_task_for_place, get_next_task_long_poll


def test_sqlite_wal_mode(db_session):
    result = db_session.execute(text("PRAGMA journal_mode;")).scalar()
    # In sqlite, journal_mode should be wal or memory (test pool may set memory)
    assert result.lower() in ["wal", "memory"]


@pytest.mark.asyncio
async def test_concurrent_long_poll_wake(db_session):
    # Register waiter in background
    device_id = "test_wake_device"

    async def poll_worker():
        return await get_next_task_long_poll(db_session, device_id=device_id, timeout_seconds=5)

    poll_task = asyncio.create_task(poll_worker())

    # Wait 0.2s then create task
    await asyncio.sleep(0.2)

    created = await create_task_for_place(
        db=db_session,
        device_id=device_id,
        title="即时唤醒测试目的地",
        address="测试地址",
        latitude=35.658581,
        longitude=139.745433,
    )

    result_task = await poll_task
    assert result_task is not None
    assert result_task.task_id == created.task_id
    assert result_task.title == "即时唤醒测试目的地"
