"""FastAPI 通用依赖：DB、当前登录用户、操作日志写入。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models.user import WebUser
from .db.session import get_db


def _decode_token_claims(token: str) -> dict | None:
    """惰性 import 避免循环引用。返回 JWT payload 或 None。"""
    from .services.auth_service import decode_jwt_claims

    return decode_jwt_claims(token)


def _auth_err(code: str, message: str) -> HTTPException:
    """统一认证错误结构，便于前端稳定解析 error.code。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
    )


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_token: Annotated[str | None, Cookie(alias="auth_token")] = None,
) -> WebUser:
    """从 HttpOnly cookie 读取 JWT，返回当前 WebUser。"""
    if not auth_token:
        raise _auth_err("AUTH_REQUIRED", "未登录")
    claims = _decode_token_claims(auth_token)
    if not claims:
        raise _auth_err("AUTH_INVALID", "登录已过期，请重新登录")
    try:
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        raise _auth_err("AUTH_INVALID", "登录已过期，请重新登录") from None
    user = (await db.execute(select(WebUser).where(WebUser.id == user_id))).scalar_one_or_none()
    if not user:
        raise _auth_err("AUTH_INVALID", "用户不存在或已被删除")
    token_pwd_v = int(claims.get("pwd_v", 0))
    user_pwd_v = int(getattr(user, "pwd_version", 0) or 0)
    if token_pwd_v != user_pwd_v:
        raise _auth_err("AUTH_INVALIDATED", "登录状态已失效，请重新登录")
    # 鉴权查询不需要跨越整个请求持有事务。System Agent 等服务会使用独立
    # session；小连接池下若多个请求同时保留这里的连接，会造成嵌套取连接饥饿。
    await db.commit()
    return user


CurrentUser = Annotated[WebUser, Depends(get_current_user)]
DBSession = Annotated[AsyncSession, Depends(get_db)]
