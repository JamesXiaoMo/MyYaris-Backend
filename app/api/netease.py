import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
import httpx
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.config import settings
from app.repositories.database import SessionLocal, get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.netease import (
    CacheStatsResponse,
    NeteaseIntelligenceRequest,
    NeteaseIntelligenceResponse,
    NeteaseLyricResponse,
    NeteaseSearchResponse,
    NeteaseSongItem,
    NeteaseSongUrlResponse,
)
from app.services.media_service import (
    RangeNotSatisfiableError,
    file_chunk_generator,
    get_audio_mime_type,
    parse_range_header,
    record_track_access,
)
from app.services.netease_service import cache_manager, netease_service

router = APIRouter(prefix="/api/v1/music", tags=["Netease Cloud Music & Caching"])


@router.get("/netease/search", response_model=NeteaseSearchResponse)
async def search_netease_songs(
    request: Request,
    keyword: str = Query(..., min_length=1, description="Search keyword"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Search Netease Cloud Music library with metadata cleaning and local cache status."""
    res = await netease_service.search(keyword=keyword, page=page, limit=limit)
    base_url = str(request.base_url).rstrip("/")

    items = []
    for s in res.get("songs", []):
        s_id = s["id"]
        cached = cache_manager.get_cached_track(db, s_id) is not None
        stream_url = f"{base_url}/api/v1/music/netease/stream/{s_id}"
        items.append(
            NeteaseSongItem(
                id=s_id,
                title=s["title"],
                artist=s["artist"],
                album=s.get("album"),
                duration_ms=s.get("duration_ms", 0),
                cover_url=s.get("cover_url"),
                cached=cached,
                stream_url=stream_url,
            )
        )

    return NeteaseSearchResponse(
        code=200,
        total=res.get("total", len(items)),
        songs=items,
    )


@router.post("/netease/intelligence", response_model=NeteaseIntelligenceResponse)
async def generate_intelligence_queue(
    req: NeteaseIntelligenceRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Generate intelligence mode (心动模式) continuous playback queue.
    Automatically falls back to similar songs if account login is not yet configured.
    """
    mode, songs = await netease_service.get_intelligence(
        seed_song_id=req.seed_song_id,
        playlist_id=req.playlist_id,
        count=req.count or 30,
    )
    base_url = str(request.base_url).rstrip("/")

    items = []
    for s in songs:
        s_id = s["id"]
        cached = cache_manager.get_cached_track(db, s_id) is not None
        stream_url = f"{base_url}/api/v1/music/netease/stream/{s_id}"
        items.append(
            NeteaseSongItem(
                id=s_id,
                title=s["title"],
                artist=s["artist"],
                album=s.get("album"),
                duration_ms=s.get("duration_ms", 0),
                cover_url=s.get("cover_url"),
                cached=cached,
                stream_url=stream_url,
            )
        )

    return NeteaseIntelligenceResponse(
        code=200,
        mode=mode,
        songs=items,
    )


@router.get("/netease/song_url", response_model=NeteaseSongUrlResponse)
async def get_netease_song_url(
    song_id: int = Query(..., description="Netease song ID"),
    level: str = Query("exhigh", description="standard | higher | exhigh | lossless"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get CDN stream URL for a given song."""
    cached_track = cache_manager.get_cached_track(db, song_id)
    if cached_track:
        return NeteaseSongUrlResponse(
            code=200,
            song_id=song_id,
            url=f"/api/v1/music/netease/stream/{song_id}",
            br=cached_track.bitrate_kbps or 320,
            size_bytes=cached_track.file_size_bytes,
            type="mp3",
            cached=True,
        )

    data = await netease_service.get_song_url(song_id=song_id, level=level)
    return NeteaseSongUrlResponse(
        code=200,
        song_id=song_id,
        url=data.get("url"),
        br=data.get("br"),
        size_bytes=data.get("size_bytes"),
        type=data.get("type", "mp3"),
        cached=False,
    )


@router.get("/netease/lyric", response_model=NeteaseLyricResponse)
async def get_netease_lyric(
    song_id: int = Query(..., description="Netease song ID"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Fetch lyrics and translation for song."""
    data = await netease_service.get_lyric(song_id=song_id)
    return NeteaseLyricResponse(
        code=200,
        song_id=song_id,
        lrc=data.get("lrc"),
        tlyric=data.get("tlyric"),
    )


@router.get("/netease/stream/{song_id}")
async def stream_netease_audio(
    song_id: int,
    background_tasks: BackgroundTasks,
    range: Optional[str] = Header(None, alias="Range"),
    db: Session = Depends(get_db),
):
    """
    Dynamic 'stream-and-cache' streaming endpoint supporting HTTP 206 Partial Content.
    - If song is cached on R9000P SSD, serves directly with instant seek and zero network cost.
    - If uncached, streams from Netease CDN while asynchronously piping into local cache.
    """
    cached_track = cache_manager.get_cached_track(db, song_id)

    # 1. Cache HIT: Serve from local disk via HTTP 206 Range generator
    if cached_track:
        file_path = Path(cached_track.file_path)
        file_size = file_path.stat().st_size
        mime_type = get_audio_mime_type(str(file_path))

        try:
            start, end = parse_range_header(range, file_size)
        except RangeNotSatisfiableError:
            return Response(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
            )

        record_track_access(db, cached_track)

        if start is not None and end is not None:
            content_length = (end - start) + 1
            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Content-Type": mime_type,
                "Cache-Control": "no-cache",
                "X-Cache-Status": "HIT",
            }
            return StreamingResponse(
                file_chunk_generator(str(file_path), start, end),
                status_code=status.HTTP_206_PARTIAL_CONTENT,
                headers=headers,
                media_type=mime_type,
            )
        else:
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Content-Type": mime_type,
                "Cache-Control": "no-cache",
                "X-Cache-Status": "HIT",
            }
            return StreamingResponse(
                file_chunk_generator(str(file_path), 0, file_size - 1),
                status_code=status.HTTP_200_OK,
                headers=headers,
                media_type=mime_type,
            )

    # 2. Cache MISS: Fetch remote CDN url
    url_info = await netease_service.get_song_url(song_id)
    cdn_url = url_info.get("url")
    if not cdn_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audio stream not available for song {song_id} (VIP copyright or unavailable)",
        )

    # If no Range or Range starts at 0, perform dynamic Tee streaming (stream + cache)
    is_full_or_start_range = (range is None or range.strip() in ("", "bytes=0-"))

    if is_full_or_start_range:
        metadata = {
            "title": f"Netease Track {song_id}",
            "artist": "Netease Cloud Music",
            "type": url_info.get("type", "mp3"),
            "bitrate_kbps": (url_info.get("br") or 320000) // 1000,
        }
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache",
            "X-Cache-Status": "MISS-CACHING",
        }
        return StreamingResponse(
            cache_manager.tee_stream_and_cache(song_id, cdn_url, metadata, SessionLocal),
            status_code=status.HTTP_200_OK,
            headers=headers,
            media_type="audio/mpeg",
        )

    # Otherwise forward Range request to CDN and schedule background full cache
    forward_headers = {"Range": range} if range else {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        cdn_resp = await client.get(cdn_url, headers=forward_headers)
        resp_status = cdn_resp.status_code
        resp_headers = dict(cdn_resp.headers)
        resp_headers["X-Cache-Status"] = "MISS-PROXY"

        return Response(
            content=cdn_resp.content,
            status_code=resp_status,
            headers=resp_headers,
            media_type=cdn_resp.headers.get("content-type", "audio/mpeg"),
        )


@router.get("/cache/stats", response_model=CacheStatsResponse)
def get_media_cache_stats(
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Retrieve current cache disk usage and file statistics."""
    stats = cache_manager.get_cache_stats(db)
    return CacheStatsResponse(**stats)


@router.post("/cache/clean")
def trigger_cache_cleanup(
    target_bytes: Optional[int] = Query(None, description="Max cache size limit to enforce"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Manually invoke LRU cache quota eviction."""
    evicted_count, evicted_bytes = cache_manager.enforce_lru_quota(db, max_bytes=target_bytes)
    return {
        "status": "ok",
        "evicted_count": evicted_count,
        "evicted_bytes": evicted_bytes,
        "remaining_stats": cache_manager.get_cache_stats(db),
    }
