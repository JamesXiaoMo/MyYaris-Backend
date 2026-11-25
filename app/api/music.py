from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.repositories.database import get_db
from app.repositories.models import MediaTrack
from app.schemas.music import ScanResultResponse, TrackListResponse, TrackResponse
from app.services.media_service import (
    RangeNotSatisfiableError,
    file_chunk_generator,
    get_audio_mime_type,
    parse_range_header,
    record_track_access,
    scan_local_tracks,
)
from app.services.netease_service import favorite_catalog, netease_service

router = APIRouter(prefix="/api/v1/music", tags=["Music & Media Streaming"])


@router.post("/scan", response_model=ScanResultResponse)
def trigger_media_scan(
    scan_dir: Optional[str] = Query(None, description="Optional custom directory to scan"),
    db: Session = Depends(get_db),
):
    """Scan local media directory and index audio files into database."""
    res = scan_local_tracks(db, scan_dir=scan_dir)
    return ScanResultResponse(**res)


@router.get("/tracks", response_model=TrackListResponse)
def list_tracks(
    request: Request,
    keyword: Optional[str] = Query(None, description="Keyword search in title, artist, album"),
    artist: Optional[str] = Query(None, description="Filter by artist"),
    album: Optional[str] = Query(None, description="Filter by album"),
    source: Optional[str] = Query(None, description="Filter by source: local_repo | netease_cache"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """List indexed music tracks with filtering and pagination."""
    query = db.query(MediaTrack)

    if source and source.strip():
        query = query.filter(MediaTrack.source == source.strip())
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        query = query.filter(
            (MediaTrack.title.ilike(kw)) | (MediaTrack.artist.ilike(kw)) | (MediaTrack.album.ilike(kw))
        )
    if artist and artist.strip():
        query = query.filter(MediaTrack.artist.ilike(f"%{artist.strip()}%"))
    if album and album.strip():
        query = query.filter(MediaTrack.album.ilike(f"%{album.strip()}%"))

    total = query.count()
    tracks = (
        query.order_by(MediaTrack.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    base_url = str(request.base_url).rstrip("/")
    items = []
    for t in tracks:
        stream_url = f"{base_url}/api/v1/music/stream/{t.track_id}"
        item = TrackResponse(
            track_id=t.track_id,
            source=t.source,
            title=t.title,
            artist=t.artist,
            album=t.album,
            duration_ms=t.duration_ms,
            file_size_bytes=t.file_size_bytes,
            bitrate_kbps=t.bitrate_kbps,
            cover_url=t.cover_url,
            hit_count=t.hit_count,
            stream_url=stream_url,
            created_at=t.created_at,
            last_accessed_at=t.last_accessed_at,
        )
        items.append(item)

    return TrackListResponse(total=total, tracks=items)


def _is_placeholder_netease_meta(track: Optional[MediaTrack]) -> bool:
    if track is None:
        return True
    title = (track.title or "").strip()
    artist = (track.artist or "").strip()
    if not title or title.startswith("Netease Track ") or title.startswith("Track "):
        return True
    if artist in ("", "Netease Cloud Music", "Unknown Artist"):
        return True
    if not (track.cover_url or "").strip():
        return True
    return False


async def _resolve_netease_metadata(song_id: int) -> Optional[dict]:
    # 1. Fast in-memory check in favorite_catalog snapshot
    try:
        snap = getattr(favorite_catalog, "_snap", None)
        if snap is not None:
            for item in getattr(snap, "tracks", ()):
                if isinstance(item, dict) and item.get("song_id") == song_id:
                    title = (item.get("title") or "").strip()
                    if title:
                        return {
                            "title": title,
                            "artist": (item.get("artist") or "Unknown Artist").strip(),
                            "album": item.get("album"),
                            "duration_ms": int(item.get("duration_ms") or 0),
                            "cover_url": item.get("cover_url"),
                        }
    except Exception:
        pass

    # 2. Fetch live metadata from NetEase upstream /song/detail
    try:
        detail = await netease_service.get_song_detail(song_id)
        name = (detail.get("name") or "").strip()
        if name and name != f"Track {song_id}":
            return {
                "title": name,
                "artist": (detail.get("artist") or "Unknown Artist").strip(),
                "album": detail.get("album"),
                "duration_ms": int(detail.get("duration_ms") or 0),
                "cover_url": detail.get("cover_url"),
            }
    except Exception:
        pass
    return None


@router.get("/tracks/{track_id}", response_model=TrackResponse)
async def get_track_detail(
    track_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Get metadata details of a single track, auto-enriching placeholder NetEase entries."""
    track = db.query(MediaTrack).filter(MediaTrack.track_id == track_id).first()

    netease_song_id: Optional[int] = None
    if track_id.startswith("netease_"):
        try:
            netease_song_id = int(track_id.split("_", 1)[1])
        except ValueError:
            netease_song_id = None

    if netease_song_id is not None and _is_placeholder_netease_meta(track):
        resolved = await _resolve_netease_metadata(netease_song_id)
        if resolved:
            if track is not None:
                track.title = resolved["title"]
                track.artist = resolved["artist"]
                if resolved.get("album"):
                    track.album = resolved["album"]
                if resolved.get("duration_ms"):
                    track.duration_ms = resolved["duration_ms"]
                if resolved.get("cover_url"):
                    track.cover_url = resolved["cover_url"]
                db.commit()
                db.refresh(track)
            else:
                base_url = str(request.base_url).rstrip("/")
                now = datetime.now(timezone.utc)
                return TrackResponse(
                    track_id=track_id,
                    source="netease_cache",
                    title=resolved["title"],
                    artist=resolved["artist"],
                    album=resolved.get("album"),
                    duration_ms=resolved.get("duration_ms") or 0,
                    file_size_bytes=0,
                    bitrate_kbps=320,
                    cover_url=resolved.get("cover_url"),
                    hit_count=0,
                    stream_url=f"{base_url}/api/v1/music/netease/stream/{netease_song_id}",
                    created_at=now,
                    last_accessed_at=now,
                )

    if not track:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    base_url = str(request.base_url).rstrip("/")
    stream_url = f"{base_url}/api/v1/music/stream/{track.track_id}"

    return TrackResponse(
        track_id=track.track_id,
        source=track.source,
        title=track.title,
        artist=track.artist,
        album=track.album,
        duration_ms=track.duration_ms,
        file_size_bytes=track.file_size_bytes,
        bitrate_kbps=track.bitrate_kbps,
        cover_url=track.cover_url,
        hit_count=track.hit_count,
        stream_url=stream_url,
        created_at=track.created_at,
        last_accessed_at=track.last_accessed_at,
    )


@router.api_route("/stream/{track_id}", methods=["GET", "HEAD"])
async def stream_audio_track(
    track_id: str,
    request: Request,
    range: Optional[str] = Header(None, alias="Range"),
    token: Optional[str] = Query(None, description="Optional authentication token"),
    db: Session = Depends(get_db),
):
    """
    Stream audio track supporting HTTP 206 Partial Content Range requests and HEAD probing.
    Supports seek, resume, and arbitrary byte ranges for ExoPlayer / iOS AVPlayer.
    """
    track = db.query(MediaTrack).filter(MediaTrack.track_id == track_id).first()
    if not track:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    file_path = Path(track.file_path)
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media file not found on disk",
        )

    file_size = file_path.stat().st_size
    mime_type = get_audio_mime_type(str(file_path))

    # Parse and validate HTTP Range header
    try:
        start, end = parse_range_header(range, file_size)
    except RangeNotSatisfiableError:
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={
                "Content-Range": f"bytes */{file_size}",
                "Accept-Ranges": "bytes",
            },
        )

    # Record play hit and timestamp
    record_track_access(db, track)

    if start is not None and end is not None:
        # HTTP 206 Partial Content
        content_length = (end - start) + 1
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": mime_type,
            "Cache-Control": "no-cache",
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
        # Full file 200 OK stream with Accept-Ranges
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": mime_type,
            "Cache-Control": "no-cache",
        }
        if request.method == "HEAD":
            return Response(status_code=status.HTTP_200_OK, headers=headers)
        return StreamingResponse(
            file_chunk_generator(str(file_path), 0, file_size - 1),
            status_code=status.HTTP_200_OK,
            headers=headers,
            media_type=mime_type,
        )
