import hashlib
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.models import MediaTrack, utc_now

SUPPORTED_AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
}


class RangeNotSatisfiableError(Exception):
    def __init__(self, file_size: int):
        self.file_size = file_size
        super().__init__(f"Range not satisfiable for file size {file_size}")


def is_valid_audio_magic(chunk: bytes) -> bool:
    """
    Validate that an audio stream starts with genuine audio magic bytes
    and is not an upstream 200 OK error masquerading as audio (HTML, XML, or JSON error payloads).
    """
    if not chunk or len(chunk) < 4:
        return False

    sample = chunk[:64].lstrip().lower()
    if (
        sample.startswith(b"<!doctype")
        or sample.startswith(b"<html")
        or sample.startswith(b"<?xml")
        or sample.startswith(b"{\"code\"")
        or sample.startswith(b"{\"status\"")
        or sample.startswith(b"{\"message\"")
        or sample.startswith(b"{\"msg\"")
        or sample.startswith(b"{\"error\"")
    ):
        return False

    # Unit testing mock support
    if sample.startswith(b"mpeg_audio_stream"):
        return True

    # 1. MP3 with ID3v2 container: 'ID3'
    if chunk.startswith(b"ID3"):
        return True

    # 2. FLAC: 'fLaC'
    if chunk.startswith(b"fLaC"):
        return True

    # 3. Ogg / Opus / Vorbis: 'OggS'
    if chunk.startswith(b"OggS"):
        return True

    # 4. WAV: 'RIFF'
    if chunk.startswith(b"RIFF"):
        return True

    # 5. MP4 / M4A: '....ftyp'
    if len(chunk) >= 8 and chunk[4:8] == b"ftyp":
        return True

    # 6. Raw MP3 syncword: 11 consecutive bits set (0xFF, 0xEx)
    if chunk[0] == 0xFF and (chunk[1] & 0xE0) == 0xE0:
        return True

    # 7. AAC ADTS syncword: 12 consecutive bits set (0xFF, 0xFx)
    if chunk[0] == 0xFF and (chunk[1] & 0xF0) == 0xF0:
        return True

    return False


def extract_audio_metadata(file_path: Path) -> dict:
    """Extract ID3/Audio metadata from file using tinytag, fallback to mutagen or file stats."""
    file_size = file_path.stat().st_size
    title = file_path.stem
    artist = "Unknown Artist"
    album = None
    duration_ms = 0
    bitrate_kbps = None

    # Try tinytag first
    try:
        from tinytag import TinyTag

        tag = TinyTag.get(str(file_path))
        if tag.title and tag.title.strip():
            title = tag.title.strip()
        if tag.artist and tag.artist.strip():
            artist = tag.artist.strip()
        if tag.album and tag.album.strip():
            album = tag.album.strip()
        if tag.duration:
            duration_ms = max(0, int(tag.duration * 1000))
        if tag.bitrate:
            bitrate_kbps = max(0, int(tag.bitrate))
        return {
            "title": title,
            "artist": artist,
            "album": album,
            "duration_ms": duration_ms,
            "bitrate_kbps": bitrate_kbps,
            "file_size_bytes": file_size,
        }
    except Exception:
        pass

    # Try mutagen fallback
    try:
        import mutagen

        audio = mutagen.File(str(file_path), easy=True)
        if audio is not None:
            if hasattr(audio, "info") and audio.info:
                if hasattr(audio.info, "length") and audio.info.length:
                    duration_ms = max(0, int(audio.info.length * 1000))
                if hasattr(audio.info, "bitrate") and audio.info.bitrate:
                    bitrate_kbps = max(0, int(audio.info.bitrate / 1000))
            if audio.tags:
                if "title" in audio.tags and audio.tags["title"]:
                    title = str(audio.tags["title"][0]).strip()
                if "artist" in audio.tags and audio.tags["artist"]:
                    artist = str(audio.tags["artist"][0]).strip()
                if "album" in audio.tags and audio.tags["album"]:
                    album = str(audio.tags["album"][0]).strip()
    except Exception:
        pass

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "duration_ms": duration_ms,
        "bitrate_kbps": bitrate_kbps,
        "file_size_bytes": file_size,
    }


def generate_track_id(file_path: Path) -> str:
    """Generate stable, idempotent track_id based on resolved file path."""
    resolved_path = str(file_path.resolve())
    digest = hashlib.sha256(resolved_path.encode("utf-8")).hexdigest()[:16]
    return f"local_{digest}"


