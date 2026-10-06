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

后端严格执行真实导航坐标保障准则，**坚决不猜测、不伪造假坐标**：

1. **直接解析成功并生成任务**（支持合法整数与浮点 WGS84 坐标）：
   - 包含明确单一地点图钉（`!3d<lat>!4d<lon>`）的长/短链接；
   - 包含明确终点查询参数的链接（`destination=<lat>,<lon>`、`daddr=<lat>,<lon>`）；
   - 包含明确经纬度图钉搜索参数（`q=<lat>,<lon>`，即使附带 `ll` 视野中心亦优先采用 `q` 目标）；
   - 兼容合法整数坐标（例如 `35, 139` 或 `0, 0`）与标准浮点坐标。
2. **返回 `needs_clarification`（澄清）而非猜测假坐标**：
   - **`ll` 单独出现或仅视野中心**（`/@lat,lon,zoom` 或 `?ll=lat,lon`）：此类链接仅代表地图浏览视野中心，并非具体目的地。系统返回清晰说明：“所分享链接仅为地图视野中心，并非具体的目的地或图钉。请在地图上长按放置图钉或点击具体地点后再分享。”
   - **路线导航链接缺终点参数**（`/dir/` 链接）：路线链接中首个出现的坐标通常为出发地/起点，为杜绝将出发地误当终点下发车机，系统不自动选取首个坐标，要求用户直接提供终点图钉或明确坐标。
   - **包含多个地点坐标**：存在多个不同 `!3d!4d` 候选点且无法证明终点时，系统不盲目取第一个，返回澄清提示。
   - **仅包含地点名称或 Place ID**：在未配置 Google Places API 等外部服务时，系统明确说明地图检索服务未配置，引导用户提供经纬度或图钉链接。
3. **SSRF 安全防护与重定向处理**：
   - 相对重定向统一使用 `urljoin` 正确还原完整 URL；
   - 重定向每跳及最终落地 URL 严格校验白名单域名（`maps.google.com`, `maps.app.goo.gl` 等）与真实解析 IP（禁止内网、回环及多播 IP）；
   - 重定向跳数耗尽时安全拦截报错，杜绝解析未经验证的目的地。

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

---

## 生产运行状态与日志 (无凭据)

- **运行方式**：Linux User Systemd 服务 (`systemctl --user status myyaris.service`)，已配置开机与无登录会话持久常驻 (`loginctl enable-linger myyaris-user`)，进程崩溃自动拉起 (`Restart=always, RestartSec=3s`)。
- **监听绑定**：`http://127.0.0.1:8000`（安全绑定本机回环，预备由 Tailscale Serve 终结 HTTPS 并反向代理）。
- **实时日志查看**：`journalctl --user -u myyaris.service -f`
- **数据持久化目录**：`/path/to/user/.local/share/myyaris/myyaris.db`（SQLite WAL 模式，WAL + SHM 双缓冲）。
- **运行配置与凭据**：高强度随机 Token 已由脚本初始化至 `/path/to/user/桌面/MyYaris/backend/.env`（权限 `0600`，绝不输出），`ALLOW_MOCK_FALLBACK=false`（严格禁用任何 mock 假坐标回退）。
- **运行记录时间**：`2026-10-06 20:00:09 JST`（PID: 1067821，验证通过）。

---

## Tailscale 私网访问与 HTTPS 配置指南

当前 Tailnet 三端实时在线状态：
- **Linux R9000P**：`192.0.2.10` (`myyaris.example.com`)
- **iPhone**：`192.0.2.10` (`iphone182`)（R9000P `tailscale ping` 响应时间 13ms）
- **车机 Ottocast P3 Pro**：`192.0.2.10` (`ottoaibox-p3-pro`)（R9000P `tailscale ping` 响应时间 45ms）

### 真实 HTTPS 目标地址（供 Flutter 与 iPhone 快捷指令使用）
```text
https://myyaris.example.com
```

### Tailscale Serve 当前状态与剩余用户操作
Tailscale 守护进程对 `serve` 配置实施权限限制：
执行 `tailscale serve --bg http://127.0.0.1:8000` 报错：
```text
sending serve config: Access denied: serve config denied
Use 'sudo tailscale serve --bg http://127.0.0.1:8000'.
To not require root, use 'sudo tailscale set --operator=$USER' once.
```
因 sudo 需要用户交互输入密码，系统遵循安全原则不猜测密码，**剩余操作需用户在 R9000P 终端执行一次授权**：

**选项 A（直接启动 Serve 反代）**：
```bash
sudo tailscale serve --bg http://127.0.0.1:8000
```

**选项 B（一次性赋予 operator 权限，后续无需 sudo）**：
```bash
sudo tailscale set --operator=myyaris-user
tailscale serve --bg http://127.0.0.1:8000
```

执行后，Tailscale 将在私网端口 443 终结 TLS 证书并自动代理至本机的 `http://127.0.0.1:8000`，使 iPhone 快捷指令与车机 P3 Pro 均能通过上述统一 HTTPS 域名安全访问。

---

## 运行自动化测试与现网巡检

1. **运行 Pytest 单元与安全测试套件**：
```bash
cd /path/to/user/桌面/MyYaris/backend
.venv/bin/pytest -v
```
*(33 项测试全量通过，覆盖整数坐标、路线起点保护、ll/q优先级、多坐标澄清、SSRF防护及跳转耗尽拦截)*

2. **运行当前活跃服务黑盒验证**：
```bash
cd /path/to/user/桌面/MyYaris/backend
.venv/bin/python tests/verify_running_service.py
```
*(已通过真实 Token 验证 /health、未鉴权与越权拒绝、测试设备 car_test_probe 坐标入队与轮询，未发送虚假车机 ack)*

