"""会话摘要后台 LLM 压缩（主链路不阻塞）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...db.base import AsyncSessionLocal
from ...db.models.system_agent import SystemAgentSession
from ..llm_invoke import invoke_structured
from ..llm_protocol import MessageRole, ModelMessage, ModelRequest
from .config import load_config, resolve_agent_providers
from .memory import should_compress_summary, trim_summary_to_limit
from .secrets import redact_known_secrets

log = logging.getLogger(__name__)

_COMPRESS_SYSTEM = (
    "你是 TelePilot 会话记忆压缩器。将下列「会话摘要」改写为状态式描述："
    "进行中任务、对象 ID、已确认偏好、结论。"
    "禁止复述密钥、Token、密码或原文长段。"
    "只输出压缩后的中文摘要正文，不要标题或解释。"
)
_COMPRESS_MAX_CHARS = 2_000


def schedule_summary_compression(session_id: int, *, summary_rev: int) -> None:
    """投递后台压缩；service 层例外允许 create_task + 独立 session。"""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("no running loop; skip summary compression for session %s", session_id)
        return
    loop.create_task(
        _compress_session_summary(session_id, expected_rev=summary_rev),
        name=f"system-agent-memory-compress-{session_id}",
    )


async def _compress_session_summary(session_id: int, *, expected_rev: int) -> None:
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemAgentSession, session_id)
            if row is None:
                return
            state = dict(row.memory_state) if isinstance(row.memory_state, dict) else {}
            current_rev = int(state.get("summary_rev") or 0)
            if current_rev != expected_rev:
                log.info(
                    "system_agent memory compress skip session=%s rev_conflict expected=%s got=%s",
                    session_id,
                    expected_rev,
                    current_rev,
                )
                return
            summary = str(row.memory_summary or "").strip()
            if not should_compress_summary(summary):
                return
            before_len = len(summary)

            cfg = await load_config(db)
            if not cfg.get("enabled") or not cfg.get("provider_id"):
                return
            try:
                resolved = await resolve_agent_providers(db, cfg)
            except Exception as exc:  # noqa: BLE001
                log.info("system_agent memory compress provider resolve failed: %s", exc)
                return

            request = ModelRequest(
                model=resolved.model,
                messages=(
                    ModelMessage.text(MessageRole.SYSTEM, _COMPRESS_SYSTEM),
                    ModelMessage.text(MessageRole.USER, f"会话摘要：\n{summary[:8000]}"),
                ),
                max_output_tokens=400,
                temperature=0.2,
            )
            try:
                async with asyncio.timeout(8.0):
                    response, _used, _fb = await invoke_structured(
                        resolved.primary,
                        resolved.providers,
                        request,
                        source="system_agent_memory",
                    )
            except Exception as exc:  # noqa: BLE001
                log.info("system_agent memory compress failed session=%s: %s", session_id, exc)
                return

            compressed = redact_known_secrets(str(response.text or "").strip())
            compressed = trim_summary_to_limit(compressed, _COMPRESS_MAX_CHARS)
            if not compressed:
                return

            # 写回前再读一遍校验 rev
            await db.refresh(row)
            state2 = dict(row.memory_state) if isinstance(row.memory_state, dict) else {}
            if int(state2.get("summary_rev") or 0) != expected_rev:
                log.info(
                    "system_agent memory compress abandon session=%s rev changed before write",
                    session_id,
                )
                return
            row.memory_summary = compressed
            await db.commit()
            log.info(
                "system_agent memory compress ok session=%s before=%s after=%s",
                session_id,
                before_len,
                len(compressed),
            )
    except Exception as exc:  # noqa: BLE001
        log.info("system_agent memory compress error session=%s: %s", session_id, exc)


async def compress_summary_text_for_tests(
    summary: str,
    *,
    model_call: Any = None,
) -> str:
    """测试钩子：同步路径压缩文本（不访问 DB）。"""

    text = str(summary or "").strip()
    if not text:
        return ""
    compressed = redact_known_secrets(text)
    return trim_summary_to_limit(compressed, _COMPRESS_MAX_CHARS)


__all__ = [
    "schedule_summary_compression",
    "should_compress_summary",
]
