import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.database import SessionLocal
from app.repositories.models import MediaTrack, utc_now
from app.services.favorite_catalog import FavoriteCatalog
from app.services.media_service import is_valid_audio_magic, record_track_access

logger = logging.getLogger(__name__)


class NeteaseUpstreamError(Exception):
    """Raised when upstream Netease API returns an error or fails to connect."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class NeteaseCookieStore:
    """Manages persistent encrypted/local storage of Netease user authorization Cookie."""

    def __init__(self, storage_path: Optional[Path] = None):
        self.path = storage_path or (Path(settings.DATA_DIR) / "netease_cookie.json")
        self.on_change_callbacks: List[Any] = []
        self.on_clear_callbacks: List[Any] = []

    def _trigger_on_clear(self):
        for cb in self.on_clear_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(f"Error in cookie on_clear callback: {e}")

    def _trigger_on_change(self):
        for cb in self.on_change_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(f"Error in cookie on_change callback: {e}")

    def save(self, cookie: str, profile: Optional[dict] = None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cookie": cookie,
            "updated_at": utc_now().isoformat(),
            "profile": profile or {},
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Netease cookie successfully saved to {self.path}")
        self._trigger_on_change()

    def load(self) -> Optional[str]:
        if not self.path.exists():
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("cookie")
        except Exception as e:
            logger.warning(f"Failed to read Netease cookie file: {e}")
            return None

    def get_info(self) -> dict:
        if not self.path.exists():
            return {"logged_in": False, "cookie": None, "profile": None, "updated_at": None}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "logged_in": bool(data.get("cookie")),
                    "cookie": data.get("cookie"),
                    "profile": data.get("profile"),
                    "updated_at": data.get("updated_at"),
                }
        except Exception:
            return {"logged_in": False, "cookie": None, "profile": None, "updated_at": None}

    def clear(self):
        if self.path.exists():
            try:
                self.path.unlink()
                logger.info("Netease cookie cleared from disk")
            except OSError:
                pass
        self._trigger_on_change()
        self._trigger_on_clear()

    @property
    def has_cookie(self) -> bool:
        return self.load() is not None


class NeteaseService:
    """Interfaces with the upstream NeteaseCloudMusicApi container."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.NETEASE_API_BASE_URL).rstrip("/")
        self.cookie_store = NeteaseCookieStore()
        self.cookie_store.on_change_callbacks.append(self.invalidate_favorite_cache)
        self._cached_favorite_playlist: Optional[Dict[str, Any]] = None
        self._cached_cookie_signature: Optional[str] = None
        self._cached_fav_timestamp: float = 0.0
        self._fav_cache_ttl_seconds: float = 300.0  # 5 minutes TTL
        self._cached_favorite_track_ids: Optional[set] = None

    def invalidate_favorite_cache(self):
        """Invalidates cached favorite playlist metadata upon session changes or manual refresh."""
        self._cached_favorite_playlist = None
        self._cached_cookie_signature = None
        self._cached_fav_timestamp = 0.0
        self._cached_favorite_track_ids = None

    def _with_cookie_param(self, params: Optional[dict] = None) -> dict:
        p = dict(params or {})
        cookie = self.cookie_store.load()
        if cookie:
            p["cookie"] = cookie
        return p

    async def get_qr_key(self) -> str:
        url = f"{self.base_url}/login/qr/key"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"timestamp": int(utc_now().timestamp() * 1000)})
            data = resp.json()
            return data.get("data", {}).get("unikey", "")

    async def create_qr(self, key: str) -> Dict[str, Any]:
        url = f"{self.base_url}/login/qr/create"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"key": key, "qrimg": "true", "timestamp": int(utc_now().timestamp() * 1000)})
            data = resp.json()
            return data.get("data", {})

    async def check_qr(self, key: str) -> Dict[str, Any]:
        url = f"{self.base_url}/login/qr/check"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"key": key, "timestamp": int(utc_now().timestamp() * 1000)})
            data = resp.json()
            code = data.get("code")

            if code == 803:
                cookie = data.get("cookie", "")
                profile = await self.get_profile_by_cookie(cookie)
                self.cookie_store.save(cookie, profile)
                nickname = profile.get("nickname") if profile else None
                return {
                    "code": 803,
                    "status": "authorized",
                    "message": "登录成功",
                    "cookie_saved": True,
                    "nickname": nickname,
                }
            elif code == 800:
                return {"code": 800, "status": "expired", "message": "二维码已过期", "cookie_saved": False}
            elif code == 801:
                return {"code": 801, "status": "waiting_scan", "message": "等待扫码", "cookie_saved": False}
            elif code == 802:
                return {"code": 802, "status": "waiting_confirm", "message": "待确认授权", "cookie_saved": False}
            else:
                return {"code": code or 500, "status": "unknown", "message": data.get("message", "未知状态"), "cookie_saved": False}

    async def get_profile_by_cookie(self, cookie: Optional[str] = None, strict: bool = False) -> Optional[dict]:
        """Resolve the account profile for a cookie.

        strict=False (login flows): any failure yields None.
        strict=True (Owner verification): an authoritative "no such session" answer raises
        PermissionError, while connectivity/5xx problems raise NeteaseUpstreamError, so callers
        can tell an expired login from an offline upstream.
        """
        c = cookie or self.cookie_store.load()
        if not c:
            return None
        url = f"{self.base_url}/login/status"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params={"cookie": c, "timestamp": int(utc_now().timestamp() * 1000)})
                if strict and resp.status_code in (401, 403):
                    raise PermissionError("Netease session credentials expired or invalid")
                if strict and resp.status_code != 200:
                    raise NeteaseUpstreamError(
                        f"Upstream login status returned HTTP {resp.status_code}", status_code=502
                    )
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                data = data if isinstance(data, dict) else {}
                profile = data.get("profile") or {}
                account = data.get("account") or {}
                if profile or account:
                    return {
                        "user_id": account.get("id") or profile.get("userId"),
                        "nickname": profile.get("nickname") or account.get("userName"),
                        "avatar_url": profile.get("avatarUrl"),
                        "vip_type": profile.get("vipType") or account.get("vipType"),
                    }
                if strict and isinstance(body, dict) and body.get("code") in (200, 301, 401):
                    raise PermissionError("Netease session credentials expired or invalid")
        except (PermissionError, NeteaseUpstreamError):
            raise
        except httpx.TimeoutException as e:
            logger.warning(f"Timeout fetching profile: {e}")
            if strict:
                raise NeteaseUpstreamError("Upstream Netease login status timed out", status_code=504)
        except Exception as e:
            logger.warning(f"Failed to fetch profile: {e}")
            if strict:
                raise NeteaseUpstreamError("Upstream Netease login status unavailable", status_code=502)
        return None

    async def refresh_cookie(self) -> bool:
        cookie = self.cookie_store.load()
        if not cookie:
            return False
        url = f"{self.base_url}/login/refresh"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params={"cookie": cookie, "timestamp": int(utc_now().timestamp() * 1000)})
                data = resp.json()
                if data.get("code") == 200:
                    new_cookie = data.get("cookie")
                    if new_cookie:
                        self.cookie_store.save(new_cookie)
                    return True
        except Exception as e:
            logger.warning(f"Failed to refresh cookie: {e}")
        return False

    async def logout(self) -> bool:
        cookie = self.cookie_store.load()
        if cookie:
            url = f"{self.base_url}/logout"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.get(url, params={"cookie": cookie})
            except Exception:
                pass
        self.cookie_store.clear()
        return True

    async def search(self, keyword: str, page: int = 1, limit: int = 20) -> Dict[str, Any]:
        offset = max(0, (page - 1) * limit)
        url = f"{self.base_url}/cloudsearch"
        params = self._with_cookie_param({"keywords": keyword, "offset": offset, "limit": limit, "type": 1})

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    logger.warning(f"Upstream Netease search returned status {resp.status_code}")
                    raise NeteaseUpstreamError(
                        f"Upstream Netease search returned status {resp.status_code}",
                        status_code=502,
                    )
                data = resp.json()
        except httpx.TimeoutException as e:
            logger.error(f"Timeout calling Netease search: {e}")
            raise NeteaseUpstreamError("Upstream Netease search timed out", status_code=504)
        except httpx.RequestError as e:
            logger.error(f"Network error calling Netease search: {e}")
            raise NeteaseUpstreamError(f"Upstream Netease search connection failed: {e}", status_code=502)
        except NeteaseUpstreamError:
            raise
        except Exception as e:
            logger.error(f"Error calling Netease search: {e}")
            raise NeteaseUpstreamError(f"Failed to query upstream Netease search: {e}", status_code=502)

        code = data.get("code")
        if code is not None and code != 200:
            logger.warning(f"Upstream Netease search returned business code {code}: {data.get('message')}")
            raise NeteaseUpstreamError(f"Upstream Netease search returned code {code}", status_code=502)

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
            privilege = s.get("privilege") or {}
            st = privilege.get("st", 0)
            fee = s.get("fee", 0)

            songs.append({
                "id": song_id,
                "title": s.get("name") or "Unknown Title",
                "artist": artist_str,
                "album": al_dict.get("name"),
                "duration_ms": s.get("dt") or 0,
                "cover_url": al_dict.get("picUrl"),
                "fee": fee,
                "playable": bool(st >= 0),
            })

        return {"total": total, "songs": songs}

    async def get_song_url(self, song_id: int, level: str = "exhigh") -> Dict[str, Any]:
        """Fetch CDN audio streaming URL for a song."""
        # Try song/url/v1 first
        url_v1 = f"{self.base_url}/song/url/v1"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                params = self._with_cookie_param({"id": song_id, "level": level})
                resp = await client.get(url_v1, params=params)
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
                params = self._with_cookie_param({"id": song_id, "br": 320000})
                resp = await client.get(url_std, params=params)
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

    async def get_song_detail(self, song_id: int) -> Dict[str, Any]:
        """Fetch metadata for a single song from upstream /song/detail."""
        url = f"{self.base_url}/song/detail"
        params = self._with_cookie_param({"ids": song_id})
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    songs = data.get("songs", [])
                    if songs:
                        s = songs[0]
                        ar_names = [ar.get("name", "") for ar in s.get("ar", []) if ar.get("name")]
                        al = s.get("al") or {}
                        return {
                            "id": song_id,
                            "name": s.get("name") or f"Track {song_id}",
                            "artist": " / ".join(ar_names) or "Unknown Artist",
                            "album": al.get("name"),
                            "duration_ms": s.get("dt", 0),
                            "cover_url": al.get("picUrl"),
                            "fee": s.get("fee", 0),
                        }
        except Exception as e:
            logger.warning(f"Error fetching song detail for {song_id}: {e}")
        return {
            "id": song_id,
            "name": f"Track {song_id}",
            "artist": "Unknown Artist",
            "album": None,
            "duration_ms": 0,
            "cover_url": None,
            "fee": 0,
        }

    async def is_song_in_favorite_playlist(self, song_id: int) -> bool:
        """
        Verifies whether a song belongs to the current verified owner's favorite playlist.
        Fetches track IDs via upstream /playlist/detail.
        """
        if self._cached_favorite_track_ids is not None:
            return song_id in self._cached_favorite_track_ids

        fav_info = await self.get_favorite_playlist_info()
        pid = fav_info["playlist_id"]
        detail = await self.get_playlist_detail(pid)
        track_ids = set()
        for item in detail.get("trackIds", []):
            if isinstance(item, dict) and "id" in item:
                track_ids.add(item["id"])
            elif isinstance(item, int):
                track_ids.add(item)
        if not track_ids and detail.get("tracks"):
            for t in detail.get("tracks", []):
                if isinstance(t, dict) and "id" in t:
                    track_ids.add(t["id"])
        self._cached_favorite_track_ids = track_ids
        return song_id in track_ids

    async def get_intelligence(self, seed_song_id: int, playlist_id: Optional[int] = None, count: int = 30) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Generate authentic intelligence (心动模式) recommended playlist from upstream /playmode/intelligence/list.
        Strictly forbids fallback to simi/song or empty lists pretending success.
        Raises PermissionError if login credentials are missing or expired (code 301/401).
        Raises NeteaseUpstreamError if upstream fails or times out.
        """
        cookie = self.cookie_store.load()
        if not cookie:
            raise PermissionError("Netease Cloud Music account is not logged in on backend.")

        url_intel = f"{self.base_url}/playmode/intelligence/list"
        params = self._with_cookie_param({"id": seed_song_id, "pid": playlist_id or ""})

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url_intel, params=params)
                if resp.status_code != 200:
                    raise NeteaseUpstreamError(
                        f"Upstream intelligence returned HTTP {resp.status_code}",
                        status_code=502,
                    )
                data = resp.json()
        except httpx.TimeoutException as e:
            logger.error(f"Timeout calling Netease intelligence for song {seed_song_id}: {e}")
            raise NeteaseUpstreamError("Upstream Netease intelligence query timed out", status_code=504)
        except httpx.RequestError as e:
            logger.error(f"Network error calling Netease intelligence for song {seed_song_id}: {e}")
            raise NeteaseUpstreamError(f"Upstream Netease intelligence connection failed: {e}", status_code=502)
        except (PermissionError, NeteaseUpstreamError):
            raise
        except Exception as e:
            logger.error(f"Error calling Netease intelligence for song {seed_song_id}: {e}")
            raise NeteaseUpstreamError(f"Failed to query upstream Netease intelligence: {e}", status_code=502)

        code = data.get("code")
        if code in (301, 401) or "need login" in str(data.get("message", "")).lower():
            raise PermissionError("Netease session credentials expired or invalid")
        if code is not None and code != 200:
            raise NeteaseUpstreamError(
                f"Upstream intelligence returned code {code}: {data.get('message')}",
                status_code=502,
            )

        raw_list = data.get("data") or []
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
                "fee": s.get("fee", 0),
                "playable": None,  # Rule 3: null when unverified
            })

        if not parsed:
            raise NeteaseUpstreamError("No tracks returned from upstream intelligence API", status_code=503)

        return "intelligence", parsed

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

    async def get_playlist_detail(self, playlist_id: int) -> Dict[str, Any]:
        """Fetch detailed playlist metadata from upstream /playlist/detail."""
        url = f"{self.base_url}/playlist/detail"
        params = self._with_cookie_param({"id": playlist_id})
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    raise NeteaseUpstreamError(
                        f"Upstream playlist detail returned HTTP {resp.status_code}",
                        status_code=502,
                    )
                data = resp.json()
        except httpx.TimeoutException as e:
            logger.error(f"Timeout calling Netease playlist/detail for {playlist_id}: {e}")
            raise NeteaseUpstreamError("Upstream Netease playlist detail query timed out", status_code=504)
        except httpx.RequestError as e:
            logger.error(f"Network error calling Netease playlist/detail for {playlist_id}: {e}")
            raise NeteaseUpstreamError(f"Upstream Netease playlist detail connection failed: {e}", status_code=502)
        except NeteaseUpstreamError:
            raise
        except Exception as e:
            logger.error(f"Error calling Netease playlist/detail for {playlist_id}: {e}")
            raise NeteaseUpstreamError(f"Failed to query upstream Netease playlist detail: {e}", status_code=502)

        code = data.get("code")
        if code is not None and code != 200:
            raise NeteaseUpstreamError(
                f"Upstream playlist detail returned code {code}: {data.get('message')}",
                status_code=502,
            )
        return data.get("playlist") or {}

    async def get_favorite_playlist_info(
        self, force_refresh: bool = False, max_age: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Resolves non-sensitive metadata for the current account's system favorite playlist (我喜欢的音乐).
        Uses third-party upstream API (Binaryify/NeteaseCloudMusicApi) specification:
        - specialType == 5 is treated strictly as a candidate type hint.
        - creator.userId MUST strictly match current valid owner UID.
        - If creator is missing in user/playlist item, attempts upstream playlist/detail to supplement and verify.
        - Never silently accepts missing creator fields.
        - Rejects candidate if creator does not match or cannot be proven.
        - Rejects with clear error if multiple candidates match or none match.
        - Invalidates cache immediately if session changes during async resolution.
        """
        cookie = self.cookie_store.load()
        if not cookie:
            raise PermissionError("Netease Cloud Music account is not logged in on backend.")

        cookie_sig = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
        now = time.time()
        if (
            not force_refresh
            and self._cached_favorite_playlist
            and self._cached_cookie_signature == cookie_sig
            and (now - self._cached_fav_timestamp)
            < (self._fav_cache_ttl_seconds if max_age is None else min(max_age, self._fav_cache_ttl_seconds))
        ):
            return dict(self._cached_favorite_playlist)

        profile = await self.get_profile_by_cookie(cookie, strict=True)
        if not profile or not profile.get("user_id"):
            raise NeteaseUpstreamError(
                "Failed to obtain Netease user profile from current session",
                status_code=502,
            )

        uid = profile["user_id"]
        url = f"{self.base_url}/user/playlist"
        # timestamp bypasses the upstream container's 2-minute GET response cache.
        params = self._with_cookie_param(
            {"uid": uid, "limit": 50, "offset": 0, "timestamp": int(time.time() * 1000)}
        )

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    raise NeteaseUpstreamError(
                        f"Upstream user playlist query returned HTTP {resp.status_code}",
                        status_code=502,
                    )
                data = resp.json()
        except httpx.TimeoutException as e:
            logger.error(f"Timeout calling Netease user/playlist: {e}")
            raise NeteaseUpstreamError("Upstream Netease user playlist query timed out", status_code=504)
        except httpx.RequestError as e:
            logger.error(f"Network error calling Netease user/playlist: {e}")
            raise NeteaseUpstreamError(f"Upstream Netease user playlist connection failed: {e}", status_code=502)
        except NeteaseUpstreamError:
            raise
        except Exception as e:
            logger.error(f"Error calling Netease user/playlist: {e}")
            raise NeteaseUpstreamError(f"Failed to query upstream Netease user playlist: {e}", status_code=502)

        code = data.get("code")
        if code is not None and code != 200:
            raise NeteaseUpstreamError(
                f"Upstream user playlist returned code {code}: {data.get('message')}",
                status_code=502,
            )

        playlists = data.get("playlist", [])
        special_candidates = [pl for pl in playlists if pl.get("specialType") == 5]
        if not special_candidates:
            raise NeteaseUpstreamError(
                "Owner favorite playlist (specialType==5) not found in user playlists",
                status_code=404,
            )

        verified_owner_candidates = []
        for pl in special_candidates:
            pid = pl.get("id")
            creator_id = (pl.get("creator") or {}).get("userId") or pl.get("userId")

            # If creator_id is missing, do NOT silently accept; supplement via upstream playlist/detail
            if creator_id is None and pid:
                try:
                    detail = await self.get_playlist_detail(pid)
                    creator_id = (detail.get("creator") or {}).get("userId") or detail.get("userId")
                    # Supplement metadata from detail if available
                    if detail:
                        pl = {
                            **pl,
                            "name": detail.get("name") or pl.get("name"),
                            "coverImgUrl": detail.get("coverImgUrl") or pl.get("coverImgUrl"),
                            "trackCount": detail.get("trackCount", pl.get("trackCount", 0)),
                            "playCount": detail.get("playCount", pl.get("playCount", 0)),
                        }
                except Exception as e:
                    logger.warning(f"Failed to fetch detail for candidate playlist {pid}: {e}")
                    creator_id = None

            # Strictly verify that creator_id matches current owner uid
            if creator_id is not None and creator_id == uid:
                verified_owner_candidates.append(pl)
            else:
                logger.info(
                    f"Candidate playlist {pid} rejected: creator_id={creator_id} does not match owner uid={uid}"
                )

        if len(verified_owner_candidates) == 0:
            raise NeteaseUpstreamError(
                "No favorite playlist (specialType==5) verified for current account owner (ownership mismatch or missing creator verification)",
                status_code=404,
            )

        if len(verified_owner_candidates) > 1:
            raise NeteaseUpstreamError(
                f"Ambiguous favorite playlist: found {len(verified_owner_candidates)} candidates matching specialType==5 for owner",
                status_code=409,
            )

        matched = verified_owner_candidates[0]
        fav_info = {
            "playlist_id": matched["id"],
            "name": matched.get("name") or "我喜欢的音乐",
            "cover_url": matched.get("coverImgUrl"),
            "track_count": matched.get("trackCount", 0),
            "play_count": matched.get("playCount", 0),
            "user_id": uid,
            # Cheap version hints (not trusted alone; the catalog also compares the like-id set).
            "signals": [
                matched.get("trackUpdateTime"),
                matched.get("trackNumberUpdateTime"),
                matched.get("trackCount"),
            ],
        }

        # Check if the session cookie changed during async execution to prevent cache pollution
        current_cookie = self.cookie_store.load()
        current_cookie_sig = hashlib.sha256(current_cookie.encode("utf-8")).hexdigest() if current_cookie else None
        if current_cookie_sig == cookie_sig:
            self._cached_favorite_playlist = fav_info
            self._cached_cookie_signature = cookie_sig
            self._cached_fav_timestamp = time.time()
            return dict(fav_info)
        else:
            logger.warning("Netease session cookie changed during async favorite playlist resolution; discarding stale cache.")
            raise PermissionError("Netease session changed during resolution; please retry.")

    # ------------------------------------------------------------------
    # Favorite playlist catalog sources (consumed by FavoriteCatalog)
    # ------------------------------------------------------------------
    async def _upstream_json(
        self,
        path: str,
        params: Dict[str, Any],
        *,
        timeout: float,
        what: str,
    ) -> Dict[str, Any]:
        """GET an upstream JSON object with auth/connectivity errors kept distinct."""
        url = f"{self.base_url}{path}"
        p = self._with_cookie_param({**params, "timestamp": int(time.time() * 1000)})
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, params=p)
                if resp.status_code in (401, 403):
                    raise PermissionError("Netease session credentials expired or invalid")
                if resp.status_code != 200:
                    raise NeteaseUpstreamError(f"Upstream {what} returned HTTP {resp.status_code}", status_code=502)
                data = resp.json()
        except (PermissionError, NeteaseUpstreamError):
            raise
        except httpx.TimeoutException as e:
            logger.error(f"Timeout calling Netease {what}: {e}")
            raise NeteaseUpstreamError(f"Upstream Netease {what} timed out", status_code=504)
        except httpx.RequestError as e:
            logger.error(f"Network error calling Netease {what}: {e}")
            raise NeteaseUpstreamError(f"Upstream Netease {what} connection failed: {e}", status_code=502)
        except Exception as e:
            logger.error(f"Error calling Netease {what}: {e}")
            raise NeteaseUpstreamError(f"Failed to query upstream {what}: {e}", status_code=502)
        if not isinstance(data, dict):
            raise NeteaseUpstreamError(f"Upstream {what} returned a non-object body", status_code=502)
        code = data.get("code")
        if code in (301, 401):
            raise PermissionError("Netease session credentials expired or invalid")
        if code is not None and code != 200:
            raise NeteaseUpstreamError(f"Upstream {what} returned code {code}: {data.get('message')}", status_code=502)
        return data

    @staticmethod
    def _normalize_track(s: Dict[str, Any], priv: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        song_id = s.get("id")
        if not isinstance(song_id, int) or isinstance(song_id, bool) or song_id <= 0:
            return None
        priv = priv or s.get("privilege") or {}
        st = priv.get("st", s.get("st", 0))
        fee = priv.get("fee", s.get("fee", 0))
        ar_names = [ar.get("name", "") for ar in (s.get("ar") or []) if isinstance(ar, dict) and ar.get("name")]
        al_dict = s.get("al") or {}
        return {
            "id": song_id,
            "title": s.get("name") or "Unknown Title",
            "artist": " / ".join(ar_names) or "Unknown Artist",
            "album": al_dict.get("name"),
            "duration_ms": s.get("dt") or 0,
            "cover_url": al_dict.get("picUrl"),
            "fee": fee if isinstance(fee, int) and not isinstance(fee, bool) else 0,
            "playable": bool((st if isinstance(st, (int, float)) else 0) >= 0),
        }

    async def get_like_ids(self, uid: Any) -> List[int]:
        """Unordered liked-song id set (~7 KB for ~750 songs); authoritative for membership."""
        data = await self._upstream_json("/likelist", {"uid": uid}, timeout=15.0, what="likelist")
        ids = data.get("ids")
        if not isinstance(ids, list) or any(not isinstance(i, int) or isinstance(i, bool) for i in ids):
            raise NeteaseUpstreamError("Upstream likelist returned an unexpected body", status_code=502)
        return ids

    async def fetch_favorite_playlist_detail(self, playlist_id: int) -> Dict[str, Any]:
        """One ordered fetch: ids, metadata and privileges come from a single /playlist/detail call.

        Ids absent from the embedded track list are filled via bounded /song/detail batches;
        ids upstream cannot describe at all are reported in ``skipped_ids`` (never invented).
        """
        data = await self._upstream_json("/playlist/detail", {"id": playlist_id}, timeout=45.0, what="playlist detail")
        playlist = data.get("playlist")
        if not isinstance(playlist, dict) or playlist.get("id") != playlist_id:
            raise NeteaseUpstreamError("Upstream playlist detail did not return the requested playlist", status_code=502)

        ids: List[int] = []
        seen: set = set()
        for item in playlist.get("trackIds") or []:
            tid = item.get("id") if isinstance(item, dict) else item
            if isinstance(tid, int) and not isinstance(tid, bool) and tid > 0 and tid not in seen:
                seen.add(tid)
                ids.append(tid)

        tracks: Dict[int, Dict[str, Any]] = {
            t["id"]: t for t in (playlist.get("tracks") or []) if isinstance(t, dict) and isinstance(t.get("id"), int)
        }
        privileges: Dict[int, Dict[str, Any]] = {
            p["id"]: p for p in (data.get("privileges") or []) if isinstance(p, dict) and isinstance(p.get("id"), int)
        }

        missing = [i for i in ids if i not in tracks]
        for start in range(0, min(len(missing), 2000), 100):
            chunk = missing[start : start + 100]
            extra = await self._upstream_json(
                "/song/detail", {"ids": ",".join(str(i) for i in chunk)}, timeout=20.0, what="song detail"
            )
            for t in extra.get("songs") or []:
                if isinstance(t, dict) and isinstance(t.get("id"), int):
                    tracks[t["id"]] = t
            for p in extra.get("privileges") or []:
                if isinstance(p, dict) and isinstance(p.get("id"), int):
                    privileges[p["id"]] = p

        entries: List[Dict[str, Any]] = []
        skipped: List[int] = []
        for tid in ids:
            norm = self._normalize_track(tracks[tid], privileges.get(tid)) if tid in tracks else None
            if norm is None:
                skipped.append(tid)
            else:
                entries.append(norm)
        return {
            "playlist_id": playlist_id,
            "name": playlist.get("name"),
            "cover_url": playlist.get("coverImgUrl"),
            "track_count": playlist.get("trackCount"),
            "entries": entries,
            "skipped_ids": skipped,
        }

    # ------------------------------------------------------------------
    # Favorite playlist reads (catalog backed; never one upstream call per page)
    # ------------------------------------------------------------------
    @staticmethod
    def _catalog_page(snap, meta: Dict[str, Any], offset: int, limit: int) -> Dict[str, Any]:
        page = list(snap.tracks[offset : offset + limit])
        total = snap.total
        return {
            "playlist_id": snap.playlist_id,
            "name": snap.name,
            "cover_url": snap.cover_url,
            "track_count": total,
            "play_count": meta.get("play_count"),
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + len(page)) < total,
            "songs": [dict(t) for t in page],
            "revision": snap.revision,
            "etag": snap.etag,
            "owner_key": snap.owner_key,
            "synced_at": snap.updated_at,
            "checked_at": meta.get("checked_at"),
            "stale": bool(meta.get("stale")),
            "stale_reason": meta.get("stale_reason"),
        }

    async def get_favorite_catalog_page(
        self,
        offset: int = 0,
        limit: int = 30,
        revision: Optional[int] = None,
        fresh: bool = False,
    ) -> Dict[str, Any]:
        """Page the Owner favorite catalog. ``revision`` pins one immutable revision."""
        snap, meta = await favorite_catalog.view(revision, fresh=fresh)
        return self._catalog_page(snap, meta, offset, limit)

    async def get_favorite_playlist_tracks(
        self,
        offset: int = 0,
        limit: int = 30,
        revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Queue-building read: checks upstream first (TTL coalesced) so explicit playback uses the new version."""
        return await self.get_favorite_catalog_page(offset, limit, revision, fresh=revision is None)


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

    def get_cached_ids(self, db: Session, song_ids: List[int]) -> set:
        """Batch variant of get_cached_track: one query for a whole page of songs."""
        if not song_ids:
            return set()
        wanted = {self.get_track_id(i): i for i in song_ids}
        rows = db.query(MediaTrack).filter(MediaTrack.track_id.in_(list(wanted))).all()
        return {
            wanted[t.track_id]
            for t in rows
            if t.track_id in wanted and os.path.isfile(t.file_path) and os.path.getsize(t.file_path) > 0
        }

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
                    first_chunk = True
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            if not chunk:
                                continue
                            if first_chunk:
                                if not is_valid_audio_magic(chunk):
                                    logger.error(f"Upstream stream for song {song_id} failed audio magic check: {chunk[:32]!r}")
                                    raise ValueError(f"Upstream returned non-audio stream for song {song_id} (error/HTML masquerade)")
                                first_chunk = False
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

    async def cache_track_background(
        self,
        song_id: int,
        remote_url: str,
        metadata: Dict[str, Any],
        db_factory,
    ):
        """Downloads full song in background and registers to disk and database cache."""
        lock = await self._get_song_lock(song_id)
        if lock.locked():
            return  # Already being cached by another task
        async with lock:
            db = db_factory()
            try:
                if self.get_cached_track(db, song_id):
                    return  # Already cached
            finally:
                db.close()

            tmp_path = self.get_temp_path(song_id)
            final_path = self.get_target_path(song_id, metadata.get("type", "mp3"))

            try:
                async with httpx.AsyncClient(timeout=45.0) as client:
                    async with client.stream("GET", remote_url) as resp:
                        if resp.status_code >= 400:
                            logger.warning(f"Background cache failed for {song_id}: HTTP {resp.status_code}")
                            return
                        total_bytes = 0
                        first_chunk = True
                        with open(tmp_path, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                if not chunk:
                                    continue
                                if first_chunk:
                                    if not is_valid_audio_magic(chunk):
                                        logger.warning(f"Background cache failed magic check for song {song_id}: {chunk[:32]!r}")
                                        return
                                    first_chunk = False
                                f.write(chunk)
                                total_bytes += len(chunk)

                if total_bytes > 0:
                    os.replace(tmp_path, final_path)
                    logger.info(f"Background cache completed for song {song_id} ({total_bytes} bytes) -> {final_path}")
                    db = db_factory()
                    try:
                        self._register_cached_track(db, song_id, str(final_path), total_bytes, metadata)
                        self.enforce_lru_quota(db)
                    finally:
                        db.close()
            except Exception as e:
                logger.warning(f"Background cache failed for song {song_id}: {e}")
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

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
favorite_catalog = FavoriteCatalog(netease_service)
netease_service.cookie_store.on_change_callbacks.append(favorite_catalog.invalidate)
netease_service.cookie_store.on_clear_callbacks.append(favorite_catalog.clear)
