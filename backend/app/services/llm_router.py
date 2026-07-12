"""LLM 自动路由：按用户消息特征选最合适的 provider。

设计目标
========

让一条 ``,ai`` 命令支持两种工作模式：

- ``fixed``  固定 provider —— 老行为，绑死某个 provider_id
- ``auto``   自动路由 —— 看消息内容自动挑 provider

路由策略（优先级从高到低）
--------------------------

1. **视觉/多模态**：消息含图（被引用消息有 photo）或关键词（识别图/这张图/截图） → 选 modality∈{vision,multimodal} 的 provider
2. **代码**：消息含 ```...``` 代码块 / def / function / class / import 等关键 token → 选 tag=code
3. **数学**：消息含 ``=``、``\\frac``、连续数字与运算符高密度 → 选 tag=math
4. **翻译**：消息含 "翻译为/translate (to|into)/翻成" 等关键词 → 选 tag=translate
5. **长上下文**：原文 + 问题字符数 ≥ 阈值（默认 1500 chars）→ 选 tag=long_context（按 cost_tier 升序兜底）
6. **复杂推理/分析**：包含"为什么/分析/比较/推导/原因/对比"等推理 trigger → 选 tag∈{reason,smart}（旗舰）
7. **闲聊/通用**：以上都不命中 → 选 tag=chat 中 cost_tier 最低（最便宜的量产档）

如果以上规则都没命中候选（标签未配齐），可选启用「分类器兜底」：
调用 ``classifier_provider`` 让一个轻量小模型返回 enum，然后再按 enum 走 tag 匹配。
分类失败 / 没配 classifier → 用 ``fallback_provider_id``；再没有 → 用候选里第一个。

为什么把规则做成"全靠 tag 匹配"而不是硬编码 provider_id？
-- 用户在前端给 provider 打标签即可改动路由，不用改代码。

无副作用
--------
路由器是纯函数式（除非启用 classifier，那时只调一次 LLM 完成短文本分类）。
不读 DB、不写日志（决策原因通过 ``RoutingDecision.reason`` 返回，由调用方决定记不记）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ── 触发关键词（Unicode 字符串匹配；case-insensitive）─────────
# 设计：保守命中——只要消息里能找到任意一个关键词就视为命中类别。
# 避免误命中：每个类别选 ≤ 12 个高特征 token。
_VISION_KEYWORDS = (
    "识别图", "这张图", "看图", "图片里", "截图", "ocr",
    "describe the image", "what's in this image", "what is in this image",
    "图中", "图里",
)

_TRANSLATE_KEYWORDS = (
    "翻译为", "翻译成", "翻成", "译为", "translate to", "translate into",
    "translate this", "翻译一下", "中译英", "英译中", "japanese to chinese",
)

_REASON_KEYWORDS = (
    "为什么", "分析一下", "推理", "推导", "为何", "原因是", "比较一下",
    "对比", "解释清楚", "step by step", "step-by-step", "reason about",
    "why does", "why is", "explain why",
)

# 代码 token：行内 / 块内任一命中即认为含代码
_CODE_TOKEN_RE = re.compile(
    r"```|"                            # 围栏代码块
    r"\bdef\s+\w+\s*\(|"               # python 函数
    r"\bfunction\s+\w+\s*\(|"          # js 函数
    r"\bclass\s+\w+\s*[:({]|"          # 类
    r"\bimport\s+[a-zA-Z_]|"           # import
    r"#include\s+<|"                   # C/C++
    r"\bconsole\.log\(|"               # js
    r"\bprint\(|"                      # python/js print
    r"=>\s*\{",                        # arrow function body
    re.IGNORECASE,
)

# 数学 trigger：含 latex \frac / \int / 较高密度数字+运算符
_MATH_LATEX_RE = re.compile(r"\\(frac|sum|int|sqrt|times|cdot|forall|exists)\b")
_MATH_DENSITY_RE = re.compile(r"\d+\s*[+\-*/=^×÷]\s*\d+")

# 长上下文阈值（用户问题 + 被回复原文合计 chars）
_LONG_CONTEXT_CHARS = 1500


# ── 模态常量（不直接 import models，避免循环；与 db.models.command 对齐）─
_MOD_TEXT = "text"
_MOD_VISION = "vision"
_MOD_MULTIMODAL = "multimodal"


@dataclass
class RoutingDecision:
    """路由决策结果。

    0.57.0 阶段 D：升级为 Provider + 已启用模型级决策。新增字段全部带默认值，
    旧调用方（只读 ``provider_id`` / ``reason`` / ``matched_tag``）保持兼容。
    """

    provider_id: int
    """选中的 provider id。"""

    reason: str
    """决策原因（短字符串，写日志/审计用）。例：``"matched tag=code"``、``"fallback"``。"""

    matched_tag: str | None = None
    """命中的 tag（若是规则路由）；分类器兜底时也会填这里。"""

    # 阶段 D：模型级路由结果（None 表示交由 provider.default_model 决定）。
    model: str | None = None
    """选中的已启用模型 id；None 时上层回落到 provider.default_model。"""

    api_format: str | None = None
    """该 provider 本次实际协议（用于身份/协议重算）。"""

    client_identity_profile: str | None = None
    """该 provider 配置的客户端身份档案（脱敏摘要用）。"""

    def summary(self) -> dict[str, Any]:
        """脱敏路由摘要（可安全返回给插件 / 前端；不含 key / base_url / 代理）。"""
        return {
            "provider_id": self.provider_id,
            "reason": self.reason,
            "matched_tag": self.matched_tag,
            "model": self.model,
            "api_format": self.api_format,
            "client_identity_profile": self.client_identity_profile,
        }


# ════════════════════════════════════════════════════════════
# 内部：候选过滤 / 评分
# ════════════════════════════════════════════════════════════


def _has_api_key(p: dict[str, Any]) -> bool:
    """provider 是否配了 api_key（除 ollama 本地外，没 key 的 provider 调不通）。"""
    if p.get("provider") == "ollama":
        # ollama 本地部署可不要 key
        return True
    return bool(p.get("api_key_enc"))


def _provider_tags(p: dict[str, Any]) -> set[str]:
    """取 provider.tags，允许字段缺失（老数据兼容）。"""
    raw = p.get("tags") or []
    if not isinstance(raw, list):
        return set()
    return {str(t).strip() for t in raw if isinstance(t, str) and t.strip()}


def _provider_modality(p: dict[str, Any]) -> str:
    return str(p.get("modality") or _MOD_TEXT)


def _cost_tier(p: dict[str, Any]) -> int:
    """取 cost_tier，缺省视为 2（中档）。"""
    v = p.get("cost_tier")
    try:
        return int(v) if v is not None else 2
    except (TypeError, ValueError):
        return 2


def _enabled_models_of(p: dict[str, Any]) -> list[str]:
    """严格返回 provider.models[].enabled == True 的模型 id（顺序保留）。"""
    out: list[str] = []
    seen: set[str] = set()
    for item in p.get("models") or []:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled")):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def _pick_model_for(p: dict[str, Any]) -> str | None:
    """为选中的 provider 选一个已启用模型。

    优先 default_model（若它在已启用集合里或没有显式启用清单），否则取第一个已启用
    模型；都没有则返回 None（上层回落 provider.default_model）。
    """
    enabled = _enabled_models_of(p)
    default_model = str(p.get("default_model") or "").strip()
    if not enabled:
        # 没有显式启用清单：沿用 default_model（None 交由上层回落）。
        return default_model or None
    if default_model and default_model in enabled:
        return default_model
    return enabled[0]


def _finalize_decision(decision: RoutingDecision, providers: dict[int, dict[str, Any]]) -> RoutingDecision:
    """在选定 provider 后补齐模型 / 协议 / 身份字段（阶段 D）。

    每次（含 fallback 切换 provider）都基于该 provider 的最新配置重算，保证
    模型级结果与协议、身份一致。
    """
    p = providers.get(int(decision.provider_id))
    if p is None:
        return decision
    decision.model = _pick_model_for(p)
    decision.api_format = str(p.get("api_format") or "") or None
    decision.client_identity_profile = str(p.get("client_identity_profile") or "auto")
    return decision


def _select_by_tag(
    candidates: list[dict[str, Any]],
    tag: str,
    *,
    prefer_cheap: bool = False,
    prefer_premium: bool = False,
) -> dict[str, Any] | None:
    """从候选中找拥有指定 tag 的 provider；按 cost_tier 排序选一个。

    - ``prefer_cheap=True``    cost_tier 升序（便宜优先；用于 chat / classify / cheap）
    - ``prefer_premium=True``  cost_tier 降序（旗舰优先；用于 reason / smart）
    - 都不指定：cost_tier 升序（默认便宜优先，省钱）
    """
    matched = [p for p in candidates if tag in _provider_tags(p)]
    if not matched:
        return None
    if prefer_premium:
        matched.sort(key=_cost_tier, reverse=True)
    else:
        # cheap / 默认 都是升序
        matched.sort(key=_cost_tier)
    return matched[0]


# ════════════════════════════════════════════════════════════
# 规则层
# ════════════════════════════════════════════════════════════


def _looks_like_vision_request(user_q: str, replied_text: str | None, has_replied_photo: bool) -> bool:
    if has_replied_photo:
        return True
    text = (user_q or "").lower()
    return any(k.lower() in text for k in _VISION_KEYWORDS)


def _looks_like_code(user_q: str, replied_text: str | None) -> bool:
    blob = f"{replied_text or ''}\n{user_q or ''}"
    return bool(_CODE_TOKEN_RE.search(blob))


def _looks_like_math(user_q: str, replied_text: str | None) -> bool:
    blob = f"{replied_text or ''}\n{user_q or ''}"
    if _MATH_LATEX_RE.search(blob):
        return True
    # 数字+运算符模式至少出现 2 次
    return len(_MATH_DENSITY_RE.findall(blob)) >= 2


def _looks_like_translate(user_q: str) -> bool:
    text = (user_q or "").lower()
    return any(k.lower() in text for k in _TRANSLATE_KEYWORDS)


def _looks_long_context(user_q: str, replied_text: str | None) -> bool:
    return len(user_q or "") + len(replied_text or "") >= _LONG_CONTEXT_CHARS


def _looks_like_reason(user_q: str) -> bool:
    text = (user_q or "").lower()
    return any(k.lower() in text for k in _REASON_KEYWORDS)


def _rule_route(
    user_q: str,
    replied_text: str | None,
    has_replied_photo: bool,
    candidates: list[dict[str, Any]],
) -> RoutingDecision | None:
    """走规则层，命中即返；没命中返回 None。"""

    # 1) 视觉：必须有 modality∈{vision,multimodal} 才匹配；没有就跳过此条规则
    if _looks_like_vision_request(user_q, replied_text, has_replied_photo):
        vis = [
            p for p in candidates
            if _provider_modality(p) in (_MOD_VISION, _MOD_MULTIMODAL)
        ]
        if vis:
            # 视觉里也按 cost_tier 升序选便宜的
            vis.sort(key=_cost_tier)
            return RoutingDecision(
                provider_id=int(vis[0]["id"]),
                reason="vision request → modality=vision/multimodal",
                matched_tag="vision",
            )
        # 没视觉模型就不匹配此条；继续看其他规则（文本路径）

    # 2) 代码
    if _looks_like_code(user_q, replied_text):
        p = _select_by_tag(candidates, "code", prefer_cheap=True)
        if p:
            return RoutingDecision(int(p["id"]), "matched tag=code", "code")

    # 3) 数学
    if _looks_like_math(user_q, replied_text):
        p = _select_by_tag(candidates, "math", prefer_cheap=True)
        if p:
            return RoutingDecision(int(p["id"]), "matched tag=math", "math")

    # 4) 翻译
    if _looks_like_translate(user_q):
        p = _select_by_tag(candidates, "translate", prefer_cheap=True)
        if p:
            return RoutingDecision(int(p["id"]), "matched tag=translate", "translate")

    # 5) 长上下文（用 cheap 兜底，不要旗舰浪费 token）
    if _looks_long_context(user_q, replied_text):
        p = _select_by_tag(candidates, "long_context", prefer_cheap=True)
        if p:
            return RoutingDecision(int(p["id"]), "matched tag=long_context", "long_context")

    # 6) 复杂推理 → smart / reason，premium 优先
    if _looks_like_reason(user_q):
        for tag in ("reason", "smart"):
            p = _select_by_tag(candidates, tag, prefer_premium=True)
            if p:
                return RoutingDecision(int(p["id"]), f"matched tag={tag}", tag)

    # 7) 通用闲聊 / 短问短答：chat 中最便宜
    p = _select_by_tag(candidates, "chat", prefer_cheap=True)
    if p:
        return RoutingDecision(int(p["id"]), "matched tag=chat (default short)", "chat")

    return None


# ════════════════════════════════════════════════════════════
# 分类器兜底（可选；调一个 classifier provider 让它返回 enum）
# ════════════════════════════════════════════════════════════


# 让分类器只回这几个 token；任何其它输出按 chat 处理
_CLASSIFIER_LABELS = ("code", "math", "translate", "vision", "reason", "chat")

_CLASSIFIER_SYSTEM = (
    "你是一个消息分类器。读用户消息，只回一个英文小写词标签，不要解释，不要标点。"
    "可选范围严格限定为：code / math / translate / vision / reason / chat。"
    "判断不准时回 chat。"
)


async def _ask_classifier(
    classifier_provider: dict[str, Any],
    user_q: str,
    replied_text: str | None,
    *,
    account_id: int | None = None,
    triggered_by_account_id: int | None = None,
    source: str = "router",
) -> str | None:
    """调 classifier provider 返回一个 label；任何错误返回 None。"""
    from .llm_client import LLMCallFailed, LLMError
    from .llm_dto import LLMProviderDTO
    from .llm_invoke import invoke as invoke_llm

    dto = LLMProviderDTO(
        id=int(classifier_provider.get("id") or 0),
        name=str(classifier_provider.get("name", "")),
        provider=str(classifier_provider.get("provider", "")),
        api_format=classifier_provider.get("api_format"),  # 修复：补充 api_format
        base_url=classifier_provider.get("base_url"),
        default_model=str(classifier_provider.get("default_model", "")),
        api_key_enc=classifier_provider.get("api_key_enc"),
        proxy_url=classifier_provider.get("proxy_url"),  # 修复：补充 proxy_url
    )

    # 把"原文 + 问题"压成短摘要送进去；max_tokens=8 防滥调
    blob = (replied_text or "")[:300] + "\n---\n" + (user_q or "")[:200]
    try:
        result, _, _ = await invoke_llm(
            dto,
            {dto.id: dto},
            _CLASSIFIER_SYSTEM,
            blob,
            max_tokens=8,
            account_id=account_id,
            triggered_by_account_id=triggered_by_account_id,
            source=source,
            matched_tag="router",
        )
    except (LLMCallFailed, LLMError, ValueError, Exception) as e:  # noqa: BLE001
        log.debug("classifier call failed: %s", type(e).__name__)
        return None

    label = (result.text or "").strip().lower().split()[0] if result.text else ""
    # 严格白名单
    if label in _CLASSIFIER_LABELS:
        return label
    return None

# ════════════════════════════════════════════════════════════
# 公共入口
# ════════════════════════════════════════════════════════════


async def pick_provider(
    user_q: str,
    replied_text: str | None,
    has_replied_photo: bool,
    providers: dict[int, dict[str, Any]],
    *,
    classifier_provider_id: int | None = None,
    fallback_provider_id: int | None = None,
    account_id: int | None = None,
    triggered_by_account_id: int | None = None,
) -> RoutingDecision:
    """选 provider 并补齐模型 / 协议 / 身份（阶段 D 对外入口）。

    在内部决策函数之上统一调用 ``_finalize_decision``，保证任何返回路径（规则 /
    分类器 / fallback）都带上模型级结果，且切换 provider 后同步重算。
    """
    decision = await _pick_provider_impl(
        user_q,
        replied_text,
        has_replied_photo,
        providers,
        classifier_provider_id=classifier_provider_id,
        fallback_provider_id=fallback_provider_id,
        account_id=account_id,
        triggered_by_account_id=triggered_by_account_id,
    )
    return _finalize_decision(decision, providers)


async def _pick_provider_impl(
    user_q: str,
    replied_text: str | None,
    has_replied_photo: bool,
    providers: dict[int, dict[str, Any]],
    *,
    classifier_provider_id: int | None = None,
    fallback_provider_id: int | None = None,
    account_id: int | None = None,
    triggered_by_account_id: int | None = None,
) -> RoutingDecision:
    """根据消息内容挑一个 provider。

    Args:
        user_q: 用户的问题文本（``,ai`` 后跟的参数拼起来）
        replied_text: 被回复消息的原文（如果有），否则 None
        has_replied_photo: 被回复消息是否含图片（影响视觉路径）
        providers: ``{provider_id: provider_dict}`` 候选池（已含 api_key_enc 等）
        classifier_provider_id: 可选；启用分类器兜底时的 provider id
        fallback_provider_id: 可选；规则 + 分类器都无果时使用

    Returns:
        ``RoutingDecision``。即使空候选也会抛 ``ValueError`` 让上层把错误信息编辑回 TG，
        而不是静默选一个错的 provider。

    Raises:
        ValueError: 没有任何可用 provider（候选池空 / 全无 api_key）
    """
    # 仅留有 api_key 的 provider（ollama 例外）
    candidates = [p for p in providers.values() if _has_api_key(p)]
    if not candidates:
        raise ValueError("没有任何可用 provider（候选池为空或全部未配 api_key）")

    # 1) 规则层
    rule = _rule_route(user_q, replied_text, has_replied_photo, candidates)
    if rule is not None:
        return rule

    # 2) 分类器兜底（如果配置了）
    if classifier_provider_id is not None:
        cls_p = providers.get(int(classifier_provider_id))
        if cls_p is not None and _has_api_key(cls_p):
            label = await _ask_classifier(
                cls_p,
                user_q,
                replied_text,
                account_id=account_id,
                triggered_by_account_id=triggered_by_account_id,
                source="router",
            )
            if label:
                p = _select_by_tag(candidates, label, prefer_cheap=(label != "reason"),
                                   prefer_premium=(label == "reason"))
                if p:
                    return RoutingDecision(
                        int(p["id"]),
                        f"classifier→tag={label}",
                        label,
                    )

    # 3) fallback_provider_id
    if fallback_provider_id is not None:
        fp = providers.get(int(fallback_provider_id))
        if fp is not None and _has_api_key(fp):
            return RoutingDecision(
                int(fp["id"]),
                "fallback (no rule/classifier match)",
                None,
            )

    # 4) 候选池里第一个（cost_tier 最低，省钱）
    candidates.sort(key=_cost_tier)
    p = candidates[0]
    return RoutingDecision(int(p["id"]), "fallback (first available)", None)


async def preview_route(
    user_q: str,
    replied_text: str | None,
    has_replied_photo: bool,
    providers: dict[int, dict[str, Any]],
    *,
    classifier_provider_id: int | None = None,
    fallback_provider_id: int | None = None,
) -> dict[str, Any]:
    """只做路由决策、不调用上游、不消耗 quota，返回脱敏摘要（阶段 D）。

    注意：为保证"预览不消耗 quota / 不调用上游"，此处**禁用**分类器兜底
    （分类器本身是一次真实 LLM 调用）。因此 ``classifier_provider_id`` 仅用于
    在摘要里标注"实际执行时会启用分类器"，不会真正调用。
    """
    decision = await _pick_provider_impl(
        user_q,
        replied_text,
        has_replied_photo,
        providers,
        classifier_provider_id=None,  # 预览绝不调用分类器（那会消耗 quota）
        fallback_provider_id=fallback_provider_id,
    )
    decision = _finalize_decision(decision, providers)
    summary = decision.summary()
    summary["classifier_enabled"] = classifier_provider_id is not None
    summary["preview_only"] = True
    return summary


__all__ = [
    "RoutingDecision",
    "pick_provider",
    "preview_route",
]
