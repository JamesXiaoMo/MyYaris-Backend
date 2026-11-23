from datetime import datetime, timezone
import io
import math
import random
import secrets
import struct
import time
from typing import Any, Dict, List, Optional
import wave

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_car_or_admin
from app.api.control import process_and_dispatch_music_command
from app.config import settings
from app.repositories.database import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.control import MusicCommandParams, MusicCommandRequest
from app.services.control_service import (
    car_status_to_response,
    get_car_status_by_id,
    music_queue_snapshot_manager,
    ws_manager,
)
from app.services.device_service import resolve_car_device_id
from app.services.netease_service import netease_service

router = APIRouter(tags=["Guest DJ"])


class _GuestSession:
    def __init__(self, token: str, device_id: str, expires_at: float):
        self.token = token
        self.device_id = device_id
        self.expires_at = expires_at
        self.queue_id = f"q_dj_{secrets.token_hex(6)}"
        self.queue: List[Dict[str, Any]] = []
        self.track_cache: Dict[str, Dict[str, Any]] = {}


class _GuestDjStore:
    def __init__(self) -> None:
        self._by_device: Dict[str, _GuestSession] = {}
        self._by_token: Dict[str, _GuestSession] = {}

    def create_or_get(self, device_id: str, force_new: bool = False) -> _GuestSession:
        now = time.time()
        existing = self._by_device.get(device_id)
        if existing and not force_new and existing.expires_at > now + 300:
            return existing
        if existing:
            self._by_token.pop(existing.token, None)
        token = secrets.token_urlsafe(18)
        sess = _GuestSession(
            token=token,
            device_id=device_id,
            expires_at=now + max(300, int(settings.DJ_SESSION_TTL_SECONDS)),
        )
        if existing:
            sess.queue = list(existing.queue)
        self._by_device[device_id] = sess
        self._by_token[token] = sess
        return sess

    def revoke(self, device_id: str) -> bool:
        existing = self._by_device.pop(device_id, None)
        if not existing:
            return False
        self._by_token.pop(existing.token, None)
        return True

    def validate(self, token: Optional[str]) -> _GuestSession:
        if not token or not token.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="缺少点歌会话令牌，请重新扫描车机二维码",
            )
        sess = self._by_token.get(token.strip())
        if not sess or sess.expires_at <= time.time():
            if sess:
                self._by_token.pop(sess.token, None)
                if self._by_device.get(sess.device_id) is sess:
                    self._by_device.pop(sess.device_id, None)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="点歌二维码已过期或已关闭，请在车机重新扫码",
            )
        return sess


dj_store = _GuestDjStore()


def _extract_token(
    t: Optional[str] = Query(None),
    x_dj_token: Optional[str] = Header(None, alias="X-DJ-Token"),
) -> str:
    return (x_dj_token or t or "").strip()


def _https_cover(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    url = raw.strip()
    if url.startswith("http://") and "music.126.net" in url:
        return "https://" + url[len("http://") :]
    return url


def _guest_car_user(device_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        identity=f"guest_dj_{device_id}",
        role="car",
        device_id=device_id,
        has_device_control=True,
    )


class DjPlayRequest(BaseModel):
    song_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=1, max_length=200)
    artist: str = Field("", max_length=200)
    album: str = Field("", max_length=200)
    duration_ms: int = Field(0, ge=0)
    cover_url: str = Field("", max_length=600)
    mode: str = Field("now", pattern="^(now|queue)$")


class DjControlRequest(BaseModel):
    action: str = Field(..., pattern="^(pause|resume|next)$")


class DjSfxRequest(BaseModel):
    sfx_id: str = Field(
        ...,
        pattern="^(applause|airhorn|cheer|rimshot|scratch|drumroll|ding|laugh)$",
    )


_SFX_CATALOG: Dict[str, str] = {
    "applause": "鼓掌",
    "airhorn": "DJ 气笛",
    "cheer": "欢呼",
    "rimshot": "梗点",
    "scratch": "搓碟",
    "drumroll": "鼓点",
    "ding": "叮咚",
    "laugh": "笑声",
}
_SFX_WAV_CACHE: Dict[str, bytes] = {}


