"""Inbound webhook API.

Token storage uses ``SystemSetting`` rows keyed by ``account_webhooks:{account_id}``.
The setting stores the account-level shared token and the configured hook keys.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..db.models.account import Account
from ..db.models.system import SystemSetting
from ..deps import CurrentUser, DBSession
from ..redis_client import get_redis
from ..services import rate_limit_service
from ..settings import settings
from ..worker.ipc import CMD_WEBHOOK_DELIVER, publish_cmd_with_ack
from ..worker.ratelimit.buckets import TokenBuckets

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

SETTING_PREFIX = "account_webhooks:"
TOKEN_HEADER = "X-TelePilot-Webhook-Token"
WEBHOOK_RATE_LIMIT_ACTION = "webhook_deliver"
MAX_WEBHOOK_BODY_BYTES = 64 * 1024
DEFAULT_HOOK_KEY = "default"
HOOK_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HEADER_VALUE_MAX_CHARS = 512
HEADER_ALLOWLIST = {
    "content-type",
    "user-agent",
    "x-request-id",
    "x-correlation-id",
    "x-github-event",
    "x-gitlab-event",
    "x-hub-signature",
    "x-hub-signature-256",
    "x-signature",
    "x-telegram-bot-api-secret-token",
}
DEFAULT_LIMITS = {
    "per_second": 2,
    "per_minute": 60,
    "per_hour": 1000,
    "per_day": 5000,
    "same_peer_per_minute": None,
}


class WebhookHookOut(BaseModel):
    key: str
    label: str
    enabled: bool = True


class WebhookRateLimitOut(BaseModel):
    action: str = WEBHOOK_RATE_LIMIT_ACTION
    per_second: int | None = None
    per_minute: int | None = None
    per_hour: int | None = None
    per_day: int | None = None


class AccountWebhookConfigOut(BaseModel):
    account_id: int
    token: str
    token_header: str = TOKEN_HEADER
    token_storage: str
    hooks: list[WebhookHookOut]
    max_body_bytes: int = MAX_WEBHOOK_BODY_BYTES
    rate_limit: WebhookRateLimitOut


class WebhookDeliverOut(BaseModel):
    ok: bool = True
    account_id: int
    hook_key: str
    delivered: bool
    body_size: int


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _setting_key(account_id: int) -> str:
    return f"{SETTING_PREFIX}{account_id}"


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _default_config() -> dict[str, Any]:
    now = _now_iso()
    return {
        "token": _new_token(),
        "hooks": [{"key": DEFAULT_HOOK_KEY, "label": "默认入口", "enabled": True}],
        "created_at": now,
        "updated_at": now,
    }


def _normalize_hook(raw: Any) -> dict[str, Any] | None:
    data = raw if isinstance(raw, dict) else {"key": raw}
    key = str(data.get("key") or "").strip()
    if not HOOK_KEY_RE.fullmatch(key):
        return None
    label = str(data.get("label") or key).strip() or key
    return {
        "key": key,
        "label": label[:64],
        "enabled": bool(data.get("enabled", True)),
    }


def _normalize_config(value: Any) -> tuple[dict[str, Any], bool]:
    changed = False
    data = dict(value) if isinstance(value, dict) else {}
    token = str(data.get("token") or "").strip()
    if not token:
        data["token"] = _new_token()
        changed = True
    raw_hooks = data.get("hooks")
    hooks = [_normalize_hook(item) for item in raw_hooks] if isinstance(raw_hooks, list) else []
    hooks = [item for item in hooks if item is not None]
    if not hooks:
        hooks = [{"key": DEFAULT_HOOK_KEY, "label": "默认入口", "enabled": True}]
        changed = True
    if hooks != data.get("hooks"):
        data["hooks"] = hooks
        changed = True
    if not data.get("created_at"):
        data["created_at"] = _now_iso()
        changed = True
    if changed or not data.get("updated_at"):
        data["updated_at"] = _now_iso()
    return data, changed


async def _ensure_account(db: Any, account_id: int) -> Account:
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ACCOUNT_NOT_FOUND", "message": "账号不存在"},
        )
    return account


async def _get_or_create_config(db: Any, account_id: int) -> dict[str, Any]:
    key = _setting_key(account_id)
    row = await db.get(SystemSetting, key)
    if row is None:
        config = _default_config()
        db.add(SystemSetting(key=key, value=config))
        await db.commit()
        return config
    config, changed = _normalize_config(row.value)
    if changed:
        row.value = config
        await db.commit()
    return config


def _hook_for(config: dict[str, Any], hook_key: str) -> dict[str, Any] | None:
    normalized = str(hook_key or "").strip()
    if not HOOK_KEY_RE.fullmatch(normalized):
        return None
    for hook in config.get("hooks") or []:
        if isinstance(hook, dict) and hook.get("key") == normalized and bool(hook.get("enabled", True)):
            return hook
    return None


def _config_out(account_id: int, config: dict[str, Any], limit: dict[str, Any]) -> AccountWebhookConfigOut:
    hooks = [
        WebhookHookOut(**hook)
        for hook in config.get("hooks", [])
        if isinstance(hook, dict) and hook.get("key")
    ]
    return AccountWebhookConfigOut(
        account_id=account_id,
        token=str(config.get("token") or ""),
        token_storage=f"system_setting:{_setting_key(account_id)}",
        hooks=hooks,
        rate_limit=WebhookRateLimitOut(**limit),
    )


def _limit_from_effective(effective: Any) -> dict[str, Any]:
    values = {
        "per_second": int(getattr(effective, "per_second", 0) or 0),
        "per_minute": int(getattr(effective, "per_minute", 0) or 0),
        "per_hour": int(getattr(effective, "per_hour", 0) or 0),
        "per_day": int(getattr(effective, "per_day", 0) or 0),
        "same_peer_per_minute": getattr(effective, "same_peer_per_minute", None),
    }
    if not any(values[key] for key in ("per_second", "per_minute", "per_hour", "per_day")):
        values.update(DEFAULT_LIMITS)
    return values


async def _effective_limit_dict(db: Any, account_id: int) -> dict[str, Any]:
    effective = await rate_limit_service.get_effective(db, account_id, WEBHOOK_RATE_LIMIT_ACTION)
    return _limit_from_effective(effective)


def _bucket_action(hook_key: str) -> str:
    return f"{WEBHOOK_RATE_LIMIT_ACTION}:{hook_key}"


async def _enforce_webhook_rate_limit(db: Any, redis: Any, account_id: int, hook_key: str) -> dict[str, Any]:
    effective = await rate_limit_service.get_effective(db, account_id, WEBHOOK_RATE_LIMIT_ACTION)
    if bool(getattr(effective, "disabled", False)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "WEBHOOK_RATE_LIMITED", "message": "该账号 webhook 投递已被风控禁用"},
            headers={"Retry-After": "60"},
        )
    limits = _limit_from_effective(effective)
    allowed, retry_after, _idx = await TokenBuckets(redis).check_and_consume(
        account_id,
        _bucket_action(hook_key),
        limits["per_second"],
        limits["per_minute"],
        limits["per_hour"],
        limits["per_day"],
        limits["same_peer_per_minute"],
        peer_id=None,
        consume=True,
    )
    if not allowed:
        retry = max(1, int(math.ceil(float(retry_after or 1))))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "WEBHOOK_RATE_LIMITED", "message": "Webhook 投递过于频繁，请稍后重试"},
            headers={"Retry-After": str(retry)},
        )
    return limits


async def _read_body(request: Request) -> tuple[Any, int]:
    length_header = request.headers.get("content-length")
    if length_header:
        try:
            if int(length_header) > MAX_WEBHOOK_BODY_BYTES:
                raise _body_too_large()
        except ValueError:
            pass
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BODY_BYTES:
        raise _body_too_large()
    content_type = request.headers.get("content-type", "")
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type.lower() and text:
        try:
            return json.loads(text), len(raw)
        except json.JSONDecodeError:
            return text, len(raw)
    return text, len(raw)


def _body_too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail={
            "code": "WEBHOOK_BODY_TOO_LARGE",
            "message": f"Webhook body 超出 {MAX_WEBHOOK_BODY_BYTES} bytes 上限",
        },
    )


def _whitelisted_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in request.headers.items():
        normalized = key.lower()
        if normalized in HEADER_ALLOWLIST:
            out[normalized] = str(value)[:HEADER_VALUE_MAX_CHARS]
    return out


def _provided_token(header_token: str | None, query_token: str | None) -> str:
    if header_token:
        return str(header_token).strip()
    if settings.webhook_allow_query_token:
        return str(query_token or "").strip()
    return ""


def _require_valid_token(config: dict[str, Any], provided: str) -> None:
    expected = str(config.get("token") or "").strip()
    if not provided or not expected or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "WEBHOOK_TOKEN_INVALID", "message": "Webhook token 无效"},
        )


@router.get("", response_model=dict[str, str])
async def webhooks_status(_user: CurrentUser) -> dict[str, str]:
    return {"status": "ok"}


@router.get("/{account_id}", response_model=AccountWebhookConfigOut)
async def get_account_webhook_config(
    account_id: int,
    db: DBSession,
    _user: CurrentUser,
) -> AccountWebhookConfigOut:
    await _ensure_account(db, account_id)
    config = await _get_or_create_config(db, account_id)
    limits = await _effective_limit_dict(db, account_id)
    return _config_out(account_id, config, limits)


@router.post("/{account_id}/token/reset", response_model=AccountWebhookConfigOut)
async def reset_account_webhook_token(
    account_id: int,
    db: DBSession,
    _user: CurrentUser,
) -> AccountWebhookConfigOut:
    await _ensure_account(db, account_id)
    config = await _get_or_create_config(db, account_id)
    config["token"] = _new_token()
    config["updated_at"] = _now_iso()
    row = await db.get(SystemSetting, _setting_key(account_id))
    if row is None:
        db.add(SystemSetting(key=_setting_key(account_id), value=config))
    else:
        row.value = config
    await db.commit()
    limits = await _effective_limit_dict(db, account_id)
    return _config_out(account_id, config, limits)


@router.post("/{account_id}/{hook_key}", response_model=WebhookDeliverOut, status_code=status.HTTP_202_ACCEPTED)
async def deliver_webhook(
    account_id: int,
    hook_key: str,
    request: Request,
    db: DBSession,
    x_telepilot_webhook_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    token: str | None = Query(default=None),
) -> WebhookDeliverOut:
    account = await db.get(Account, account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "WEBHOOK_TOKEN_INVALID", "message": "Webhook token 无效"},
        )
    config = await _get_or_create_config(db, account_id)
    _require_valid_token(config, _provided_token(x_telepilot_webhook_token, token))
    hook = _hook_for(config, hook_key)
    if hook is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "WEBHOOK_HOOK_NOT_FOUND", "message": "Webhook hook_key 不存在或已停用"},
        )

    redis = get_redis()
    await _enforce_webhook_rate_limit(db, redis, account_id, hook_key)
    body, body_size = await _read_body(request)
    payload = {
        "hook_key": hook["key"],
        "body": body,
        "headers": _whitelisted_headers(request),
        "body_size": body_size,
        "content_type": request.headers.get("content-type"),
        "received_at": _now_iso(),
    }
    try:
        delivered = await publish_cmd_with_ack(
            redis,
            account_id,
            CMD_WEBHOOK_DELIVER,
            timeout=5.0,
            **payload,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "WEBHOOK_DELIVERY_FAILED", "message": f"Webhook 投递失败：{type(exc).__name__}"},
        ) from exc
    if not delivered:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "WEBHOOK_WORKER_OFFLINE", "message": "账号 worker 未在线或未确认 webhook 投递"},
        )
    return WebhookDeliverOut(
        account_id=account_id,
        hook_key=hook["key"],
        delivered=True,
        body_size=body_size,
    )
