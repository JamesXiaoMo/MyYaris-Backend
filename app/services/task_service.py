import asyncio
from datetime import datetime, timedelta, timezone
import time
from typing import Optional, Tuple
import uuid
from sqlalchemy.orm import Session
from app.config import settings
from app.repositories.models import Task, TaskEvent, utc_now
from app.services.queue_notifier import queue_notifier


def expire_stale_tasks(db: Session, device_id: Optional[str] = None) -> int:
    now = utc_now()
    query = db.query(Task).filter(
        Task.status.in_(["queued", "received"]),
        Task.expires_at < now,
    )
    if device_id:
        query = query.filter(Task.device_id == device_id)
    stale_tasks = query.all()
    count = 0
    for task in stale_tasks:
        task.status = "expired"
        count += 1
    if count > 0:
        db.commit()
    return count


async def create_task_for_place(
    db: Session,
    device_id: str,
    title: str,
    address: Optional[str],
    latitude: float,
    longitude: float,
    coord_system: str = "wgs84",
    request_id: Optional[str] = None,
) -> Task:
    expire_stale_tasks(db, device_id)
    now = utc_now()

    # Mark existing queued/received tasks as superseded
    active_tasks = (
        db.query(Task)
        .filter(Task.device_id == device_id, Task.status.in_(["queued", "received"]))
        .all()
    )

    new_task_id = str(uuid.uuid4())
    for old_task in active_tasks:
        old_task.status = "superseded"
        old_task.superseded_by = new_task_id

    expires_at = now + timedelta(minutes=settings.TASK_DEFAULT_EXPIRY_MINUTES)
    task = Task(
        task_id=new_task_id,
        request_id=request_id,
        device_id=device_id,
        title=title,
        address=address,
        latitude=latitude,
        longitude=longitude,
        coord_system=coord_system,
        status="queued",
        created_at=now,
        expires_at=expires_at,
        delivery_attempts=0,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # Wake long polling clients
    await queue_notifier.notify(device_id)
    return task


def _fetch_next_queued_task(db: Session, device_id: str) -> Optional[Task]:
    expire_stale_tasks(db, device_id)
    task = (
        db.query(Task)
        .filter(Task.device_id == device_id, Task.status == "queued")
        .order_by(Task.created_at.desc())
        .first()
    )
    if task:
        task.delivery_attempts += 1
        task.last_delivered_at = utc_now()
        task.ack_deadline = utc_now() + timedelta(seconds=settings.ACK_TIMEOUT_SECONDS)
        db.commit()
        db.refresh(task)
        return task
    return None


async def get_next_task_long_poll(
    db: Session,
    device_id: str,
    timeout_seconds: Optional[int] = None,
) -> Optional[Task]:
    if timeout_seconds is None or timeout_seconds <= 0:
        timeout_seconds = settings.LONG_POLL_TIMEOUT_SECONDS

    # 1. Quick check
    task = _fetch_next_queued_task(db, device_id)
    if task:
        return task

    # 2. Wait with notifier
    event = await queue_notifier.register_waiter(device_id)
    deadline = time.time() + timeout_seconds

    try:
        while time.time() < deadline:
            wait_slice = max(0.1, min(2.0, deadline - time.time()))
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_slice)
            except asyncio.TimeoutError:
                pass

            task = _fetch_next_queued_task(db, device_id)
            if task:
                return task
            if event.is_set():
                event.clear()
    finally:
        await queue_notifier.remove_waiter(device_id, event)

    return None


def record_task_ack(
    db: Session,
    task_id: str,
    phase: str,
    device_timestamp: Optional[datetime] = None,
    error_code: Optional[str] = None,
    error_detail: Optional[str] = None,
) -> Tuple[bool, str, Optional[Task]]:
    task = db.query(Task).filter(Task.task_id == task_id).first()
    if not task:
        return False, "Task not found", None

    # Calculate next event sequence number
    last_event = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.seq.desc())
        .first()
    )
    next_seq = (last_event.seq + 1) if last_event else 1

    event = TaskEvent(
        event_id=str(uuid.uuid4()),
        task_id=task_id,
        seq=next_seq,
        phase=phase,
        device_timestamp=device_timestamp,
        server_timestamp=utc_now(),
        error_code=error_code,
        error_detail=error_detail,
    )
    db.add(event)

    # Monotonic transition rules
    # Status progression: queued -> received -> launch_requested
    # Terminal/Non-regressible states: expired, superseded, failed
    if task.status in ["expired", "superseded", "failed"]:
        db.commit()
        return False, f"Task is already {task.status}; state not changed", task

    if task.status == "received" and phase == "received":
        db.commit()
        return True, "Task already received; duplicate ack recorded", task

    if task.status == "launch_requested":
        if phase == "received":
            db.commit()
            return False, "Task is already launch_requested; ignoring duplicate received ack", task
        if phase == "failed":
            task.status = "failed"
            task.error_message = error_detail or error_code
            db.commit()
            return True, "Task updated to failed", task
        # duplicate launch_requested
        db.commit()
        return True, "Task already launch_requested; ack recorded", task

    if phase == "received":
        task.status = "received"
    elif phase == "launch_requested":
        task.status = "launch_requested"
    elif phase == "failed":
        task.status = "failed"
        task.error_message = error_detail or error_code

    db.commit()
    db.refresh(task)
    return True, f"Ack recorded, status is now {task.status}", task
