from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
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


def test_netease_search_legitimate_empty_results(client, phone_headers):
    mock_search_data = {
        "code": 200,
        "result": {
            "songCount": 0,
            "songs": [],
        },
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_search_data
        mock_get.return_value = mock_resp

        resp = client.get("/api/v1/music/netease/search?keyword=nonexistentsong12345", headers=phone_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == 200
        assert data["total"] == 0
        assert data["songs"] == []


def test_netease_search_upstream_500_error(client, phone_headers):
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp

        resp = client.get("/api/v1/music/netease/search?keyword=孤勇者", headers=phone_headers)
        assert resp.status_code == 502
        assert "upstream search failed" in resp.json()["detail"].lower()


def test_netease_search_upstream_timeout(client, phone_headers):
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.TimeoutException("Connection timed out")

        resp = client.get("/api/v1/music/netease/search?keyword=孤勇者", headers=phone_headers)
        assert resp.status_code == 504
        assert "timed out" in resp.json()["detail"].lower()


def test_netease_search_upstream_business_error(client, phone_headers):
    mock_search_data = {
        "code": 400,
        "message": "Bad request parameters",
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_search_data
        mock_get.return_value = mock_resp

        resp = client.get("/api/v1/music/netease/search?keyword=孤勇者", headers=phone_headers)
        assert resp.status_code == 502
        assert "code 400" in resp.json()["detail"].lower()


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

    with patch.object(netease_service.cookie_store, "load", return_value="dummy_cookie=1"), \
         patch.object(netease_service, "is_song_in_favorite_playlist", new_callable=AsyncMock, return_value=True), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
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


def test_is_valid_audio_magic_validation():
    from app.services.media_service import is_valid_audio_magic

    # Valid audio signatures
    assert is_valid_audio_magic(b"ID3\x04\x00\x00\x00\x00\x00\x23TSSE") is True
    assert is_valid_audio_magic(b"fLaC\x00\x00\x00\x22\x10\x00") is True
    assert is_valid_audio_magic(b"OggS\x00\x02\x00\x00\x00\x00") is True
    assert is_valid_audio_magic(b"RIFF\x24\x00\x00\x00WAVEfmt ") is True
    assert is_valid_audio_magic(b"\x00\x00\x00\x20ftypM4A ") is True
    assert is_valid_audio_magic(b"\xff\xfb\x90\x64\x00\x00\x00") is True
    assert is_valid_audio_magic(b"\xff\xf1\x50\x80\x00\x00\x00") is True

    # Rejection of HTML / JSON error masquerades
    assert is_valid_audio_magic(b"<!DOCTYPE html><html><body>Error</body></html>") is False
    assert is_valid_audio_magic(b"<html><head><title>403 Forbidden</title></head></html>") is False
    assert is_valid_audio_magic(b'{"code": 404, "message": "Song copyright restricted"}') is False
    assert is_valid_audio_magic(b'{"status": "error", "msg": "Token expired"}') is False
    assert is_valid_audio_magic(b"") is False
    assert is_valid_audio_magic(b"abc") is False


def test_stream_head_method_support(client, db_session):
    test_song_id = 88776655
    cached_path = Path(settings.MEDIA_CACHE_DIR) / f"netease_{test_song_id}.mp3"
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(b"ID3\x04" + b"\x00" * 4092)

    track = MediaTrack(
        track_id=f"netease_{test_song_id}",
        source="netease_cache",
        title="Test Song",
        artist="Test Artist",
        file_path=str(cached_path),
        file_size_bytes=4096,
        bitrate_kbps=320,
    )
    db_session.add(track)
    db_session.commit()

    try:
        # 1. HEAD request on /api/v1/music/netease/stream/{id}
        head_resp = client.head(f"/api/v1/music/netease/stream/{test_song_id}?token=test_token")
        assert head_resp.status_code == 200
        assert head_resp.headers.get("Accept-Ranges") == "bytes"
        assert head_resp.headers.get("Content-Type") == "audio/mpeg"
        assert len(head_resp.content) == 0

        # 2. HEAD with Range
        head_range_resp = client.head(
            f"/api/v1/music/netease/stream/{test_song_id}",
            headers={"Range": "bytes=0-1023"},
        )
        assert head_range_resp.status_code == 206
        assert head_range_resp.headers.get("Content-Range") == "bytes 0-1023/4096"
        assert len(head_range_resp.content) == 0

        # 3. HEAD request on /api/v1/music/stream/{track_id}
        track_head_resp = client.head(f"/api/v1/music/stream/netease_{test_song_id}")
        assert track_head_resp.status_code == 200
        assert track_head_resp.headers.get("Accept-Ranges") == "bytes"
        assert len(track_head_resp.content) == 0
    finally:
        if cached_path.exists():
            cached_path.unlink()
        db_session.delete(track)
        db_session.commit()


def test_stream_rejects_html_masquerade(client):
    test_song_id = 77665544
    html_error = b"<html><head><title>404 Not Found</title></head><body>Song unavailable</body></html>"

    with patch.object(
        netease_service,
        "get_song_url",
        new=AsyncMock(return_value={"song_id": test_song_id, "url": "http://fake-cdn/blocked.mp3", "type": "mp3"}),
    ):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200  # Masqueraded 200 OK
            mock_resp.headers = {"content-type": "text/html"}
            mock_resp.content = html_error
            mock_get.return_value = mock_resp

            # When requesting initial bytes (Range 0-1023), backend must detect non-audio and return 502
            resp = client.get(
                f"/api/v1/music/netease/stream/{test_song_id}",
                headers={"Range": "bytes=0-1023"},
            )
            assert resp.status_code == 502
            assert "non-audio" in resp.json()["detail"].lower() or "masquerade" in resp.json()["detail"].lower()


def test_music_command_params_preserves_source_and_song_id():
    from app.schemas.control import MusicCommandRequest

    req = MusicCommandRequest(
        action="play_track",
        params={
            "track_id": "netease_1357375695",
            "source": "netease",
            "song_id": 1357375695,
            "stream_url": "https://example.com/api/v1/music/netease/stream/1357375695",
            "title": "海阔天空",
            "artist": "Beyond",
            "volume": 0.8,
            "seek_ms": 15000,
            "loop_mode": "all",
        },
    )
    dumped = req.params.model_dump(exclude_unset=True)
    assert dumped["source"] == "netease"
    assert dumped["song_id"] == 1357375695
    assert dumped["title"] == "海阔天空"
    assert dumped["artist"] == "Beyond"
    assert dumped["volume"] == 0.8
    assert dumped["seek_ms"] == 15000
    assert dumped["loop_mode"] == "all"


# ---------------------------------------------------------------------------
# Favorite Playlist Endpoint Tests
# ---------------------------------------------------------------------------

def test_favorite_playlist_unauthenticated_returns_401(client):
    resp = client.get("/api/v1/music/netease/playlist/favorite")
    assert resp.status_code == 401


def test_favorite_playlist_car_device_forbidden_returns_403(client, car_headers):
    resp = client.get("/api/v1/music/netease/playlist/favorite", headers=car_headers)
    assert resp.status_code == 403
    assert "Administrator privileges required" in resp.json()["detail"]


def test_favorite_playlist_standard_user_forbidden_returns_403(client, db_session):
    from datetime import timedelta
    from app.repositories.models import User, UserSession, utc_now
    import secrets

    # Create standard user
    test_uid = f"user_{secrets.token_hex(4)}"
    test_session_id = secrets.token_urlsafe(32)
    user = User(
        id=test_uid,
        username=f"standard_{secrets.token_hex(4)}",
        password_hash="fakehash",
        role="user",
        is_active=True,
    )
    session = UserSession(
        session_id=test_session_id,
        user_id=test_uid,
        expires_at=utc_now() + timedelta(days=1),
        is_revoked=False,
    )
    db_session.add(user)
    db_session.add(session)
    db_session.commit()

    user_headers = {"Authorization": f"Bearer {test_session_id}"}
    resp = client.get("/api/v1/music/netease/playlist/favorite", headers=user_headers)
    assert resp.status_code == 403


def test_favorite_playlist_not_logged_in_netease_returns_409(client, admin_headers):
    with patch.object(netease_service.cookie_store, "load", return_value=None):
        resp = client.get("/api/v1/music/netease/playlist/favorite", headers=admin_headers)
        assert resp.status_code == 409
        assert "not logged in" in resp.json()["detail"].lower()


def test_favorite_playlist_success_pagination_and_special_type_identification(client, admin_headers):
    fake_uid = 88887777
    netease_service.invalidate_favorite_cache()

    mock_profile = {"user_id": fake_uid, "nickname": "FavOwner"}
    mock_playlists_data = {
        "code": 200,
        "playlist": [
            {
                "id": 1001,
                "name": "普通歌单",
                "specialType": 0,
                "creator": {"userId": fake_uid},
                "trackCount": 10,
            },
            {
                "id": 1002,
                "name": "我喜欢的音乐(伪)",
                "specialType": 0,
                "creator": {"userId": 999999},  # Other user's playlist with same name
                "trackCount": 20,
            },
            {
                "id": 777777,
                "name": "My Favorite Music",  # English name
                "specialType": 5,  # Official convention for favorite playlist
                "creator": {"userId": fake_uid},
                "trackCount": 45,
                "playCount": 1200,
                "coverImgUrl": "https://p1.music.126.net/fav_cover.jpg",
            },
        ],
    }

    song_objs = [
        {
            "id": 123456,
            "name": "测试歌曲A",
            "ar": [{"id": 1, "name": "歌手A"}],
            "al": {"id": 10, "name": "专辑A", "picUrl": "https://p1.music.126.net/a.jpg"},
            "dt": 180000,
        },
        {
            "id": 654321,
            "name": "测试歌曲B",
            "ar": [{"id": 2, "name": "歌手B"}],
            "al": {"id": 20, "name": "专辑B", "picUrl": "https://p1.music.126.net/b.jpg"},
            "dt": 210000,
        },
    ]
    # One /playlist/detail call carries ordered ids, metadata and privileges (real upstream shape).
    mock_detail_data = {
        "code": 200,
        "playlist": {
            "id": 777777,
            "name": "My Favorite Music",
            "coverImgUrl": "https://p1.music.126.net/fav_cover.jpg",
            "trackCount": 2,
            "trackIds": [{"id": 123456}, {"id": 654321}],
            "tracks": song_objs,
        },
        "privileges": [
            {"id": 123456, "st": 0, "fee": 0},
            {"id": 654321, "st": -200, "fee": 1},  # Unavailable / grayed out copyright
        ],
    }

    with patch.object(netease_service.cookie_store, "load", return_value="MUSIC_U=valid_mock_cookie"):
        with patch.object(netease_service, "get_profile_by_cookie", new_callable=AsyncMock) as mock_prof:
            mock_prof.return_value = mock_profile
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                def fake_get(url, **kwargs):
                    m = MagicMock()
                    m.status_code = 200
                    if "/user/playlist" in url:
                        m.json.return_value = mock_playlists_data
                    elif "/playlist/detail" in url:
                        m.json.return_value = mock_detail_data
                    else:
                        m.json.return_value = {"code": 200}
                    return m

                mock_get.side_effect = fake_get

                resp = client.get("/api/v1/music/netease/playlist/favorite?offset=0&limit=30", headers=admin_headers)
                assert resp.status_code == 200
                data = resp.json()
                assert data["code"] == 200
                assert data["playlist_id"] == 777777  # Matched specialType == 5, not the other two!
                assert data["name"] == "My Favorite Music"
                # Total is the catalog's own consistent count (never a stale trackCount hint).
                assert data["total"] == 2
                assert data["offset"] == 0
                assert data["limit"] == 30
                assert data["has_more"] is False
                assert data["revision"] and data["etag"] and data["owner_key"] and data["stale"] is False
                assert len(data["songs"]) == 2
                assert len(data["tracks"]) == 2

                # Verify first song fields
                song_a = data["songs"][0]
                assert song_a["id"] == 123456
                assert song_a["song_id"] == 123456
                assert song_a["title"] == "测试歌曲A"
                assert song_a["artist"] == "歌手A"
                assert song_a["album"] == "专辑A"
                assert song_a["duration_ms"] == 180000
                assert song_a["source"] == "netease"
                assert song_a["playable"] is True
                assert song_a["fee"] == 0
                assert "/api/v1/music/netease/stream/123456" in song_a["stream_url"]

                # Verify second song (copyright unavailable st < 0)
                song_b = data["songs"][1]
                assert song_b["id"] == 654321
                assert song_b["playable"] is False
                assert song_b["fee"] == 1

                # Second page request inside the TTL window: pure catalog read, zero upstream calls.
                calls_before = mock_get.call_count
                again = client.get(
                    f"/api/v1/music/netease/playlist/favorite?offset=1&limit=1&revision={data['revision']}",
                    headers=admin_headers,
                )
                assert again.status_code == 200
                assert [x["id"] for x in again.json()["songs"]] == [654321]
                assert mock_get.call_count == calls_before


def test_favorite_playlist_out_of_range_offset_returns_empty_page(client, admin_headers):
    fake_uid = 88887777
    netease_service.invalidate_favorite_cache()

    mock_profile = {"user_id": fake_uid, "nickname": "FavOwner"}
    mock_playlists_data = {
        "code": 200,
        "playlist": [
            {
                "id": 777777,
                "name": "我喜欢的音乐",
                "specialType": 5,
                "creator": {"userId": fake_uid},
                "trackCount": 10,
            },
        ],
    }

    with patch.object(netease_service.cookie_store, "load", return_value="MUSIC_U=valid_mock_cookie"):
        with patch.object(netease_service, "get_profile_by_cookie", new_callable=AsyncMock) as mock_prof:
            mock_prof.return_value = mock_profile
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                def fake_get(url, **kwargs):
                    m = MagicMock()
                    m.status_code = 200
                    if "/playlist/detail" in url:
                        songs = [
                            {"id": 500 + i, "name": f"S{i}", "ar": [], "al": {}, "dt": 1000}
                            for i in range(10)
                        ]
                        m.json.return_value = {
                            "code": 200,
                            "playlist": {
                                "id": 777777,
                                "name": "我喜欢的音乐",
                                "trackCount": 10,
                                "trackIds": [{"id": s["id"]} for s in songs],
                                "tracks": songs,
                            },
                            "privileges": [{"id": s["id"], "st": 0, "fee": 0} for s in songs],
                        }
                    else:
                        m.json.return_value = mock_playlists_data
                    return m

                mock_get.side_effect = fake_get

                resp = client.get("/api/v1/music/netease/playlist/favorite?offset=50&limit=30", headers=admin_headers)
                assert resp.status_code == 200
                data = resp.json()
                assert data["total"] == 10
                assert data["offset"] == 50
                assert data["has_more"] is False
                assert data["songs"] == []
                assert data["tracks"] == []


def test_favorite_playlist_limit_validation(client, admin_headers):
    resp_over = client.get("/api/v1/music/netease/playlist/favorite?limit=101", headers=admin_headers)
    assert resp_over.status_code == 422

    resp_neg = client.get("/api/v1/music/netease/playlist/favorite?offset=-1", headers=admin_headers)
    assert resp_neg.status_code == 422


def test_favorite_playlist_upstream_error_propagation(client, admin_headers):
    netease_service.invalidate_favorite_cache()
    mock_profile = {"user_id": 12345, "nickname": "FavOwner"}

    # 1. Upstream 500 error propagation -> HTTP 502
    with patch.object(netease_service.cookie_store, "load", return_value="MUSIC_U=cookie"):
        with patch.object(netease_service, "get_profile_by_cookie", new_callable=AsyncMock) as mock_prof:
            mock_prof.return_value = mock_profile
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                m = MagicMock()
                m.status_code = 500
                mock_get.return_value = m

                resp = client.get("/api/v1/music/netease/playlist/favorite", headers=admin_headers)
                assert resp.status_code == 502
                assert "upstream error" in resp.json()["detail"].lower()

    # 2. Upstream Timeout propagation -> HTTP 504
    netease_service.invalidate_favorite_cache()
    with patch.object(netease_service.cookie_store, "load", return_value="MUSIC_U=cookie"):
        with patch.object(netease_service, "get_profile_by_cookie", new_callable=AsyncMock) as mock_prof:
            mock_prof.return_value = mock_profile
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                mock_get.side_effect = httpx.TimeoutException("Upstream timed out")

                resp = client.get("/api/v1/music/netease/playlist/favorite", headers=admin_headers)
                assert resp.status_code == 504
                assert "timed out" in resp.json()["detail"].lower()


def test_favorite_playlist_cache_invalidation_on_cookie_change():
    netease_service.invalidate_favorite_cache()
    netease_service._cached_favorite_playlist = {"playlist_id": 9999}
    netease_service._cached_cookie_signature = "sig123"

    # Saving a new cookie must invalidate cache
    netease_service.cookie_store.save("MUSIC_U=new_token")
    assert netease_service._cached_favorite_playlist is None
    assert netease_service._cached_cookie_signature is None

    # Setting cache again
    netease_service._cached_favorite_playlist = {"playlist_id": 8888}
    netease_service._cached_cookie_signature = "sig456"

    # Clearing cookie must invalidate cache
    netease_service.cookie_store.clear()
    assert netease_service._cached_favorite_playlist is None
    assert netease_service._cached_cookie_signature is None


def test_favorite_playlist_missing_creator_supplemented_by_detail_success(client, admin_headers):
    fake_uid = 11223344
    netease_service.invalidate_favorite_cache()

    mock_profile = {"user_id": fake_uid, "nickname": "TestUser"}
    # Candidate lacks creator field in user/playlist
    mock_playlists_data = {
        "code": 200,
        "playlist": [
            {
                "id": 998877,
                "name": "我喜欢的音乐",
                "specialType": 5,
                # creator is completely missing
                "trackCount": 15,
            },
        ],
    }
    # But /playlist/detail provides creator with matching userId
    mock_detail_data = {
        "code": 200,
        "playlist": {
            "id": 998877,
            "name": "我喜欢的音乐",
            "creator": {"userId": fake_uid},
            "trackCount": 15,
            "coverImgUrl": "https://example.com/cover.jpg",
        },
    }
    mock_detail_data = {
        **mock_detail_data,
        "playlist": {
            **mock_detail_data["playlist"],
            "trackCount": 1,
            "trackIds": [{"id": 101}],
            "tracks": [{"id": 101, "name": "Song 1", "ar": [], "al": {}, "dt": 120000}],
        },
        "privileges": [{"id": 101, "st": 0, "fee": 0}],
    }

    with patch.object(netease_service.cookie_store, "load", return_value="MUSIC_U=valid_cookie"):
        with patch.object(netease_service, "get_profile_by_cookie", new_callable=AsyncMock) as mock_prof:
            mock_prof.return_value = mock_profile
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                def fake_get(url, **kwargs):
                    m = MagicMock()
                    m.status_code = 200
                    if "/user/playlist" in url:
                        m.json.return_value = mock_playlists_data
                    elif "/playlist/detail" in url:
                        m.json.return_value = mock_detail_data
                    else:
                        m.json.return_value = {"code": 200}
                    return m

                mock_get.side_effect = fake_get

                resp = client.get("/api/v1/music/netease/playlist/favorite", headers=admin_headers)
                assert resp.status_code == 200
                data = resp.json()
                assert data["playlist_id"] == 998877
                assert data["total"] == 1
                assert len(data["songs"]) == 1


def test_favorite_playlist_missing_creator_detail_fails_rejected(client, admin_headers):
    fake_uid = 11223344
    netease_service.invalidate_favorite_cache()

    mock_profile = {"user_id": fake_uid, "nickname": "TestUser"}
    mock_playlists_data = {
        "code": 200,
        "playlist": [
            {
                "id": 998877,
                "name": "我喜欢的音乐",
                "specialType": 5,
                # creator missing
            },
        ],
    }
    # /playlist/detail also fails or has wrong creator
    mock_detail_data = {
        "code": 200,
        "playlist": {
            "id": 998877,
            "creator": {"userId": 99999999},  # Wrong owner
        },
    }

    with patch.object(netease_service.cookie_store, "load", return_value="MUSIC_U=valid_cookie"):
        with patch.object(netease_service, "get_profile_by_cookie", new_callable=AsyncMock) as mock_prof:
            mock_prof.return_value = mock_profile
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                def fake_get(url, **kwargs):
                    m = MagicMock()
                    m.status_code = 200
                    if "/user/playlist" in url:
                        m.json.return_value = mock_playlists_data
                    elif "/playlist/detail" in url:
                        m.json.return_value = mock_detail_data
                    return m

                mock_get.side_effect = fake_get

                resp = client.get("/api/v1/music/netease/playlist/favorite", headers=admin_headers)
                assert resp.status_code == 404
                assert "ownership mismatch" in resp.json()["detail"].lower() or "not found" in resp.json()["detail"].lower()


def test_favorite_playlist_wrong_owner_strictly_rejected(client, admin_headers):
    fake_uid = 11223344
    netease_service.invalidate_favorite_cache()

    mock_profile = {"user_id": fake_uid, "nickname": "TestUser"}
    # Candidate has specialType 5 but belongs to someone else (subscribed favorite playlist)
    mock_playlists_data = {
        "code": 200,
        "playlist": [
            {
                "id": 554433,
                "name": "朋友喜欢的音乐",
                "specialType": 5,
                "creator": {"userId": 88888888},  # NOT fake_uid
                "trackCount": 50,
            },
        ],
    }

    with patch.object(netease_service.cookie_store, "load", return_value="MUSIC_U=valid_cookie"):
        with patch.object(netease_service, "get_profile_by_cookie", new_callable=AsyncMock) as mock_prof:
            mock_prof.return_value = mock_profile
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                m = MagicMock()
                m.status_code = 200
                m.json.return_value = mock_playlists_data
                mock_get.return_value = m

                resp = client.get("/api/v1/music/netease/playlist/favorite", headers=admin_headers)
                assert resp.status_code == 404


def test_favorite_playlist_multiple_matching_candidates_returns_409(client, admin_headers):
    fake_uid = 11223344
    netease_service.invalidate_favorite_cache()

    mock_profile = {"user_id": fake_uid, "nickname": "TestUser"}
    # Two candidates both matching specialType == 5 and creator.userId == fake_uid
    mock_playlists_data = {
        "code": 200,
        "playlist": [
            {
                "id": 10001,
                "name": "喜欢的音乐1",
                "specialType": 5,
                "creator": {"userId": fake_uid},
                "trackCount": 20,
            },
            {
                "id": 10002,
                "name": "喜欢的音乐2",
                "specialType": 5,
                "creator": {"userId": fake_uid},
                "trackCount": 30,
            },
        ],
    }

    with patch.object(netease_service.cookie_store, "load", return_value="MUSIC_U=valid_cookie"):
        with patch.object(netease_service, "get_profile_by_cookie", new_callable=AsyncMock) as mock_prof:
            mock_prof.return_value = mock_profile
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                m = MagicMock()
                m.status_code = 200
                m.json.return_value = mock_playlists_data
                mock_get.return_value = m

                resp = client.get("/api/v1/music/netease/playlist/favorite", headers=admin_headers)
                assert resp.status_code == 409
                assert "ambiguous" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_session_change_during_async_prevents_cache_pollution():
    netease_service.invalidate_favorite_cache()

    # Initial session
    initial_cookie = "MUSIC_U=user_a_cookie"
    new_cookie = "MUSIC_U=user_b_cookie"
    current_cookie_state = initial_cookie

    def mock_load():
        return current_cookie_state

    with patch.object(netease_service.cookie_store, "load", side_effect=mock_load):
        with patch.object(netease_service, "get_profile_by_cookie", new_callable=AsyncMock) as mock_prof:
            mock_prof.return_value = {"user_id": 11111, "nickname": "UserA"}
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                def fake_get(url, **kwargs):
                    nonlocal current_cookie_state
                    # Simulate session cookie changing while waiting for network response
                    current_cookie_state = new_cookie
                    m = MagicMock()
                    m.status_code = 200
                    m.json.return_value = {
                        "code": 200,
                        "playlist": [
                            {"id": 8888, "specialType": 5, "creator": {"userId": 11111}, "trackCount": 10}
                        ],
                    }
                    return m

                mock_get.side_effect = fake_get

                # When calling get_favorite_playlist_info, cookie changes to new_cookie before cache write
                with pytest.raises(PermissionError) as exc_info:
                    await netease_service.get_favorite_playlist_info()

                assert "session changed" in str(exc_info.value).lower()
                # Verify that cache was NOT populated with UserA's playlist!
                assert netease_service._cached_favorite_playlist is None


# ---------------------------------------------------------------------------
# Fake CDN Range / Short-Probe / Container Magic / Protocol Tests
# ---------------------------------------------------------------------------

def _make_fake_cdn_handler(full_audio: bytes, content_type: str = "audio/mpeg", ignore_range: bool = False):
    """
    Build a deterministic Fake CDN handler for httpx.AsyncClient.get that honors
    or ignores HTTP Range headers and records all upstream requests.
    """
    calls = []

    async def fake_get(url, headers=None, **kwargs):
        req_headers = dict(headers or {})
        calls.append({"url": url, "headers": req_headers})
        range_hdr = req_headers.get("Range") or req_headers.get("range")
        total = len(full_audio)

        resp = MagicMock()
        if ignore_range or not range_hdr:
            resp.status_code = 200
            resp.content = full_audio
            resp.headers = {
                "content-type": content_type,
                "content-length": str(total),
                "accept-ranges": "bytes",
            }
            return resp

        from app.services.media_service import parse_range_header, RangeNotSatisfiableError
        try:
            start, end = parse_range_header(range_hdr, total)
        except RangeNotSatisfiableError:
            resp.status_code = 416
            resp.content = b""
            resp.headers = {
                "content-range": f"bytes */{total}",
                "accept-ranges": "bytes",
            }
            return resp

        chunk = full_audio[start : end + 1]
        resp.status_code = 206
        resp.content = chunk
        resp.headers = {
            "content-type": content_type,
            "content-range": f"bytes {start}-{end}/{total}",
            "content-length": str(len(chunk)),
            "accept-ranges": "bytes",
        }
        return resp

    return fake_get, calls


@pytest.mark.parametrize(
    "container_type,header_bytes",
    [
        ("mp3", b"ID3\x04\x00\x00\x00\x00\x00\x23TSSE" + b"\x00" * 50),
        ("wav", b"RIFF\x24\x10\x00\x00WAVEfmt " + b"\x00" * 48),
        ("m4a", b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00" + b"\x00" * 48),
        ("flac", b"fLaC\x00\x00\x00\x22\x10\x00\x10\x00" + b"\x00" * 52),
    ],
)
def test_stream_uncached_short_range_probes_0_1_and_0_0_and_0_1023(client, container_type, header_bytes):
    """
    Verify that short initial probes on uncached tracks:
    - Range: bytes=0-1 (2 bytes, e.g. iOS AVURLAsset initial probe)
    - Range: bytes=0-0 (1 byte, e.g. Car InAppAudioStreamProxy HEAD rewrite)
    - Range: bytes=0-1023 (1024 bytes, e.g. iOS probeStream pre-check)
    succeed with HTTP 206, exact Content-Length and Content-Range matching the client request,
    perform a bounded upstream prefix check (>= 64 bytes, <= 512 bytes for 1-2B probes),
    and do NOT trigger full-track background downloads during probing.
    """
    song_id = 509512457
    full_audio = header_bytes + (b"\x55\xaa\x33\xcc" * 2000)  # > 8 KB
    total_len = len(full_audio)
    fake_get, calls = _make_fake_cdn_handler(full_audio)

    with patch.object(
        netease_service,
        "get_song_url",
        new=AsyncMock(
            return_value={
                "song_id": song_id,
                "url": "https://fake-cdn.music.126.net/track.mp3",
                "type": container_type,
                "br": 320000,
                "size": total_len,
            }
        ),
    ), patch("httpx.AsyncClient.get", side_effect=fake_get), patch.object(
        cache_manager, "cache_track_background", new_callable=AsyncMock
    ) as mock_bg_cache:
        # 1. Range: bytes=0-1 (2 bytes)
        r01 = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=0-1"},
        )
        assert r01.status_code == 206
        assert r01.content == full_audio[:2]
        assert r01.headers["Content-Length"] == "2"
        assert r01.headers["Content-Range"] == f"bytes 0-1/{total_len}"
        assert r01.headers["Accept-Ranges"] == "bytes"
        # Upstream probe must have fetched a bounded validation window, not just 2 bytes
        assert calls[-1]["headers"]["Range"] == "bytes=0-511"
        assert mock_bg_cache.call_count == 0

        # 2. Range: bytes=0-0 (1 byte)
        r00 = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=0-0"},
        )
        assert r00.status_code == 206
        assert r00.content == full_audio[:1]
        assert r00.headers["Content-Length"] == "1"
        assert r00.headers["Content-Range"] == f"bytes 0-0/{total_len}"
        assert calls[-1]["headers"]["Range"] == "bytes=0-511"
        assert mock_bg_cache.call_count == 0

        # 3. Range: bytes=0-1023 (1024 bytes pre-check probe)
        r1023 = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=0-1023"},
        )
        assert r1023.status_code == 206
        assert r1023.content == full_audio[:1024]
        assert r1023.headers["Content-Length"] == "1024"
        assert r1023.headers["Content-Range"] == f"bytes 0-1023/{total_len}"
        assert calls[-1]["headers"]["Range"] == "bytes=0-1023"
        # Probe <= 1024 bytes must NOT trigger background full-song download
        assert mock_bg_cache.call_count == 0


def test_stream_uncached_nonzero_and_suffix_and_playback_ranges(client):
    """
    Verify non-zero Range (bytes=1024-2047), open-ended non-zero Range (bytes=2048-),
    suffix Range (bytes=-500), and large playback start Range (bytes=0-4095) on uncached tracks.
    """
    song_id = 509512458
    full_audio = b"ID3\x04\x00\x00\x00\x00\x00\x23TSSE" + bytes(range(256)) * 20  # 5134 bytes
    total_len = len(full_audio)
    fake_get, calls = _make_fake_cdn_handler(full_audio)

    with patch.object(
        netease_service,
        "get_song_url",
        new=AsyncMock(
            return_value={
                "song_id": song_id,
                "url": "https://fake-cdn.music.126.net/track.mp3",
                "type": "mp3",
                "br": 320000,
                "size": total_len,
            }
        ),
    ), patch("httpx.AsyncClient.get", side_effect=fake_get), patch.object(
        cache_manager, "cache_track_background", new_callable=AsyncMock
    ) as mock_bg_cache:
        # 1. Arbitrary non-zero Range: bytes=1024-2047
        r_mid = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=1024-2047"},
        )
        assert r_mid.status_code == 206
        assert r_mid.content == full_audio[1024:2048]
        assert r_mid.headers["Content-Length"] == "1024"
        assert r_mid.headers["Content-Range"] == f"bytes 1024-2047/{total_len}"
        assert mock_bg_cache.call_count == 1

        # 2. Suffix Range: bytes=-500
        r_suffix = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=-500"},
        )
        assert r_suffix.status_code == 206
        assert r_suffix.content == full_audio[-500:]
        assert r_suffix.headers["Content-Length"] == "500"
        assert r_suffix.headers["Content-Range"] == f"bytes {total_len - 500}-{total_len - 1}/{total_len}"
        assert mock_bg_cache.call_count == 2

        # 3. Large playback Range from 0: bytes=0-4095 (> 1024B) triggers background cache
        r_play = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=0-4095"},
        )
        assert r_play.status_code == 206
        assert r_play.content == full_audio[:4096]
        assert r_play.headers["Content-Length"] == "4096"
        assert r_play.headers["Content-Range"] == f"bytes 0-4095/{total_len}"
        assert mock_bg_cache.call_count == 3


