"""LLM usage 持久化回调。"""

from __future__ import annotations

import json
import logging

from ..db.base import AsyncSessionLocal
from ..db.models.llm_usage import LLMUsage
from .llm_runtime import UsageRecord, register_usage_callback

log = logging.getLogger(__name__)

_registered = False


async def _persist_usage(record: UsageRecord) -> None:
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                LLMUsage(
                    account_id=record.account_id,
                    triggered_by_account_id=record.triggered_by_account_id,
                    provider_id=record.provider_id,
                    provider_name=record.provider_name,
                    model=record.model,
                    client_identity_profile=record.client_identity_profile,
                    execution_backend=record.execution_backend,
                    gateway_version=record.gateway_version,
                    gateway_request_id=record.gateway_request_id,
                    gateway_stage=record.gateway_stage,
                    source=record.source,
                    input_tokens=int(record.input_tokens or 0),
                    output_tokens=int(record.output_tokens or 0),
                    latency_ms=int(record.latency_ms or 0),
                    success=bool(record.success),
                    error_type=record.error_type,
                    used_fallback=bool(record.used_fallback),
                    fallback_chain=json.dumps(record.fallback_chain or [], ensure_ascii=False),
                    request_preview=record.request_preview,
                    response_preview=record.response_preview,
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        log.debug("LLM usage 持久化失败", exc_info=True)


def ensure_llm_usage_callback_registered() -> None:
    global _registered
    if _registered:
        return
    register_usage_callback(_persist_usage)
    _registered = True