def _synth_sfx_wav(sfx_id: str) -> bytes:
    """Synthesizes a crisp 16-bit 22050Hz mono WAV sound effect with zero external deps."""
    if sfx_id in _SFX_WAV_CACHE:
        return _SFX_WAV_CACHE[sfx_id]

    sr = 22050
    rng = random.Random(hash(sfx_id) & 0xFFFFFFFF)
    samples: List[float] = []

    def _add_buf(buf: List[float], start_s: float, vals: List[float]) -> None:
        idx0 = int(start_s * sr)
        need = idx0 + len(vals)
        if need > len(buf):
            buf.extend([0.0] * (need - len(buf)))
        for k, v in enumerate(vals):
            buf[idx0 + k] += v

    if sfx_id == "airhorn":
        # Classic DJ triple-blast airhorn chord (F#4 + A#4 + C#5 sawtooths)
        dur = 1.45
        samples = [0.0] * int(dur * sr)
        blasts = [(0.0, 0.14), (0.18, 0.14), (0.36, 0.95)]
        for start, length in blasts:
            n = int(length * sr)
            vals = []
            for i in range(n):
                t = i / sr
                env = min(1.0, t / 0.012) * min(1.0, (length - t) / 0.08)
                glide = 1.0 - 0.045 * (t / max(length, 0.01))
                v = 0.0
                for base_f in (370.0, 466.16, 554.37, 740.0):
                    phase = (t * base_f * glide) % 1.0
                    v += (2.0 * phase - 1.0) * 0.22
                vals.append(v * env)
            _add_buf(samples, start, vals)
    elif sfx_id == "applause":
        # Layered rhythmic clap bursts with warm bandpass feel
        dur = 2.0
        samples = [0.0] * int(dur * sr)
        for _ in range(42):
            st = rng.uniform(0.0, 1.55)
            clap_len = int(rng.uniform(0.055, 0.11) * sr)
            amp = rng.uniform(0.14, 0.28)
            lp = 0.0
            vals = []
            for i in range(clap_len):
                t = i / sr
                env = math.exp(-t * 28.0)
                noise = rng.uniform(-1.0, 1.0)
                lp = lp * 0.55 + noise * 0.45
                vals.append((noise - lp * 0.4) * env * amp)
            _add_buf(samples, st, vals)
    elif sfx_id == "cheer":
        # Rising celebratory major chord fanfare + crowd shimmer
        dur = 1.8
        samples = [0.0] * int(dur * sr)
        notes = [(0.0, 523.25), (0.10, 659.25), (0.20, 783.99), (0.32, 1046.50)]
        for st, freq in notes:
            n = int(0.85 * sr)
            vals = [
                (math.sin(2 * math.pi * freq * (i / sr)) + 0.35 * math.sin(4 * math.pi * freq * (i / sr)))
                * math.exp(-(i / sr) * 2.8)
                * 0.25
                for i in range(n)
            ]
            _add_buf(samples, st, vals)
        n_sh = int(1.6 * sr)
        shimmer = []
        lp = 0.0
        for i in range(n_sh):
            t = i / sr
            env = math.sin(math.pi * min(1.0, t / 1.6)) * 0.22
            lp = lp * 0.72 + rng.uniform(-1.0, 1.0) * 0.28
            shimmer.append(lp * env)
        _add_buf(samples, 0.1, shimmer)
    elif sfx_id == "rimshot":
        # Classic Ba-Dum-Tss
        dur = 1.35
        samples = [0.0] * int(dur * sr)
        # Ba (snare)
        n1 = int(0.16 * sr)
        _add_buf(samples, 0.0, [
            (math.sin(2 * math.pi * 190 * (i / sr)) * 0.55 + rng.uniform(-0.5, 0.5)) * math.exp(-(i / sr) * 22.0) * 0.75
            for i in range(n1)
        ])
        # Dum (tom)
        n2 = int(0.22 * sr)
        _add_buf(samples, 0.22, [
            math.sin(2 * math.pi * (145 - 45 * (i / n2)) * (i / sr)) * math.exp(-(i / sr) * 14.0) * 0.85
            for i in range(n2)
        ])
        # Tss (crash cymbal)
        n3 = int(0.82 * sr)
        prev = 0.0
        cym = []
        for i in range(n3):
            t = i / sr
            raw = rng.uniform(-1.0, 1.0)
            hp = raw - prev * 0.85
            prev = raw
            cym.append(hp * math.exp(-t * 4.5) * 0.55)
        _add_buf(samples, 0.45, cym)
    elif sfx_id == "scratch":
        # DJ vinyl double-chirp scratch
        dur = 0.95
        samples = [0.0] * int(dur * sr)
        for st, dir_up in [(0.0, True), (0.26, False), (0.50, True)]:
            n = int(0.22 * sr)
            phase = 0.0
            vals = []
            for i in range(n):
                u = i / n
                freq = (240.0 + 1300.0 * u) if dir_up else (1540.0 - 1250.0 * u)
                phase += 2 * math.pi * freq / sr
                env = math.sin(math.pi * u)
                vals.append(math.tanh(math.sin(phase) * 2.2) * env * 0.55)
            _add_buf(samples, st, vals)
    elif sfx_id == "drumroll":
        # Rapid snare drumroll crescendo
        dur = 1.6
        samples = [0.0] * int(dur * sr)
        t_cur = 0.0
        step = 0.055
        while t_cur < 1.35:
            amp = 0.25 + 0.6 * (t_cur / 1.35)
            n = int(0.06 * sr)
            _add_buf(samples, t_cur, [
                (math.sin(2 * math.pi * 200 * (i / sr)) * 0.4 + rng.uniform(-0.6, 0.6))
                * math.exp(-(i / sr) * 38.0) * amp * 0.55
                for i in range(n)
            ])
            t_cur += step
            step = max(0.032, step * 0.96)
    elif sfx_id == "laugh":
        # Playful bouncy "Ha-Ha-Ha-Ha" synth bursts
        dur = 1.25
        samples = [0.0] * int(dur * sr)
        pitches = [660.0, 587.3, 622.2, 523.2, 466.2]
        for idx, f in enumerate(pitches):
            n = int(0.18 * sr)
            vals = [
                (math.sin(2 * math.pi * (f - 60 * (i / n)) * (i / sr))
                 + 0.4 * math.sin(4 * math.pi * f * (i / sr)))
                * math.sin(math.pi * (i / n)) * 0.48
                for i in range(n)
            ]
            _add_buf(samples, idx * 0.21, vals)
    else:  # ding
        dur = 1.15
        samples = [0.0] * int(dur * sr)
        for st, f in [(0.0, 1046.5), (0.14, 1567.98)]:
            n = int(0.95 * sr)
            _add_buf(samples, st, [
                (math.sin(2 * math.pi * f * (i / sr)) + 0.28 * math.sin(6 * math.pi * f * (i / sr)))
                * math.exp(-(i / sr) * 4.5) * 0.55
                for i in range(n)
            ])

    peak = max((abs(x) for x in samples), default=1.0)
    scale = 29000.0 / max(peak, 1.0)
    pcm = struct.pack("<" + "h" * len(samples), *(int(max(-32767, min(32767, x * scale))) for x in samples))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    data = buf.getvalue()
    _SFX_WAV_CACHE[sfx_id] = data
    return data


