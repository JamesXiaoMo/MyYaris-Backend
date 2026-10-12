from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class NeteaseSongItem(BaseModel):
    id: int = Field(..., description="Netease song ID")
    title: str = Field(..., description="Song title")
    artist: str = Field(..., description="Joined artist names")
    album: Optional[str] = Field(None, description="Album name")
    duration_ms: int = Field(0, description="Duration in milliseconds")
    cover_url: Optional[str] = Field(None, description="Album cover image URL")
    cached: bool = Field(False, description="Whether currently cached on local R9000P disk")
    stream_url: str = Field(..., description="Direct or proxy stream URL")


class NeteaseSearchResponse(BaseModel):
    code: int = 200
    total: int = 0
    songs: List[NeteaseSongItem] = []


class NeteaseIntelligenceRequest(BaseModel):
    seed_song_id: int = Field(..., description="Seed song ID to generate recommendations from")
    playlist_id: Optional[int] = Field(None, description="Optional playlist ID")
    count: Optional[int] = Field(30, ge=1, le=100, description="Number of songs to recommend")


class NeteaseIntelligenceResponse(BaseModel):
    code: int = 200
    mode: str = Field("intelligence", description="intelligence or simi_fallback")
    songs: List[NeteaseSongItem] = []


class NeteaseSongUrlResponse(BaseModel):
    code: int = 200
    song_id: int
    url: Optional[str] = None
    br: Optional[int] = None
    size_bytes: Optional[int] = None
    type: Optional[str] = None
    cached: bool = False


class NeteaseLyricResponse(BaseModel):
    code: int = 200
    song_id: int
    lrc: Optional[str] = None
    tlyric: Optional[str] = None


class CacheStatsResponse(BaseModel):
    cache_dir: str
    total_files: int
    total_bytes: int
    max_bytes: int
    usage_percentage: float
    tracks_in_db: int


class NeteaseQRKeyResponse(BaseModel):
    code: int = 200
    unikey: str


class NeteaseQRCreateResponse(BaseModel):
    code: int = 200
    qrurl: str
    qrimg: str


class NeteaseQRCheckResponse(BaseModel):
    code: int
    status: str
    message: str
    cookie_saved: bool = False
    nickname: Optional[str] = None


class NeteaseLoginStatusResponse(BaseModel):
    code: int = 200
    logged_in: bool
    user_id: Optional[int] = None
    nickname: Optional[str] = None
    avatar_url: Optional[str] = None
    vip_type: Optional[int] = None
    cookie_updated_at: Optional[str] = None
