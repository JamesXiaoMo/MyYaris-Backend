from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class NeteaseSongItem(BaseModel):
    id: int = Field(..., description="Netease song ID")
    song_id: Optional[int] = Field(None, description="Alias for song ID for client compatibility")
    title: str = Field(..., description="Song title")
    artist: str = Field(..., description="Joined artist names")
    album: Optional[str] = Field(None, description="Album name")
    duration_ms: int = Field(0, description="Duration in milliseconds")
    cover_url: Optional[str] = Field(None, description="Album cover image URL")
    cached: bool = Field(False, description="Whether currently cached on local server disk")
    stream_url: str = Field(..., description="Direct or proxy stream URL")
    fee: Optional[int] = Field(0, description="0=free, 1=vip, 4=album, 8=pay")
    playable: Optional[bool] = Field(True, description="Whether playable without regional/copyright restriction")
    source: str = Field("netease", description="Music source identifier")


class NeteaseFavoritePlaylistResponse(BaseModel):
    code: int = 200
    playlist_id: int = Field(..., description="Unique favorite playlist ID")
    name: str = Field(..., description="Playlist display name")
    cover_url: Optional[str] = Field(None, description="Playlist cover image URL")
    track_count: int = Field(..., description="Total track count reported by playlist")
    play_count: Optional[int] = Field(None, description="Cumulative play count")
    total: int = Field(..., description="Total track count available")
    offset: int = Field(..., description="Pagination offset")
    limit: int = Field(..., description="Pagination page limit")
    has_more: bool = Field(..., description="Whether subsequent tracks exist")
    songs: List[NeteaseSongItem] = Field(default_factory=list, description="List of track items on this page")
    tracks: List[NeteaseSongItem] = Field(default_factory=list, description="Alias for songs compatible with client models")


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


class NeteaseCookieLoginRequest(BaseModel):
    cookie: str