@router.post("/api/v1/dj/session")
def create_dj_session(
    request: Request,
    device_id: Optional[str] = Query(None),
    force_new: bool = Query(False),
    user: AuthenticatedUser = Depends(require_car_or_admin),
) -> Dict[str, Any]:
    target = resolve_car_device_id(device_id or user.device_id)
    sess = dj_store.create_or_get(target, force_new=force_new)
    public_base = (settings.DJ_PUBLIC_BASE_URL or str(request.base_url)).rstrip("/")
    url = f"{public_base}/dj?t={sess.token}"
    return {
        "active": True,
        "device_id": sess.device_id,
        "token": sess.token,
        "url": url,
        "expires_at": int(sess.expires_at),
        "queue_count": len(sess.queue),
    }


@router.delete("/api/v1/dj/session")
def revoke_dj_session(
    device_id: Optional[str] = Query(None),
    user: AuthenticatedUser = Depends(require_car_or_admin),
) -> Dict[str, Any]:
    target = resolve_car_device_id(device_id or user.device_id)
    revoked = dj_store.revoke(target)
    return {"active": False, "device_id": target, "revoked": revoked}


def _is_heartbeat_fresh(iso_str: Optional[str], max_age_sec: float = 35.0) -> bool:
    if not iso_str:
        return False
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() <= max_age_sec
    except Exception:
        return False


def _resolve_track_meta(sess: _GuestSession, track_id: Optional[str]) -> Dict[str, str]:
    if not track_id:
        return {"title": "等待播放", "artist": "", "cover_url": ""}
    cached = sess.track_cache.get(track_id)
    if cached:
        return {
            "title": str(cached.get("title") or track_id),
            "artist": str(cached.get("artist") or ""),
            "cover_url": _https_cover(cached.get("cover_url")),
        }
    active_q = music_queue_snapshot_manager.get_active_queue(sess.device_id)
    if active_q and getattr(active_q, "tracks", None):
        for t in active_q.tracks:
            tid = t.get("track_id") if isinstance(t, dict) else getattr(t, "track_id", None)
            if tid == track_id:
                title = t.get("title") if isinstance(t, dict) else getattr(t, "title", "")
                artist = t.get("artist") if isinstance(t, dict) else getattr(t, "artist", "")
                cover = t.get("cover_url") if isinstance(t, dict) else getattr(t, "cover_url", "")
                return {"title": str(title or track_id), "artist": str(artist or ""), "cover_url": _https_cover(cover)}
    return {"title": track_id, "artist": "", "cover_url": ""}