def test_stream_uncached_when_upstream_ignores_range_and_returns_200(client):
    """
    If upstream CDN ignores Range and returns 200 OK with full audio payload,
    backend must still validate audio magic at byte 0, slice the exact requested range,
    and return 206 Partial Content with consistent Content-Range and Content-Length
    (or 416 if requested range is beyond total size).
    """
    song_id = 509512459
    full_audio = b"ID3\x04\x00\x00\x00\x00\x00\x23TSSE" + bytes(range(256)) * 10  # 2574 bytes
    total_len = len(full_audio)
    fake_get, _ = _make_fake_cdn_handler(full_audio, ignore_range=True)

    with patch.object(
        netease_service,
        "get_song_url",
        new=AsyncMock(
            return_value={
                "song_id": song_id,
                "url": "https://fake-cdn.music.126.net/track.mp3",
                "type": "mp3",
                "br": 320000,
                "size": total_len,
            }
        ),
    ), patch("httpx.AsyncClient.get", side_effect=fake_get), patch.object(
        cache_manager, "cache_track_background", new_callable=AsyncMock
    ):
        # 1. Short probe bytes=0-1 against 200-only upstream -> sliced to 2 bytes 206
        r01 = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=0-1"},
        )
        assert r01.status_code == 206
        assert r01.content == full_audio[:2]
        assert r01.headers["Content-Length"] == "2"
        assert r01.headers["Content-Range"] == f"bytes 0-1/{total_len}"

        # 2. Non-zero range bytes=100-199 against 200-only upstream -> sliced to 100 bytes 206
        r_mid = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=100-199"},
        )
        assert r_mid.status_code == 206
        assert r_mid.content == full_audio[100:200]
        assert r_mid.headers["Content-Length"] == "100"
        assert r_mid.headers["Content-Range"] == f"bytes 100-199/{total_len}"

        # 3. Suffix range bytes=-200 against 200-only upstream -> sliced to last 200 bytes 206
        r_suf = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=-200"},
        )
        assert r_suf.status_code == 206
        assert r_suf.content == full_audio[-200:]
        assert r_suf.headers["Content-Length"] == "200"
        assert r_suf.headers["Content-Range"] == f"bytes {total_len - 200}-{total_len - 1}/{total_len}"

        # 4. Out-of-bounds range against 200-only upstream -> 416
        r_416 = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": f"bytes={total_len + 500}-"},
        )
        assert r_416.status_code == 416
        assert r_416.headers["Content-Range"] == f"bytes */{total_len}"


