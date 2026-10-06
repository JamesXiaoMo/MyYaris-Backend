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
| `POST` | `/destinations` | Phone / Admin | 提交文字、坐标或地图分享链接 |
| `GET` | `/destinations/{id}` | Any | 查询解析进度、候选列表、澄清问题或关联任务 |
| `POST` | `/destinations/{id}/confirm` | Phone / Admin | 确认候选地点并生成下发任务 |
| `GET` | `/devices/{id}/tasks/next` | Car / Admin | 车机端长轮询获取下一条待执行任务 |
| `POST` | `/tasks/{id}/ack` | Car / Admin | 车机端上报接收或打开导航回执 |
| `GET` | `/tasks/{id}` | Any | 查询指定任务详细流转状态与地理坐标 |
| `POST` | `/voice` | Any | 上传车机/手机录音并触发转写与地点解析 |

---

## 运行自动化测试

在远端或本地运行 pytest：

```bash
cd backend
source .venv/bin/activate
pytest -v
```
