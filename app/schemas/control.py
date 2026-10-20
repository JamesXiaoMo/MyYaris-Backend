from datetime import datetime
from typing import Any, Dict, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MusicCommandParams(BaseModel):
    model_config = ConfigDict(extra="allow")

    track_id: Optional[str] = None
    stream_url: Optional[str] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    source: Optional[str] = None
    song_id: Optional[Union[int, str]] = None
    seek_ms: Optional[int] = None
    seek_position_ms: Optional[int] = None
    position_ms: Optional[int] = None
    loop_mode: Optional[str] = None  # all | one | off
    shuffle: Optional[bool] = None
    volume: Optional[float] = None
    mode: Optional[str] = None  # list_loop | shuffle | intelligence | single_loop
    seed_song_id: Optional[int] = None
    seed_track_id: Optional[str] = None
    expected_queue_id: Optional[str] = None
    expected_queue_version: Optional[int] = None
    queue_id: Optional[str] = None
    queue_version: Optional[int] = None
    queue_index: Optional[int] = None
    start_index: Optional[int] = None
    start_track_id: Optional[str] = None
    keep_playback_state: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_and_validate_seek(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        seek_vals = {}
        for key in ("seek_ms", "seek_position_ms", "position_ms"):
            val = data.get(key)
            if val is not None:
                try:
                    seek_vals[key] = int(val)
                except (ValueError, TypeError):
                    pass
        if len(seek_vals) > 1:
            unique_vals = set(seek_vals.values())
            if len(unique_vals) == 1:
                unified = next(iter(unique_vals))
                data["seek_ms"] = unified
                data["seek_position_ms"] = unified
                data["position_ms"] = unified
        elif len(seek_vals) == 1:
            unified = next(iter(seek_vals.values()))
            data["seek_ms"] = unified
            data["seek_position_ms"] = unified
            data["position_ms"] = unified
        return data


class MusicCommandRequest(BaseModel):
    command_id: Optional[str] = Field(None, description="Optional caller command ID or UUID")
    device_id: Optional[str] = Field(None, description="Target vehicle device ID")
    action: str = Field(..., description="Action: play_track, pause, resume, stop, next, prev, seek, set_loop, set_shuffle, set_volume, set_playback_mode, switch_queue")
    params: Optional[MusicCommandParams] = Field(default_factory=MusicCommandParams)
    expires_in_seconds: Optional[int] = Field(None, ge=5, le=300, description="Optional command TTL in seconds")


class MusicCommandResponse(BaseModel):
    status: str = "ok"  # "ok" for client backward compatibility
    lifecycle_status: str = "queued"  # queued | sent | received | applied | failed | expired
    command_id: str
    device_id: str
    action: str
    params: Optional[Dict[str, Any]] = None
    queued_at: str
    dispatched_to_ws: bool = False
    expires_at: Optional[str] = None


class MusicCommandAckRequest(BaseModel):
    current_track_id: Optional[str] = None
    status: str = Field(..., description="received | applied | executed | failed")
    applied_mode: Optional[str] = None
    queue_version: Optional[int] = None
    current_index: Optional[int] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    device_timestamp: Optional[str] = None


class MusicCommandAckResponse(BaseModel):
    command_id: str
    status: str  # received | applied | failed
    accepted: bool = True
    message: Optional[str] = None


class MusicCommandDetailResponse(BaseModel):
    command_id: str
    device_id: str
    action: str
    params: Optional[Dict[str, Any]] = None
    status: str  # queued | sent | received | applied | failed | expired
    sender_role: str = "phone"
    dispatched_channel: Optional[str] = None
    delivery_attempts: int = 0
    created_at: str
    expires_at: str
    sent_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None


class CarStatusReportRequest(BaseModel):
    device_id: Optional[str] = Field(None, description="Device reporting status")
    playback_state: str = Field("stopped", description="playing | paused | stopped")
    current_track_id: Optional[str] = None
    position_ms: int = Field(0, ge=0)
    duration_ms: Optional[int] = Field(0, ge=0)
    software_volume: Optional[float] = Field(1.0, ge=0.0, le=1.0)
    loop_mode: Optional[str] = Field("all", description="all | one | off")
    shuffle: Optional[bool] = Field(False)
    applied_mode: Optional[str] = None
    requested_mode: Optional[str] = None
    queue_id: Optional[str] = None
    queue_version: Optional[int] = None
    queue_source: Optional[str] = None
    current_index: Optional[int] = None
    consecutive_failures: Optional[int] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    capabilities: Optional[list[str]] = None
    status_seq: Optional[int] = None
    last_applied_command_id: Optional[str] = None
    updated_at: Optional[Union[int, str]] = Field(None, description="Client unix timestamp in ms or ISO string")


class CarStatusResponse(BaseModel):
    device_id: str
    playback_state: str
    current_track_id: Optional[str] = None
    position_ms: int = 0
    duration_ms: int = 0
    software_volume: float = 1.0
    loop_mode: str = "all"
    shuffle_enabled: bool = False
    applied_mode: Optional[str] = "list_loop"
    requested_mode: Optional[str] = "list_loop"
    queue_id: Optional[str] = None
    queue_version: Optional[int] = 1
    queue_source: Optional[str] = "netease_favorite"
    current_index: Optional[int] = 0
    consecutive_failures: Optional[int] = 0
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    capabilities: list[str] = []
    status_seq: int = 0
    last_applied_command_id: Optional[str] = None
    last_heartbeat_at: str
    updated_at: str
