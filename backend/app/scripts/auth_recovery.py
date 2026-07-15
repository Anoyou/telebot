"""生成 Web 登录一次性恢复码。

用法：

    python -m app.scripts.auth_recovery
    python -m app.scripts.auth_recovery --username admin --ttl 900

恢复码只在密码正确后用于绕过通知 Bot OTP / TOTP 这层二次校验，不能绕过密码。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC

from app.db.base import AsyncSessionLocal
from app.services import auth_login_security


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 TelePilot Web 登录一次性恢复码")
    parser.add_argument("--username", default=None, help="指定 Web 管理员用户名；默认使用最早创建的用户")
    parser.add_argument("--ttl", type=int, default=None, help="有效期秒数；不填则使用 Web 登录安全设置")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        user = await auth_login_security.select_recovery_user(db, args.username)
        if user is None:
            print("未找到 Web 管理员用户，无法生成恢复码。")
            return 1
        config = await auth_login_security.get_login_security_config(db)
        ttl = int(args.ttl or config.recovery_code_ttl_seconds)
        code, expires_at = await auth_login_security.create_recovery_code(
            db,
            user=user,
            ttl_seconds=max(60, ttl),
        )
        await db.commit()

    expires_text = expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("TelePilot Web 登录一次性恢复码：")
    print(code)
    print(f"用户：{user.username}")
    print(f"过期时间：{expires_text}")
    print("提示：登录时仍需输入正确密码；恢复码只能成功使用一次。")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
