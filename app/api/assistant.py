import asyncio
import base64
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
import re
import secrets
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_car_or_admin
from app.api.control import process_and_dispatch_music_command
from app.api.dj import _sync_dj_queue_to_car, dj_store
from app.config import settings
from app.repositories.database import get_db
from app.repositories.models import Favorite, FavoriteFolder, Trip, VehicleState
from app.schemas.auth import AuthenticatedUser
from app.schemas.control import MusicCommandParams, MusicCommandRequest
from app.services.control_service import get_car_status_by_id, music_queue_snapshot_manager
from app.services.device_service import resolve_car_device_id
from app.services.netease_service import netease_service
from app.services.task_service import create_task_for_place

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/assistant", tags=["AI Assistant"])

# --- Bounded in-memory TTS audio cache (MP3 clips, 10-min TTL) ---
_TTS_CACHE: "OrderedDict[str, Tuple[float, bytes]]" = OrderedDict()
_TTS_CACHE_MAX = 40
_TTS_TTL_SECONDS = 600.0

# --- Pending fuzzy place candidates per device (90s TTL) ---
_PENDING_PLACES: Dict[str, Dict[str, Any]] = {}
_PENDING_TTL_SECONDS = 90.0

# --- Lazy-loaded Faster-Whisper singleton ---
_WHISPER_MODEL: Any = None
_WHISPER_LOCK = asyncio.Lock()


class AssistantTextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    lat: Optional[float] = None
    lng: Optional[float] = None
    device_id: Optional[str] = None
    navitime_context: Optional[Dict[str, Any]] = None


class AssistantTtsRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=300)


class AssistantBriefingRequest(BaseModel):
    device_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


