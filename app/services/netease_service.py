import asyncio
import logging
import os
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.database import SessionLocal
from app.repositories.models import MediaTrack, utc_now
from app.services.media_service import record_track_access

logger = logging.getLogger(__name__)


class NeteaseService:
    """Interfaces with the upstream NeteaseCloudMusicApi container."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.NETEASE_API_BASE_URL).rstrip("/")

    async def search(self, keyword: str, page: int = 1, limit: int = 20) -> Dict[str, Any]:
        offset = max(0, (page - 1) * limit)
        url = f"{self.base_url}/cloudsearch"
        params = {"keywords": keyword, "offset": offset, "limit": limit, "type": 1}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    logger.warning(f"Upstream Netease search returned status {resp.status_code}")
                    return {"total": 0, "songs": []}
                data = resp.json()
        except Exception as e:
            logger.error(f"Error calling Netease search: {e}")
            return {"total": 0, "songs": []}

        result = data.get("result", {})
        total = result.get("songCount", 0)
        raw_songs = result.get("songs", [])

        songs = []
        for s in raw_songs:
            song_id = s.get("id")
            if not song_id:
                continue
            ar_names = [ar.get("name", "") for ar in s.get("ar", []) if ar.get("name")]
            artist_str = " / ".join(ar_names) or "Unknown Artist"
            al_dict = s.get("al") or {}

            songs.append({
                "id": song_id,
                "title": s.get("name") or "Unknown Title",
                "artist": artist_str,
                "album": al_dict.get("name"),
                "duration_ms": s.get("dt") or 0,
                "cover_url": al_dict.get("picUrl"),
            })

        return {"total": total, "songs": songs}

    async def get_song_url(self, song_id: int, level: str = "exhigh") -> Dict[str, Any]:
        """Fetch CDN audio streaming URL for a song."""
        # Try song/url/v1 first
        url_v1 = f"{self.base_url}/song/url/v1"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url_v1, params={"id": song_id, "level": level})
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    if data and data[0].get("url"):
                        item = data[0]
                        return {
                            "song_id": song_id,
                            "url": item.get("url"),
                            "br": item.get("br"),
                            "size_bytes": item.get("size"),
                            "type": item.get("type", "mp3"),
                        }
        except Exception as e:
            logger.warning(f"Error with song/url/v1 for {song_id}: {e}")

        # Fallback to standard song/url
        url_std = f"{self.base_url}/song/url"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url_std, params={"id": song_id, "br": 320000})
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    if data and data[0].get("url"):
                        item = data[0]
                        return {
                            "song_id": song_id,
                            "url": item.get("url"),
                            "br": item.get("br"),
                            "size_bytes": item.get("size"),
                            "type": item.get("type", "mp3"),
                        }
        except Exception as e:
            logger.error(f"Error with fallback song/url for {song_id}: {e}")

        return {"song_id": song_id, "url": None, "br": 0, "size_bytes": 0, "type": "mp3"}

    async def get_intelligence(self, seed_song_id: int, playlist_id: Optional[int] = None, count: int = 30) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Generate intelligence (心动模式) recommended playlist.
        Falls back to simi/song if login credentials are not present (code 301).
        """
        url_intel = f"{self.base_url}/playmode/intelligence/list"
        params = {"id": seed_song_id, "pid": playlist_id or ""}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url_intel, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 200 and data.get("data"):
                        raw_list = data["data"]
                        parsed = []
                        for item in raw_list[:count]:
                            s = item.get("songInfo") or item
                            s_id = s.get("id")
                            if not s_id:
                                continue
                            ar_names = [ar.get("name", "") for ar in s.get("ar", []) if ar.get("name")]
                            parsed.append({
                                "id": s_id,
                                "title": s.get("name", "Unknown Title"),
                                "artist": " / ".join(ar_names) or "Unknown Artist",
                                "album": (s.get("al") or {}).get("name"),
                                "duration_ms": s.get("dt", 0),
                                "cover_url": (s.get("al") or {}).get("picUrl"),
                            })
                        if parsed:
                            return "intelligence", parsed
        except Exception as e:
            logger.info(f"Intelligence API attempt exception: {e}")

        # Fallback to simi/song
        url_simi = f"{self.base_url}/simi/song"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url_simi, params={"id": seed_song_id})
                if resp.status_code == 200:
                    data = resp.json()
                    raw_songs = data.get("songs", [])
                    parsed = []
                    for s in raw_songs[:count]:
                        s_id = s.get("id")
                        if not s_id:
                            continue
                        artists = [a.get("name", "") for a in s.get("artists", []) if a.get("name")]
                        parsed.append({
                            "id": s_id,
                            "title": s.get("name", "Unknown Title"),
                            "artist": " / ".join(artists) or "Unknown Artist",
                            "album": (s.get("album") or {}).get("name"),
                            "duration_ms": s.get("duration", 0),
                            "cover_url": (s.get("album") or {}).get("picUrl"),
                        })
                    return "simi_fallback", parsed
        except Exception as e:
            logger.error(f"Simi song fallback error: {e}")

        return "empty", []

    async def get_lyric(self, song_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/lyric"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params={"id": song_id})
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "song_id": song_id,
                        "lrc": (data.get("lrc") or {}).get("lyric"),
                        "tlyric": (data.get("tlyric") or {}).get("lyric"),
                    }
        except Exception as e:
            logger.error(f"Error getting lyric for {song_id}: {e}")

        return {"song_id": song_id, "lrc": None, "tlyric": None}


