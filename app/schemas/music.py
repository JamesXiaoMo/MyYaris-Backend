from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict


class TrackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    track_id: str
    source: str
    title: str
    artist: str
    album: Optional[str] = None
    duration_ms: int
    file_size_bytes: int
    bitrate_kbps: Optional[int] = None
    cover_url: Optional[str] = None
    hit_count: int = 0
    stream_url: Optional[str] = None
    created_at: datetime
    last_accessed_at: datetime


class TrackListResponse(BaseModel):
    total: int
    tracks: List[TrackResponse]


class ScanResultResponse(BaseModel):
    status: str = "ok"
    scanned_count: int
    added_count: int
    updated_count: int
    total_tracks: int