def test_stream_uncached_416_and_invalid_range_headers_and_head(client):
    """
    Verify:
    - Invalid Range headers (syntax error, start > end, suffix 0, multipart) return 416 without hitting CDN
    - Upstream 416 Range Not Satisfiable is preserved as 416 (not converted to 502)
    - Uncached HEAD with and without Range returns accurate 200 / 206 / 416 headers and empty body
    """
    song_id = 509512460
    full_audio = b"ID3\x04\x00\x00\x00\x00\x00\x23TSSE" + b"\x00" * 4000
    total_len = len(full_audio)
    fake_get, calls = _make_fake_cdn_handler(full_audio)

    with patch.object(
        netease_service,
        "get_song_url",
        new=AsyncMock(
            return_value={
                "song_id": song_id,
                "url": "https://fake-cdn.music.126.net/track.mp3",
                "type": "mp3",
                "br": 320000,
                "size": total_len,
            }
        ),
    ), patch("httpx.AsyncClient.get", side_effect=fake_get):
        # 1. Invalid Range headers -> 416 without calling upstream CDN
        for bad_range in ("invalid_range", "bytes=", "bytes=500-200", "bytes=-0", "bytes=0-1, 4-5"):
            res = client.get(
                f"/api/v1/music/netease/stream/{song_id}",
                headers={"Range": bad_range},
            )
            assert res.status_code == 416, f"Expected 416 for {bad_range}, got {res.status_code}"
            assert res.headers.get("Accept-Ranges") == "bytes"
        assert len(calls) == 0

        # 2. Valid Range syntax but beyond EOF -> upstream returns 416 -> preserved as 416
        res_eof = client.get(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": f"bytes={total_len + 1000}-"},
        )
        assert res_eof.status_code == 416
        assert res_eof.headers.get("Content-Range") == f"bytes */{total_len}"

        # 3. HEAD on uncached song without Range -> 200 OK with Content-Length from size
        head_200 = client.head(f"/api/v1/music/netease/stream/{song_id}")
        assert head_200.status_code == 200
        assert head_200.headers.get("Accept-Ranges") == "bytes"
        assert head_200.headers.get("Content-Length") == str(total_len)
        assert len(head_200.content) == 0

        # 4. HEAD on uncached song with Range: bytes=0-1 -> 206 Partial Content
        head_206 = client.head(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=0-1"},
        )
        assert head_206.status_code == 206
        assert head_206.headers.get("Content-Range") == f"bytes 0-1/{total_len}"
        assert head_206.headers.get("Content-Length") == "2"
        assert len(head_206.content) == 0

        # 5. HEAD on uncached song with invalid Range -> 416
        head_416 = client.head(
            f"/api/v1/music/netease/stream/{song_id}",
            headers={"Range": "bytes=999-100"},
        )
        assert head_416.status_code == 416