def _https_cover(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    url = raw.strip()
    if url.startswith("http://") and "music.126.net" in url:
        return "https://" + url[len("http://") :]
    return url


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _store_tts_clip(mp3_bytes: bytes) -> str:
    now = time.time()
    # Prune expired
    expired = [k for k, (ts, _) in _TTS_CACHE.items() if now - ts > _TTS_TTL_SECONDS]
    for k in expired:
        _TTS_CACHE.pop(k, None)
    while len(_TTS_CACHE) >= _TTS_CACHE_MAX:
        _TTS_CACHE.popitem(last=False)
    clip_id = secrets.token_hex(8)
    _TTS_CACHE[clip_id] = (now, mp3_bytes)
    return f"/api/v1/assistant/tts/{clip_id}.mp3"


_TTS_TEXT_CACHE: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
_LOCAL_TTS_URL = (os.getenv("LOCAL_TTS_URL") or "http://127.0.0.1:8012/tts").strip()


def _normalize_spoken_chinese(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    # Convert common Japanese road/facility terms into natural spoken Chinese
    replacements = [
        ("ランプ", "匝道口"),
        ("インター", "高速口"),
        ("サービスエリア", "服务区"),
        ("パーキングエリア", "停车区"),
        ("パーキング", "停车区"),
        ("道の駅", "公路休息站"),
        ("ジャンクション", "立交桥"),
        ("JCT", "立交"),
        ("IC", "高速口"),
        ("ＩＣ", "高速口"),
        ("SA", "服务区"),
        ("PA", "停车区"),
        ("方面", "方向"),
        ("km/h", "公里每小时"),
        ("km", "公里"),
    ]
    for src, dst in replacements:
        s = s.replace(src, dst)
    # Remove markdown, brackets, and stray Japanese kana that confuse Chinese TTS
    s = re.sub(r"[*#`~\[\]()（）<>]", "", s)
    s = re.sub(r"[\u3040-\u309F\u30A0-\u30FF]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:260]


async def _synthesize_chinese_tts(text: str) -> Optional[str]:
    clean = _normalize_spoken_chinese(text)
    if not clean:
        return None

    # 0. Fast in-memory text cache hit (0ms for repeated lane/turn phrases)
    now = time.time()
    cached = _TTS_TEXT_CACHE.get(clean)
    if cached and (now - cached[0]) < _TTS_TTL_SECONDS:
        clip_id = cached[1].rsplit("/", 1)[-1].replace(".mp3", "")
        if clip_id in _TTS_CACHE:
            return cached[1]

    # 1. Primary: Local R9000P GPU neural Chinese TTS (Kokoro-82M-v1.1-zh)
    if _LOCAL_TTS_URL:
        try:
            async with httpx.AsyncClient(timeout=4.5) as client:
                resp = await client.post(
                    _LOCAL_TTS_URL,
                    json={"text": clean},
                )
                if resp.status_code == 200 and resp.content and len(resp.content) > 512:
                    url = _store_tts_clip(resp.content)
                    _TTS_TEXT_CACHE[clean] = (now, url)
                    while len(_TTS_TEXT_CACHE) > 64:
                        _TTS_TEXT_CACHE.popitem(last=False)
                    return url
        except Exception as exc:
            logger.debug("Local GPU TTS unavailable, falling back to edge-tts: %s", exc)

    # 2. Fallback: edge-tts with strict 6s timeout & natural Xiaoyi voice
    try:
        import edge_tts

        voice = settings.ASSISTANT_TTS_VOICE or "zh-CN-XiaoyiNeural"

        async def _run_edge() -> bytes:
            communicate = edge_tts.Communicate(clean, voice=voice, rate="+0%")
            chunks: List[bytes] = []
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        audio_bytes = await asyncio.wait_for(_run_edge(), timeout=6.0)
        if not audio_bytes:
            return None
        url = _store_tts_clip(audio_bytes)
        _TTS_TEXT_CACHE[clean] = (now, url)
        while len(_TTS_TEXT_CACHE) > 64:
            _TTS_TEXT_CACHE.popitem(last=False)
        return url
    except Exception as exc:
        logger.warning("Edge-TTS synthesis failed: %s", exc)
        return None


def _decode_audio_to_float32_16k(file_path: str):
    import numpy as np

    if file_path.lower().endswith(".wav"):
        import wave

        with wave.open(file_path, "rb") as wf:
            n_ch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        if sw == 2:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if n_ch > 1:
                arr = arr.reshape(-1, n_ch).mean(axis=1)
            if sr == 16000:
                return arr

    import av

    resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
    chunks = []
    with av.open(file_path, mode="r") as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            return np.zeros((0,), dtype=np.float32)
        for frame in container.decode(stream):
            for out_frame in resampler.resample(frame):
                chunks.append(out_frame.to_ndarray().reshape(-1))
        for out_frame in resampler.resample(None):
            chunks.append(out_frame.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros((0,), dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32, copy=False)


def _transcribe_audio_sync(file_path: str) -> str:
    global _WHISPER_MODEL
    audio_arr = _decode_audio_to_float32_16k(file_path)
    if audio_arr.size < 1600:
        return ""

    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel

        model_size = settings.WHISPER_MODEL_SIZE or "small"
        try:
            _WHISPER_MODEL = WhisperModel(model_size, device="cuda", compute_type="float16")
            logger.info("Loaded faster-whisper '%s' on CUDA", model_size)
        except Exception:
            _WHISPER_MODEL = WhisperModel(model_size, device="cpu", compute_type="int8")
            logger.info("Loaded faster-whisper '%s' on CPU (int8)", model_size)

    try:
        segments, _ = _WHISPER_MODEL.transcribe(
            audio_arr,
            beam_size=3,
            vad_filter=True,
            initial_prompt="以下是车载中文语音指令，可能包含日本地名、歌曲名或歌手名。",
        )
        return "".join(seg.text for seg in segments).strip()
    except Exception as exc:
        logger.warning("Whisper transcribe failed on active device (%s), falling back to CPU int8", exc)
        from faster_whisper import WhisperModel

        model_size = settings.WHISPER_MODEL_SIZE or "small"
        _WHISPER_MODEL = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _ = _WHISPER_MODEL.transcribe(
            audio_arr,
            beam_size=3,
            vad_filter=True,
            initial_prompt="以下是车载中文语音指令，可能包含日本地名、歌曲名或歌手名。",
        )
        return "".join(seg.text for seg in segments).strip()


async def _transcribe_audio_bytes(audio_bytes: bytes, suffix: str = ".wav") -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        async with _WHISPER_LOCK:
            return await asyncio.to_thread(_transcribe_audio_sync, tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _resolve_car_gps(
    db: Session, device_id: str, lat: Optional[float], lng: Optional[float]
) -> Tuple[Optional[float], Optional[float]]:
    if lat is not None and lng is not None and not (lat == 0 and lng == 0):
        return float(lat), float(lng)
    row = db.query(VehicleState).filter(VehicleState.device_id == device_id).first()
    if row and row.parked_lat is not None and row.parked_lon is not None:
        return float(row.parked_lat), float(row.parked_lon)
    return None, None


async def _search_google_places(
    query: str,
    lat: Optional[float],
    lng: Optional[float],
    radius_meters: int = 5000,
    max_results: int = 3,
) -> List[Dict[str, Any]]:
    api_key = (settings.GOOGLE_PLACES_API_KEY or settings.GOOGLE_MAPS_API_KEY or "").strip()
    if not api_key:
        raise RuntimeError("未配置 GOOGLE_PLACES_API_KEY")

    payload: Dict[str, Any] = {
        "textQuery": query,
        "languageCode": "zh-CN",
        "maxResultCount": max(1, min(max_results, 5)),
    }
    if lat is not None and lng is not None:
        payload["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(max(500, min(radius_meters, 50000))),
            }
        }

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,"
            "places.location,places.rating,places.userRatingCount"
        ),
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers=headers,
            json=payload,
        )
        if resp.status_code != 200:
            logger.warning("Google Places error %s: %s", resp.status_code, resp.text[:300])
            raise RuntimeError(f"地图搜索服务返回异常({resp.status_code})")
        data = resp.json()

    results: List[Dict[str, Any]] = []
    for p in data.get("places") or []:
        loc = p.get("location") or {}
        plat = loc.get("latitude")
        plng = loc.get("longitude")
        if plat is None or plng is None:
            continue
        name = (p.get("displayName") or {}).get("text") or query
        addr = p.get("formattedAddress") or ""
        rating = p.get("rating")
        dist_km = _haversine_km(lat, lng, float(plat), float(plng)) if (lat is not None and lng is not None) else None
        results.append(
            {
                "name": str(name),
                "address": str(addr),
                "lat": float(plat),
                "lng": float(plng),
                "rating": float(rating) if isinstance(rating, (int, float)) else None,
                "distance_km": round(dist_km, 2) if dist_km is not None else None,
            }
        )
    return results


async def _dispatch_navigation_to_car(
    db: Session, device_id: str, place: Dict[str, Any]
) -> Dict[str, Any]:
    task = await create_task_for_place(
        db=db,
        device_id=device_id,
        title=place["name"],
        address=place.get("address") or "",
        latitude=float(place["lat"]),
        longitude=float(place["lng"]),
        coord_system="wgs84",
    )
    return {
        "task_id": task.task_id,
        "name": place["name"],
        "address": place.get("address") or "",
        "lat": place["lat"],
        "lng": place["lng"],
        "distance_km": place.get("distance_km"),
    }


def _score_netease_track(item: Dict[str, Any], query: str) -> int:
    """Prefers original artist versions and penalizes covers/karaoke/DJ remixes."""
    q_low = (query or "").lower()
    title = str(item.get("title") or item.get("name") or "")
    artist = str(item.get("artist") or "")
    t_low = title.lower()
    a_low = artist.lower()
    score = 0

    # Reward matching query tokens in artist & title
    tokens = [tok for tok in re.split(r"[\s,，、/]+", q_low) if len(tok) >= 1]
    for tok in tokens:
        if tok in a_low:
            score += 45
        if tok in t_low:
            score += 35

    # Penalize covers / instrumentals / DJ remixes unless explicitly requested
    penalties = [
        ("翻唱", 80),
        ("原唱", 85),
        ("cover", 75),
        ("伴奏", 100),
        ("karaoke", 100),
        ("铃声", 100),
        ("beat", 95),
        ("trap", 75),
        ("dj版", 70),
        ("dj ", 65),
        ("remix", 45),
        ("sped up", 60),
        ("slowed", 60),
        ("弦乐", 50),
        ("钢琴", 40),
        ("吉他", 40),
        ("live", 25),
        ("合唱", 30),
    ]
    for kw, pen in penalties:
        if kw in t_low and kw not in q_low:
            score -= pen

    # Slightly prefer standard song length (1.5min - 8min)
    dur_ms = int(item.get("duration_ms") or 0)
    if 90_000 <= dur_ms <= 480_000:
        score += 10
    elif 0 < dur_ms < 60_000:
        score -= 40
    return score


def _rank_netease_candidates(raw_items: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    playable = [
        it for it in raw_items
        if int(it.get("song_id") or it.get("id") or 0) > 0 and it.get("playable") is not False
    ]
    return sorted(playable, key=lambda it: _score_netease_track(it, query), reverse=True)


async def _dispatch_music_search_and_play(
    db: Session,
    device_id: str,
    user: AuthenticatedUser,
    query: str,
    mood_queries: Optional[List[str]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    queries = [q.strip() for q in (mood_queries or []) if isinstance(q, str) and q.strip()]
    if not queries and query.strip():
        queries = [query.strip()]
    if not queries:
        return "请告诉我你想听什么歌或什么风格的音乐。", []

    collected: List[Dict[str, Any]] = []
    seen_ids = set()

    if len(queries) == 1:
        raw = await netease_service.search(queries[0], limit=12)
        ranked = _rank_netease_candidates(raw.get("tracks") or raw.get("songs") or [], queries[0])
        for item in ranked:
            sid = int(item.get("song_id") or item.get("id") or 0)
            if sid <= 0 or sid in seen_ids:
                continue
            seen_ids.add(sid)
            collected.append(
                {
                    "song_id": sid,
                    "title": str(item.get("title") or item.get("name") or ""),
                    "artist": str(item.get("artist") or ""),
                    "album": str(item.get("album") or ""),
                    "duration_ms": int(item.get("duration_ms") or 0),
                    "cover_url": _https_cover(item.get("cover_url")),
                }
            )
            if len(collected) >= 6:
                break
    else:
        for q in queries[:8]:
            try:
                raw = await netease_service.search(q, limit=6)
                ranked = _rank_netease_candidates(raw.get("tracks") or raw.get("songs") or [], q)
                for item in ranked:
                    sid = int(item.get("song_id") or item.get("id") or 0)
                    if sid <= 0 or sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    collected.append(
                        {
                            "song_id": sid,
                            "title": str(item.get("title") or item.get("name") or ""),
                            "artist": str(item.get("artist") or ""),
                            "album": str(item.get("album") or ""),
                            "duration_ms": int(item.get("duration_ms") or 0),
                            "cover_url": _https_cover(item.get("cover_url")),
                        }
                    )
                    break
            except Exception:
                continue

    if not collected:
        return f"没有在网易云找到与「{queries[0]}」相关的可播放歌曲。", []

    first = collected[0]
    artist_part = f"{first['artist']}的" if first["artist"] else ""

    # If a Guest DJ session is currently active on this car and user asked for a single song,
    # insert it into the active DJ queue instead of wiping out friends' queued tracks!
    if len(queries) == 1:
        dj_sess = dj_store._by_device.get(device_id)
        car_row = get_car_status_by_id(db, device_id)
        if dj_sess and dj_sess.queue and car_row and car_row.queue_id == dj_sess.queue_id:
            existing_idx = next(
                (i for i, x in enumerate(dj_sess.queue) if x.get("song_id") == first["song_id"]),
                None,
            )
            if existing_idx is not None:
                target_idx = existing_idx
            else:
                cur_idx = int(car_row.queue_index or 0)
                insert_pos = min(max(cur_idx + 1, 0), len(dj_sess.queue))
                dj_sess.queue.insert(insert_pos, first)
                target_idx = insert_pos
            await _sync_dj_queue_to_car(dj_sess, db, play_index=target_idx, play_now=True)
            return f"好的，已为你插播{artist_part}{first['title']}，原扫码点歌队列已保留。", [first]

    queue_id = f"q_ai_{secrets.token_hex(5)}"
    snap = await music_queue_snapshot_manager.prepare_custom_queue(
        device_id=device_id,
        tracks=collected,
        source="ai_queue",
        name=f"AI 点歌：{queries[0][:12]}",
        queue_id=queue_id,
        activate=True,
        db=db,
    )
    sid = int(first["song_id"])
    cmd = MusicCommandRequest(
        device_id=device_id,
        action="play_track",
        params=MusicCommandParams(
            source="ai_queue",
            queue_id=snap.queue_id,
            queue_version=snap.queue_version,
            queue_index=0,
            song_id=sid,
            track_id=f"netease_{sid}",
            stream_url=f"/api/v1/music/netease/stream/{sid}",
            title=first["title"],
            artist=first["artist"],
            album=first["album"],
            duration_ms=first["duration_ms"],
            cover_url=first["cover_url"],
        ),
    )
    await process_and_dispatch_music_command(
        cmd, user, db, target_device=device_id, source_channel="ai_assistant"
    )
    if len(collected) > 1 and len(queries) > 1:
        reply = f"好的，已为你精选{len(collected)}首歌，先播放{artist_part}{first['title']}。"
    else:
        reply = f"好的，正在为你播放{artist_part}{first['title']}。"
    return reply, collected


def _check_pending_selection_shortcut(text: str) -> Optional[int]:
    """Fast local parser for explicit '去第一家 / 第二个 / 选3 / 取消' when candidates exist."""
    t = re.sub(r"[\s，。！？,.!?]+", "", (text or "").strip())
    if not t:
        return None
    if re.fullmatch(r"(?:取消|算了|不去|不用了|不要了|都不去)", t):
        return -1
    if re.fullmatch(r"(?:去|选|要|到|导航到?)?首个(?:吧|呢)?", t):
        return 1
    m = re.fullmatch(r"(?:去|选|要|到|导航到?)?(?:第)?([一二三123])(?:个|家|号)?(?:吧|呢|啊)?", t)
    if not m:
        return None
    tok = m.group(1)
    if tok in ("一", "1"):
        return 1
    if tok in ("二", "2"):
        return 2
    if tok in ("三", "3"):
        return 3
    return None


def _get_active_pending(device_id: str) -> Optional[List[Dict[str, Any]]]:
    entry = _PENDING_PLACES.get(device_id)
    if not entry:
        return None
    if time.time() - entry["ts"] > _PENDING_TTL_SECONDS:
        _PENDING_PLACES.pop(device_id, None)
        return None
    return entry["places"]


_DEEPSEEK_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": "在车机上搜索并播放音乐。可以播放指定歌曲、歌手，或根据用户的氛围/场景描述生成3到6首歌曲列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "单曲或歌手搜索词，例如 '周杰伦 晴天' 或 '宇多田光 First Love'",
                    },
                    "mood_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "如果用户说的是氛围或风格（如'深夜高速City Pop'、'安静一点的纯音乐'），在这里给出4到6首具体歌名加歌手",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_music",
            "description": "控制车机音乐播放、音量、收藏或离线缓存（暂停、继续播放、下一首、上一首、播放我喜欢的歌、喜欢/收藏当前歌曲、把当前歌曲加入本地缓存、调大或调小音乐/AI播报音量）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "pause",
                            "resume",
                            "next",
                            "previous",
                            "return_favorites",
                            "like_current",
                            "cache_current",
                            "like_and_cache_current",
                            "volume_up",
                            "volume_down",
                        ],
                    },
                    "target": {
                        "type": "string",
                        "enum": ["music", "ai"],
                        "description": "仅用于 volume_up / volume_down：调节音乐音量选 music（默认），调节 AI 播报音量选 ai。",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_place_and_navigate",
            "description": "搜索地点并发起车机导航。如果用户说的是明确地标/具体店铺则 is_fuzzy=false 直接导航；如果是模糊类别（如附近拉面店、加油站、咖啡厅、便利店、停车场）则 is_fuzzy=true 播报前2到3家让用户选。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "适合 Google Maps 在日本检索的准确关键词（可保留日文或中文准确地名）",
                    },
                    "is_fuzzy": {
                        "type": "boolean",
                        "description": "模糊搜索（附近拉面店/加油站/便利店等）为 true；明确目的地（东京塔/羽田机场T3/新宿站等）为 false",
                    },
                    "radius_meters": {
                        "type": "integer",
                        "description": "周边搜索半径（米），默认 5000",
                    },
                },
                "required": ["query", "is_fuzzy"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_place_candidate",
            "description": "当系统刚刚播报了候选地点列表，用户选择去第几家时调用（1、2或3）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "候选序号，从 1 开始（1, 2, 3）",
                    }
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_vehicle_status",
            "description": "查询当前 Yaris 车辆状态（剩余油量、续航里程、12V 电瓶电压是否正常、水温、今天开了多少公里及油耗等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "enum": ["all", "fuel", "battery", "today_trips", "temp"],
                        "description": "用户重点询问的项目：油量/续航选 fuel，电瓶电压选 battery，今日里程/油耗选 today_trips，水温选 temp，综合询问选 all。",
                    }
                },
            },
        },
    },
]


