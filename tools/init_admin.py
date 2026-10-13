#!/usr/bin/env python3
"""
MyYaris - Initial Administrator Bootstrap Tool

Interactive CLI script for securely initializing the initial administrator account.
Uses getpass to prevent sensitive passwords from appearing in shell histories, process tables, or logs.
Strictly relies on the legacy ADMIN_TOKEN and local endpoint protection.
"""

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path
import urllib.error
import urllib.request

# Ensure backend root is on sys.path to load application settings if available
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from app.config import settings
    CONFIG_LOADED = True
except Exception:
    settings = None
    CONFIG_LOADED = False

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Securely bootstrap the initial MyYaris system administrator.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("BACKEND_URL", "http://127.0.0.1:8000"),
        help="Backend base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--admin-token",
        default=None,
        help="Legacy ADMIN_TOKEN (if omitted, automatically read from backend settings or prompted securely)",
    )
    return parser.parse_args()


def get_admin_token(cli_token: str | None) -> str:
    if cli_token and cli_token.strip():
        return cli_token.strip()

    # Attempt to load from backend configuration
    if CONFIG_LOADED and settings and getattr(settings, "ADMIN_TOKEN", None):
        cfg_token = settings.ADMIN_TOKEN.strip()
        if cfg_token:
            return cfg_token

    # Environment variable fallback
    env_token = os.getenv("ADMIN_TOKEN", "").strip()
    if env_token:
        return env_token

    # Prompt securely via getpass
    print("未在配置或环境变量中检测到 ADMIN_TOKEN。")
    token_input = getpass.getpass("请输入 legacy ADMIN_TOKEN 进行鉴权: ").strip()
    if not token_input:
        print("❌ 错误: ADMIN_TOKEN 不能为空。")
        sys.exit(1)
    return token_input


def prompt_credentials() -> tuple[str, str, str]:
    print("=" * 60)
    print("MyYaris 初始管理员初始化向导 (Interactive Admin Bootstrap)")
    print("=" * 60)

    # 1. Username
    while True:
        raw_user = input("请输入初始管理员用户名 [默认: myyaris-user]: ").strip()
        username = raw_user if raw_user else "myyaris-user"
        if USERNAME_PATTERN.match(username):
            break
        print("❌ 用户名格式不合法: 必须为 3-32 位字母、数字、下划线或连字符。请重新输入。")

    # 2. Nickname
    raw_nick = input(f"请输入管理员昵称 [默认: {username}]: ").strip()
    nickname = raw_nick if raw_nick else username

    # 3. Password
    while True:
        pwd1 = getpass.getpass("请输入管理员密码 (至少 8 位): ")
        if len(pwd1) < 8 or len(pwd1) > 128:
            print("❌ 密码长度必须在 8 到 128 个字符之间，请重新输入。")
            continue

        pwd2 = getpass.getpass("请再次输入以确认密码: ")
        if pwd1 != pwd2:
            print("❌ 两次输入的密码不一致，请重新输入。")
            continue

        password = pwd1
        break

    return username, nickname, password


def main():
    args = parse_args()
    base_url = args.url.rstrip("/")
    bootstrap_url = f"{base_url}/api/v1/auth/bootstrap-admin"

    # Pre-flight check config if loaded
    if CONFIG_LOADED and settings and not getattr(settings, "ENABLE_BOOTSTRAP_ADMIN", True):
        print("❌ 错误: 生产服务端已关闭 ENABLE_BOOTSTRAP_ADMIN 开关，禁止初始化管理员。")
        sys.exit(1)

    admin_token = get_admin_token(args.admin_token)
    username, nickname, password = prompt_credentials()

    payload = {
        "username": username,
        "password": password,
        "nickname": nickname,
    }
    data_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        bootstrap_url,
        data=data_bytes,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {admin_token}",
        },
        method="POST",
    )

    print("\n正在向后端发送初始化请求...")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode("utf-8")
            result = json.loads(resp_body) if resp_body else {}

            print("\n" + "=" * 60)
            print("🎉 初始管理员创建成功！")
            print("=" * 60)
            print(f"用户 ID   : {result.get('id', 'N/A')}")
            print(f"用户名    : {result.get('username', username)}")
            print(f"用户昵称  : {result.get('nickname', nickname)}")
            print(f"角色权限  : {result.get('role', 'admin')}")
            print(f"账号状态  : {'已激活' if result.get('is_active') else '未知'}")
            print(f"创建时间  : {result.get('created_at', 'N/A')}")
            print("=" * 60)
            print("下一步提示:")
            print("  1. iOS 客户端现在可以使用该账号密码直接登录。")
            print("  2. 管理员可使用 iOS App 或 API 管理后续注册用户与设备绑定权限。")
            print("=" * 60)

    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        try:
            err_json = json.loads(err_body)
            detail = err_json.get("detail", err_body)
        except Exception:
            detail = err_body

        print(f"\n❌ 初始化失败 (HTTP {e.code}): {detail}")
        if e.code == 401:
            print("提示: ADMIN_TOKEN 鉴权失败，请确认后端配置的 ADMIN_TOKEN 是否一致。")
        elif e.code == 403:
            print("提示: 系统已存在管理员账号或 BOOTSTRAP 开关被禁用，禁止重复初始化。")
        sys.exit(1)

    except urllib.error.URLError as e:
        print(f"\n❌ 连接失败: 无法连接至 {bootstrap_url} ({e.reason})")
        print("提示: 请确认后端服务 (myyaris.service) 正在运行并监听对应端口。")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 未知异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
