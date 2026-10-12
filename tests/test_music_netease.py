from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import settings
from app.repositories.models import MediaTrack
from app.services.netease_service import cache_manager, netease_service


def test_netease_search_endpoint(client, phone_headers):
    mock_search_data = {
        "result": {
            "songCount": 1,
            "songs": [
                {
                    "id": 1901371647,
                    "name": "孤勇者",
                    "ar": [{"id": 2116, "name": "陈奕迅"}],
                    "al": {"id": 135760815, "name": "孤勇者", "picUrl": "https://p1.music.126.net/xyz.jpg"},
                    "dt": 256000,
                }
            ],
        }
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_search_data
        mock_get.return_value = mock_resp

        resp = client.get("/api/v1/music/netease/search?keyword=孤勇者", headers=phone_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert data["total"] == 1
        assert len(data["songs"]) == 1
        song = data["songs"][0]
        assert song["id"] == 1901371647
        assert song["title"] == "孤勇者"
        assert song["artist"] == "陈奕迅"
        assert song["cached"] is False
        assert "/api/v1/music/netease/stream/1901371647" in song["stream_url"]


def test_netease_intelligence_endpoint(client, phone_headers):
    mock_intel_data = {
        "code": 200,
        "data": [
            {
                "songInfo": {
                    "id": 1901371648,
                    "name": "推荐歌曲1",
                    "ar": [{"name": "歌手1"}],
                    "al": {"name": "专辑1", "picUrl": "https://img.jpg"},
                    "dt": 180000,
                }
            }
        ],
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_intel_data
        mock_get.return_value = mock_resp

        payload = {"seed_song_id": 1901371647, "count": 10}
        resp = client.post("/api/v1/music/netease/intelligence", json=payload, headers=phone_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert data["mode"] == "intelligence"
        assert len(data["songs"]) == 1
        assert data["songs"][0]["title"] == "推荐歌曲1"


def test_netease_song_url_and_lyric(client, phone_headers):
    # 1. Test song URL
    mock_url_data = {
        "data": [
            {
                "id": 1901371647,
                "url": "http://m801.music.126.net/demo.mp3",
                "br": 320000,
                "size": 5242880,
                "type": "mp3",
            }
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_url_data
        mock_get.return_value = mock_resp

        resp = client.get("/api/v1/music/netease/song_url?song_id=1901371647", headers=phone_headers)
        assert resp.status_code == 200
        assert resp.json()["url"] == "http://m801.music.126.net/demo.mp3"
        assert resp.json()["br"] == 320000

    # 2. Test lyric
    mock_lyric_data = {
        "code": 200,
        "lrc": {"lyric": "[00:01.00]去吗 配吗 这褴褛的披风"},
        "tlyric": {"lyric": "[00:01.00]Are you going?"},
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_lyric_data
        mock_get.return_value = mock_resp

        lyric_resp = client.get("/api/v1/music/netease/lyric?song_id=1901371647", headers=phone_headers)
        assert lyric_resp.status_code == 200
        assert "褴褛的披风" in lyric_resp.json()["lrc"]


def test_stream_miss_caching_and_hit(client, db_session):
    test_song_id = 99887766
    audio_bytes = b"MPEG_AUDIO_STREAM_CONTENT_SAMPLE_FOR_TESTING_" * 100  # 4500 bytes

    # Mock get_song_url
    with patch.object(
        netease_service,
        "get_song_url",
        new=AsyncMock(return_value={"song_id": test_song_id, "url": "http://fake-cdn/track.mp3", "type": "mp3", "br": 320000}),
    ):
        # Mock httpx.AsyncClient.stream for the first MISS-CACHING request
        class MockStreamContext:
            async def __aenter__(self):
                mock_stream_resp = AsyncMock()
                mock_stream_resp.raise_for_status = MagicMock()

                async def chunk_iter(chunk_size=65536):
                    yield audio_bytes

                mock_stream_resp.aiter_bytes = chunk_iter
                return mock_stream_resp

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        with patch("httpx.AsyncClient.stream", return_value=MockStreamContext()):
            # 1. First stream request (MISS-CACHING)
            resp1 = client.get(f"/api/v1/music/netease/stream/{test_song_id}")
            assert resp1.status_code == 200
            assert resp1.headers.get("X-Cache-Status") == "MISS-CACHING"
            assert resp1.content == audio_bytes

    # Verify cached on disk and in database
    cached_path = cache_manager.get_target_path(test_song_id, "mp3")
    assert cached_path.exists()
    assert cached_path.stat().st_size == len(audio_bytes)

    # 2. Second request should be a cache HIT and support HTTP 206 Partial Content
    range_header = {"Range": "bytes=0-99"}
    resp2 = client.get(f"/api/v1/music/netease/stream/{test_song_id}", headers=range_header)
    assert resp2.status_code == 206
    assert resp2.headers.get("X-Cache-Status") == "HIT"
    assert resp2.headers.get("Content-Range") == f"bytes 0-99/{len(audio_bytes)}"
    assert resp2.content == audio_bytes[:100]

    # Clean up test file and db row
    try:
        cached_path.unlink()
    except OSError:
        pass
    row = db_session.query(MediaTrack).filter(MediaTrack.track_id == f"netease_{test_song_id}").first()
    if row:
        db_session.delete(row)
        db_session.commit()


def test_cache_lru_quota_and_stats(client, db_session, phone_headers):
    # 1. Create two fake cached tracks
    track1_path = Path(settings.MEDIA_CACHE_DIR) / "netease_1001.mp3"
    track2_path = Path(settings.MEDIA_CACHE_DIR) / "netease_1002.mp3"
    track1_path.write_bytes(b"A" * 1000)
    track2_path.write_bytes(b"B" * 1000)

    track1 = MediaTrack(
        track_id="netease_1001",
        source="netease_cache",
        title="Track 1",
        artist="Artist 1",
        file_path=str(track1_path),
        file_size_bytes=1000,
    )
    track2 = MediaTrack(
        track_id="netease_1002",
        source="netease_cache",
        title="Track 2",
        artist="Artist 2",
        file_path=str(track2_path),
        file_size_bytes=1000,
    )
    db_session.add(track1)
    db_session.add(track2)
    db_session.commit()

    # 2. Check stats endpoint
    stats_resp = client.get("/api/v1/music/cache/stats", headers=phone_headers)
    assert stats_resp.status_code == 200
    assert stats_resp.json()["tracks_in_db"] >= 2

    # 3. Trigger clean with max_bytes=1500 (total is 2000, so track1 should be evicted)
    clean_resp = client.post("/api/v1/music/cache/clean?target_bytes=1500", headers=phone_headers)
    assert clean_resp.status_code == 200
    assert clean_resp.json()["evicted_count"] >= 1

    # Cleanup any remaining files
    for p in (track1_path, track2_path):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    db_session.query(MediaTrack).filter(MediaTrack.source == "netease_cache").delete()
    db_session.commit()