def _format_navitime_prompt_hint(navitime_context: Optional[Dict[str, Any]]) -> str:
    if not isinstance(navitime_context, dict) or not navitime_context:
        return ""
    parts: List[str] = []
    dest = str(navitime_context.get("next_destination_name") or "").strip()
    dest_dist = str(navitime_context.get("next_destination_distance") or "").strip()
    turn_dir = str(navitime_context.get("next_turn_direction") or "").strip()
    if dest or dest_dist:
        parts.append(f"下一关键目标：{dest}（{dest_dist}{'，' + turn_dir if turn_dir else ''}）")
    two_dist = str(navitime_context.get("two_ahead_distance") or "").strip()
    two_dir = str(navitime_context.get("two_ahead_direction") or "").strip()
    if two_dist:
        parts.append(f"再下一个路口：{two_dist}（{two_dir or '连续转向'}）")
    speed_limit = navitime_context.get("speed_limit_kmh")
    if isinstance(speed_limit, (int, float)) and speed_limit > 0:
        parts.append(f"当前路段限速：{int(speed_limit)} km/h")
    eta = str(navitime_context.get("arrival_time") or "").strip()
    rem = str(navitime_context.get("remaining_distance_km") or "").strip()
    if eta or rem:
        parts.append(f"预计到达：{eta}，剩余总里程：{rem}km")
    items = navitime_context.get("upcoming_intersections")
    if isinstance(items, list) and items:
        traffic_cn = {"smooth": "畅通", "slow": "缓行", "congested": "拥堵"}
        lines = []
        for it in items[:5]:
            if not isinstance(it, dict):
                continue
            name = str(it.get("inter_name") or "").strip()
            dist = str(it.get("distance_text") or "").strip()
            t_label = traffic_cn.get(str(it.get("traffic") or ""), "")
            lane = str(it.get("lane_summary") or "").strip()
            seg = f"- {name or '前方路口'} {dist}"
            if t_label:
                seg += f"（路况:{t_label}）"
            if lane:
                seg += f"【车道:{lane}】"
            lines.append(seg)
        if lines:
            parts.append("前方路口与车道：\n" + "\n".join(lines))
    if not parts:
        return ""
    return "\n当前 CAR NAVITIME 实时导航状态：\n" + "\n".join(parts)


