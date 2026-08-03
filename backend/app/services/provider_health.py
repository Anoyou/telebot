"""Provider+model 运行时健康（进程内存真相源，Redis 可选镜像）。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)

_MAX_COOLDOWN_SECONDS = 600  # 10 分钟封顶
_BASE_COOLDOWN_SECONDS = 15
_REDIS_KEY = "telepilot:provider_health:v1"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    COOLING = "cooling"
    UNCERTAIN = "uncertain"  # Redis 不可用时的呈现，不阻断请求


class ErrorClass(StrEnum):
    TRANSIENT = "transient"  # 超时/连接/5xx → 计入冷却
    RATE_LIMIT = "rate_limit"  # 429 → 短冷却
    CREDENTIAL = "credential"  # 401/403 → 不计冷却
    CAPABILITY = "capability"  # 能力不支持/参数错误 → 不算健康故障
    OTHER = "other"


@dataclass
class HealthRecord:
    provider_id: int
    model: str
    consecutive_failures: int = 0
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_error_class: str | None = None
    last_error_message: str | None = None
    cooldown_until: float | None = None

    def to_public(self, *, now: float | None = None) -> dict[str, Any]:
        ts = now if now is not None else time.time()
        cooling = self.cooldown_until is not None and self.cooldown_until > ts
        state = HealthState.COOLING.value if cooling else HealthState.HEALTHY.value
        remaining = max(0, int((self.cooldown_until or 0) - ts)) if cooling else 0
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "state": state,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error_class": self.last_error_class,
            "last_error_message": self.last_error_message,
            "cooldown_until": self.cooldown_until,
            "cooldown_remaining_seconds": remaining,
        }


_records: dict[str, HealthRecord] = {}


def _key(provider_id: int, model: str) -> str:
    return f"{int(provider_id)}::{str(model or '').strip()}"


def classify_error(exc: BaseException | str | None) -> ErrorClass:
    text = str(exc or "").lower()
    category = str(getattr(exc, "category", "") or text).lower()
    scope = str(getattr(exc, "scope", "") or "").lower()
    code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    try:
        code_i = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_i = None
    if category in {
        "auth_failed",
        "permission_denied",
        "client_rejected",
        "official_account_required",
        "account_policy",
        "quota_exhausted",
    } or code_i in {401, 403} or "unauthorized" in text or "forbidden" in text:
        return ErrorClass.CREDENTIAL
    if category == "rate_limited" or code_i == 429 or "rate limit" in text or "too many requests" in text:
        return ErrorClass.RATE_LIMIT
    # 能力不支持 / 参数错误：记录但不算健康故障
    if category in {"request_invalid", "context_limit", "model_missing", "endpoint_missing"} or scope in {"capability_mismatch", "request_invalid"} or any(
        token in text
        for token in (
            "capability_mismatch",
            "request_invalid",
            "unsupported",
            "not support",
            "does not support",
            "invalid parameter",
            "invalid_request",
            "unknown parameter",
            "tools are not supported",
            "function calling",
            "missing required",
        )
    ):
        return ErrorClass.CAPABILITY
    if category in {"timeout", "network_error", "gateway_unavailable", "gateway_overloaded", "upstream_error"} or (code_i is not None and code_i >= 500):
        return ErrorClass.TRANSIENT
    if any(
        token in text
        for token in (
            "timeout",
            "timed out",
            "connection",
            "connect",
            "temporarily unavailable",
            "network",
            "reset by peer",
        )
    ):
        return ErrorClass.TRANSIENT
    return ErrorClass.OTHER


def _cooldown_seconds(failures: int, error_class: ErrorClass) -> int:
    if error_class == ErrorClass.RATE_LIMIT:
        return min(_MAX_COOLDOWN_SECONDS, 30)
    if error_class in {ErrorClass.CREDENTIAL, ErrorClass.CAPABILITY}:
        return 0
    # 指数退避：15 * 2^(n-1)，封顶 600
    exp = max(0, failures - 1)
    return min(_MAX_COOLDOWN_SECONDS, _BASE_COOLDOWN_SECONDS * (2**exp))


def _skip_health_source(source: str | None) -> bool:
    """测活/诊断/记忆压缩等不得写入生产健康。"""

    if not source:
        return False
    text = str(source)
    if text.startswith("liveness") or text.startswith("diagnostic:"):
        return True
    if text in {"system_agent_memory"}:
        return True
    return False


def record_success(provider_id: int, model: str, *, source: str | None = None) -> None:
    if _skip_health_source(source):
        return
    key = _key(provider_id, model)
    rec = _records.get(key) or HealthRecord(provider_id=int(provider_id), model=str(model or ""))
    rec.consecutive_failures = 0
    rec.last_success_at = time.time()
    rec.cooldown_until = None
    rec.last_error_class = None
    rec.last_error_message = None
    _records[key] = rec
    _mirror_to_redis(key, rec)


def record_failure(
    provider_id: int,
    model: str,
    exc: BaseException | str | None,
    *,
    source: str | None = None,
) -> None:
    if _skip_health_source(source):
        return
    error_class = classify_error(exc)
    key = _key(provider_id, model)
    rec = _records.get(key) or HealthRecord(provider_id=int(provider_id), model=str(model or ""))
    now = time.time()
    rec.last_failure_at = now
    rec.last_error_class = error_class.value
    rec.last_error_message = str(exc or "")[:300]
    if error_class == ErrorClass.CAPABILITY:
        # 能力/参数问题：不算健康故障，不累计冷却
        rec.last_error_class = error_class.value
        rec.last_error_message = str(exc or "")[:300]
        _records[key] = rec
        _mirror_to_redis(key, rec)
        return
    if error_class == ErrorClass.CREDENTIAL:
        # 凭据问题：标记但不进冷却
        rec.consecutive_failures = rec.consecutive_failures + 1
        rec.cooldown_until = None
    else:
        rec.consecutive_failures = rec.consecutive_failures + 1
        cool = _cooldown_seconds(rec.consecutive_failures, error_class)
        rec.cooldown_until = now + cool if cool > 0 else None
    _records[key] = rec
    _mirror_to_redis(key, rec)


def get_health(provider_id: int, model: str) -> dict[str, Any]:
    key = _key(provider_id, model)
    rec = _records.get(key)
    if rec is None:
        return {
            "provider_id": int(provider_id),
            "model": str(model or ""),
            "state": HealthState.HEALTHY.value,
            "consecutive_failures": 0,
            "cooldown_remaining_seconds": 0,
        }
    return rec.to_public()


def list_health() -> list[dict[str, Any]]:
    now = time.time()
    return [rec.to_public(now=now) for rec in _records.values()]


def sort_provider_candidates(
    candidates: list[tuple[int, str]],
) -> list[tuple[int, str]]:
    """cooling 排后但不摘除；全 cooling 时保持相对顺序。"""

    now = time.time()

    def score(item: tuple[int, str]) -> tuple[int, float]:
        pid, model = item
        rec = _records.get(_key(pid, model))
        if rec is None or rec.cooldown_until is None or rec.cooldown_until <= now:
            return (0, 0.0)
        return (1, rec.cooldown_until)

    return sorted(candidates, key=score)


def _mirror_to_redis(key: str, rec: HealthRecord) -> None:
    # 进程内存为真相源；Redis 镜像仅在同步客户端可用时写入。
    # 异步 Redis 不在同步路径 await，避免未 await 协程告警。
    try:
        from ..redis_client import get_redis

        r = get_redis()
        if r is None:
            return
        # 当前项目 redis 客户端为 async，跳过同步镜像
        # 当前项目 redis 为 async 客户端：同步路径无法 await，直接跳过
        if hasattr(r, "execute_command") or callable(getattr(r, "hset", None)):
            return
    except Exception:  # noqa: BLE001
        log.debug("provider health redis mirror skipped", exc_info=True)


def reset_for_tests() -> None:
    _records.clear()


__all__ = [
    "ErrorClass",
    "HealthState",
    "classify_error",
    "get_health",
    "list_health",
    "record_failure",
    "record_success",
    "reset_for_tests",
    "sort_provider_candidates",
]
