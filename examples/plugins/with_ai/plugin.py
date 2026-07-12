"""受控 AI facade 示例模块。"""

from __future__ import annotations

from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register

DEFAULT_PROVIDER_TAG = "chat"


async def ai_providers_handler(
    client: Any,
    event: Any,
    args: list[str],
    account_id: int,
    ctx: PluginContext,
) -> None:
    """Show desensitized provider metadata exposed by ctx.ai."""

    if ctx.ai is None:
        await event.edit("ctx.ai 未初始化：请确认 manifest 声明 ai_text 权限。")
        return

    providers = await ctx.ai.list_providers()
    if not providers:
        await event.edit("当前没有可用的 AI provider。")
        return

    lines = [
        f"- {provider.name} tags={','.join(provider.tags or []) or '-'}"
        for provider in providers[:5]
    ]
    await event.edit("可见 AI provider（已脱敏）：\n" + "\n".join(lines))


async def ai_complete_handler(
    client: Any,
    event: Any,
    args: list[str],
    account_id: int,
    ctx: PluginContext,
) -> None:
    """Call ctx.ai.complete with an optional provider_tag and edit the command."""

    if ctx.ai is None:
        await event.edit("ctx.ai 未初始化：请确认 manifest 声明 ai_text 权限。")
        return

    prompt = " ".join(args).strip() or "用一句话介绍 TelePilot 插件。"
    provider_tag = str(ctx.config.get("provider_tag") or DEFAULT_PROVIDER_TAG).strip() or None

    # route 可选：fixed / tag / auto。留空时按传入参数推断，保持旧行为兼容。
    # 这里显式声明 tag 路由；平台在带该 tag 的可用 provider 中挑成本优先项。
    result = await ctx.ai.complete(
        system="你是 TelePilot 第三方模块里的简洁助手。",
        user=prompt,
        route="tag" if provider_tag else "auto",
        provider_tag=provider_tag,
        max_tokens=160,
        timeout_seconds=20,
    )
    # result.routing 是脱敏路由摘要（mode/provider/model/tag/protocol/identity/fallback），
    # 不含 api_key / base_url / 代理，可安全展示或记录。
    await event.edit(result.text.strip()[:800] or "AI 返回了空内容。")


@register
class WithAIPlugin(Plugin):
    key = "with_ai"
    display_name = "AI 示例"
    commands = {
        "ai_providers": ai_providers_handler,
        "ai_complete": ai_complete_handler,
    }
    owner_only = True


__all__ = ["WithAIPlugin", "ai_complete_handler", "ai_providers_handler"]