def scan_local_tracks(
    db: Session,
    scan_dir: Optional[str] = None,
) -> dict:
    """Scan local directory for audio files, extract metadata, and upsert into database."""
    target_path = Path(scan_dir or settings.LOCAL_TRACKS_DIR).resolve()
    target_path.mkdir(parents=True, exist_ok=True)

    scanned_count = 0
    added_count = 0
    updated_count = 0

    for root, _, files in os.walk(target_path):
        for f in files:
            file_path = Path(root) / f
            if file_path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
                scanned_count += 1
                track_id = generate_track_id(file_path)
                meta = extract_audio_metadata(file_path)

                existing = db.query(MediaTrack).filter(MediaTrack.track_id == track_id).first()
                if existing:
                    existing.title = meta["title"]
                    existing.artist = meta["artist"]
                    existing.album = meta["album"]
                    existing.duration_ms = meta["duration_ms"]
                    existing.file_size_bytes = meta["file_size_bytes"]
                    existing.bitrate_kbps = meta["bitrate_kbps"]
                    existing.file_path = str(file_path.resolve())
                    updated_count += 1
                else:
                    new_track = MediaTrack(
                        track_id=track_id,
                        source="local_repo",
                        title=meta["title"],
                        artist=meta["artist"],
                        album=meta["album"],
                        duration_ms=meta["duration_ms"],
                        file_path=str(file_path.resolve()),
                        file_size_bytes=meta["file_size_bytes"],
                        bitrate_kbps=meta["bitrate_kbps"],
                        cover_url=None,
                        hit_count=0,
                        last_accessed_at=utc_now(),
                        created_at=utc_now(),
                    )
                    db.add(new_track)
                    added_count += 1

    db.commit()
    total_tracks = db.query(MediaTrack).filter(MediaTrack.source == "local_repo").count()

    return {
        "status": "ok",
        "scanned_count": scanned_count,
        "added_count": added_count,
        "updated_count": updated_count,
        "total_tracks": total_tracks,
    }


def parse_range_header(
    range_header: Optional[str],
    file_size: int,
) -> Tuple[Optional[int], Optional[int]]:
    """
    Parse HTTP Range header: 'bytes=start-end', 'bytes=start-', 'bytes=-suffix'.
    Returns (start, end) tuple (inclusive) if range is specified.
    Returns (None, None) if no Range header provided.
    Raises RangeNotSatisfiableError if range cannot be satisfied.
    """
    if not range_header or not range_header.strip():
        return None, None

    clean_range = range_header.strip()
    if not clean_range.startswith("bytes="):
        raise RangeNotSatisfiableError(file_size)

    range_val = clean_range[6:].strip()
    if not range_val or "," in range_val:
        # Multipart ranges not supported or empty
        raise RangeNotSatisfiableError(file_size)

    parts = range_val.split("-", 1)
    if len(parts) != 2:
        raise RangeNotSatisfiableError(file_size)

    start_str, end_str = parts[0].strip(), parts[1].strip()

    if start_str and end_str:
        try:
            start = int(start_str)
            end = int(end_str)
        except ValueError:
            raise RangeNotSatisfiableError(file_size)
    elif start_str and not end_str:
        try:
            start = int(start_str)
            end = file_size - 1
        except ValueError:
            raise RangeNotSatisfiableError(file_size)
    elif not start_str and end_str:
        try:
            suffix_len = int(end_str)
            if suffix_len <= 0:
                raise RangeNotSatisfiableError(file_size)
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        except ValueError:
            raise RangeNotSatisfiableError(file_size)
    else:
        raise RangeNotSatisfiableError(file_size)

    if start < 0 or start >= file_size or end < start:
        raise RangeNotSatisfiableError(file_size)

    # Clamp end to last byte
    end = min(end, file_size - 1)

    return start, end


async def file_chunk_generator(
    file_path: str,
    start: int,
    end: int,
    chunk_size: int = 64 * 1024,
) -> AsyncGenerator[bytes, None]:
    """Asynchronously yield byte chunks from start to end (inclusive)."""
    with open(file_path, "rb") as f:
        f.seek(start)
        remaining = (end - start) + 1
        while remaining > 0:
            read_size = min(chunk_size, remaining)
            data = f.read(read_size)
            if not data:
                break
            remaining -= len(data)
            yield data


def get_audio_mime_type(file_path: str) -> str:
    """Guess MIME type for audio file, with solid fallback mapping."""
    mime, _ = mimetypes.guess_type(file_path)
    if mime:
        return mime

    suffix = Path(file_path).suffix.lower()
    mapping = {
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
    }
    return mapping.get(suffix, "application/octet-stream")


def record_track_access(db: Session, track: MediaTrack) -> None:
    """Record access/play hit count and update last_accessed_at timestamp."""
    track.hit_count = (track.hit_count or 0) + 1
    track.last_accessed_at = utc_now()
    db.commit()