class CacheManager:
    """Manages the on-demand caching of Netease songs and LRU cache eviction."""

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = Path(cache_dir or settings.MEDIA_CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._download_locks: Dict[int, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    def get_track_id(self, song_id: int) -> str:
        return f"netease_{song_id}"

    def get_cached_track(self, db: Session, song_id: int) -> Optional[MediaTrack]:
        t_id = self.get_track_id(song_id)
        track = db.query(MediaTrack).filter(MediaTrack.track_id == t_id).first()
        if track and os.path.isfile(track.file_path) and os.path.getsize(track.file_path) > 0:
            return track
        return None

    def get_target_path(self, song_id: int, ext: str = "mp3") -> Path:
        return self.cache_dir / f"netease_{song_id}.{ext}"

    def get_temp_path(self, song_id: int) -> Path:
        return self.cache_dir / f"netease_{song_id}.tmp"

    async def _get_song_lock(self, song_id: int) -> asyncio.Lock:
        async with self._meta_lock:
            if song_id not in self._download_locks:
                self._download_locks[song_id] = asyncio.Lock()
            return self._download_locks[song_id]

    async def tee_stream_and_cache(
        self,
        song_id: int,
        remote_url: str,
        metadata: Dict[str, Any],
        db_factory,
    ) -> AsyncGenerator[bytes, None]:
        """
        Streams audio chunks directly to client while simultaneously saving to disk.
        Atomic move and database indexing occur once stream is fully buffered.
        """
        tmp_path = self.get_temp_path(song_id)
        final_path = self.get_target_path(song_id, metadata.get("type", "mp3"))

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream("GET", remote_url) as resp:
                    resp.raise_for_status()
                    total_bytes = 0
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                                total_bytes += len(chunk)
                                yield chunk

            # Download finished successfully - move to final destination
            if total_bytes > 0:
                os.replace(tmp_path, final_path)
                logger.info(f"Successfully cached Netease song {song_id} ({total_bytes} bytes) -> {final_path}")

                # Register in SQLite database
                db = db_factory()
                try:
                    self._register_cached_track(db, song_id, str(final_path), total_bytes, metadata)
                    self.enforce_lru_quota(db)
                finally:
                    db.close()
        except Exception as e:
            logger.error(f"Error during tee streaming for song {song_id}: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def _register_cached_track(
        self,
        db: Session,
        song_id: int,
        file_path: str,
        file_size: int,
        metadata: Dict[str, Any],
    ) -> MediaTrack:
        t_id = self.get_track_id(song_id)
        track = db.query(MediaTrack).filter(MediaTrack.track_id == t_id).first()
        now = utc_now()

        if not track:
            track = MediaTrack(
                track_id=t_id,
                source="netease_cache",
                title=metadata.get("title", f"Netease Track {song_id}"),
                artist=metadata.get("artist", "Unknown Artist"),
                album=metadata.get("album"),
                duration_ms=metadata.get("duration_ms", 0),
                file_path=file_path,
                file_size_bytes=file_size,
                bitrate_kbps=metadata.get("bitrate_kbps", 320),
                cover_url=metadata.get("cover_url"),
                hit_count=1,
                last_accessed_at=now,
                created_at=now,
            )
            db.add(track)
        else:
            track.file_path = file_path
            track.file_size_bytes = file_size
            track.hit_count = (track.hit_count or 0) + 1
            track.last_accessed_at = now

        db.commit()
        db.refresh(track)
        return track

    def enforce_lru_quota(self, db: Session, max_bytes: Optional[int] = None) -> Tuple[int, int]:
        """Evicts oldest accessed cached files when total cache size exceeds quota."""
        quota = max_bytes or settings.MEDIA_CACHE_MAX_BYTES

        tracks = (
            db.query(MediaTrack)
            .filter(MediaTrack.source == "netease_cache")
            .order_by(MediaTrack.last_accessed_at.asc())
            .all()
        )

        total_bytes = sum(t.file_size_bytes for t in tracks)
        if total_bytes <= quota:
            return 0, 0

        target_bytes = int(quota * 0.85)  # Evict down to 85% headroom
        evicted_count = 0
        evicted_bytes = 0

        for t in tracks:
            if total_bytes <= target_bytes:
                break
            try:
                if os.path.exists(t.file_path):
                    os.remove(t.file_path)
            except OSError as e:
                logger.warning(f"Failed to remove evicted cache file {t.file_path}: {e}")

            total_bytes -= t.file_size_bytes
            evicted_bytes += t.file_size_bytes
            evicted_count += 1
            db.delete(t)

        db.commit()
        logger.info(f"LRU cache eviction completed: removed {evicted_count} tracks, freed {evicted_bytes} bytes")
        return evicted_count, evicted_bytes

    def get_cache_stats(self, db: Session) -> Dict[str, Any]:
        tracks = db.query(MediaTrack).filter(MediaTrack.source == "netease_cache").all()
        total_bytes = sum(t.file_size_bytes for t in tracks)
        max_bytes = settings.MEDIA_CACHE_MAX_BYTES
        usage_pct = round((total_bytes / max_bytes * 100), 2) if max_bytes > 0 else 0.0

        return {
            "cache_dir": str(self.cache_dir),
            "total_files": len(tracks),
            "total_bytes": total_bytes,
            "max_bytes": max_bytes,
            "usage_percentage": usage_pct,
            "tracks_in_db": len(tracks),
        }


# Singletons
netease_service = NeteaseService()
cache_manager = CacheManager()
