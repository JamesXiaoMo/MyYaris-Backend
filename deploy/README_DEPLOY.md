# MyYaris 后端部署与运维指南

## 1. 运行环境与架构

- **主机**: Lenovo Legion R9000P (Ubuntu 24.04 LTS)
- **部署路径**: `/path/to/user/桌面/MyYaris/backend`
- **数据与数据库目录**: `/path/to/user/.local/share/myyaris/`（包含 `myyaris.db` 与 `audio_temp/`）
- **网络接入**: Tailscale 私有网段 + Tailscale HTTPS 证书代理

---

## 2. 启动与调试 (手动模式)

在后端目录启动开发/调试服务：

```bash
cd /path/to/user/桌面/MyYaris/backend
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

---

## 3. Tailscale HTTPS 暴露与证书管理

Tailscale 在本机已启用 MagicDNS 及 TLS 域名：
`myyaris.example.com`

通过 Tailscale Serve 可以原生提供受信任的 HTTPS 终止并将流量反向代理到本地 8000 端口：

### 启动 Tailscale Serve HTTPS:
```bash
tailscale serve --bg 8000
```
或者指定自定义端口：
```bash
tailscale serve --https=8443 http://127.0.0.1:8000
```

### 验证 Tailscale Serve 状态:
```bash
tailscale serve status
```

### 客户端访问地址:
- **健康检查**: `https://myyaris.example.com/health`
- **长轮询接口**: `https://myyaris.example.com/devices/car_p3pro/tasks/next?timeout=20`

---

## 4. systemd 服务管理

如需开机自启和异常自动拉起，可安装提供的 systemd 服务单元：

```bash
# 复制服务文件
sudo cp /path/to/user/桌面/MyYaris/backend/deploy/myyaris.service /etc/systemd/system/

# 重载并启动
sudo systemctl daemon-reload
sudo systemctl enable --now myyaris.service

# 查看运行状态与实时日志
sudo systemctl status myyaris.service
journalctl -u myyaris.service -f
```

---

## 5. SQLite 数据库安全与备份

- SQLite 引擎已配置 `PRAGMA journal_mode=WAL;` 与 `PRAGMA busy_timeout=5000;`。
- **备份注意**：由于处于 WAL 模式，直接 `cp myyaris.db` 可能导致复制到未提交或不一致的状态。
- 建议使用 SQLite 在线备份命令：
  ```bash
  sqlite3 /path/to/user/.local/share/myyaris/myyaris.db ".backup /path/to/user/backup/myyaris_$(date +%Y%m%d).db"
  ```
