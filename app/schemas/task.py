from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RECEIVED = "received"
    LAUNCH_REQUESTED = "launch_requested"
    FAILED = "failed"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class TaskAckPhase(str, Enum):
    RECEIVED = "received"
    LAUNCH_REQUESTED = "launch_requested"
    FAILED = "failed"


class TaskOut(BaseModel):
    task_id: str
    device_id: str
    title: str
    address: Optional[str] = None
    latitude: float
    longitude: float
    coord_system: str = "wgs84"
    status: str
    created_at: str
    expires_at: str
    delivery_attempts: int = 0
    error_message: Optional[str] = None


class TaskAckRequest(BaseModel):
    phase: TaskAckPhase
    device_timestamp: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None


class TaskAckResponse(BaseModel):
    task_id: str
    status: str
    accepted: bool
    message: str
