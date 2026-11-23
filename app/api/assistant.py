import asyncio
from collections import OrderedDict
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
from app.config import settings
from app.repositories.database import get_db
from app.repositories.models import VehicleState
from app.schemas.auth import AuthenticatedUser
from app.schemas.control import MusicCommandParams, MusicCommandRequest
from app.services.control_service import music_queue_snapshot_manager
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


async def _synthesize_chinese_tts(text: str) -> Optional[str]:
    clean = re.sub(r"[*#`~\[\]()（）<>]", "", (text or "")).strip()
    if not clean:
        return None
    if len(clean) > 260:
        clean = clean[:260]
    try:
        import edge_tts

        voice = settings.ASSISTANT_TTS_VOICE or "zh-CN-XiaoxiaoNeural"
        communicate = edge_tts.Communicate(clean, voice=voice, rate="+6%")
        chunks: List[bytes] = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.append(chunk["data"])
        if not chunks:
            return None
        return _store_tts_clip(b"".join(chunks))
    except Exception as exc:
        logger.warning("Edge-TTS synthesis failed: %s", exc)
        return None


def _transcribe_audio_sync(file_path: str) -> str:
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel

        model_size = settings.WHISPER_MODEL_SIZE or "small"
        try:
            _WHISPER_MODEL = WhisperModel(model_size, device="cuda", compute_type="float16")
            logger.info("Loaded faster-whisper '%s' on CUDA", model_size)
        except Exception:
            _WHISPER_MODEL = WhisperModel(model_size, device="cpu", compute_type="int8")
            logger.info("Loaded faster-whisper '%s' on CPU (int8)", model_size)

    segments, _ = _WHISPER_MODEL.transcribe(
        file_path,
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
    if row and row.last_lat is not None and row.last_lng is not None:
        return float(row.last_lat), float(row.last_lng)
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
        raw = await netease_service.search(queries[0], limit=8)
        for item in raw.get("tracks") or raw.get("songs") or []:
            sid = int(item.get("song_id") or item.get("id") or 0)
            if sid <= 0 or sid in seen_ids or item.get("playable") is False:
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
                raw = await netease_service.search(q, limit=3)
                for item in raw.get("tracks") or raw.get("songs") or []:
                    sid = int(item.get("song_id") or item.get("id") or 0)
                    if sid <= 0 or sid in seen_ids or item.get("playable") is False:
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
    first = collected[0]
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
    artist_part = f"{first['artist']}的" if first["artist"] else ""
    if len(collected) > 1 and len(queries) > 1:
        reply = f"好的，已为你精选{len(collected)}首歌，先播放{artist_part}{first['title']}。"
    else:
        reply = f"好的，正在为你播放{artist_part}{first['title']}。"
    return reply, collected


def _check_pending_selection_shortcut(text: str) -> Optional[int]:
    """Fast local parser for '去第一家 / 第二个 / 选3 / 取消' when candidates exist."""
    t = text.strip()
    if any(k in t for k in ("取消", "算了", "不去", "不用了")):
        return -1
    if re.search(r"(第?一|第?1|第一个|第一家|首个)", t):
        return 1
    if re.search(r"(第?二|第?2|第二个|第二家)", t):
        return 2
    if re.search(r"(第?三|第?3|第三个|第三家)", t):
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
            "description": "控制车机当前音乐播放（暂停、继续播放、下一首、上一首）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pause", "resume", "next", "previous"],
                    }
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
            "description": "查询当前 Yaris 车辆状态（油量、水温、车速、里程等）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


async def _call_deepseek(
    user_text: str, pending_places: Optional[List[Dict[str, Any]]]
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

    system_prompt = (
        "你是丰田 Yaris Hybrid 车载 AI 语音助手。"
        "你必须严格遵守以下规则：\n"
        "1. 所有回复必须使用简短、自然、口语化的纯中文（1~2句话），禁止使用 Markdown、表情符号或多余寒暄。\n"
        "2. 用户想找地点或导航时，调用 search_place_and_navigate；注意把模糊搜索（附近拉面店、加油站、咖啡厅、便利店等）的 is_fuzzy 设为 true，明确地标设为 false。\n"
        "3. 用户想听歌、换歌、找氛围歌单时，调用 play_music 或 control_music。\n"
        "4. 用户在候选列表中选择第几个时，调用 select_place_candidate。"
        + pending_hint
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
    ds_resp = await _call_deepseek(clean_text, pending)
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
        idx = int(args.get("index") or 0)
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
        radius = int(args.get("radius_meters") or 5000)
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
        row = db.query(VehicleState).filter(VehicleState.device_id == device_id).first()
        if not row:
            reply = "暂时还没有收到车辆的最新车况数据。"
        else:
            bits = []
            if row.fuel_level_pct is not None:
                bits.append(f"剩余油量约百分之{round(row.fuel_level_pct)}")
            if row.coolant_temp_c is not None:
                bits.append(f"水温{round(row.coolant_temp_c)}度")
            if row.odometer_km is not None:
                bits.append(f"总里程{round(row.odometer_km)}公里")
            reply = "当前车况：" + "，".join(bits) + "。" if bits else "车辆在线，但暂无详细传感器数值。"
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


@router.post("/text")
async def assistant_text_command(
    req: AssistantTextRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
) -> Dict[str, Any]:
    device_id = resolve_car_device_id(req.device_id or user.device_id)
    try:
        return await _handle_assistant_text(
            text=req.text, db=db, user=user, device_id=device_id, lat=req.lat, lng=req.lng
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


@router.post("/voice")
async def assistant_voice_command(
    audio: UploadFile = File(...),
    lat: Optional[float] = Form(None),
    lng: Optional[float] = Form(None),
    device_id: Optional[str] = Form(None),
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

    try:
        transcript = await _transcribe_audio_bytes(audio_bytes, suffix=suffix)
        return await _handle_assistant_text(
            text=transcript,
            db=db,
            user=user,
            device_id=target_device,
            lat=lat,
            lng=lng,
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