def test_stream_rejects_error_pages_and_invalid_containers_across_ranges(client):
    """
    Ensure neither short probes (bytes=0-1) nor non-zero ranges (bytes=100-200) nor
    truncated/non-audio containers (e.g. RIFF....WEBP, truncated ftyp) pass as valid audio.
    """
    from app.services.media_service import is_valid_audio_magic

    # Container boundary checks
    assert is_valid_audio_magic(b"RIFF\x24\x00\x00\x00WEBP") is False
    assert is_valid_audio_magic(b"RIFF\x24\x00") is False
    assert is_valid_audio_magic(b"\x00\x00\x00\x18ftyp") is False
    assert is_valid_audio_magic(b"\xff\xff\xff\xff\xff\xff") is False

    song_id = 509512461
    html_payload = b"   \n<!DOCTYPE html><html><body>Copyright Restricted</body></html>" * 20
    fake_get_html, _ = _make_fake_cdn_handler(html_payload, content_type="text/html; charset=utf-8")

    with patch.object(
        netease_service,
        "get_song_url",
        new=AsyncMock(
            return_value={
                "song_id": song_id,
                "url": "https://fake-cdn.music.126.net/error.mp3",
                "type": "mp3",
                "br": 320000,
                "size": len(html_payload),
            }
        ),
    ), patch("httpx.AsyncClient.get", side_effect=fake_get_html), patch.object(
        cache_manager, "cache_track_background", new_callable=AsyncMock
    ) as mock_bg_cache:
        for rng in ("bytes=0-1", "bytes=0-0", "bytes=0-1023", "bytes=100-200", "bytes=-100"):
            res = client.get(
                f"/api/v1/music/netease/stream/{song_id}",
                headers={"Range": rng},
            )
            assert res.status_code == 502, f"Expected 502 for HTML error payload on Range {rng}, got {res.status_code}"
        assert mock_bg_cache.call_count == 0



