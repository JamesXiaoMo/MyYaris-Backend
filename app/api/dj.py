from datetime import datetime, timezone
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
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
        id=0,
        username=f"guest_dj_{device_id}",
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
            "online": bool(ws_manager.is_device_connected(sess.device_id)),
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
        ws_manager.is_device_connected(sess.device_id)
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
    for item in raw.get("tracks", []):
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


async def _dispatch_single_track(
    device_id: str,
    track: Dict[str, Any],
    db: Session,
) -> None:
    sid = int(track["song_id"])
    cmd = MusicCommandRequest(
        device_id=device_id,
        action="play_track",
        params=MusicCommandParams(
            source="single_track",
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
    await process_and_dispatch_music_command(
        cmd, _guest_car_user(device_id), db, target_device=device_id, source_channel="dj_web"
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
    if req.mode == "queue":
        if len(sess.queue) >= 30:
            raise HTTPException(status_code=400, detail="点歌队列已满（最多 30 首）")
        sess.queue.append(item)
        return {"ok": True, "mode": "queue", "queue_count": len(sess.queue), "queue": sess.queue}

    await _dispatch_single_track(sess.device_id, item, db)
    return {"ok": True, "mode": "now", "queue_count": len(sess.queue)}


@router.post("/api/v1/dj/control")
async def dj_control(
    req: DjControlRequest,
    token: str = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    sess = dj_store.validate(token)
    if req.action == "next" and sess.queue:
        next_track = sess.queue.pop(0)
        await _dispatch_single_track(sess.device_id, next_track, db)
        return {"ok": True, "action": "next", "from_dj_queue": True, "track": next_track, "queue": sess.queue}

    cmd = MusicCommandRequest(device_id=sess.device_id, action=req.action)
    await process_and_dispatch_music_command(
        cmd, _guest_car_user(sess.device_id), db, target_device=sess.device_id, source_channel="dj_web"
    )
    return {"ok": True, "action": req.action, "from_dj_queue": False, "queue": sess.queue}


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
  document.getElementById("qTitle").textContent = `已点歌队列 (${q.length} 首)`;
  const list = document.getElementById("qList");
  list.innerHTML = q.length
    ? q.map((x,i)=>`<div class="q-item"><span>${i+1}. ${esc(x.title)}</span><span style="color:#6E7382">${esc(x.artist)}</span></div>`).join("")
    : `<div class="empty" style="padding:10px 0">暂无插队歌曲</div>`;
}

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
            <button class="btn-q" onclick="pick(${i},'queue')">+ 插队</button>
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
