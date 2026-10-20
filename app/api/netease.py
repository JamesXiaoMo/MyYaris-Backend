from app.services.control_service import get_car_status_by_id
import asyncio
from datetime import datetime, timezone
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

from app.api.auth import (
    get_current_user,
    require_admin,
    require_admin_or_legacy_phone,
    require_owner_or_car,
    verify_csrf,
)
from app.services.device_service import is_same_device, resolve_car_device_id
from app.config import settings
from app.repositories.database import SessionLocal, get_db
from app.repositories.models import utc_now
from app.schemas.auth import AuthenticatedUser
from app.schemas.netease import (
    CacheStatsResponse,
    NeteaseCookieLoginRequest,
    NeteaseFavoritePlaylistResponse,
    NeteaseFavoriteSyncRequest,
    NeteaseFavoriteSyncResponse,
    NeteaseIntelligenceRequest,
    NeteaseIntelligenceResponse,
    NeteaseLoginStatusResponse,
    NeteaseLyricResponse,
    NeteaseQRCheckResponse,
    NeteaseQRCreateResponse,
    NeteaseQRKeyResponse,
    NeteaseQueueTrackItem,
    NeteaseSearchResponse,
    NeteaseSongItem,
    NeteaseSongUrlResponse,
)
from app.services.control_service import music_queue_snapshot_manager
from app.services.media_service import (
    RangeNotSatisfiableError,
    file_chunk_generator,
    get_audio_mime_type,
    is_valid_audio_magic,
    parse_range_header,
    record_track_access,
)
from app.services.favorite_catalog import FavoriteRevisionGone
from app.services.netease_service import (
    NeteaseUpstreamError,
    cache_manager,
    favorite_catalog,
    netease_service,
)

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
    try:
        res = await netease_service.search(keyword=keyword, page=page, limit=limit)
    except NeteaseUpstreamError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Netease upstream search failed: {e.message}",
        )
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
                fee=s.get("fee", 0),
                playable=s.get("playable", True),
            )
        )

    return NeteaseSearchResponse(
        code=200,
        total=res.get("total", len(items)),
        songs=items,
    )


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _favorite_http_error(e: Exception) -> HTTPException:
    if isinstance(e, PermissionError):
        # Backend NetEase session missing/expired: an auth problem, not "offline".
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    if isinstance(e, FavoriteRevisionGone):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": 409,
                "error_code": "FAVORITE_REVISION_CONFLICT",
                "error_detail": str(e),
                "current_revision": e.current,
                "message": "喜欢歌单版本已更新，请从第一页重新拉取",
            },
        )
    assert isinstance(e, NeteaseUpstreamError)
    return HTTPException(status_code=e.status_code, detail=f"Netease upstream error: {e.message}")


