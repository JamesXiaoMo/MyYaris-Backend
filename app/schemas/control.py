from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class MusicCommandParams(BaseModel):
    track_id: Optional[str] = None
    stream_url: Optional[str] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    seek_ms: Optional[int] = None
    loop_mode: Optional[str] = None  # all | one | off
    shuffle: Optional[bool] = None
    volume: Optional[float] = None


class MusicCommandRequest(BaseModel):
    command_id: Optional[str] = Field(None, description="Optional caller command ID or UUID")
    device_id: Optional[str] = Field(None, description="Target vehicle device ID")
    action: str = Field(..., description="Action: play_track, pause, resume, next, prev, seek, set_loop, set_shuffle, set_volume")
    params: Optional[MusicCommandParams] = Field(default_factory=MusicCommandParams)


class MusicCommandResponse(BaseModel):
    status: str = "ok"
    command_id: str
    device_id: str
    action: str
    params: Optional[Dict[str, Any]] = None
    queued_at: str
    dispatched_to_ws: bool = False


class CarStatusReportRequest(BaseModel):
    device_id: Optional[str] = Field(None, description="Device reporting status")
    playback_state: str = Field("stopped", description="playing | paused | stopped")
    current_track_id: Optional[str] = None
    position_ms: int = Field(0, ge=0)
    duration_ms: Optional[int] = Field(0, ge=0)
    software_volume: Optional[float] = Field(1.0, ge=0.0, le=1.0)
    loop_mode: Optional[str] = Field("all", description="all | one | off")
    shuffle: Optional[bool] = Field(False)
    updated_at: Optional[int] = Field(None, description="Client unix timestamp in ms")


class CarStatusResponse(BaseModel):
    device_id: str
    playback_state: str
    current_track_id: Optional[str] = None
    position_ms: int = 0
    duration_ms: int = 0
    software_volume: float = 1.0
    loop_mode: str = "all"
    shuffle_enabled: bool = False
    last_heartbeat_at: str
    updated_at: str