def _norm_place_text(raw: str) -> str:
    return re.sub(r"[\s\-_·・()（）\[\]【】.,，。'\"`]+", "", (raw or "").strip().lower())


def _list_saved_favorite_names(db: Session, limit: int = 35) -> List[str]:
    try:
        rows = (
            db.query(Favorite.name)
            .filter(Favorite.name.isnot(None))
            .order_by(Favorite.updated_at.desc())
            .limit(limit)
            .all()
        )
        return [str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()]
    except Exception:
        return []


def _search_saved_favorites(
    db: Session,
    query: str,
    car_lat: Optional[float],
    car_lng: Optional[float],
) -> List[Dict[str, Any]]:
    q_norm = _norm_place_text(query)
    if not q_norm or len(q_norm) < 1:
        return []
    try:
        rows = (
            db.query(Favorite, FavoriteFolder.name)
            .outerjoin(FavoriteFolder, Favorite.folder_id == FavoriteFolder.id)
            .all()
        )
    except Exception as exc:
        logger.warning("Saved favorites query failed: %s", exc)
        return []

    # Extract meaningful alphanumeric & CJK chunks for fuzzy matching
    tokens = [
        t for t in re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff\u3040-\u30ff]{2,}", query.lower())
        if t
    ]

    matches: List[Dict[str, Any]] = []
    for fav, folder_name in rows:
        name = (fav.name or "").strip()
        addr = (fav.address or "").strip()
        folder = (folder_name or "").strip()
        if not name and not addr:
            continue
        name_norm = _norm_place_text(name)
        hay_norm = _norm_place_text(f"{name} {addr} {folder}")

        score = 0
        if name_norm and q_norm == name_norm:
            score = 100
        elif name_norm and (q_norm in name_norm or (len(name_norm) >= 2 and name_norm in q_norm)):
            score = 85
        elif q_norm in hay_norm:
            score = 75
        elif tokens:
            hit_name = sum(1 for t in tokens if t in name_norm)
            hit_hay = sum(1 for t in tokens if t in hay_norm)
            if hit_name == len(tokens):
                score = 80
            elif hit_hay == len(tokens):
                score = 68
            elif hit_name > 0 and hit_name >= (len(tokens) + 1) // 2:
                score = 55

        if score <= 0:
            continue

        dist_km: Optional[float] = None
        if car_lat is not None and car_lng is not None:
            dist_km = round(_haversine_km(car_lat, car_lng, fav.latitude, fav.longitude), 1)
        matches.append(
            {
                "name": name or addr,
                "address": addr or name,
                "lat": float(fav.latitude),
                "lng": float(fav.longitude),
                "rating": None,
                "distance_km": dist_km,
                "from_favorites": True,
                "_score": score,
            }
        )

    matches.sort(key=lambda m: (-m["_score"], m["distance_km"] if m["distance_km"] is not None else 99999.0))
    return matches[:5]


