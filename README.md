# MyYaris 后端服务 (FastAPI + SQLite)

MyYaris 是一套针对车机与外出手机联动的目的地自动流转后端服务。  
它负责统一接收：
1. **iPhone 快捷指令**分享的 Google Maps 地图链接或文本地址；
2. **车机前端 (Flutter / Ottocast P3 Pro)** 的中文语音与文字输入；
并在解析确认目的地后生成单调流转任务，通过 Tailscale 安全网络及 HTTPS 长轮询机制下发给车机，触发 CAR NAVITIME 导航。

---

## 核心特性

- **Tailscale 私有网络与 HTTPS 契约**：服务监听在 Tailscale 私网内部，客户端统一通过带有合法 TLS 证书的 HTTPS 连接。
- **SQLite 坚固持久化**：启用 WAL（Write-Ahead Logging）模式、5 秒 busy_timeout 以及外键约束，保证轻量且可靠的状态事实来源。
- **任务单调流转与回执**：状态严格遵循 `queued` -> `received` -> `launch_requested`，自动抵御网络乱序、重复回执造成的倒退。
- **幂等性与去重**：支持客户端请求 `idempotency_key` 去重，相同请求直接返回已有结果，不同载荷冲突抛出 409。
- **任务替代（Supersede）**：提交新目的地时，在同一事务中将尚未执行的旧任务标记为 `superseded`，车机仅领取最新有效任务。
- **长轮询与低功耗即时唤醒**：内置内存通知器 `QueueNotifier`，新任务入队瞬间唤醒挂起请求；无任务时 20 秒超时返回 204。
- **SSRF 严密防御**：地图外部链接重定向跟随严格校验白名单域名与真实解析 IP（禁止内网、回环及多播 IP）。
- **供应商解耦与可插拔**：ASR（语音识别）、NLP（地点意图抽取）、Maps（地图搜索）均具备抽象接口，未配置实际 Key 时支持平滑返回明确不可用状态或使用测试替身（Test Doubles）。

---

## API 端点概览

| 方法 | 路径 | 鉴权角色 | 描述 |
| --- | --- | --- | --- |
| `GET` | `/health` | 公开 | 服务健康检查 |
| `POST` | `/destinations` | Phone / Admin | 提交文字、明确坐标或地图分享链接 |
| `GET` | `/destinations/{id}` | Any (已认证) | 查询解析进度、候选列表、澄清问题或关联任务 |
| `POST` | `/destinations/{id}/confirm` | Phone / Admin | 确认候选地点并生成下发任务 |
| `GET` | `/devices/{id}/tasks/next` | Car / Admin | 车机端长轮询获取下一条待执行任务 |
| `POST` | `/tasks/{id}/ack` | Car / Admin | 车机端上报接收或打开导航回执 |
| `GET` | `/tasks/{id}` | Any (已认证) | 查询指定任务详细流转状态与地理坐标 |
| `POST` | `/voice` | Any (已认证) | 上传车机/手机录音并触发转写与地点解析 |

---

## iPhone 快捷指令对接指南

iPhone 快捷指令配置为在“共享表单 (Share Sheet)”中接收 URL 或文本，向 MyYaris 后端发送 POST 请求。

### 1. 提交明确 WGS84 坐标
适用于已获得经纬度的自动化流：
```bash
curl -X POST "https://myyaris.example.com/destinations" \
  -H "Authorization: Bearer <PHONE_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "input_type": "coords",
    "coordinate": {
      "latitude": 35.658581,
      "longitude": 139.745433,
      "coord_system": "wgs84"
    },
    "target_device_id": "car_p3pro"
  }'
```
响应直接进入 `resolved` 状态并生成 `task_id`。

### 2. 提交 Google Maps 分享链接
快捷指令从 Google Maps App 点击“分享”获取链接（如 `https://maps.app.goo.gl/...`）：
```bash
curl -X POST "https://myyaris.example.com/destinations" \
  -H "Authorization: Bearer <PHONE_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "input_type": "share_link",
    "url": "https://maps.app.goo.gl/EXAMPLE",
    "target_device_id": "car_p3pro"
  }'
```

---

## 链接真实支持范围与解析策略

后端严格执行真实导航坐标保障准则，**坚决不生成假坐标**：

1. **直接解析成功并生成任务**：
   - 包含 Google Maps 真实图钉属性（`!3d<lat>!4d<lon>`）的长/短链接；
   - 包含明确查询参数图钉（`destination=<lat>,<lon>`、`daddr=<lat>,<lon>`、`q=<lat>,<lon>`、`ll=<lat>,<lon>`）；
2. **返回 `needs_clarification`（澄清）而非假坐标**：
   - **仅包含视野中心**（`/@lat,lon,zoom`）：此类链接仅代表用户浏览地图时的相机中心，并非目的地。系统返回清晰提示：“所分享链接仅为地图视野中心，并非具体的目的地或图钉。请在地图上长按放置图钉或点击具体地点后再分享。”
   - **仅包含地点名称或 Place ID**：在未配置 Google Places API 时，系统返回澄清说明并提示用户提供图钉坐标或经纬度，**杜绝猜测或伪造测试坐标**。
3. **SSRF 安全防护**：
   - 严格跟随白名单域名（`maps.google.com`, `maps.app.goo.gl` 等）；
   - 解析目标 DNS IP，彻底阻断私有网段（`10.0.0.0/8`, `172.16.0.0/12`, `192.0.2.20/16`）及回环地址（`127.0.0.0/8`）。

---

## 状态机与流转契约

### 解析请求状态 (`ParseRequest.status`)
- `resolving`: 正在异步处理或等待外部解析。
- `resolved`: 目的地坐标已明确确定，`task_id` 已创建并下发。
- `needs_clarification`: 存在歧义或需要用户确认（包含 `candidates` 列表与 `clarification_question`）。
- `failed`: 参数校验失败或未配置的供应商调用失败。

### 任务状态 (`Task.status`)
状态严格遵循**单调递增**流转：
- `queued`: 任务已在数据库持久化排队，等待车机领取。
- `received`: 车机长轮询已成功拉取该任务并上报回执。
- `launch_requested`: 车机已成功发出调用系统 CAR NAVITIME 的 Intent。
- `failed`: 车机调起失败或超时失败（终端状态，**不可倒退**）。
- `superseded`: 用户发起新目的地，旧任务在事务中自动失效替代。
- `expired`: 任务超过有效期限（默认 60 分钟）未被执行。

---

## 服务访问地址 (无凭据)

- **本地回环调试**：`http://127.0.0.1:8000`
- **Tailscale 私网访问**：`https://myyaris.example.com`（通过 `tailscale serve --bg 8000` 代理，提供统一 TLS 证书，对外不开放公网端口）

---

## 运行自动化测试

在远端或本地运行 pytest：

```bash
cd backend
source .venv/bin/activate
pytest -v
```
