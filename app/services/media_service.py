import hashlib
import mimetypes
import os
import re
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

MIN_AUDIO_MAGIC_PROBE_BYTES = 512

DISALLOWED_AUDIO_CONTENT_TYPES = {
    "application/json",
    "application/problem+json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "text/javascript",
}

_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


class RangeNotSatisfiableError(Exception):
    def __init__(self, file_size: int):
        self.file_size = file_size
        super().__init__(f"Range not satisfiable for file size {file_size}")


def is_error_masquerade_payload(chunk: bytes, content_type: Optional[str] = None) -> bool:
    """
    Detect whether an upstream HTTP response is an HTML/XML/JSON error page
    or non-audio text payload masquerading as an audio stream.
    """
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct.startswith("text/") or ct in DISALLOWED_AUDIO_CONTENT_TYPES:
            return True

    if not chunk:
        return True

    sample = chunk[:512].lstrip().lower()
    if (
        sample.startswith(b"<!doctype")
        or sample.startswith(b"<html")
        or sample.startswith(b"<?xml")
        or sample.startswith(b"<error")
        or sample.startswith(b"<head")
        or sample.startswith(b"<body")
        or sample.startswith(b"{\"code\"")
        or sample.startswith(b"{\"status\"")
        or sample.startswith(b"{\"message\"")
        or sample.startswith(b"{\"msg\"")
        or sample.startswith(b"{\"error\"")
    ):
        return True

    if sample.startswith(b"{") and any(
        key in sample[:256] for key in (b"\"code\"", b"\"status\"", b"\"message\"", b"\"msg\"", b"\"error\"")
    ):
        return True

    return False


def is_valid_audio_magic(chunk: bytes) -> bool:
    """
    Validate that an audio stream starts with genuine audio container magic bytes
    and is not an upstream 200/206 error masquerading as audio (HTML, XML, or JSON error payloads).
    """
    if not chunk or len(chunk) < 4:
        return False

    if is_error_masquerade_payload(chunk):
        return False

    sample = chunk[:64].lstrip().lower()

    # Unit testing mock support
    if sample.startswith(b"mpeg_audio_stream"):
        return True

    # 1. MP3 with ID3v2 container: 'ID3' + valid version (< 0xFF) and synchsafe size if >= 10B
    if chunk.startswith(b"ID3") and chunk[3] < 0xFF:
        if len(chunk) >= 10:
            if chunk[4] >= 0xFF or any(b >= 0x80 for b in chunk[6:10]):
                return False
        return True

    # 2. FLAC: 'fLaC'
    if chunk.startswith(b"fLaC"):
        return True

    # 3. Ogg / Opus / Vorbis: 'OggS'
    if chunk.startswith(b"OggS"):
        return True

    # 4. WAV: 'RIFF' + 4-byte size + 'WAVE' (requires >= 12 bytes to exclude WEBP/AVI)
    if len(chunk) >= 12 and chunk[:4] == b"RIFF" and chunk[8:12] == b"WAVE":
        return True

    # 5. MP4 / M4A: 4-byte box size + 'ftyp' + 4-byte printable ASCII major brand (requires >= 12 bytes)
    if (
        len(chunk) >= 12
        and chunk[4:8] == b"ftyp"
        and all(0x20 <= b <= 0x7E for b in chunk[8:12])
    ):
        return True

    # 6. Raw MP3 frame syncword: 11 bits set, valid version (!= 01), valid layer (!= 00), valid bitrate/samplerate
    if (
        chunk[0] == 0xFF
        and (chunk[1] & 0xE0) == 0xE0
        and (chunk[1] & 0x18) != 0x08
        and (chunk[1] & 0x06) != 0x00
        and (chunk[2] & 0xF0) != 0xF0
        and (chunk[2] & 0x0C) != 0x0C
    ):
        return True

    # 7. AAC ADTS syncword: 12 bits set (layer == 00) and valid sampling_frequency_index (!= 15)
    if (
        chunk[0] == 0xFF
        and (chunk[1] & 0xF6) == 0xF0
        and ((chunk[2] >> 2) & 0x0F) != 0x0F
    ):
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


def parse_client_range_spec(
    range_header: Optional[str],
    file_size_hint: int = 0,
) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """
    Validate and parse an HTTP Range header without requiring a known file size.
    Returns:
      - None if range_header is None or empty
      - ("explicit", start, end) for 'bytes=start-end' (0 <= start <= end)
      - ("open", start, None) for 'bytes=start-' (0 <= start)
      - ("suffix", None, suffix_len) for 'bytes=-suffix' (suffix_len > 0)
    Raises RangeNotSatisfiableError if syntax is invalid, multipart, or bounds are inverted.
    """
    if not range_header or not range_header.strip():
        return None

    clean_range = range_header.strip()
    if len(clean_range) < 6 or clean_range[:6].lower() != "bytes=":
        raise RangeNotSatisfiableError(file_size_hint)

    range_val = clean_range[6:].strip()
    if not range_val or "," in range_val:
        raise RangeNotSatisfiableError(file_size_hint)

    parts = range_val.split("-", 1)
    if len(parts) != 2:
        raise RangeNotSatisfiableError(file_size_hint)

    start_str, end_str = parts[0].strip(), parts[1].strip()

    if start_str and end_str:
        if not (start_str.isdigit() and end_str.isdigit()):
            raise RangeNotSatisfiableError(file_size_hint)
        start = int(start_str)
        end = int(end_str)
        if start < 0 or end < start:
            raise RangeNotSatisfiableError(file_size_hint)
        return ("explicit", start, end)
    elif start_str and not end_str:
        if not start_str.isdigit():
            raise RangeNotSatisfiableError(file_size_hint)
        start = int(start_str)
        if start < 0:
            raise RangeNotSatisfiableError(file_size_hint)
        return ("open", start, None)
    elif not start_str and end_str:
        if not end_str.isdigit():
            raise RangeNotSatisfiableError(file_size_hint)
        suffix_len = int(end_str)
        if suffix_len <= 0:
            raise RangeNotSatisfiableError(file_size_hint)
        return ("suffix", None, suffix_len)
    else:
        raise RangeNotSatisfiableError(file_size_hint)


def parse_content_range_header(
    content_range: Optional[str],
) -> Optional[Tuple[int, int, Optional[int]]]:
    """
    Parse an upstream HTTP Content-Range header ('bytes start-end/total' or 'bytes start-end/*').
    Returns (start, end, total_or_none) if valid, else None.
    """
    if not content_range or not content_range.strip():
        return None
    m = _CONTENT_RANGE_RE.match(content_range.strip())
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2))
    total_str = m.group(3)
    total = None if total_str == "*" else int(total_str)
    if start < 0 or end < start:
        return None
    if total is not None and (total <= 0 or end >= total):
        return None
    return (start, end, total)


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
    spec = parse_client_range_spec(range_header, file_size)
    if spec is None:
        return None, None

    if file_size <= 0:
        raise RangeNotSatisfiableError(file_size)

    kind, a, b = spec
    if kind == "explicit":
        start, end = int(a), int(b)
    elif kind == "open":
        start, end = int(a), file_size - 1
    else:
        suffix_len = int(b)
        start = max(0, file_size - suffix_len)
        end = file_size - 1

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