async def _call_deepseek(
    user_text: str,
    pending_places: Optional[List[Dict[str, Any]]],
    navitime_context: Optional[Dict[str, Any]] = None,
    saved_place_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    api_key = (settings.DEEPSEEK_API_KEY or "").strip()
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")

    pending_hint = ""
    if pending_places:
        lines = [
            f"{i + 1}. {p['name']}（{p.get('distance_km', '?')}公里）"
            for i, p in enumerate(pending_places)
        ]
        pending_hint = "\n当前待用户选择的候选地点：\n" + "\n".join(lines)

    fav_hint = ""
    if saved_place_names:
        fav_hint = "\n用户已保存的常用收藏地址（用户提到这些地点时请直接把最接近的收藏名作为 search_place_and_navigate 的 query，且 is_fuzzy 设为 false）：\n" + "、".join(saved_place_names[:35])

    nav_hint = _format_navitime_prompt_hint(navitime_context)

    system_prompt = (
        "你是丰田 Yaris Hybrid 车载 AI 语音助手。"
        "你必须严格遵守以下规则：\n"
        "1. 所有回复必须使用简短、自然、口语化的纯中文（1~2句话），禁止使用 Markdown、表情符号或多余寒暄。\n"
        "2. 用户想找地点或导航时，调用 search_place_and_navigate；注意把模糊搜索（附近拉面店、加油站、咖啡厅、便利店等）的 is_fuzzy 设为 true，明确地标或用户收藏地址设为 false。\n"
        "3. 用户想听歌、换歌、暂停、继续、播放我喜欢的歌、喜欢/收藏/缓存当前歌曲、或调大调小音乐/AI声音时，调用 play_music 或 control_music。\n"
        "4. 用户询问油量、续航、电瓶电压、水温、今天开了多少公里或油耗等车况时，调用 query_vehicle_status。\n"
        "5. 用户在候选列表中选择第几个时，调用 select_place_candidate。\n"
        "6. 如果用户询问前方走哪条车道、前面堵不堵、下个路口、还有多久到等导航问题，直接结合下方的 CAR NAVITIME 实时导航状态用自然中文回答，不要调用导航搜索工具。"
        + pending_hint
        + fav_hint
        + nav_hint
    )

    body = {
        "model": settings.DEEPSEEK_MODEL or "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "tools": _DEEPSEEK_TOOLS,
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": 260,
    }

    base_url = (settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com").rstrip("/")
    async with httpx.AsyncClient(timeout=18.0) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if resp.status_code != 200:
            logger.warning("DeepSeek error %s: %s", resp.status_code, resp.text[:300])
            raise RuntimeError(f"DeepSeek 接口异常({resp.status_code})")
        return resp.json()


async def _handle_assistant_text(
    text: str,
    db: Session,
    user: AuthenticatedUser,
    device_id: str,
    lat: Optional[float],
    lng: Optional[float],
    navitime_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    clean_text = (text or "").strip()
    if not clean_text:
        reply = "我没听清，请再说一遍。"
        tts_url = await _synthesize_chinese_tts(reply)
        return {
            "ok": True,
            "transcript": "",
            "intent": "empty",
            "reply_text": reply,
            "tts_url": tts_url,
            "candidates": [],
        }

    car_lat, car_lng = _resolve_car_gps(db, device_id, lat, lng)
    pending = _get_active_pending(device_id)

    # Fast local shortcut if user is answering a pending 1/2/3 place selection
    if pending:
        pick = _check_pending_selection_shortcut(clean_text)
        if pick == -1:
            _PENDING_PLACES.pop(device_id, None)
            reply = "好的，已取消本次选点。"
            return {
                "ok": True,
                "transcript": clean_text,
                "intent": "cancel_navigation",
                "reply_text": reply,
                "tts_url": await _synthesize_chinese_tts(reply),
                "candidates": [],
            }
        if pick is not None and 1 <= pick <= len(pending):
            chosen = pending[pick - 1]
            _PENDING_PLACES.pop(device_id, None)
            nav_data = await _dispatch_navigation_to_car(db, device_id, chosen)
            reply = f"好的，已为你导航到{chosen['name']}。"
            return {
                "ok": True,
                "transcript": clean_text,
                "intent": "navigate",
                "reply_text": reply,
                "tts_url": await _synthesize_chinese_tts(reply),
                "navigation": nav_data,
                "candidates": [],
            }

    # Ask DeepSeek for tool call or direct Chinese response
    saved_names = _list_saved_favorite_names(db)
    ds_resp = await _call_deepseek(
        clean_text,
        pending,
        navitime_context=navitime_context,
        saved_place_names=saved_names,
    )
    choice = (ds_resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    tool_calls = msg.get("tool_calls") or []

    if not tool_calls:
        reply = str(msg.get("content") or "收到，还有什么需要帮忙吗？").strip()
        return {
            "ok": True,
            "transcript": clean_text,
            "intent": "chat",
            "reply_text": reply,
            "tts_url": await _synthesize_chinese_tts(reply),
            "candidates": [],
        }

    tc = tool_calls[0]
    fn = (tc.get("function") or {}).get("name") or ""
    raw_args = (tc.get("function") or {}).get("arguments") or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except Exception:
        args = {}

    if fn == "play_music":
        reply, tracks = await _dispatch_music_search_and_play(
            db=db,
            device_id=device_id,
            user=user,
            query=str(args.get("query") or clean_text),
            mood_queries=args.get("mood_queries"),
        )
        return {
            "ok": True,
            "transcript": clean_text,
            "intent": "play_music",
            "reply_text": reply,
            "tts_url": await _synthesize_chinese_tts(reply),
            "tracks": tracks[:6],
            "candidates": [],
        }

    if fn == "control_music":
        action = str(args.get("action") or "").strip()
        target = str(args.get("target") or "music").strip().lower()
        if target not in ("music", "ai"):
            target = "music"

        if action == "return_favorites":
            cmd = MusicCommandRequest(
                device_id=device_id,
                action="switch_queue",
                params=MusicCommandParams(source="netease_favorite"),
            )
            await process_and_dispatch_music_command(
                cmd, user, db, target_device=device_id, source_channel="ai_assistant"
            )
            reply = "好的，已为你播放我喜欢的音乐。"
            return {
                "ok": True,
                "transcript": clean_text,
                "intent": "control_music",
                "reply_text": reply,
                "tts_url": await _synthesize_chinese_tts(reply),
                "candidates": [],
            }

        if action in ("volume_up", "volume_down"):
            delta = 0.15 if action == "volume_up" else -0.15
            label = "AI 播报音量" if target == "ai" else "音乐音量"
            reply = f"好的，已帮你调{'大' if delta > 0 else '小'}一点{label}。"
            return {
                "ok": True,
                "transcript": clean_text,
                "intent": "control_music",
                "reply_text": reply,
                "tts_url": await _synthesize_chinese_tts(reply),
                "client_action": {
                    "type": "adjust_volume",
                    "target": target,
                    "delta": delta,
                },
                "candidates": [],
            }

        if action in ("like_current", "cache_current", "like_and_cache_current"):
            car_row = get_car_status_by_id(db, device_id)
            raw_tid = (car_row.current_track_id or "").strip() if car_row else ""
            m = re.search(r"(\d+)", raw_tid)
            song_id = int(m.group(1)) if m else 0
            liked_ok = False
            if action in ("like_current", "like_and_cache_current"):
                if song_id > 0:
                    try:
                        liked_ok = await netease_service.like_song(song_id, like=True)
                    except Exception as exc:
                        logger.warning("Voice like_song(%s) failed: %s", song_id, exc)
            client_action = (
                {"type": "cache_current"}
                if action in ("cache_current", "like_and_cache_current")
                else None
            )
            if action == "cache_current":
                reply = "好的，正在把这首歌缓存到本地存储卡。"
            elif action == "like_and_cache_current":
                reply = (
                    "好的，已把这首歌加入网易云喜欢，并开始缓存到本地。"
                    if liked_ok
                    else "好的，正在把这首歌缓存到本地，网易云收藏暂未成功。"
                )
            else:
                if song_id <= 0:
                    reply = "当前没有识别到正在播放的网易云歌曲。"
                elif liked_ok:
                    reply = "好的，已把这首歌加入你的网易云喜欢。"
                else:
                    reply = "收藏这首歌时出了点问题，请稍后再试。"
            resp_obj: Dict[str, Any] = {
                "ok": True,
                "transcript": clean_text,
                "intent": "control_music",
                "reply_text": reply,
                "tts_url": await _synthesize_chinese_tts(reply),
                "candidates": [],
            }
            if client_action:
                resp_obj["client_action"] = client_action
            return resp_obj

        label_map = {
            "pause": "好的，已暂停音乐。",
            "resume": "好的，继续播放音乐。",
            "next": "好的，已切换到下一首。",
            "previous": "好的，已切换到上一首。",
        }
        if action in label_map:
            cmd = MusicCommandRequest(device_id=device_id, action=action)
            await process_and_dispatch_music_command(
                cmd, user, db, target_device=device_id, source_channel="ai_assistant"
            )
            reply = label_map[action]
        else:
            reply = "暂不支持这个播放控制指令。"
        return {
            "ok": True,
            "transcript": clean_text,
            "intent": "control_music",
            "reply_text": reply,
            "tts_url": await _synthesize_chinese_tts(reply),
            "candidates": [],
        }

    if fn == "select_place_candidate":
        try:
            idx = int(args.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if pending and 1 <= idx <= len(pending):
            chosen = pending[idx - 1]
            _PENDING_PLACES.pop(device_id, None)
            nav_data = await _dispatch_navigation_to_car(db, device_id, chosen)
            reply = f"好的，已为你导航到{chosen['name']}。"
            return {
                "ok": True,
                "transcript": clean_text,
                "intent": "navigate",
                "reply_text": reply,
                "tts_url": await _synthesize_chinese_tts(reply),
                "navigation": nav_data,
                "candidates": [],
            }
        reply = "当前没有待选择的地点，请先告诉我你想去哪里。"
        return {
            "ok": True,
            "transcript": clean_text,
            "intent": "navigate_none",
            "reply_text": reply,
            "tts_url": await _synthesize_chinese_tts(reply),
            "candidates": [],
        }

    if fn == "search_place_and_navigate":
        query = str(args.get("query") or clean_text).strip()
        is_fuzzy = bool(args.get("is_fuzzy", False))
        try:
            radius = int(args.get("radius_meters") or 5000)
        except (TypeError, ValueError):
            radius = 5000

        # 1. Search user's saved favorite addresses FIRST
        fav_matches = _search_saved_favorites(db, query, car_lat, car_lng)
        if fav_matches and fav_matches[0]["_score"] >= 70:
            top_score = fav_matches[0]["_score"]
            clear_winner = (
                len(fav_matches) == 1
                or (top_score >= 80 and fav_matches[0]["_score"] > fav_matches[1]["_score"])
                or (not is_fuzzy and top_score >= 75)
            )
            if clear_winner:
                chosen = {k: v for k, v in fav_matches[0].items() if k != "_score"}
                _PENDING_PLACES.pop(device_id, None)
                nav_data = await _dispatch_navigation_to_car(db, device_id, chosen)
                dist_txt = (
                    f"，距离{chosen['distance_km']}公里"
                    if chosen.get("distance_km") is not None
                    else ""
                )
                reply = f"在你的收藏地址里找到了{chosen['name']}{dist_txt}，已为你发送导航。"
                return {
                    "ok": True,
                    "transcript": clean_text,
                    "intent": "navigate",
                    "reply_text": reply,
                    "tts_url": await _synthesize_chinese_tts(reply),
                    "navigation": nav_data,
                    "candidates": [],
                }
            top_favs = [{k: v for k, v in m.items() if k != "_score"} for m in fav_matches[:3]]
            _PENDING_PLACES[device_id] = {"ts": time.time(), "places": top_favs}
            ordinals = ["第一个", "第二个", "第三个"]
            parts = []
            for i, p in enumerate(top_favs):
                seg = f"{ordinals[i]}{p['name']}"
                if p.get("distance_km") is not None:
                    seg += f"，距离{p['distance_km']}公里"
                parts.append(seg)
            reply = f"在你的收藏地址里找到{len(top_favs)}个地点：" + "；".join(parts) + "。你想去第几个？"
            return {
                "ok": True,
                "transcript": clean_text,
                "intent": "navigate_candidates",
                "reply_text": reply,
                "tts_url": await _synthesize_chinese_tts(reply),
                "candidates": top_favs,
            }

        # 2. Fallback to Google Places if not found in saved addresses
        places = await _search_google_places(
            query=query, lat=car_lat, lng=car_lng, radius_meters=radius, max_results=3
        )
        if not places:
            reply = f"附近没有找到与「{query}」匹配的地点，换个说法试试吧。"
            return {
                "ok": True,
                "transcript": clean_text,
                "intent": "navigate_not_found",
                "reply_text": reply,
                "tts_url": await _synthesize_chinese_tts(reply),
                "candidates": [],
            }

        if not is_fuzzy or len(places) == 1:
            chosen = places[0]
            _PENDING_PLACES.pop(device_id, None)
            nav_data = await _dispatch_navigation_to_car(db, device_id, chosen)
            dist_txt = f"，距离{chosen['distance_km']}公里" if chosen.get("distance_km") is not None else ""
            reply = f"好的，已为你导航到{chosen['name']}{dist_txt}。"
            return {
                "ok": True,
                "transcript": clean_text,
                "intent": "navigate",
                "reply_text": reply,
                "tts_url": await _synthesize_chinese_tts(reply),
                "navigation": nav_data,
                "candidates": [],
            }

        # Fuzzy search -> store top 2-3 places & speak them in Chinese for confirmation
        top_places = places[:3]
        _PENDING_PLACES[device_id] = {"ts": time.time(), "places": top_places}
        ordinals = ["第一家", "第二家", "第三家"]
        parts = []
        for i, p in enumerate(top_places):
            seg = f"{ordinals[i]}{p['name']}"
            if p.get("distance_km") is not None:
                seg += f"，距离{p['distance_km']}公里"
            if p.get("rating") is not None:
                seg += f"，{p['rating']:.1f}分"
            parts.append(seg)
        reply = f"找到{len(top_places)}个地点：" + "；".join(parts) + "。请问要去第几家？"
        return {
            "ok": True,
            "transcript": clean_text,
            "intent": "select_place",
            "reply_text": reply,
            "tts_url": await _synthesize_chinese_tts(reply),
            "candidates": top_places,
        }

    if fn == "query_vehicle_status":
        focus = str(args.get("focus") or "all").strip().lower()
        row = db.query(VehicleState).filter(VehicleState.device_id == device_id).first()
        today_km, today_trips = _query_today_trip_summary(db, device_id)
        if not row and today_trips == 0:
            reply = "暂时还没有收到车辆的最新车况数据。"
        else:
            reply = _build_vehicle_status_reply(row, focus, today_km, today_trips)
        return {
            "ok": True,
            "transcript": clean_text,
            "intent": "vehicle_status",
            "reply_text": reply,
            "tts_url": await _synthesize_chinese_tts(reply),
            "candidates": [],
        }

    reply = "我暂时还不会处理这个指令。"
    return {
        "ok": True,
        "transcript": clean_text,
        "intent": "unknown",
        "reply_text": reply,
        "tts_url": await _synthesize_chinese_tts(reply),
        "candidates": [],
    }


_JST = timezone(timedelta(hours=9))


def _query_today_trip_summary(db: Session, device_id: str) -> Tuple[float, int]:
    """Returns (total_distance_km, trip_count) for today in JST (UTC+9)."""
    try:
        now_jst = datetime.now(_JST)
        start_jst = now_jst.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start_jst.astimezone(timezone.utc)
        rows = (
            db.query(Trip.distance_km)
            .filter(Trip.device_id == device_id, Trip.started_at >= start_utc)
            .all()
        )
        total_km = sum(float(r[0] or 0.0) for r in rows)
        return round(total_km, 1), len(rows)
    except Exception as exc:
        logger.warning("Today trip summary query failed: %s", exc)
        return 0.0, 0


def _extract_km_per_l(row: Optional[VehicleState]) -> Optional[float]:
    if not row:
        return None
    if row.extra_json:
        try:
            extra = json.loads(row.extra_json)
            if isinstance(extra, dict):
                for key in ("avg_km_per_l", "km_per_l", "fuel_economy_km_l"):
                    v = extra.get(key)
                    if isinstance(v, (int, float)) and 3.0 <= v <= 60.0:
                        return round(float(v), 1)
        except Exception:
            pass
    if (
        row.estimated_range_km is not None
        and row.fuel_level_liters is not None
        and row.fuel_level_liters >= 2.0
    ):
        est = row.estimated_range_km / row.fuel_level_liters
        if 5.0 <= est <= 50.0:
            return round(est, 1)
    return None


def _build_vehicle_status_reply(
    row: Optional[VehicleState],
    focus: str,
    today_km: float,
    today_trips: int,
) -> str:
    km_per_l = _extract_km_per_l(row)

    if focus == "battery":
        v = row.aux_12v_voltage if row else None
        if v is None:
            return "目前还没有读到 12 伏电瓶电压数据，请确认蓝牙 OBD 已连接。"
        if v >= 12.4:
            status = "非常健康"
        elif v >= 12.0:
            status = "略微偏低，建议多留意"
        else:
            status = "偏低，注意亏电风险"
        return f"当前 12 伏电瓶电压是 {v:.2f} 伏，状态{status}。"

    if focus == "fuel":
        bits = []
        if row and row.fuel_level_percent is not None:
            bits.append(f"剩余油量约百分之{round(row.fuel_level_percent)}")
        if row and row.fuel_level_liters is not None:
            bits.append(f"约 {row.fuel_level_liters:.1f} 升")
        if row and row.estimated_range_km is not None:
            bits.append(f"预计还能行驶 {round(row.estimated_range_km)} 公里")
        return "，".join(bits) + "。" if bits else "暂时还没有读到油量数据。"

    if focus == "today_trips":
        parts = []
        if today_trips > 0:
            parts.append(f"今天一共开了 {today_trips} 段行程，合计 {today_km:.1f} 公里")
        else:
            parts.append("今天还没有记录到行驶里程")
        if km_per_l is not None:
            l_per_100 = round(100.0 / km_per_l, 1)
            parts.append(f"近期参考油耗约每升 {km_per_l:.1f} 公里，折合百公里 {l_per_100:.1f} 升")
        return "，".join(parts) + "。"

    if focus == "temp":
        if row and row.coolant_temp is not None:
            return f"当前发动机水温 {round(row.coolant_temp)} 度。"
        return "暂时还没有读到水温数据。"

    bits = []
    if row and row.fuel_level_percent is not None:
        bits.append(f"剩余油量百分之{round(row.fuel_level_percent)}")
    if row and row.estimated_range_km is not None:
        bits.append(f"续航约 {round(row.estimated_range_km)} 公里")
    if row and row.aux_12v_voltage is not None:
        bits.append(f"电瓶 {row.aux_12v_voltage:.1f} 伏")
    if today_trips > 0:
        bits.append(f"今天已行驶 {today_km:.1f} 公里")
    if km_per_l is not None:
        bits.append(f"参考油耗每升 {km_per_l:.1f} 公里")
    if row and row.coolant_temp is not None:
        bits.append(f"水温 {round(row.coolant_temp)} 度")
    return "当前车况：" + "，".join(bits) + "。" if bits else "车辆在线，但暂无详细传感器数值。"


def _wmo_weather_cn(code: Optional[int]) -> str:
    if code is None:
        return ""
    if code == 0:
        return "晴朗"
    if code in (1, 2):
        return "多云"
    if code == 3:
        return "阴天"
    if code in (45, 48):
        return "有雾"
    if code in (51, 53, 55, 56, 57):
        return "有毛毛雨"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "有雨"
    if code in (71, 73, 75, 77, 85, 86):
        return "有雪"
    if code in (95, 96, 99):
        return "有雷阵雨"
    return ""


async def _fetch_brief_weather(lat: Optional[float], lng: Optional[float]) -> Optional[str]:
    if lat is None or lng is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=3.5) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": round(lat, 4),
                    "longitude": round(lng, 4),
                    "current": "temperature_2m,weather_code",
                    "daily": "precipitation_probability_max",
                    "forecast_days": 1,
                    "timezone": "Asia/Tokyo",
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            cur = data.get("current") or {}
            temp = cur.get("temperature_2m")
            wcode = cur.get("weather_code")
            desc = _wmo_weather_cn(int(wcode)) if isinstance(wcode, (int, float)) else ""
            daily = data.get("daily") or {}
            probs = daily.get("precipitation_probability_max") or []
            rain_prob = int(probs[0]) if probs and isinstance(probs[0], (int, float)) else None

            segs = []
            if desc and isinstance(temp, (int, float)):
                segs.append(f"车外{desc}，气温 {round(temp)} 度")
            elif isinstance(temp, (int, float)):
                segs.append(f"车外气温 {round(temp)} 度")
            if rain_prob is not None and rain_prob >= 40:
                segs.append(f"今天降水概率百分之{rain_prob}，注意雨天路滑")
            return "，".join(segs) if segs else None
    except Exception as exc:
        logger.debug("Open-Meteo weather fetch skipped: %s", exc)
        return None


@router.post("/briefing")
async def assistant_startup_briefing(
    req: AssistantBriefingRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
) -> Dict[str, Any]:
    device_id = resolve_car_device_id(req.device_id or user.device_id)
    now_jst = datetime.now(_JST)
    hour = now_jst.hour
    if 5 <= hour < 11:
        greeting = "早上好"
    elif 11 <= hour < 14:
        greeting = "中午好"
    elif 14 <= hour < 18:
        greeting = "下午好"
    else:
        greeting = "晚上好"

    car_lat, car_lng = _resolve_car_gps(db, device_id, req.lat, req.lng)
    row = db.query(VehicleState).filter(VehicleState.device_id == device_id).first()
    weather_txt = await _fetch_brief_weather(car_lat, car_lng)

    parts = [greeting]
    if row and row.fuel_level_percent is not None:
        fuel_pct = round(row.fuel_level_percent)
        if row.estimated_range_km is not None:
            parts.append(f"油量百分之{fuel_pct}，预计续航 {round(row.estimated_range_km)} 公里")
        else:
            parts.append(f"油量百分之{fuel_pct}")
        if fuel_pct <= 20:
            parts.append("油量偏低记得顺路加油")
    if row and row.aux_12v_voltage is not None and row.aux_12v_voltage < 12.2:
        parts.append(f"12伏电瓶电压 {row.aux_12v_voltage:.1f} 伏略偏低")
    if weather_txt:
        parts.append(weather_txt)
    parts.append("祝你一路顺风")

    reply = "，".join(parts) + "。"
    tts_url = await _synthesize_chinese_tts(reply)
    return {
        "ok": True,
        "intent": "startup_briefing",
        "reply_text": reply,
        "tts_url": tts_url,
    }


@router.get("/status")
def assistant_status(user: AuthenticatedUser = Depends(require_car_or_admin)) -> Dict[str, Any]:
    return {
        "deepseek_configured": bool((settings.DEEPSEEK_API_KEY or "").strip()),
        "google_places_configured": bool(
            (settings.GOOGLE_PLACES_API_KEY or settings.GOOGLE_MAPS_API_KEY or "").strip()
        ),
        "tts_voice": settings.ASSISTANT_TTS_VOICE,
        "whisper_model": settings.WHISPER_MODEL_SIZE,
    }


@router.get("/tts/{clip_id}.mp3")
def get_assistant_tts(clip_id: str) -> Response:
    entry = _TTS_CACHE.get(clip_id)
    if not entry:
        raise HTTPException(status_code=404, detail="语音播报片段已过期")
    _, mp3_bytes = entry
    return Response(
        content=mp3_bytes,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.post("/tts")
async def synthesize_assistant_tts(
    req: AssistantTtsRequest,
    user: AuthenticatedUser = Depends(require_car_or_admin),
) -> Dict[str, Any]:
    tts_url = await _synthesize_chinese_tts(req.text)
    if not tts_url:
        raise HTTPException(status_code=503, detail="语音合成暂不可用")
    return {"ok": True, "tts_url": tts_url}


@router.post("/text")
async def assistant_text_command(
    req: AssistantTextRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
) -> Dict[str, Any]:
    device_id = resolve_car_device_id(req.device_id or user.device_id)
    try:
        return await _handle_assistant_text(
            text=req.text,
            db=db,
            user=user,
            device_id=device_id,
            lat=req.lat,
            lng=req.lng,
            navitime_context=req.navitime_context,
        )
    except Exception as exc:
        logger.exception("Assistant text error")
        reply = "抱歉，AI 助手刚才开了个小差，请稍后再试。"
        return {
            "ok": False,
            "error": str(exc),
            "transcript": req.text,
            "intent": "error",
            "reply_text": reply,
            "tts_url": await _synthesize_chinese_tts(reply),
            "candidates": [],
        }


_VISUAL_NAV_RE = re.compile(
    r"(前面怎么走|怎么走|哪条道|哪条车道|走哪边|看图|路牌|匝道|立交|分岔|高速口|路口|限速|堵不堵|拥堵)"
)


async def _try_vision_nav_explain(
    user_text: str,
    navitime_context: Optional[Dict[str, Any]],
    screenshot_bytes: Optional[bytes],
) -> Optional[str]:
    if not screenshot_bytes or not _VISUAL_NAV_RE.search(user_text or ""):
        return None
    vision_key = (os.getenv("VISION_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if not vision_key:
        return None
    vision_url = (
        os.getenv("VISION_API_URL") or "https://api.openai.com/v1/chat/completions"
    ).strip()
    vision_model = (os.getenv("VISION_MODEL") or "gpt-4o-mini").strip()
    b64_img = base64.b64encode(screenshot_bytes).decode("ascii")
    ctx_hint = _format_navitime_prompt_hint(navitime_context)
    payload = {
        "model": vision_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是丰田 Yaris Hybrid 车载导航副驾助手。请结合 CAR NAVITIME 实时画面和文字数据，"
                    "用1到2句极其简洁自然的纯中文口语告诉驾驶员怎么走、走哪条蓝色推荐车道、以及路况。"
                    "禁止使用 Markdown 或表情符号。" + ctx_hint
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"},
                    },
                ],
            },
        ],
        "max_tokens": 180,
        "temperature": 0.2,
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                vision_url,
                headers={
                    "Authorization": f"Bearer {vision_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    except Exception as exc:
        logger.warning("Vision nav explain fallback: %s", exc)
    return None


@router.post("/voice")
async def assistant_voice_command(
    audio: UploadFile = File(...),
    lat: Optional[float] = Form(None),
    lng: Optional[float] = Form(None),
    device_id: Optional[str] = Form(None),
    navitime_context: Optional[str] = Form(None),
    navitime_screenshot: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
) -> Dict[str, Any]:
    audio_bytes = await audio.read()
    if not audio_bytes or len(audio_bytes) < 1000:
        reply = "录音太短了，请按住或点开后说完再点结束。"
        return {
            "ok": False,
            "transcript": "",
            "intent": "empty",
            "reply_text": reply,
            "tts_url": await _synthesize_chinese_tts(reply),
            "candidates": [],
        }
    if len(audio_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="语音文件过大")

    suffix = os.path.splitext(audio.filename or "voice.wav")[1] or ".wav"
    target_device = resolve_car_device_id(device_id or user.device_id)
    parsed_nav_ctx: Optional[Dict[str, Any]] = None
    if navitime_context and navitime_context.strip():
        try:
            raw_ctx = json.loads(navitime_context)
            if isinstance(raw_ctx, dict):
                parsed_nav_ctx = raw_ctx
        except Exception:
            parsed_nav_ctx = None

    screenshot_bytes: Optional[bytes] = None
    if navitime_screenshot is not None:
        raw_shot = await navitime_screenshot.read()
        if raw_shot and len(raw_shot) <= 2 * 1024 * 1024:
            screenshot_bytes = raw_shot

    try:
        transcript = await _transcribe_audio_bytes(audio_bytes, suffix=suffix)
        vision_reply = await _try_vision_nav_explain(
            transcript, parsed_nav_ctx, screenshot_bytes
        )
        if vision_reply:
            return {
                "ok": True,
                "transcript": transcript,
                "intent": "vision_nav_guide",
                "reply_text": vision_reply,
                "tts_url": await _synthesize_chinese_tts(vision_reply),
                "candidates": [],
            }
        return await _handle_assistant_text(
            text=transcript,
            db=db,
            user=user,
            device_id=target_device,
            lat=lat,
            lng=lng,
            navitime_context=parsed_nav_ctx,
        )
    except Exception as exc:
        logger.exception("Assistant voice error")
        reply = "抱歉，语音处理出现了一点问题，请稍后再试。"
        return {
            "ok": False,
            "error": str(exc),
            "transcript": "",
            "intent": "error",
            "reply_text": reply,
            "tts_url": await _synthesize_chinese_tts(reply),
            "candidates": [],
        }