@router.get("/api/v1/dj/status")
def get_dj_status(
    token: str = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    sess = dj_store.validate(token)
    row = get_car_status_by_id(db, sess.device_id)
    if not row:
        return {
            "online": bool(ws_manager.has_car_client(sess.device_id)),
            "playing": False,
            "playback_state": "unknown",
            "title": "等待播放",
            "artist": "",
            "cover_url": "",
            "position_ms": 0,
            "duration_ms": 0,
            "queue": sess.queue,
        }
    resp = car_status_to_response(row)
    state = resp.playback_state or "unknown"
    online = bool(
        ws_manager.has_car_client(sess.device_id)
        or _is_heartbeat_fresh(resp.last_heartbeat_at)
    )
    meta = _resolve_track_meta(sess, resp.current_track_id)
    return {
        "online": online,
        "playing": state == "playing",
        "playback_state": state,
        "title": meta["title"],
        "artist": meta["artist"],
        "cover_url": meta["cover_url"],
        "position_ms": int(resp.position_ms or 0),
        "duration_ms": int(resp.duration_ms or 0),
        "queue": sess.queue,
    }


@router.get("/api/v1/dj/search")
async def search_dj_songs(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(15, ge=1, le=25),
    token: str = Depends(_extract_token),
) -> Dict[str, Any]:
    dj_store.validate(token)
    raw = await netease_service.search(q.strip(), page=1, limit=limit)
    tracks: List[Dict[str, Any]] = []
    for item in (raw.get("songs") or raw.get("tracks") or []):
        sid = item.get("song_id") or item.get("id")
        if not isinstance(sid, int) or sid <= 0:
            continue
        tracks.append(
            {
                "song_id": sid,
                "title": str(item.get("title") or item.get("name") or ""),
                "artist": str(item.get("artist") or ""),
                "album": str(item.get("album") or ""),
                "duration_ms": int(item.get("duration_ms") or 0),
                "cover_url": _https_cover(item.get("cover_url")),
                "playable": item.get("playable", True) is not False,
            }
        )
    return {"total": int(raw.get("total") or len(tracks)), "tracks": tracks}


async def _sync_dj_queue_to_car(
    sess: _GuestSession,
    db: Session,
    play_index: Optional[int] = None,
    play_now: bool = False,
) -> None:
    if not sess.queue:
        return
    snap = await music_queue_snapshot_manager.prepare_custom_queue(
        device_id=sess.device_id,
        tracks=sess.queue,
        source="dj_queue",
        name="扫码点歌列表",
        queue_id=sess.queue_id,
        activate=True,
        db=db,
    )
    row = get_car_status_by_id(db, sess.device_id)
    car_on_dj = bool(row and row.queue_id == sess.queue_id and row.playback_state == "playing")

    if play_now or not car_on_dj:
        idx = 0 if play_index is None else max(0, min(play_index, len(sess.queue) - 1))
        track = sess.queue[idx]
        sid = int(track["song_id"])
        cmd = MusicCommandRequest(
            device_id=sess.device_id,
            action="play_track",
            params=MusicCommandParams(
                source="dj_queue",
                queue_id=snap.queue_id,
                queue_version=snap.queue_version,
                queue_index=idx,
                song_id=sid,
                track_id=f"netease_{sid}",
                stream_url=f"/api/v1/music/netease/stream/{sid}",
                title=str(track.get("title") or ""),
                artist=str(track.get("artist") or ""),
                album=str(track.get("album") or ""),
                duration_ms=int(track.get("duration_ms") or 0),
                cover_url=_https_cover(track.get("cover_url")),
            ),
        )
    else:
        current_tid = row.current_track_id if row else None
        cmd = MusicCommandRequest(
            device_id=sess.device_id,
            action="switch_queue",
            params=MusicCommandParams(
                source="dj_queue",
                queue_id=snap.queue_id,
                queue_version=snap.queue_version,
                start_track_id=current_tid,
                start_index=0,
            ),
        )
    await process_and_dispatch_music_command(
        cmd, _guest_car_user(sess.device_id), db, target_device=sess.device_id, source_channel="dj_web"
    )


@router.post("/api/v1/dj/play")
async def dj_play_song(
    req: DjPlayRequest,
    token: str = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    sess = dj_store.validate(token)
    item = {
        "song_id": req.song_id,
        "title": req.title.strip(),
        "artist": req.artist.strip(),
        "album": req.album.strip(),
        "duration_ms": req.duration_ms,
        "cover_url": _https_cover(req.cover_url),
    }
    sess.track_cache[f"netease_{req.song_id}"] = item

    existing_idx = next((i for i, x in enumerate(sess.queue) if x.get("song_id") == req.song_id), None)
    if existing_idx is not None:
        sess.queue[existing_idx] = item
        target_idx = existing_idx
    else:
        if len(sess.queue) >= 30:
            raise HTTPException(status_code=400, detail="点歌队列已满（最多 30 首）")
        sess.queue.append(item)
        target_idx = len(sess.queue) - 1

    await _sync_dj_queue_to_car(sess, db, play_index=target_idx, play_now=(req.mode == "now"))
    return {
        "ok": True,
        "mode": req.mode,
        "index": target_idx,
        "queue_count": len(sess.queue),
        "queue": sess.queue,
    }


@router.post("/api/v1/dj/queue/{index}/play")
async def dj_play_queue_index(
    index: int,
    token: str = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    sess = dj_store.validate(token)
    if index < 0 or index >= len(sess.queue):
        raise HTTPException(status_code=404, detail="队列中不存在该歌曲")
    await _sync_dj_queue_to_car(sess, db, play_index=index, play_now=True)
    return {"ok": True, "index": index, "queue": sess.queue}


@router.delete("/api/v1/dj/queue/{index}")
async def dj_remove_queue_index(
    index: int,
    token: str = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    sess = dj_store.validate(token)
    if index < 0 or index >= len(sess.queue):
        raise HTTPException(status_code=404, detail="队列中不存在该歌曲")
    sess.queue.pop(index)
    if sess.queue:
        await _sync_dj_queue_to_car(sess, db, play_now=False)
    return {"ok": True, "queue_count": len(sess.queue), "queue": sess.queue}


@router.post("/api/v1/dj/control")
async def dj_control(
    req: DjControlRequest,
    token: str = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    sess = dj_store.validate(token)
    cmd = MusicCommandRequest(device_id=sess.device_id, action=req.action)
    await process_and_dispatch_music_command(
        cmd, _guest_car_user(sess.device_id), db, target_device=sess.device_id, source_channel="dj_web"
    )
    return {"ok": True, "action": req.action, "queue": sess.queue}


@router.get("/api/v1/dj/sfx/{sfx_id}.wav")
def get_dj_sfx_wav(sfx_id: str) -> Response:
    if sfx_id not in _SFX_CATALOG:
        raise HTTPException(status_code=404, detail="未知音效")
    wav_bytes = _synth_sfx_wav(sfx_id)
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("/api/v1/dj/sfx")
async def trigger_dj_sfx(
    req: DjSfxRequest,
    token: str = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    sess = dj_store.validate(token)
    cmd = MusicCommandRequest(
        device_id=sess.device_id,
        action="play_sfx",
        params=MusicCommandParams(
            sfx_id=req.sfx_id,
            sfx_url=f"/api/v1/dj/sfx/{req.sfx_id}.wav",
            title=_SFX_CATALOG.get(req.sfx_id, req.sfx_id),
        ),
    )
    await process_and_dispatch_music_command(
        cmd, _guest_car_user(sess.device_id), db, target_device=sess.device_id, source_channel="dj_web"
    )
    return {"ok": True, "sfx_id": req.sfx_id, "label": _SFX_CATALOG.get(req.sfx_id, req.sfx_id)}


_DJ_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover" />
<title>YARIS 车机点歌</title>
<style>
  :root{
    --canvas:#FAF9F4; --paper:#FFFFFF; --line:#E8E6DF;
    --peri:#E5EAFF; --blue:#3548E8; --red:#A82F36;
    --ink:#181B24; --muted:#6E7382; --green:#1E8E5A;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
  body{
    margin:0; font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","SF Pro Text",sans-serif;
    background:var(--canvas); color:var(--ink);
    padding:env(safe-area-inset-top) 16px calc(92px + env(safe-area-inset-bottom));
    max-width:520px; margin-inline:auto;
  }
  .topbar{display:flex;align-items:center;justify-content:space-between;padding:14px 0 12px;}
  .brand{background:var(--red);color:#fff;font-weight:700;font-size:13px;padding:6px 14px;border-radius:999px;letter-spacing:.3px;}
  .status{display:flex;align-items:center;gap:6px;background:var(--paper);border:1px solid var(--line);padding:6px 12px;border-radius:999px;font-size:12.5px;font-weight:600;color:var(--ink);}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);}
  .dot.on{background:var(--green);}
  .now{
    background:linear-gradient(135deg,#E5EAFF 0%,#EEF2FF 100%);
    border:1px solid #DCE3FF; border-radius:22px; padding:14px;
    display:flex; align-items:center; gap:12px; margin-bottom:14px;
  }
  .cover{width:62px;height:62px;border-radius:50%;object-fit:cover;background:#D6DDF8;flex-shrink:0;border:2px solid #fff;}
  .now-meta{flex:1;min-width:0;}
  .now-tag{display:inline-block;font-size:11px;font-weight:700;color:var(--blue);background:rgba(255,255,255,.78);padding:2px 8px;border-radius:999px;margin-bottom:4px;}
  .now-title{font-size:16px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .now-artist{font-size:12.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;}
  .prog{height:4px;background:rgba(53,72,232,.18);border-radius:99px;overflow:hidden;margin-top:8px;}
  .prog > i{display:block;height:100%;width:0%;background:var(--blue);border-radius:99px;transition:width .4s linear;}
  .ctrls{display:flex;align-items:center;gap:6px;flex-shrink:0;}
  .icon-btn{width:40px;height:40px;border-radius:50%;border:none;background:#fff;color:var(--ink);font-size:16px;font-weight:700;display:grid;place-items:center;cursor:pointer;box-shadow:0 2px 8px rgba(24,27,36,.06);}
  .icon-btn:active{transform:scale(.94);}
  .search-wrap{
    display:flex;align-items:center;gap:8px;background:var(--paper);
    border:1px solid var(--line);border-radius:999px;padding:6px 6px 6px 14px;
    margin-bottom:14px; box-shadow:0 2px 10px rgba(24,27,36,.03);
  }
  .search-wrap input{flex:1;border:none;outline:none;font-size:15px;background:transparent;color:var(--ink);min-width:0;}
  .search-btn{border:none;background:var(--blue);color:#fff;font-weight:700;font-size:14px;padding:9px 18px;border-radius:999px;cursor:pointer;flex-shrink:0;}
  .search-btn:disabled{opacity:.55;}
  .sfx-card{background:var(--paper);border:1px solid var(--line);border-radius:20px;padding:12px 14px;margin-bottom:14px;}
  .sfx-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;font-size:13.5px;font-weight:800;}
  .sfx-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;}
  .sfx-btn{border:1px solid var(--line);background:var(--canvas);color:var(--ink);border-radius:14px;padding:9px 4px;font-size:12px;font-weight:700;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:3px;transition:transform .1s,background .15s;}
  .sfx-btn:active{transform:scale(.93);background:var(--peri);color:var(--blue);}
  .sfx-ico{font-size:18px;line-height:1.1;}
  .card{background:var(--paper);border:1px solid var(--line);border-radius:22px;padding:14px;}
  .card-title{font-size:16px;font-weight:800;margin:0 0 10px;}
  .row{display:flex;align-items:center;gap:11px;padding:10px 0;border-top:1px solid #F1EFE9;}
  .row:first-of-type{border-top:none;padding-top:4px;}
  .thumb{width:48px;height:48px;border-radius:12px;object-fit:cover;background:var(--peri);flex-shrink:0;}
  .info{flex:1;min-width:0;}
  .t-name{font-size:14.5px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .t-sub{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;}
  .acts{display:flex;gap:6px;flex-shrink:0;}
  .btn-q{border:none;background:var(--peri);color:var(--blue);font-size:12px;font-weight:700;padding:7px 10px;border-radius:999px;cursor:pointer;}
  .btn-p{border:none;background:var(--blue);color:#fff;font-size:12px;font-weight:700;padding:7px 12px;border-radius:999px;cursor:pointer;}
  .empty{text-align:center;color:var(--muted);font-size:13.5px;padding:24px 8px;}
  .dock{
    position:fixed;left:50%;transform:translateX(-50%);bottom:calc(12px + env(safe-area-inset-bottom));
    width:min(calc(100% - 32px),488px);background:var(--paper);border:1px solid var(--line);
    border-radius:18px;padding:12px 16px;box-shadow:0 8px 24px rgba(24,27,36,.10);
  }
  .dock-head{display:flex;align-items:center;justify-content:space-between;font-weight:700;font-size:14px;cursor:pointer;}
  .dock-list{margin-top:10px;max-height:200px;overflow:auto;display:none;}
  .dock-list.open{display:block;}
  .q-item{display:flex;justify-content:space-between;padding:6px 0;border-top:1px solid #F1EFE9;font-size:13px;}
  .toast{
    position:fixed;left:50%;top:18px;transform:translateX(-50%);
    background:#181B24;color:#fff;padding:9px 16px;border-radius:999px;font-size:13px;font-weight:600;
    opacity:0;pointer-events:none;transition:opacity .2s;z-index:99;
  }
  .toast.show{opacity:1;}
</style>
</head>
<body>
  <div id="toast" class="toast"></div>
  <div class="topbar">
    <div class="brand">YARIS 车机点歌</div>
    <div class="status"><span id="dot" class="dot"></span><span id="st">连接中</span></div>
  </div>

  <div class="now">
    <img id="cover" class="cover" alt="" />
    <div class="now-meta">
      <div class="now-tag">正在播放</div>
      <div id="title" class="now-title">等待播放</div>
      <div id="artist" class="now-artist">—</div>
      <div class="prog"><i id="bar"></i></div>
    </div>
    <div class="ctrls">
      <button id="btnToggle" class="icon-btn" title="播放/暂停">▶</button>
      <button id="btnNext" class="icon-btn" title="下一曲">⏭</button>
    </div>
  </div>

  <div class="sfx-card">
    <div class="sfx-head"><span>车机氛围音效板</span><span style="font-size:11.5px;color:var(--muted);font-weight:600">点击即在车内叠加播放</span></div>
    <div class="sfx-grid">
      <button class="sfx-btn" type="button" onclick="playSfx('applause','鼓掌')"><span class="sfx-ico">👏</span><span>鼓掌</span></button>
      <button class="sfx-btn" type="button" onclick="playSfx('airhorn','DJ气笛')"><span class="sfx-ico">📯</span><span>DJ气笛</span></button>
      <button class="sfx-btn" type="button" onclick="playSfx('cheer','欢呼')"><span class="sfx-ico">🎉</span><span>欢呼</span></button>
      <button class="sfx-btn" type="button" onclick="playSfx('scratch','搓碟')"><span class="sfx-ico">💿</span><span>搓碟</span></button>
      <button class="sfx-btn" type="button" onclick="playSfx('rimshot','梗点')"><span class="sfx-ico">🥁</span><span>梗点</span></button>
      <button class="sfx-btn" type="button" onclick="playSfx('drumroll','鼓点')"><span class="sfx-ico">⚡</span><span>鼓点</span></button>
      <button class="sfx-btn" type="button" onclick="playSfx('laugh','笑声')"><span class="sfx-ico">😂</span><span>笑声</span></button>
      <button class="sfx-btn" type="button" onclick="playSfx('ding','叮咚')"><span class="sfx-ico">✨</span><span>叮咚</span></button>
    </div>
  </div>

  <form id="sf" class="search-wrap">
    <input id="kw" type="search" placeholder="搜索网易云歌曲、歌手…" autocomplete="off" />
    <button id="sbtn" class="search-btn" type="submit">搜索</button>
  </form>

  <div class="card">
    <div class="card-title">搜索结果</div>
    <div id="results"><div class="empty">输入歌名或歌手，点歌直接在车机播放</div></div>
  </div>

  <div class="dock">
    <div id="dockHead" class="dock-head">
      <span id="qTitle">已点歌队列 (0 首)</span>
      <span id="qArrow">▲</span>
    </div>
    <div id="qList" class="dock-list"></div>
  </div>

<script>
const token = new URLSearchParams(location.search).get("t") || "";
const headers = {"X-DJ-Token": token, "Content-Type": "application/json"};
let isPlaying = false;

function toast(msg){
  const el = document.getElementById("toast");
  el.textContent = msg; el.classList.add("show");
  clearTimeout(window._tt); window._tt = setTimeout(()=>el.classList.remove("show"), 2200);
}
function esc(s){return String(s||"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

async function api(path, opts={}){
  const res = await fetch(path, {...opts, headers: {...headers, ...(opts.headers||{})}});
  const data = await res.json().catch(()=>({}));
  if(!res.ok){
    const err = new Error(data.detail || "操作失败");
    err.status = res.status;
    throw err;
  }
  return data;
}

function renderQueue(q){
  document.getElementById("qTitle").textContent = `点歌列表（已同步车机 · ${q.length} 首）`;
  const list = document.getElementById("qList");
  list.innerHTML = q.length
    ? q.map((x,i)=>`<div class="q-item" style="align-items:center;gap:8px"><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer" onclick="playQ(${i})">${i+1}. ${esc(x.title)} <span style="color:#6E7382;font-size:12px">· ${esc(x.artist)}</span></span><button class="btn-q" style="padding:4px 8px;font-size:11px" onclick="playQ(${i})">播放</button><button class="btn-q" style="padding:4px 8px;font-size:11px;background:#FDECEC;color:#A82F36" onclick="delQ(${i})">移除</button></div>`).join("")
    : `<div class="empty" style="padding:10px 0">点击下方「+ 列表」或「立即播放」自动同步到车机</div>`;
}
window.playQ = async (i)=>{
  try{
    const r = await api(`/api/v1/dj/queue/${i}/play`, {method:"POST"});
    if(r.queue) renderQueue(r.queue);
    toast("已切到该曲目");
    setTimeout(refreshStatus, 600);
  }catch(e){ toast(e.message); }
};
window.delQ = async (i)=>{
  try{
    const r = await api(`/api/v1/dj/queue/${i}`, {method:"DELETE"});
    if(r.queue) renderQueue(r.queue);
    toast("已从点歌列表移除");
  }catch(e){ toast(e.message); }
};

async function refreshStatus(){
  if(!token){ document.getElementById("st").textContent="缺少二维码参数"; return; }
  try{
    const d = await api("/api/v1/dj/status");
    document.getElementById("dot").className = "dot" + (d.online ? " on" : "");
    document.getElementById("st").textContent = d.online ? "已连接车机" : "车机离线";
    document.getElementById("title").textContent = d.title || "等待播放";
    document.getElementById("artist").textContent = d.artist || "—";
    if(d.cover_url) document.getElementById("cover").src = d.cover_url;
    isPlaying = !!d.playing;
    document.getElementById("btnToggle").textContent = isPlaying ? "⏸" : "▶";
    const pct = d.duration_ms > 0 ? Math.min(100, (d.position_ms / d.duration_ms)*100) : 0;
    document.getElementById("bar").style.width = pct.toFixed(1) + "%";
    renderQueue(d.queue || []);
  }catch(e){
    document.getElementById("dot").className = "dot";
    document.getElementById("st").textContent = e.status === 401 ? "会话已失效" : "重连中…";
  }
}

document.getElementById("btnToggle").onclick = async ()=>{
  try{
    await api("/api/v1/dj/control", {method:"POST", body:JSON.stringify({action: isPlaying ? "pause" : "resume"})});
    toast(isPlaying ? "已暂停车机" : "已继续播放");
    setTimeout(refreshStatus, 500);
  }catch(e){ toast(e.message); }
};
document.getElementById("btnNext").onclick = async ()=>{
  try{
    await api("/api/v1/dj/control", {method:"POST", body:JSON.stringify({action: "next"})});
    toast("已切到下一首");
    setTimeout(refreshStatus, 600);
  }catch(e){ toast(e.message); }
};

document.getElementById("dockHead").onclick = ()=>{
  const el = document.getElementById("qList");
  const open = el.classList.toggle("open");
  document.getElementById("qArrow").textContent = open ? "▼" : "▲";
};

window.playSfx = async (id, label)=>{
  try{
    await api("/api/v1/dj/sfx", {method:"POST", body:JSON.stringify({sfx_id: id})});
    toast(`已发送音效：${label}`);
  }catch(e){ toast(e.message); }
};

let lastTracks = [];
window.pick = async (idx, mode)=>{
  const t = lastTracks[idx]; if(!t) return;
  try{
    const r = await api("/api/v1/dj/play", {method:"POST", body:JSON.stringify({...t, mode})});
    toast(mode === "queue" ? `已加入队列：${t.title}` : `正在车机播放：${t.title}`);
    if(r.queue) renderQueue(r.queue);
    setTimeout(refreshStatus, 700);
  }catch(e){ toast(e.message); }
};

document.getElementById("sf").onsubmit = async (ev)=>{
  ev.preventDefault();
  const q = document.getElementById("kw").value.trim();
  if(!q) return;
  const btn = document.getElementById("sbtn");
  btn.disabled = true; btn.textContent = "搜索中…";
  try{
    const d = await api("/api/v1/dj/search?q=" + encodeURIComponent(q));
    lastTracks = d.tracks || [];
    const box = document.getElementById("results");
    if(!lastTracks.length){
      box.innerHTML = `<div class="empty">没有找到相关歌曲</div>`;
    }else{
      box.innerHTML = lastTracks.map((t,i)=>`
        <div class="row">
          <img class="thumb" src="${esc(t.cover_url)}" alt="" />
          <div class="info">
            <div class="t-name">${esc(t.title)}</div>
            <div class="t-sub">${esc(t.artist)}${t.album ? " · " + esc(t.album) : ""}</div>
          </div>
          <div class="acts">
            <button class="btn-q" onclick="pick(${i},'queue')">+ 列表</button>
            <button class="btn-p" onclick="pick(${i},'now')">立即播放</button>
          </div>
        </div>
      `).join("");
    }
  }catch(e){ toast(e.message); }
  finally{ btn.disabled = false; btn.textContent = "搜索"; }
};

refreshStatus();
setInterval(refreshStatus, 3000);
</script>
</body>
</html>
"""


@router.get("/dj", response_class=HTMLResponse)
def guest_dj_page() -> HTMLResponse:
    return HTMLResponse(content=_DJ_HTML)
