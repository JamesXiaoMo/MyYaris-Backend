from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field
from app.schemas.common import Coordinate


class DestinationInputType(str, Enum):
    AUTO = "auto"
    COORDS = "coords"
    PLUS_CODE = "plus_code"
    TEXT = "text"
    SHARE_LINK = "share_link"
    VOICE = "voice"


class DestinationCreateRequest(BaseModel):
    input_type: DestinationInputType
    coordinate: Optional[Coordinate] = None
    plus_code: Optional[str] = Field(default=None, max_length=256)
    reference_coordinate: Optional[Coordinate] = None
    text: Optional[str] = None
    url: Optional[str] = None
    idempotency_key: Optional[str] = None
    client_lat: Optional[float] = None
    client_lon: Optional[float] = None
    client_time: Optional[str] = None
    target_device_id: Optional[str] = "car_p3pro"
    auto_create_task: bool = True
    purpose: Optional[str] = None


class CandidateOut(BaseModel):
    candidate_id: str
    title: str
    address: Optional[str] = None
    latitude: float
    longitude: float
    coord_system: str = "wgs84"
    source: str = "manual"
    place_id: Optional[str] = None


class DestinationQueryResponse(BaseModel):
    request_id: str
    idempotency_key: Optional[str] = None
    source: str
    status: str  # resolving | needs_clarification | resolved | failed
    message: Optional[str] = None  # Human-readable status message for iPhone Shortcuts
    transcribed_text: Optional[str] = None
    candidates: List[CandidateOut] = []
    clarification_question: Optional[str] = None
    task_id: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str


class DestinationConfirmRequest(BaseModel):
    candidate_id: str
    target_device_id: Optional[str] = "car_p3pro"
