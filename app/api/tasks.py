from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session
from app.api.auth import get_current_user, require_car_or_admin
from app.config import settings
from app.repositories.database import get_db
from app.repositories.models import Task
from app.schemas.auth import AuthenticatedUser
from app.schemas.task import TaskAckRequest, TaskAckResponse, TaskOut
from app.services.task_service import get_next_task_long_poll, record_task_ack

router = APIRouter(tags=["Tasks"])


def _to_task_out(task: Task) -> TaskOut:
    return TaskOut(
        task_id=task.task_id,
        device_id=task.device_id,
        title=task.title,
        address=task.address,
        latitude=task.latitude,
        longitude=task.longitude,
        coord_system=task.coord_system,
        status=task.status,
        created_at=task.created_at.isoformat(),
        expires_at=task.expires_at.isoformat(),
        delivery_attempts=task.delivery_attempts,
        error_message=task.error_message,
    )


@router.get("/devices/{id}/tasks/next", response_model=Optional[TaskOut])
async def get_next_task(
    id: str,
    timeout: Optional[int] = Query(default=None, ge=1, le=60),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
):
    # Car client can only poll its assigned device
    if user.role == "car" and user.device_id and user.device_id != id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: Car token cannot poll for device {id}",
        )

    task = await get_next_task_long_poll(db, device_id=id, timeout_seconds=timeout)
    if not task:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return _to_task_out(task)


@router.get("/api/v1/tasks/poll", response_model=Optional[TaskOut])
async def poll_tasks(
    device_id: Optional[str] = Query(default=None),
    timeout: Optional[int] = Query(default=None, ge=1, le=60),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
):
    dev_id = device_id or user.device_id or settings.DEFAULT_CAR_DEVICE_ID
    return await get_next_task(id=dev_id, timeout=timeout, db=db, user=user)


@router.post("/tasks/{id}/ack", response_model=TaskAckResponse)
@router.post("/api/v1/tasks/{id}/ack", response_model=TaskAckResponse)
def acknowledge_task(
    id: str,
    ack: TaskAckRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
):
    task_obj = db.query(Task).filter(Task.task_id == id).first()
    if not task_obj:
        raise HTTPException(status_code=404, detail="Task not found.")

    # Prevent car client from acknowledging other devices' tasks
    if user.role == "car" and user.device_id and task_obj.device_id != user.device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: Car token cannot acknowledge tasks for device {task_obj.device_id}",
        )

    dt = None
    if ack.device_timestamp:
        try:
            dt = datetime.fromisoformat(ack.device_timestamp)
        except ValueError:
            pass

    accepted, message, task = record_task_ack(
        db=db,
        task_id=id,
        phase=ack.phase.value,
        device_timestamp=dt,
        error_code=ack.error_code,
        error_detail=ack.error_detail,
    )

    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

    return TaskAckResponse(
        task_id=task.task_id,
        status=task.status,
        accepted=accepted,
        message=message,
    )


@router.get("/tasks/{id}", response_model=TaskOut)
def get_task(
    id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    task = db.query(Task).filter(Task.task_id == id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

    # Car client can only view its own device's tasks
    if user.role == "car" and user.device_id and task.device_id != user.device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Car token cannot view tasks for other devices.",
        )

    return _to_task_out(task)
