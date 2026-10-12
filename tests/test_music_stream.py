import struct
import wave
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.repositories.models import MediaTrack
from app.services.media_service import (
    extract_audio_metadata,
    generate_track_id,
    parse_range_header,
    RangeNotSatisfiableError,
)


def create_dummy_wav(file_path: Path, duration_seconds: float = 1.0, sample_rate: int = 44100):
    """Generate a valid minimal PCM WAV file for testing."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    num_frames = int(sample_rate * duration_seconds)
    with wave.open(str(file_path), "wb") as wf:
        wf.setnchannels(1)  # Mono
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        # Write silent PCM frames
        raw_data = struct.pack(f"<{num_frames}h", *(0 for _ in range(num_frames)))
        wf.writeframes(raw_data)


def test_parse_range_header_variants():
    file_size = 10000

    # 1. No header
    assert parse_range_header(None, file_size) == (None, None)
    assert parse_range_header("", file_size) == (None, None)

    # 2. Standard start-end
    assert parse_range_header("bytes=0-499", file_size) == (0, 499)
    assert parse_range_header("bytes=100-200", file_size) == (100, 200)

    # 3. Start to EOF
    assert parse_range_header("bytes=1000-", file_size) == (1000, 9999)

    # 4. Suffix range
    assert parse_range_header("bytes=-500", file_size) == (9500, 9999)

    # 5. Invalid ranges -> RangeNotSatisfiableError
    with pytest.raises(RangeNotSatisfiableError):
        parse_range_header("bytes=10000-", file_size)  # start == file_size

    with pytest.raises(RangeNotSatisfiableError):
        parse_range_header("bytes=20000-", file_size)  # start > file_size

    with pytest.raises(RangeNotSatisfiableError):
        parse_range_header("bytes=500-200", file_size)  # start > end

    with pytest.raises(RangeNotSatisfiableError):
        parse_range_header("invalid_range", file_size)


def test_metadata_and_id_extraction(tmp_path):
    wav_path = tmp_path / "song1.wav"
    create_dummy_wav(wav_path, duration_seconds=2.0)

    track_id = generate_track_id(wav_path)
    assert track_id.startswith("local_")
    # Idempotent
    assert generate_track_id(wav_path) == track_id

    meta = extract_audio_metadata(wav_path)
    assert meta["title"] == "song1"
    assert meta["file_size_bytes"] > 0
    assert meta["duration_ms"] > 0


def test_media_scan_and_listing_api(client: TestClient, db_session, tmp_path):
    music_dir = tmp_path / "music_repo"
    create_dummy_wav(music_dir / "track_alpha.wav", duration_seconds=1.5)
    create_dummy_wav(music_dir / "subdir" / "track_beta.wav", duration_seconds=2.5)

    # 1. Trigger scan
    res = client.post(f"/api/v1/music/scan?scan_dir={music_dir}")
    assert res.status_code == 200
    scan_data = res.json()
    assert scan_data["status"] == "ok"
    assert scan_data["scanned_count"] == 2
    assert scan_data["added_count"] == 2
    assert scan_data["total_tracks"] == 2

    # 2. Rescan idempotent
    res2 = client.post(f"/api/v1/music/scan?scan_dir={music_dir}")
    assert res2.status_code == 200
    assert res2.json()["added_count"] == 0
    assert res2.json()["updated_count"] == 2

    # 3. List tracks
    list_res = client.get("/api/v1/music/tracks")
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert list_data["total"] == 2
    tracks = list_data["tracks"]
    titles = [t["title"] for t in tracks]
    assert "track_alpha" in titles
    assert "track_beta" in titles
    assert all("/api/v1/music/stream/" in t["stream_url"] for t in tracks)

    # 4. Search keyword
    search_res = client.get("/api/v1/music/tracks?keyword=alpha")
    assert search_res.status_code == 200
    assert search_res.json()["total"] == 1
    assert search_res.json()["tracks"][0]["title"] == "track_alpha"

    # 5. Detail API
    track_id = search_res.json()["tracks"][0]["track_id"]
    detail_res = client.get(f"/api/v1/music/tracks/{track_id}")
    assert detail_res.status_code == 200
    assert detail_res.json()["track_id"] == track_id

    # 6. Non-existent track
    err_res = client.get("/api/v1/music/tracks/local_nonexistent")
    assert err_res.status_code == 404


def test_http_206_streaming_playback(client: TestClient, db_session, tmp_path):
    music_dir = tmp_path / "stream_repo"
    test_file = music_dir / "streaming_sample.wav"
    create_dummy_wav(test_file, duration_seconds=1.0)
    full_content = test_file.read_bytes()
    file_size = len(full_content)

    client.post(f"/api/v1/music/scan?scan_dir={music_dir}")
    tracks = client.get("/api/v1/music/tracks").json()["tracks"]
    track_id = tracks[0]["track_id"]

    # --- Test 1: Full Playback 200 OK (No Range header) ---
    res_200 = client.get(f"/api/v1/music/stream/{track_id}")
    assert res_200.status_code == 200
    assert res_200.headers["Accept-Ranges"] == "bytes"
    assert res_200.headers["Content-Length"] == str(file_size)
    assert res_200.content == full_content

    # --- Test 2: HTTP 206 Partial Content (Prefix bytes 0-1023) ---
    res_206_start = client.get(
        f"/api/v1/music/stream/{track_id}",
        headers={"Range": "bytes=0-1023"},
    )
    assert res_206_start.status_code == 206
    assert res_206_start.headers["Accept-Ranges"] == "bytes"
    assert res_206_start.headers["Content-Range"] == f"bytes 0-1023/{file_size}"
    assert res_206_start.headers["Content-Length"] == "1024"
    assert res_206_start.content == full_content[0:1024]

    # --- Test 3: HTTP 206 Partial Content (Middle chunk bytes 1024-2047) ---
    res_206_mid = client.get(
        f"/api/v1/music/stream/{track_id}",
        headers={"Range": "bytes=1024-2047"},
    )
    assert res_206_mid.status_code == 206
    assert res_206_mid.headers["Content-Range"] == f"bytes 1024-2047/{file_size}"
    assert res_206_mid.headers["Content-Length"] == "1024"
    assert res_206_mid.content == full_content[1024:2048]

    # --- Test 4: HTTP 206 Range open-ended (bytes 2048-) ---
    res_206_tail = client.get(
        f"/api/v1/music/stream/{track_id}",
        headers={"Range": "bytes=2048-"},
    )
    assert res_206_tail.status_code == 206
    assert res_206_tail.headers["Content-Range"] == f"bytes 2048-{file_size - 1}/{file_size}"
    assert res_206_tail.content == full_content[2048:]

    # --- Test 5: HTTP 206 Suffix range (bytes=-500) ---
    res_206_suffix = client.get(
        f"/api/v1/music/stream/{track_id}",
        headers={"Range": "bytes=-500"},
    )
    assert res_206_suffix.status_code == 206
    assert res_206_suffix.headers["Content-Range"] == f"bytes {file_size - 500}-{file_size - 1}/{file_size}"
    assert res_206_suffix.content == full_content[-500:]

    # --- Test 6: HTTP 416 Range Not Satisfiable (Out of bounds) ---
    res_416 = client.get(
        f"/api/v1/music/stream/{track_id}",
        headers={"Range": f"bytes={file_size + 1000}-"},
    )
    assert res_416.status_code == 416
    assert res_416.headers["Content-Range"] == f"bytes */{file_size}"
    assert res_416.headers["Accept-Ranges"] == "bytes"

    # --- Test 7: Verify Hit Count incremented ---
    track_record = db_session.query(MediaTrack).filter(MediaTrack.track_id == track_id).first()
    assert track_record.hit_count >= 5