@router.get("/netease/playlist/favorite", response_model=NeteaseFavoritePlaylistResponse)
async def get_netease_favorite_playlist(
    request: Request,
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(30, ge=1, le=100, description="Items per page (default 30, max 100)"),
    revision: Optional[int] = Query(None, ge=1, description="Pin paging to one catalog revision (from a previous response)"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """
    Paginates the current NetEase account's default favorite music playlist (我喜欢的音乐)
    from the backend's bounded metadata catalog.
    - Strictly restricted to MyYaris Administrator (or authorized legacy phone token for transitional compatibility).
    - Standard registered users (role='user') and vehicle devices (role='car') are forbidden (403).
    - Unauthenticated requests are rejected (401).
    - Returns HTTP 409 Conflict if backend NetEase account is not logged in/expired (never served from cache).
    - System favorite playlist is identified via specialType==5 and verified Owner UID match.
    - Paging is served from one immutable revision; pass ?revision=R from the first page to pin it
      (409 FAVORITE_REVISION_CONFLICT means: restart from page 1).
    - Upstream unreachable -> last verified catalog of the same Owner with stale=true; no catalog -> 502/504.
    - Does NOT pre-download, cache, or probe audio streams for all playlist tracks.
    """
    try:
        data = await netease_service.get_favorite_catalog_page(offset=offset, limit=limit, revision=revision)
    except (PermissionError, FavoriteRevisionGone, NeteaseUpstreamError) as e:
        raise _favorite_http_error(e)

    base_url = str(request.base_url).rstrip("/")
    page_songs = data.get("songs", [])
    cached_ids = cache_manager.get_cached_ids(db, [s["id"] for s in page_songs])
    items = []
    for s in page_songs:
        s_id = s["id"]
        stream_url = f"{base_url}/api/v1/music/netease/stream/{s_id}"
        items.append(
            NeteaseSongItem(
                id=s_id,
                song_id=s_id,
                title=s["title"],
                artist=s["artist"],
                album=s.get("album"),
                duration_ms=s.get("duration_ms", 0),
                cover_url=s.get("cover_url"),
                cached=s_id in cached_ids,
                stream_url=stream_url,
                fee=s.get("fee", 0),
                playable=s.get("playable", True),
                source="netease",
            )
        )

    return NeteaseFavoritePlaylistResponse(
        code=200,
        playlist_id=data["playlist_id"],
        name=data["name"],
        cover_url=data.get("cover_url"),
        track_count=data["track_count"],
        play_count=data.get("play_count"),
        total=data["total"],
        offset=data["offset"],
        limit=data["limit"],
        has_more=data["has_more"],
        songs=items,
        tracks=items,
        revision=data.get("revision"),
        etag=data.get("etag"),
        owner_key=data.get("owner_key"),
        synced_at=_iso(data.get("synced_at")),
        checked_at=_iso(data.get("checked_at")),
        stale=bool(data.get("stale")),
        stale_reason=data.get("stale_reason"),
    )


@router.post("/netease/playlist/favorite/sync", response_model=NeteaseFavoriteSyncResponse)
async def sync_netease_favorite_playlist(
    request: Request,
    body: NeteaseFavoriteSyncRequest = NeteaseFavoriteSyncRequest(),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """
    Checks the Owner favorite playlist for additions/removals/reorders and publishes a new immutable
    catalog revision only when something changed. Reads upstream metadata only (no like/unlike).
    Concurrent calls share one upstream pass; an unchanged playlist costs two small upstream calls
    (never the multi-megabyte detail). Errors are reported honestly (no stale success here):
    409 = NetEase session/Owner unprovable, 502/504 = upstream unreachable.
    """
    verify_csrf(request, user)
    try:
        outcome = await favorite_catalog.sync(force=body.force)
    except (PermissionError, NeteaseUpstreamError) as e:
        raise _favorite_http_error(e)
    snap = outcome.snapshot
    return NeteaseFavoriteSyncResponse(
        status=outcome.status,
        changed=body.known_revision != snap.revision,
        revision=snap.revision,
        etag=snap.etag,
        owner_key=snap.owner_key,
        playlist_id=snap.playlist_id,
        name=snap.name,
        total=snap.total,
        added=outcome.added,
        removed=outcome.removed,
        reordered=outcome.reordered,
        updated=outcome.updated,
        synced_at=_iso(snap.updated_at) or "",
        checked_at=_iso(outcome.checked_at) or "",
        mode=outcome.upstream_mode,
    )


@router.post("/netease/intelligence", response_model=NeteaseIntelligenceResponse)
async def generate_intelligence_queue(
    req: NeteaseIntelligenceRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_owner_or_car),
):
    target = resolve_car_device_id(req.device_id, user.device_id)
    async with music_queue_snapshot_manager.operation_lock(target):
        return await _generate_intelligence_queue(req, request, db, user)


async def _generate_intelligence_queue(req, request, db, user):
    verify_csrf(request, user)
    """
    Generate authentic intelligence mode (心动模式) continuous playback queue from upstream.
    Initial seed song (batch_index == 0) must belong to the owner's favorite playlist.
    Replenishment (batch_index > 0) appends to the active queue with stable indices.
    """
    target_device = resolve_car_device_id(req.device_id, user.device_id)

    # Device ownership validation for car clients
    if user.role == "car":
        if not user.device_id or not is_same_device(user.device_id, target_device):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Car token cannot access music queue of other devices.",
            )

    batch_idx = req.batch_index or 0

    # Replay before touching upstream or evaluating a now-advanced batch.
    cached = music_queue_snapshot_manager._replenish_cache.get((target_device, req.request_id)) if req.request_id else None
    if cached:
        original = music_queue_snapshot_manager._intelligence_request_payloads.get((target_device, req.request_id))
        if original is not None and original != req.model_dump():
            raise HTTPException(409, detail={"error_code": "INTELLIGENCE_REQUEST_CONFLICT"})
        return NeteaseIntelligenceResponse(mode="intelligence", tracks=[NeteaseQueueTrackItem(**t) for t in cached.tracks],
                                          queue_id=cached.queue_id, queue_version=cached.queue_version, batch_index=batch_idx)

    # 1. Seed qualification check
    if batch_idx == 0:
        try:
            is_in_fav = await netease_service.is_song_in_favorite_playlist(req.seed_song_id)
            if not is_in_fav:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "code": 422,
                        "error_code": "INVALID_SEED_SONG",
                        "error_detail": "Initial intelligence seed song must belong to owner favorite playlist",
                        "message": "心动模式初始种子曲目必须属于我喜欢的音乐",
                    },
                )
        except HTTPException:
            raise
        except PermissionError as pe:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": 409,
                    "error_code": "INTELLIGENCE_AUTH_INVALID",
                    "error_detail": str(pe),
                    "message": "网易云登录凭证失效，心动模式不可用",
                },
            )
        except NeteaseUpstreamError as nue:
            raise HTTPException(
                status_code=nue.status_code,
                detail={
                    "code": nue.status_code,
                    "error_code": "INTELLIGENCE_UPSTREAM_FAILED",
                    "error_detail": nue.message,
                    "message": "上游网易云服务请求异常",
                },
            )
    else:
        # Replenishment seed song must belong to active intelligence queue
        active_q = music_queue_snapshot_manager.get_active_queue(target_device)
        if not active_q or active_q.source != "netease_intelligence":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": 409,
                    "error_code": "QUEUE_NOT_READY",
                    "error_detail": "No active intelligence queue found for replenishment",
                    "message": "当前未处于心动模式队列，无法续批",
                },
            )
        if (req.queue_id is not None and req.queue_id != active_q.queue_id) or (req.queue_version is not None and req.queue_version != active_q.queue_version):
            raise HTTPException(409, detail={"error_code": "QUEUE_VERSION_CONFLICT"})
        if batch_idx != active_q.batch_count:
            raise HTTPException(409, detail={"error_code": "INTELLIGENCE_BATCH_CONFLICT"})
        if active_q.batch_count >= 5 or active_q.total >= 150:
            raise HTTPException(503, detail={"error_code": "INTELLIGENCE_QUEUE_EXHAUSTED"})
        current = get_car_status_by_id(db, target_device)
        current_song = next((t["song_id"] for t in active_q.tracks if current and t["track_id"] == current.current_track_id), None)
        valid_seed_ids = {active_q.tracks[-1]["song_id"], current_song}
        if req.seed_song_id not in valid_seed_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": 422,
                    "error_code": "INVALID_SEED_SONG",
                    "error_detail": "Replenishment seed song must belong to active intelligence queue",
                    "message": "续批种子歌曲必须来自当前心动模式队列",
                },
            )

    # 2. Query upstream intelligence API
    try:
        mode, songs = await netease_service.get_intelligence(
            seed_song_id=req.seed_song_id,
            playlist_id=req.playlist_id,
            count=req.count or 30,
        )
    except PermissionError as pe:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": 409,
                "error_code": "INTELLIGENCE_AUTH_INVALID",
                "error_detail": str(pe),
                "message": "网易云登录凭证失效，心动模式不可用",
            },
        )
    except NeteaseUpstreamError as nue:
        err_code = "INTELLIGENCE_QUEUE_EXHAUSTED" if nue.status_code == 503 else "INTELLIGENCE_UPSTREAM_FAILED"
        msg = "心动模式推荐曲目已达上限" if nue.status_code == 503 else "上游网易云服务请求异常"
        raise HTTPException(
            status_code=nue.status_code,
            detail={
                "code": nue.status_code,
                "error_code": err_code,
                "error_detail": nue.message,
                "message": msg,
            },
        )

    if mode != "intelligence" or not songs:
        raise HTTPException(502, detail={"error_code": "INTELLIGENCE_UPSTREAM_FAILED", "message": "未获得真实心动队列"})

    if batch_idx > 0 and music_queue_snapshot_manager.get_active_queue(target_device) is not active_q:
        raise HTTPException(409, detail={"error_code": "QUEUE_VERSION_CONFLICT", "message": "续批期间实际队列已变化"})

    # 3. Update device queue snapshot
    if batch_idx == 0:
        snapshot = await music_queue_snapshot_manager.create_intelligence_queue(
            device_id=target_device,
            seed_song_id=req.seed_song_id,
            songs=songs,
            request_id=req.request_id,
            db=db,
            activate=False,
        )
    else:
        snapshot = await music_queue_snapshot_manager.append_intelligence_tracks(
            device_id=target_device,
            songs=songs,
            request_id=req.request_id,
            db=db,
        )

    items = []
    for s in songs:
        s_id = s["id"]
        cached = cache_manager.get_cached_track(db, s_id) is not None
        stream_url = f"/api/v1/music/netease/stream/{s_id}"
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
                fee=s.get("fee", 0),
                playable=None,
            )
        )

    if req.request_id:
        music_queue_snapshot_manager._intelligence_request_payloads[(target_device, req.request_id)] = req.model_dump()
    track_items = [NeteaseQueueTrackItem(**t) for t in snapshot.tracks]

    return NeteaseIntelligenceResponse(
        code=200,
        mode="intelligence",
        songs=items,
        tracks=track_items,
        queue_id=snapshot.queue_id,
        queue_version=snapshot.queue_version,
        batch_index=batch_idx,
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


@router.api_route("/netease/stream/{song_id}", methods=["GET", "HEAD"])
async def stream_netease_audio(
    song_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    range: Optional[str] = Header(None, alias="Range"),
    token: Optional[str] = Query(None, description="Optional authentication token"),
    db: Session = Depends(get_db),
):
    """
    Dynamic 'stream-and-cache' streaming endpoint supporting HTTP 206 Partial Content and HEAD probing.
    - If song is cached on local server SSD, serves directly with instant seek and zero network cost.
    - If uncached, streams from Netease CDN while asynchronously piping into local cache.
    - Strictly rejects non-audio error masquerades (HTML/JSON) with HTTP 502/404.
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
            if request.method == "HEAD":
                return Response(status_code=status.HTTP_206_PARTIAL_CONTENT, headers=headers)
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
            if request.method == "HEAD":
                return Response(status_code=status.HTTP_200_OK, headers=headers)
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

    metadata = {
        "title": f"Netease Track {song_id}",
        "artist": "Netease Cloud Music",
        "type": url_info.get("type", "mp3"),
        "bitrate_kbps": (url_info.get("br") or 320000) // 1000,
    }

    # HEAD request on uncached song
    if request.method == "HEAD":
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache",
            "X-Cache-Status": "MISS",
        }
        if url_info.get("size_bytes"):
            headers["Content-Length"] = str(url_info["size_bytes"])
        return Response(status_code=status.HTTP_200_OK, headers=headers)

    # If no Range or Range starts at 0, perform dynamic Tee streaming (stream + cache)
    is_full_or_start_range = (range is None or range.strip() in ("", "bytes=0-"))

    if is_full_or_start_range:
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
        try:
            cdn_resp = await client.get(cdn_url, headers=forward_headers)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to connect to upstream audio CDN: {e}",
            )

        if cdn_resp.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream audio CDN returned HTTP {cdn_resp.status_code}",
            )

        # Magic bytes validation for initial range chunks (e.g. bytes=0-1023)
        # to prevent 200 HTML masquerade / copyright error pages
        range_clean = (range or "").strip().lower()
        if range_clean.startswith("bytes=0-"):
            if not is_valid_audio_magic(cdn_resp.content):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Upstream CDN returned non-audio payload (masquerade/copyright restriction)",
                )

        # Trigger background caching so subsequent seeks hit local disk
        background_tasks.add_task(cache_manager.cache_track_background, song_id, cdn_url, metadata, SessionLocal)

        # Sanitize headers (strip hop-by-hop headers)
        clean_headers = {}
        for k, v in cdn_resp.headers.items():
            if k.lower() not in {
                "connection",
                "keep-alive",
                "transfer-encoding",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailers",
                "upgrade",
            }:
                clean_headers[k] = v
        clean_headers["X-Cache-Status"] = "MISS-PROXY"
        clean_headers["Accept-Ranges"] = "bytes"

        return Response(
            content=cdn_resp.content,
            status_code=cdn_resp.status_code,
            headers=clean_headers,
            media_type=clean_headers.get("content-type", "audio/mpeg"),
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
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Manually invoke LRU cache quota eviction (Admin or authorized phone)."""
    evicted_count, evicted_bytes = cache_manager.enforce_lru_quota(db, max_bytes=target_bytes)
    return {
        "status": "ok",
        "evicted_count": evicted_count,
        "evicted_bytes": evicted_bytes,
        "remaining_stats": cache_manager.get_cache_stats(db),
    }


@router.get("/netease/login/qr/key", response_model=NeteaseQRKeyResponse)
async def get_login_qr_key(
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Generate unikey for Netease QR login session (Admin or authorized phone)."""
    key = await netease_service.get_qr_key()
    return NeteaseQRKeyResponse(code=200, unikey=key)


@router.get("/netease/login/qr/create", response_model=NeteaseQRCreateResponse)
async def create_login_qr(
    key: str = Query(..., description="Unikey from /login/qr/key"),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Create QR code image (Base64 PNG) and URL for scanning (Admin or authorized phone)."""
    data = await netease_service.create_qr(key=key)
    return NeteaseQRCreateResponse(
        code=200,
        qrurl=data.get("qrurl", ""),
        qrimg=data.get("qrimg", ""),
    )


@router.get("/netease/login/qr/check", response_model=NeteaseQRCheckResponse)
async def check_login_qr(
    key: str = Query(..., description="Unikey from /login/qr/key"),
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Poll QR code scan and authorization status (Admin or authorized phone)."""
    res = await netease_service.check_qr(key=key)
    return NeteaseQRCheckResponse(**res)


@router.get("/netease/login/status", response_model=NeteaseLoginStatusResponse)
async def get_login_status(
    user: AuthenticatedUser = Depends(get_current_user),
):
    """
    Check current user login status from stored cookie.
    Hides private account profile from standard registered users to protect administrator privacy.
    """
    info = netease_service.cookie_store.get_info()
    if not info.get("logged_in"):
        return NeteaseLoginStatusResponse(code=200, logged_in=False)

    # For standard registered users, only expose logged_in boolean to protect administrator privacy
    if user.role == "user" and not user.is_legacy_token:
        return NeteaseLoginStatusResponse(code=200, logged_in=True)

    profile = info.get("profile") or {}
    return NeteaseLoginStatusResponse(
        code=200,
        logged_in=True,
        user_id=profile.get("user_id"),
        nickname=profile.get("nickname"),
        avatar_url=profile.get("avatar_url"),
        vip_type=profile.get("vip_type"),
        cookie_updated_at=info.get("updated_at"),
    )


@router.post("/netease/login/refresh")
async def refresh_login_cookie(
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Refresh and renew stored Netease login cookie (Admin or authorized phone)."""
    ok = await netease_service.refresh_cookie()
    return {"status": "ok" if ok else "failed", "refreshed": ok}


@router.post("/netease/login/logout")
async def logout_netease(
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Clear Netease login session and remove stored cookie (Admin or authorized phone)."""
    ok = await netease_service.logout()
    return {"status": "ok", "logged_out": ok}


@router.post("/netease/login/cookie", response_model=NeteaseLoginStatusResponse)
async def login_with_cookie(
    body: NeteaseCookieLoginRequest,
    user: AuthenticatedUser = Depends(require_admin_or_legacy_phone),
):
    """Directly save and authenticate with a provided Netease Cookie string (Admin or authorized phone)."""
    raw_cookie = body.cookie.strip()
    if not raw_cookie.startswith("MUSIC_U=") and "=" not in raw_cookie:
        cookie_str = f"MUSIC_U={raw_cookie};"
    else:
        cookie_str = raw_cookie

    profile = await netease_service.get_profile_by_cookie(cookie_str)
    netease_service.cookie_store.save(cookie_str, profile)
    return NeteaseLoginStatusResponse(
        code=200,
        logged_in=True,
        user_id=profile.get("user_id") if profile else None,
        nickname=profile.get("nickname") if profile else None,
        avatar_url=profile.get("avatar_url") if profile else None,
        vip_type=profile.get("vip_type") if profile else None,
        cookie_updated_at=utc_now().isoformat(),
    )

