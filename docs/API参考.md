# MyYaris 全量后端 API 参考手册 (API Reference)

**版本**：V2.3.1  
**基准日期**：2026-10-13  
**后端 Git 提交 Hash**：`3dde1d8` (`3dde1d85a5fa1cd284fc53f6483de8229d757fc3`)  
**部署宿主机**：Linux Edge Server (Ubuntu, systemd user service: `myyaris.service`)  
**关联核心契约**：
- [三端协同与播控契约 (docs/API-V2三端契约.md)](../../docs/API-V2三端契约.md)
- [收藏夹业务契约 (docs/收藏夹API契约.md)](../../docs/收藏夹API契约.md)

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
- **本机/局域网回环**：`http://127.0.0.1:8000` / `http://<LAN_IP>:8000`
- **安全传输基地址 (HTTPS / WSS)**：`https://<YOUR_SERVER>.your-tailnet.ts.net`

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
    "raw_input": "MPR3+PP",            // 支持: 纯坐标、Google Maps 分享短链、Plus Code、地名关键词
    "source": "ios_share",              // "ios_share" | "car_ui" | "voice"
    "purpose": "favorite_preview",      // "favorite_preview" (默认) | "navigation"
    "reference_coordinate": {          // 短 Plus Code 必填，且必须为 wgs84
      "latitude": 35.6812,
      "longitude": 139.7671,
      "coord_system": "wgs84"
    }
  }
  ```
- **核心逻辑与边界约束**：
  - **输入校验防御**：`raw_input` 严禁为空或纯空白字符，违者直接返回 **422 Unprocessable Entity**（`detail: "输入内容不能为空。"`），且无任何数据库副作用；
  - **Open Location Code 区域中心**：Plus Code 解码结果为该网格几何中心 (`latitudeCenter`, `longitudeCenter`)，非建筑物入口精确坐标；
  - **短码严格约束**：短码必须提供显式 WGS84 参考坐标；若缺省或仅附带城市文字，直接返回 **422 Unprocessable Entity**（不调用在线城市查询服务猜测），杜绝 `(0.0, 0.0)` 伪装；
  - **Purpose 契约与确认状态语义**：
    - `purpose` 默认值为 `"favorite_preview"`。短码经参考还原、全码粗编码（长度 < 10）或多候选结果固定返回 `needs_confirmation: true` 与 `status: "needs_clarification"`；
    - **统一预览防派发保护**：所有以 `purpose="favorite_preview"` 解析生成的记录，**统一严禁用于派发车机导航任务**——无论是调用 `POST /api/v1/tasks/destination` 还是 `POST /destinations/{id}/confirm`，服务端均直接返回 **400 Bad Request** 拦截；
    - **iOS 真实导航派发**：客户端若需直接发起真实车机导航，解析时必须显式指定 `"purpose": "navigation"`，待解析状态为 `"resolved"` 后再行调度派发；
  - **零任务副作用**：`auto_create_task=False` 严格生效，解析阶段绝不提前生成车机导航任务；多候选时通过 `candidates` 数组返回全量列表。
- **响应示例 (200 OK)**：
  ```json
  {
    "code": 200,
    "destination_id": "3b196fb4-2f34-4465-ae66-7f0cd3dc2975",
    "name": "Plus Code 8Q7XMPR3+PP",
    "latitude": 35.698383,
    "longitude": 139.773072,
    "confidence": 0.85,
    "status": "needs_clarification",
    "message": null,
    "clarification_question": "已按参照位置还原最近匹配的短编码区域中心，请确认是否为目标地点；参照位置偏差可能导致错误地点。",
    "needs_confirmation": true,
    "candidates": [
      {
        "candidate_id": "cand_12345678",
        "title": "Plus Code 8Q7XMPR3+PP",
        "address": "Plus Code 编码区域中心（按参照位置还原，非建筑物入口精确坐标）",
        "latitude": 35.698383,
        "longitude": 139.773072,
        "coord_system": "wgs84",
        "source": "plus_code"
      }
    ]
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
- **门禁拦截说明**：
  - 若 `destination_id` 对应的解析请求为收藏预览用途（`purpose="favorite_preview"`），**直接拦截并返回 400 Bad Request**；
  - 若解析请求状态仍为 `needs_clarification`，**直接拦截并返回 400 Bad Request**；
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
- **门禁拦截说明**：若目标记录为收藏预览用途（`purpose="favorite_preview"`），**同样直接拦截并返回 400 Bad Request**。如需发起真实导航请使用 `purpose="navigation"` 解析。

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
- **鉴权**：`phone` / `admin` / 绑定自身设备的 `car`
- **请求体 (`MusicCommandRequest`)**：
  ```json
  {
    "command_id": "cmd_music_99a8b7c6d5e4", // 客户端唯一UUID
    "device_id": "car_example",
    "action": "play_track", // 动作枚举，详见下表
    "params": {
      "track_id": "netease_1901371647",
      "title": "海阔天空",
      "artist": "Beyond",
      "stream_url": "/api/v1/music/netease/stream/1901371647"
    },
    "expires_in_seconds": 60 // 可选，超时秒数
  }
  ```
- **支持动作清单 (`action`)**：
  - `play_track`（指定曲目播放，支持 `track_id`, `queue_id`, `queue_index`, `stream_url` 等）
  - `set_playback_mode`（显式切换播放模式：`mode="list_loop"` | `"shuffle"` | `"intelligence"` | `"single_loop"`, 可带 `seed_track_id`, `expected_queue_id`, `expected_queue_version`；车机无 `queue_v2` 返回 422 `CAPABILITY_NOT_SUPPORTED`）
  - `switch_queue`（显式切换播放队列源：`source="netease_favorite"` | `"netease_intelligence"` | `"single_track"`, 可带 `start_index`, `start_track_id`；车机无 `queue_v2` 返回 422 `CAPABILITY_NOT_SUPPORTED`）
  - `pause`（暂停）、`resume`（继续）、`stop`（停止）
  - `next`（下一首，车机自主维持全量循环）、`prev`（上一首）
  - `seek`（进度跳转，`position_ms`，TTL 默认 20s）
  - `set_volume`（软件音量 `volume: 0.0~1.0`，TTL 默认 20s）
  - `set_loop`（兼容映射）、`set_shuffle`（兼容映射）
- **Seek 进度参数归一化与严格校验**：
  - 统一支持 `seek_position_ms`、`seek_ms`、`position_ms`；
  - 若同时传入不同数值导致冲突 -> 返回 **HTTP 422 Unprocessable Entity** (`SEEK_PARAMETER_CONFLICT`)；
  - 若 `action == "seek"` 且所有 seek 参数均为空 -> 返回 **HTTP 422** (`SEEK_PARAMETER_REQUIRED`)；
  - 若传入负数 -> 返回 **HTTP 422** (`INVALID_SEEK_POSITION`)；
  - 校验通过后统一归一化为 `seek_position_ms`，同时兼容填充 `seek_ms` 与 `position_ms`。
- **单指令原子状态机流转 (Atomic Single-Command Protocol)**：
  - 手机发送 `play_track` / `set_playback_mode` / `switch_queue` / `resume` 时，服务端内存准备队列快照并在指令参数中注入 `queue_id`、`queue_version`，同时仅更新数据库中的期望模式 `requested_mode`；
  - **绝不立即更新 `applied_mode` 与 `queue_source`**；只有在车机端真正执行成功并通过 `/ack` 或 `/status` 上报 `status == "applied"` 时，数据库中的 `applied_mode` 与 `queue_source` 方正式提交生效；执行失败则保持既有模式。
- **幂等性与冲突规则**：
  - **幂等校验优先原则**：相同 `command_id` 重放时，优先进行入参比对与设备匹配。比对时自动忽略服务端注入生成的队列元数据（`queue_id`, `queue_version`, `source` 等）；匹配成功直接返回 HTTP 200 既有记录，不受车机动态能力掉落或队列版本变更干扰；
  - 相同 `command_id` + 不同 payload 或不同设备 -> 返回 **HTTP 409 Conflict** (`COMMAND_CONFLICT`)；
  - 携带 `expected_queue_id` 或 `expected_queue_version` 前置断言与服务端当前不符时 -> 返回 **HTTP 409 Conflict** (`QUEUE_PRECONDITION_FAILED`)。

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
    "applied_mode": "list_loop",
    "queue_version": 102,
    "current_index": 45,
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
  - 关键状态字段：`device_id`, `playback_state` (`playing`|`paused`|`stopped`), `applied_mode`, `requested_mode`, `queue_id`, `queue_version`, `queue_source`, `current_index`, `current_track_id`, `position_ms`, `duration_ms`, `software_volume`, `consecutive_failures`, `error_code`, `error_detail`, `capabilities`, `status_seq`, `last_applied_command_id`
  - 单调序列保护：仅当 `status_seq > current_status_seq` 时全量更新快照，迟到 ACK 不回退终态。
- **查询当前快照**：`GET /api/v1/music/control/status?device_id=car_example`

### 5.7 全量播放队列元数据与分页 (`GET /api/v1/music/queue`)
- **方法 & 路径**：`GET /api/v1/music/queue`
- **鉴权**：`phone` / `car` / `admin`
- **Query 参数**：
  - `device_id`: 车机标识（如 `car_example`）
  - `source`: `"netease_favorite"` (默认喜欢歌单) | `"netease_intelligence"` | `"single_track"`（仅作只读查询过滤，严禁激活或切换队列）
  - `queue_id`: 队列标识（第 0 页可选，后续分页必带）
  - `queue_version`: 队列版本（第 0 页可选，后续分页必带）
  - `offset`: 分页偏移量 (默认 0)
  - `limit`: 每页大小 (默认 50，最大 100)
- **响应结构 (`MusicQueueResponse`)**：
  - 返回 `code`, `queue_id` (Opaque 如 `q_fav_<hash>`), `queue_version`, `source`, `name`, `total` (全量曲目数), `is_complete`, `queue_state`, `offset`, `limit`, `has_more`, `tracks` 列表。
- **错误响应码**：
  - `409 QUEUE_VERSION_CONFLICT`：传入的 `queue_version` 与服务端当前版本不一致，客户端丢弃脏分页从第 0 页重拉；
  - `409 EMPTY_PLAYLIST`：Owner 喜欢歌单为 0 首，无法生成播放队列；
  - `409 QUEUE_NOT_READY`：当前队列尚未就绪；
  - `409 INTELLIGENCE_AUTH_INVALID`：网易云登录会话失效。
- **契约核心保障**：
  - 纯读接口，绝不带切换当前播放队列的副作用；
  - 列表循环与随机播放必须覆盖 `total` 全量喜欢曲目，严禁使用 3 首样本代替；
  - 媒体直链 `stream_url` 必须为相对路径或安全可信 HTTPS 反代，严禁 `127.0.0.1`；未校验音轨 `playable` 严格返回 `null`。
  - 详见专篇：`docs/音乐播放队列与状态同步契约.md`。

---

## 六、 全双工 WebSocket 实时通道

### 6.1 端点连接与握手
- **协议 & 路径**：`WSS /api/v1/ws/music`
- **Query 参数**：
  - `token`: 必填鉴权 Token（手机 Token 或车机 Token）
  - `client_type`: `"phone"`（手机客户端）或 `"car"`（车机端）
  - `device_id`: 车机标识（如 `car_example`）

### 6.2 双信封协议架构 (Dual Envelope)
广播状态时同时支持外层平铺字段与内层 `data` 对象，兼顾车机轻量解析与 iOS 强类型模型：
```json
{
  "type": "status_update",
  "timestamp": "2026-10-13T09:47:00Z",
  "device_id": "car_example",
  "playback_state": "playing",
  "current_track_id": "netease_1901371647",
  "position_ms": 45000,
  "duration_ms": 240000,
  "software_volume": 0.8,
  "data": {
    "device_id": "car_example",
    "playback_state": "playing",
    "current_track_id": "netease_1901371647",
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
- `GET /api/v1/music/netease/playlist/favorite?offset=0&limit=30`：分页获取当前网易云账号专属「我喜欢的音乐」
  - **权限约束**：仅限 `admin` 管理员角色（及过渡期 legacy phone 凭据）访问；普通注册用户 (`user`) 或车机硬件 (`car`) 访问返回 **HTTP 403 Forbidden**；匿名未鉴权请求返回 **HTTP 401 Unauthorized**；
  - **会话状态与错误语义**：
    - 若后端全局网易云账号尚未登录，返回 **HTTP 409 Conflict**，避免误触发客户端注销 MyYaris 自身会话；
    - 若账号切换导致异步定位中会话凭证失效，安全中断并返回 **HTTP 409 Conflict**，杜绝跨账号脏数据污染缓存；
    - 若歌单存在多个匹配候选，返回 **HTTP 409 Conflict**（明确指示多重歧义，杜绝误读）；
    - 若无证据证明归属于当前 Owner 或 Owner 匹配不一致，返回 **HTTP 404 Not Found**；
  - **严格所有者闭环识别**：依据第三方 API（`Binaryify/NeteaseCloudMusicApi`）规范：
    - `specialType == 5` 仅作为候选类型提示，必须满足 `creator.userId == current_owner_uid`；
    - 若上游 `/user/playlist` 列表条目缺失 `creator` 属性，后端**严禁静默接纳**，自动穿透调用 `/playlist/detail` 获取全量歌单元数据补全所有者验证；若补全后仍缺失或所有者不一致则断然剔除；
    - 绝不依赖硬编码 Playlist ID，绝不依赖歌单中文名模糊匹配，严禁返回其他账号私人歌单；
  - **分页与排序**：支持 `offset`（ge=0）与 `limit`（ge=1, le=100，默认 30），保持上游原生返回顺序；offset 越界或空歌单返回合法空页 (`songs: []`, `has_more: false`)；
  - **音轨与权限元数据**：返回标准化 `NeteaseSongItem`（包含 `id`, `song_id`, `title`, `artist`, `album`, `duration_ms`, `cover_url`, `cached`, `stream_url`, `fee`, `playable`, `source: "netease"`），兼容现有 Stage 1 播放模型（同时提供 `songs` 与 `tracks` 字段便于客户端直接调用 `playTrack`）；
  - **上游错误透传**：上游 5xx 返回 **HTTP 502 Bad Gateway**，超时返回 **HTTP 504 Gateway Timeout**，严禁吞咽异常掩盖为空列表；
  - **零副作用保障**：纯只读拉取，不触发点赞/取消红心，不派发播控或导航指令，不改变全局账号授权数据，不自动预探测、预下载或预缓存全量 700+ 音频；
- `GET /api/v1/music/netease/song_url?song_id=...`：解析音频直链
- `GET /api/v1/music/netease/lyric?song_id=...`：获取逐字歌词
- `GET /api/v1/music/queue`：获取全量播放队列不可变快照分页音轨元数据
  - **权限约束**：严格要求 **Owner 或绑定车机**（`require_owner_or_car`）。Admin 或过渡期 Legacy Phone Token 可访问；车机硬件（`role == "car"`）仅可访问与自身绑定设备完全一致的队列；普通注册用户（`role == "user"`，即使具备车辆硬件控制权限 `has_device_control == True`）**一律返回 HTTP 403 Forbidden**，防止泄露车主私密收藏；未鉴权返回 HTTP 401；
  - **纯读幂等无副作用**：Query 传入 `?source=...` 仅作为条件过滤查询，绝对不激活或更改服务端当前播放队列；
  - **不可变快照分页规则**：首页（`offset=0`）返回最新快照及确定的 `queue_id`、`queue_version`；后续分页（`offset>0`）必须带上此版本号，若发生版本变更直接返回 **HTTP 409 Conflict** (`QUEUE_VERSION_CONFLICT`)，由客户端丢弃脏分页并从第 0 页重新拉取；
  - **全局单调版本**：`queue_version` 采用高精度毫秒单调递增水位，在服务重启或内存清空后依然单调递增，永不碰撞；
  - **全量歌单覆盖**：`netease_favorite` 在初始化时完整遍历 `has_more` 自动完成多页拉取与去重，杜绝截断前 100 首；若歌单为空返回 **HTTP 409 Conflict** (`EMPTY_PLAYLIST`)。
- `POST /api/v1/music/netease/intelligence`：基于目标歌曲生成心动智能推荐队列
  - **权限与安全约束**：同样严格要求 `require_owner_or_car`（普通注册用户无论有无设备控制权均拒绝 403；车机硬件仅限自身设备）；开启严密的 CSRF 双重 Cookie 校验；
  - **请求体 (`NeteaseIntelligenceRequest`)**：`{"request_id": "...", "seed_song_id": 1901371647, "playlist_id": 987654321, "batch_index": 0, "count": 30}`
  - **首批种子资格校验 (`batch_index == 0`)**：`seed_song_id` 必须属于当前 Owner 已验证的喜欢歌单，否则返回 **HTTP 422 Unprocessable Entity** (`INVALID_SEED_SONG`)；
  - **动态续批与幂等防重 (`batch_index > 0`)**：续批种子必须为前序批次末尾歌曲或当前播放曲目，携带 `request_id` 幂等去重；单次心动会话续批上限 5 次（总曲目 150 首）；
  - **不可变快照追加**：续批时对既有音轨浅拷贝后追加新音轨并递增生成新 `QueueSnapshot`，历史版本快照在内存中完全保持只读不变；
  - **真实心动模式保障**：必须来自上游 `/playmode/intelligence/list` 真实推荐流；
  - **严禁伪装**：上游未登录/凭证失效返回 **HTTP 409 Conflict** (`INTELLIGENCE_AUTH_INVALID`)，上游服务异常返回 **HTTP 502 Bad Gateway** (`INTELLIGENCE_UPSTREAM_FAILED`)；曲目耗尽且续批失败返回 **HTTP 503** (`INTELLIGENCE_QUEUE_EXHAUSTED`)；断然禁止使用 `simi_fallback` 相似曲或空列表假冒成功；
  - **无害容错**：失败时车机保留原播放状态与原队列，绝不中断当前音频，绝不自动重新登录，绝不调用 `like/unlike` 接口。

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

## 九、 收藏夹与单层分类文件夹管理模块 (WGS84-Only)

专为 iOS 第二页「行驶」底部抽屉打造的常用地点与单层分类文件夹管理模块。

### 9.1 核心约束
1. **WGS84 原生坐标约束**：严格仅接受 `"wgs84"`，传入 `gcj02` 或 `bd09` 直接返回 **422 Unprocessable Entity**（杜绝无转换算法导致 NAVITIME 偏移）。
2. **严格单账号隔离与数据库级同名唯一约束**：登录账号（`user_id` 非空）仅访问自己的文件夹与收藏地址；过渡期 Legacy 静态 Token 仅访问 Legacy 池（`user_id IS NULL`）。严禁任何跨账号文件夹/地址引用（包括管理员跨用户归类）。`favorite_folders` 通过 `(owner_scope, name_norm)` 数据库级唯一索引与 `IntegrityError -> 409 Conflict` 事务回滚映射保障并发创建/重命名无重名绕过。
3. **非空文件夹安全删除与新旧表约束边界**：新库建表 `favorites.folder_id` 带 `REFERENCES favorite_folders(id) ON DELETE SET NULL`；既有生产库经 `ALTER TABLE favorites ADD COLUMN folder_id VARCHAR(64)` 迁移不带底层 `REFERENCES` 约束，由应用层在删除文件夹时于同一数据库事务内执行 `UPDATE favorites SET folder_id = NULL` + `DELETE FROM favorite_folders`、写入校验及读取孤儿归一化保障，**严禁级联删除用户已存地址**。
4. **安全非目标与重试约束**：仅支持单层文件夹；不提供后端 POI 搜索接口；不自动逆地理编码；不自动生成车机导航任务；`POST /api/v1/favorites` 无 `idempotency_key`，客户端超时不得盲目自动重试；重复 `DELETE` 返回 `404`，客户端收到 `404` 应刷新列表。


### 9.2 单层文件夹接口 (`/api/v1/favorite-folders`，兼容 `/api/v1/favorites/folders`)
- **文件夹列表**：`GET /api/v1/favorite-folders?limit=50&offset=0&sort_by=created_at&order=asc&search=...`（返回 `items` 含 `favorite_count`、`total`、`has_more`、`uncategorized_count`）
- **新建文件夹**：`POST /api/v1/favorite-folders`
  - Body：`{"name": "日常通勤"}`（去空白后 1~64 字符，同账号重名返回 `409 Conflict`）
- **文件夹详情**：`GET /api/v1/favorite-folders/{folder_id}`
- **重命名文件夹**：`PATCH /api/v1/favorite-folders/{folder_id}`（同时支持 `PUT`）
- **删除文件夹**：`DELETE /api/v1/favorite-folders/{folder_id}`（同事务将旗下地址 `folder_id` 置为 `NULL`，返回 `moved_to_uncategorized_count`）

### 9.3 收藏地址接口 (`/api/v1/favorites`)
- **列表与分类筛选**：`GET /api/v1/favorites?limit=50&offset=0&sort_by=created_at&order=desc&search=...&folder_id=...&uncategorized=...`
  - 不传 `folder_id` / `uncategorized`：返回当前账号全部收藏；
  - `folder_id=fld_xxx`：仅返回指定文件夹下的收藏；
  - `uncategorized=true` 或 `folder_id=uncategorized`：仅返回未分类（`folder_id IS NULL`）收藏；
- **新建收藏**：`POST /api/v1/favorites`
  - Body：`{"name": "公司", "address": "详细地址", "latitude": 39.9567, "longitude": 116.3212, "coord_system": "wgs84", "folder_id": "fld_xxx"}`（`address` 可选，缺省/null/空白默认转为 `""`；`name` 与 `address` 均可为空；`folder_id` 可选）
  - 返回：`201 Created`
- **详情查询**：`GET /api/v1/favorites/{favorite_id}`
- **修改编辑与移动归类**：`PATCH /api/v1/favorites/{favorite_id}`（同时支持 `PUT`；不传 `address` 保持原值，传 `""`/`null`/空白清除为空串；不传 `folder_id` 保持原归属，传 `"fld_xxx"` 移动至目标文件夹，传 `null` 或 `""` 移回未分类）
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
| **部署验证 (Deployment Verified)** | 部署于生产服务（Git Commit: `3dde1d8`），系统全量通过 **119 项自动化测试 (0 失败)**。Tailscale TLS 健康检查返回 `200 OK`，实测拒绝非 WGS84 坐标返回 `422`，网易云搜索失败返回 `502/504`，生产库洁净无脏数据。 |
| **真机端到端未测 (Client Hardware Unverified)** | 车机 Android 硬件播放器实机物理音频发声效果、iOS 端行驶页 UI 收藏夹实际交互与真实网络抖动，需待三端联合实机验收时确认。 |


## 2026-10-20 播放统一修订（覆盖早期测试入口说明）

- 用户显式点播放且未选来源时，默认完整、当前已验证 Owner 的 `netease_favorite`。循环顺序首尾循环；随机在全量元数据内无放回遍历。搜索显式单曲使用 `single_track`，自然结束停止；选择循环/随机明确返回喜欢歌单。
- 打开 App、登录恢复、WS 重连不发起播放，也不暂停已有播放。车机仅恢复已有快照的元数据和 cursor/position；既无快照也无来源时，等待用户点播放才准备喜欢队列。手机不后台代下一曲。
- 模式切换保留播放/暂停状态。当前歌曲仍在目标快照时保留其索引与音频源，不重新加载同曲、不归零进度；正常暂停 resume 不采用服务器迟到的旧 cursor 覆盖车机实际位置。冷播放器恢复采用服务器实际 cursor/position。
- `CarMusicQueueController` 是唯一队列调度器；just_audio 始终只加载当前一首，原生 loop/shuffle 关闭，由 completed 事件驱动下一首。首页、内置页、系统媒体按钮统一进入后端播控管线；移除本地三样例默认源及第三方 MediaSession 模式分叉。
- 心动只接受真实 `intelligence` 上游结果；`simi_fallback`、`empty`、普通随机与空数组均失败并保留旧状态。首批生成只准备快照；切模式指令 pending 后仅由车机 applied 激活。GET 未存在的心动来源返回 `QUEUE_NOT_READY`，不回喜欢伪装成功。
- 心动 `is_complete=false` 表示未来推荐尚可续批，**不等于当前有限批次元数据不可加载**。客户端仍必须分页完整加载 `total` 首并验证 `queue_state=ready`、版本、索引和前缀；`batch_index` 由服务器给出。
- 心动续批携带 `device_id`、`queue_id`、`queue_version`、稳定 `request_id`、`batch_index`、`seed_song_id` 和可选 `playlist_id`。同 ID 同参数在调用上游前重放缓存；不同参数 409。只允许上一批尾曲或实际当前曲作种子，最多总计 5 批/150 首，不生成本地版本号。失败不换来源；耗尽停止并显示原因。
- 元数据完整分页不等于媒体预下载：只按需读取当前曲媒体流，接受后端同源可信 HTTPS 或解析到该后端的相对地址，保留严格 TLS。不可播按有限尝试跳过，最多连续 3 首；失败有可见原因，错误回滚原队列。授权/心动准备失败保持旧状态。
- HTTP/WS 命令串行执行并按 command_id 去重，自动完成事件也进入相同执行序列；过期/重复/迟到 ACK 在修改实际状态之前检查，包含同 queue_id 的旧模式 ACK。`received` 不代表 applied，applied 不代表真机实声。
- 车机 `status_seq` 发出前持久化，冷进程使用持久化水位与当前时钟的最大值递增；并发状态构建串行。后端仅接受严格更大的序列，相等重复不覆盖；`updated_at` 继续发送整数毫秒，并保留服务端整数/ISO 兼容修复。
- 后端 HTTP `/music/control/command` 也接受绑定车机自身 token，执行同一管线；车机不能控制/读取别的设备，WS 不能伪装 phone/car 角色。普通用户的 Owner 资产门禁仍保留。旧 `set_loop`/`set_shuffle` 归一化到正式模式；不支持的语义显式失败。
- 三个主要菜单为喜欢循环、喜欢随机、网易云心动。兼容单曲循环仍可经协议使用，手机仅在已应用该模式时显示兼容项。手机歌曲详情的心动按钮复用统一模式入口，不再独立生成并冒充播放队列。

验证使用隔离 SQLite、fake 上游、真实 HTTP/WS 路由、fake just_audio 原生平台和本地回环 HTTP。没有发送生产播放命令、调用真实心动/点赞/导航或实车 OBD。构建、部署、设备安装和实际出声必须分别记录，不能相互代替。
