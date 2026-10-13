# MyYaris 全量后端 API 参考手册 (API Reference)

**版本**：V2.3.1  
**基准日期**：2026-10-13  
**后端 Git 提交 Hash**：`3dde1d8` (`3dde1d85a5fa1cd284fc53f6483de8229d757fc3`)  
**部署宿主机**：Lenovo Legion R9000P (Ubuntu, systemd user service: `myyaris.service`)  
**关联核心契约**：
- [三端协同与播控契约 (docs/API-V2三端契约.md)](file:///path/to/user/Desktop/MyYaris/docs/API-V2三端契约.md)
- [收藏夹业务契约 (docs/收藏夹API契约.md)](file:///path/to/user/Desktop/MyYaris/docs/收藏夹API契约.md)

---

## 目录
- [一、 网络架构与网络访问边界](#一-网络架构与网络访问边界)
- [二、 全局鉴权与角色访问控制](#二-全局鉴权与角色访问控制)
- [三、 系统状态与 OpenAPI 规范接口](#三-系统状态与-openapi-规范接口)
- [四、 智能导航与目的地流转模块](#四-智能导航与目的地流转模块)
- [五、 播控指令调度与状态同步模块 (V2.1 契约)](#五-播控指令调度与状态同步模块-v21-契约)
- [六、 全双工 WebSocket 实时通道](#六-全双工-websocket-实时通道)
- [七、 本地曲库与媒体流服务 (HTTP Range)](#七-本地曲库与媒体流服务-http-range)
- [八、 网易云在线音乐与智能缓存代理](#八-网易云在线音乐与智能缓存代理)
- [九、 收藏夹管理模块 (WGS84-Only)](#九-收藏夹管理模块-wgs84-only)
- [十、 语音上传与 Provider 实际可用边界](#十-语音上传与-provider-实际可用边界)
- [十一、 实施状态与验证边界声明](#十一-实施状态与验证边界声明)

---

## 一、 网络架构与网络访问边界

### 1.1 服务端点与协议
- **本机/局域网回环**：`http://127.0.0.1:8000` / `http://192.0.2.20:8000`
- **安全传输基地址 (HTTPS / WSS)**：`https://myyaris.example.com`

### 1.2 Tailscale 私网访问性重要说明 (非公网暴露)
> [!IMPORTANT]
> 本服务**不是**暴露于公共互联网的开放 Web 服务。
> 整个通信链路依托 **Tailscale 专用加密网格 (WireGuard-based Private Tailnet)**。只有登录并授权加入该 Tailnet 的受信任设备（如绑定同一 Tailscale 账号的车主 iPhone、车机 Ottocast 平板）才能解析并连接上述域名。公共互联网中未组网的外部设备无法访问或探测该服务。

---

## 二、 全局鉴权与角色访问控制

### 2.1 鉴权头规范 (仅占位符，严禁明文凭据)
系统内置基于 Bearer Token 的角色鉴权机制，支持通过标准 Header 传递：

| 客户端身份 | 角色名称 (`role`) | 推荐鉴权 Header | 兼容鉴权 Header | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| **iPhone 客户端** | `phone` | `Authorization: Bearer <PHONE_TOKEN>` | `X-Device-Token: <PHONE_TOKEN>` | 发起导航、下发播控、管理收藏夹、查询音乐 |
| **车机 Ottocast 端** | `car` | `X-Device-Token: <CAR_TOKEN>` | `Authorization: Bearer <CAR_TOKEN>` | 长轮询任务、拉取播控、上报播放状态、上报执行 ACK |
| **运维管理端** | `admin` | `Authorization: Bearer <ADMIN_TOKEN>` | `X-Device-Token: <ADMIN_TOKEN>` | 具备所有接口的全量访问与调试权限 |

- **WebSocket 鉴权**：除上述 Header 外，支持在握手 URL 中通过 Query 参数传递：`?token=<TOKEN>`。
- **错误状态码**：
  - `401 Unauthorized`：未提供 Token、Token 为空或签名无效；
  - `403 Forbidden`：Token 有效但角色权限不足（例如车机角色尝试写收藏夹）。

---

## 三、 系统状态与 OpenAPI 规范接口

### 3.1 获取服务健康状态
- **方法 & 路径**：`GET /health`
- **鉴权**：公开访问，无需鉴权
- **响应示例 (200 OK)**：
  ```json
  {
    "status": "ok",
    "version": "1.0.0",
    "timestamp": "2026-10-13T09:47:00.000000+00:00"
  }
  ```

### 3.2 交互式 API 文档与元数据规范
- **Swagger UI**：`GET /docs`（Tailscale 私网内浏览器直接访问，交互式调试）
- **ReDoc UI**：`GET /redoc`（格式化静态阅读视图）
- **OpenAPI 规范架构**：`GET /openapi.json`（返回全量 OpenAPI 3.1 JSON 元数据）

---

## 四、 智能导航与目的地流转模块

本模块支持“标准 V1 两阶段流转（Phase 1 解析 + Phase 2 派发）”及“历史快捷指令一键流转”。

### 4.1 Phase 1: 目的地坐标解析 (`/api/v1/destinations/resolve`)
- **方法 & 路径**：`POST /api/v1/destinations/resolve`
- **鉴权**：`phone` / `admin`
- **请求体**：
  ```json
  {
    "raw_input": "35.6812, 139.7671",  // 支持: 纯坐标、Google Maps 分享短链、地名关键词
    "source": "ios_share"              // "ios_share" | "car_ui" | "voice"
  }
  ```
- **核心逻辑**：纯坐标与有效地图图钉直接解析为 WGS84 经纬度；**此步骤绝对不创建任务（No Eager Task Creation）**。
- **响应示例 (200 OK)**：
  ```json
  {
    "code": 200,
    "destination_id": "3b196fb4-2f34-4465-ae66-7f0cd3dc2975",
    "name": "秋叶原电车站",
    "latitude": 35.698383,
    "longitude": 139.773072,
    "confidence": 0.98
  }
  ```

### 4.2 Phase 2: 目标车机正式派发任务 (`/api/v1/tasks/destination`)
- **方法 & 路径**：`POST /api/v1/tasks/destination`
- **鉴权**：`phone` / `admin`
- **请求体**：
  ```json
  {
    "destination_id": "3b196fb4-2f34-4465-ae66-7f0cd3dc2975",
    "target_device_id": "car_p3pro",    // 自动归一化别名 (如 ottocast_p3_pro -> car_p3pro)
    "expires_in_seconds": 3600
  }
  ```
- **响应示例 (200 OK)**：返回已入队的 `Task` 对象（状态为 `queued`）。

### 4.3 多候选点人工确认派发 (`/destinations/{id}/confirm`)
- **方法 & 路径**：`POST /destinations/{id}/confirm`
- **鉴权**：`phone` / `admin`
- **请求体**：
  ```json
  {
    "candidate_id": "cand_0123456789",
    "target_device_id": "car_p3pro"
  }
  ```

### 4.4 历史单步兼容接口 (`/destinations`)
- **方法 & 路径**：`POST /destinations`
- **说明**：保持原有行为，明确坐标或单图钉链接解析成功后，**自动创建并调度 Task**，确保现有 iPhone Siri 快捷指令即点即走。
- **查询解析状态**：`GET /destinations/{id}`

### 4.5 车机任务拉取与生命周期确认
- **车机长轮询拉取下一条任务**：
  - `GET /devices/{id}/tasks/next?timeout=30`（车机主要使用）
  - `GET /api/v1/tasks/poll?device_id=car_p3pro&timeout=30`
  - 响应：有待执行任务返回 JSON，无任务超时返回 `204 No Content`。
- **车机确认执行结果 (ACK)**：
  - `POST /api/v1/tasks/{id}/ack` 或 `POST /tasks/{id}/ack`
  - 请求体：`{"phase": "received" | "launch_requested" | "failed", "error_code": null, "error_detail": null}`
- **查询任务详情**：`GET /tasks/{id}`

---

## 五、 播控指令调度与状态同步模块 (V2.1 契约)

遵循“至少一次投递 + 车机去重 + 显式两阶段 ACK + SQLite WAL 进程重启恢复”工业级规范。

### 5.1 指令生命周期状态机
```
queued (入库待发) -> sent (WS/HTTP派发) -> received (车机已接单) -> applied / failed (终态保护)
                                                                 -> expired (TTL超时)
```

### 5.2 手机下发播控指令 (`/api/v1/music/control/command`)
- **方法 & 路径**：`POST /api/v1/music/control/command`
- **鉴权**：`phone` / `admin`
- **请求体 (`MusicCommandRequest`)**：
  ```json
  {
    "command_id": "cmd_music_99a8b7c6d5e4", // 推荐客户端传入唯一UUID
    "device_id": "car_p3pro",
    "action": "play_track", // 动作枚举，详见下表
    "params": {
      "track_id": "netease_123456",
      "title": "晴天",
      "artist": "周杰伦"
    },
    "expires_in_seconds": 60 // 可选，超时秒数
  }
  ```
- **支持动作清单 (`action`)**：
  - `play_track`（切歌播放）、`pause`（暂停）、`resume`（继续）、`stop`（停止）
  - `next`（下一首）、`prev`（上一首）
  - `seek`（进度跳转，`position_ms`，TTL 默认 20s）
  - `set_volume`（软件音量 `volume: 0.0~1.0`，TTL 默认 20s）
  - `set_loop`（`mode: "all"|"one"|"off"`）、`set_shuffle`（`shuffle: true|false`）
- **幂等与冲突规则**：
  - 相同 `command_id` + 相同 payload -> 返回 200 OK 既有记录（不重发）；
  - 相同 `command_id` + 不同 payload 或不同设备 -> 返回 **409 Conflict**。

### 5.3 车机长轮询备用拉取 (`/api/v1/music/control/command/next`)
- **方法 & 路径**：`GET /api/v1/music/control/command/next`
- **鉴权**：`car` / `admin`
- **特性**：
  - WebSocket 派发失败、掉线或未在 3s 内确认的指令，由 HTTP 长轮询无缝接管；
  - 服务端进程重启后，未确认指令自动从 SQLite 恢复并供给 HTTP 拉取。
  - 无指令超时返回 `204 No Content`。

### 5.4 车机执行 ACK 回执 (`/api/v1/music/control/command/{command_id}/ack`)
- **方法 & 路径**：`POST /api/v1/music/control/command/{command_id}/ack`
- **鉴权**：`car` / `admin`（严格校验必须为目标车机，跨设备确认返回 403 Forbidden）
- **请求体 (`MusicCommandAckRequest`)**：
  ```json
  {
    "status": "applied",     // "received" | "applied" | "failed"
    "error_code": null,      // 失败时填写，如 "TRACK_DECODE_ERROR"
    "error_detail": null
  }
  ```
- **核心契约**：
  - `received` 不代表完成，不移出队列；
  - `applied` 与 `failed` 为终态，受终态保护不可逆转；
  - 接收到执行 ACK 后，自动向所有在线手机广播 `{type: "command_status", ...}`。

### 5.5 指令生命周期查询 (`/api/v1/music/control/command/{command_id}`)
- **方法 & 路径**：`GET /api/v1/music/control/command/{command_id}`
- **鉴权**：`phone` / `car` / `admin`

### 5.6 车机播放状态上报与查询
- **车机上报状态**：`POST /api/v1/music/control/status`（上报后实时通过 WebSocket 广播给手机）
- **查询当前快照**：`GET /api/v1/music/control/status?device_id=car_p3pro`

---

## 六、 全双工 WebSocket 实时通道

### 6.1 端点连接与握手
- **协议 & 路径**：`WSS /api/v1/ws/music`
- **Query 参数**：
  - `token`: 必填鉴权 Token（手机 Token 或车机 Token）
  - `client_type`: `"phone"`（手机客户端）或 `"car"`（车机端）
  - `device_id`: 车机标识（默认 `car_p3pro`）

### 6.2 双信封协议架构 (Dual Envelope)
广播状态时同时支持外层平铺字段与内层 `data` 对象，兼顾车机轻量解析与 iOS 强类型模型：
```json
{
  "type": "status_update",
  "timestamp": "2026-10-13T09:47:00Z",
  "device_id": "car_p3pro",
  "playback_state": "playing",
  "current_track_id": "netease_123456",
  "position_ms": 45000,
  "duration_ms": 240000,
  "software_volume": 0.8,
  "data": {
    "device_id": "car_p3pro",
    "playback_state": "playing",
    "current_track_id": "netease_123456",
    "position_ms": 45000,
    "duration_ms": 240000,
    "software_volume": 0.8
  }
}
```

### 6.3 交互协议帧类型
1. **心跳探活**：客户端发送 `{"type": "ping"}` -> 服务端返回 `{"type": "pong", "timestamp": "..."}`。
2. **手机发指令**：`{"type": "command", "data": { ... }}` -> 服务端回复 `{"type": "command_ack", "status": "dispatched"}` 并即刻推向车机。
3. **车机状态上报**：`{"type": "status_update", "data": { ... }}` -> 服务端落库并全量广播。
4. **车机执行 ACK**：`{"type": "command_ack", "command_id": "...", "status": "applied"}` -> 服务端向手机广播 `{"type": "command_status", ...}`。

---

## 七、 本地曲库与媒体流服务 (HTTP Range)

### 7.1 本地媒体扫描与检索
- **触发目录扫描**：`POST /api/v1/music/scan`（扫描 `DATA_DIR/media` 下本地音频）
- **曲库列表**：`GET /api/v1/music/tracks`（支持分页、搜索）
- **音轨详情**：`GET /api/v1/music/tracks/{track_id}`

### 7.2 音频流式播放 (HTTP Range 分片传输)
- **方法 & 路径**：`GET /api/v1/music/stream/{track_id}`
- **鉴权**：支持 Header 鉴权或 Query Token (`?token=...`)
- **关键特性**：
  - 完整实现 **RFC 7233 HTTP Range 请求**（支持 `Range: bytes=0-`，`bytes=1048576-` 等）；
  - 支持 `206 Partial Content`、`Accept-Ranges: bytes`、`Content-Range`；
  - 满足车机播放器与 iOS AVPlayer 极速拖动进度条（Seek）与缓冲播放需求。

---

## 八、 网易云在线音乐与智能缓存代理

集成第三方代理实现车机轻量播放、边播边存与离线自愈。

### 8.1 扫码登录与保活
- `GET /api/v1/music/netease/login/qr/key`：生成二维码 Key
- `GET /api/v1/music/netease/login/qr/create?key=...`：生成二维码 DataURL 与 Base64 图片
- `GET /api/v1/music/netease/login/qr/check?key=...`：轮询扫码状态（返回 800 超时 / 801 等待 / 802 待确认 / 803 成功）
- `GET /api/v1/music/netease/login/status`：获取当前登录会话状态
- `POST /api/v1/music/netease/login/cookie`：通过 Cookie 字符串直接导入
- `POST /api/v1/music/netease/login/refresh`：刷新当前凭证
- `POST /api/v1/music/netease/login/logout`：注销登录

### 8.2 曲库检索与播放
- `GET /api/v1/music/netease/search?keyword=...&limit=20`：在线歌曲检索
  - **正常命中**：返回 HTTP 200，包含清洗后的音轨元数据列表与分页总量；
  - **合法空结果**：上游检索确实无匹配曲目时，返回 HTTP 200，`{"code": 200, "total": 0, "songs": []}`；
  - **上游异常处理**：若网易云服务不可用、上游返回 5xx/4xx 错误或连接超时，直接向客户端返回 **HTTP 502 Bad Gateway** 或 **HTTP 504 Gateway Timeout**，杜绝将上游挂死/网络故障伪装为 0 结果空列表；
- `GET /api/v1/music/netease/song_url?song_id=...`：解析音频直链
- `GET /api/v1/music/netease/lyric?song_id=...`：获取逐字歌词
- `POST /api/v1/music/netease/intelligence`：基于目标歌曲生成心动智能推荐队列

### 8.3 边播边存智能缓存代理 (`/api/v1/music/netease/stream/{song_id}`)
- **方法 & 路径**：`GET, HEAD /api/v1/music/netease/stream/{song_id}`
- **鉴权**：支持 Header 鉴权或 URL 携带 Token (`?token=...`)
- **工作机制**：
  - **HEAD 探测支持**：原生支持 HTTP HEAD 请求，返回音频元数据头（`Accept-Ranges: bytes`, `Content-Length`, `Content-Type: audio/mpeg`）而无需拉取音频实体，彻底规避播放器预检 405 Method Not Allowed；
  - **真实音频 Magic Bytes 校验**：严格校验上游首分块 Magic Bytes（ID3v2, MPEG Frame Sync, FLAC, Ogg, RIFF 等），如遭遇上游风控/VIP 限制返回的 200 HTML/JSON 伪装错误页，立即中断并返回 `502 Bad Gateway`，杜绝播放器解析 HTML 崩溃；
  - **缓存写入原子性保障**：采用 `netease_<id>.tmp` 临时文件流式写入并在流完整拉取 (`total_bytes > 0`) 后通过系统级 `os.replace` 原子替换为最终缓存文件，若中途连接中断则立即清理临时文件，从根本上杜绝部分截断破损音频被错误索引并永久缓存在 SSD 中的风险；
  - **Cache HIT**：命中本地 SSD 缓存时，立即以标准 `206 Partial Content` 毫秒级零延迟流式返回，响应头带有 `X-Cache-Status: HIT`；
  - **Cache MISS & 后台静默缓存**：未命中时代理上游 CDN 流，支持 `bytes=0-` 及任意 Range 分片，并在后台静默将完整音轨同步写入持久化 SSD 缓存库（首片全量流带有 `X-Cache-Status: MISS-CACHING`）。
- **缓存统计与清理**：
  - `GET /api/v1/music/cache/stats`：查询已缓存曲目总数与磁盘占用字节
  - `POST /api/v1/music/cache/clean`：按 LRU 策略清理陈旧缓存

---

## 九、 收藏夹管理模块 (WGS84-Only)

专为 iOS 第二页「行驶」打造的常用地点管理模块。

### 9.1 核心约束
1. **WGS84 原生坐标约束**：首版**严格仅接受 `"wgs84"`**，传入 `gcj02` 或 `bd09` 直接返回 **422 Unprocessable Entity**（杜绝无转换算法导致 NAVITIME 偏移）。
2. **账号边界说明**：属于单车车主自用/家庭共享收藏夹池（基于 `PHONE_TOKEN` 鉴权），`created_by` 记录调用者角色，不属于多租户独立隔离体系。
3. **安全非目标**：不自动逆地理编码、不自动生成车机导航任务、删除前必须由客户端完成二次确认。

### 9.2 接口列表
- **列表查询**：`GET /api/v1/favorites?limit=50&offset=0&sort_by=created_at&order=desc&search=...`
- **新建收藏**：`POST /api/v1/favorites`
  - Body：`{"name": "公司", "address": "详细地址", "latitude": 39.9567, "longitude": 116.3212, "coord_system": "wgs84"}`
  - 返回：`201 Created`
- **详情查询**：`GET /api/v1/favorites/{favorite_id}`
- **修改编辑**：`PATCH /api/v1/favorites/{favorite_id}`（同时支持 `PUT`）
- **删除项**：`DELETE /api/v1/favorites/{favorite_id}`

---

## 十、 语音上传与 Provider 实际可用边界

### 10.1 语音接口
- **方法 & 路径**：`POST /voice`
- **请求格式**：`multipart/form-data`
- **表单字段**：`file` (音频文件)、`idempotency_key`、`client_lat`、`client_lon`、`target_device_id`

### 10.2 Provider 实际可用边界说明
> [!NOTE]
> - **ASR (语音识别) 边界**：当前系统默认内置 `MockASRProvider`（当环境变量 `ALLOW_MOCK_FALLBACK=true` 且 `ASR_PROVIDER=mock` 时可用）。当前生产环境**未接入外部云端语音识别（如 Whisper / 百度 / 腾讯 / 阿里）**，若未启用 Mock，请求将抛出 `ProviderNotConfiguredError`。
> - **NLP (自然语言解析) 边界**：默认内置 `MockNLPProvider`。
> - **Maps (地图编码器) 边界**：支持 `nominatim`（基于 OpenStreetMap 的实时地理编码服务）与 `mock`。

---

## 十一、 实施状态与验证边界声明

| 状态层级 | 验证范围与实际依据 |
| :--- | :--- |
| **代码实现 (Code Completed)** | 后端 41 个路由（40 个 HTTP 接口 + 1 个 WebSocket 接口）全量源码已就绪，位于 `app/api/` 各路由模块中。 |
| **部署验证 (Deployment Verified)** | 部署于 R9000P 生产服务（Git Commit: `3dde1d8`），系统全量通过 **119 项自动化测试 (0 失败)**。Tailscale TLS 健康检查返回 `200 OK`，实测拒绝非 WGS84 坐标返回 `422`，网易云搜索失败返回 `502/504`，生产库洁净无脏数据。 |
| **真机端到端未测 (Client Hardware Unverified)** | 车机 Android 硬件播放器实机物理音频发声效果、iOS 端行驶页 UI 收藏夹实际交互与真实网络抖动，需待三端联合实机验收时确认。 |
