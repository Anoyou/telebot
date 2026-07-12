"""自定义命令模板与 LLM Provider 数据模型（Sprint2 #2）。

包含 3 张表：
- ``command_template``  全局模板库；每条记录 = 一个 ``,name`` 命令的"配方"
- ``account_command_link``  账号 × 模板 的多对多映射（仅勾选启用的才在 worker 生效）
- ``llm_provider``  AI 类命令调用的大模型供应商；``api_key_enc`` 落库前必须经
  ``app.crypto.encrypt_str`` 加密

设计要点：
- 模板与账号解耦：一份模板可被任意多个账号启用 / 禁用，互不影响；
- ``CommandTemplate.config`` 是 JSONB，按 ``type`` 字段约定结构（reply_text/forward_to/run_plugin/ai）；
- ``LLMProvider.api_key_enc`` 是 Fernet token；GET 接口禁返明文，只返 ``has_api_key:bool``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# ── 命令类型枚举 ────────────────────────────────────────────
COMMAND_TYPE_REPLY_TEXT = "reply_text"   # 收到 → 编辑原消息为指定文本
COMMAND_TYPE_FORWARD_TO = "forward_to"   # 收到 → 转发被引用消息到指定 chat_id
COMMAND_TYPE_RUN_PLUGIN = "run_plugin"   # 占位：调用某插件方法（V1 暂不实装）
COMMAND_TYPE_AI = "ai"                   # 收到 → 调 LLM → 编辑回原消息

ALL_COMMAND_TYPES = {
    COMMAND_TYPE_REPLY_TEXT,
    COMMAND_TYPE_FORWARD_TO,
    COMMAND_TYPE_RUN_PLUGIN,
    COMMAND_TYPE_AI,
}


# ── LLM Provider 厂商枚举 ──────────────────────────────────
LLM_PROVIDER_OPENAI = "openai"
LLM_PROVIDER_ANTHROPIC = "anthropic"
LLM_PROVIDER_OLLAMA = "ollama"

ALL_LLM_PROVIDERS = {
    LLM_PROVIDER_OPENAI,
    LLM_PROVIDER_ANTHROPIC,
    LLM_PROVIDER_OLLAMA,
}


# ── API 格式枚举（独立于 provider 厂商；同一个 base_url 可能支持多种）─────
# - chat_completions    POST {base_url}/chat/completions   OpenAI 经典协议
# - responses           POST {base_url}/responses          OpenAI 2024 出的新协议
# - anthropic_messages  POST {base_url}/messages           Anthropic /v1/messages
LLM_API_FORMAT_CHAT_COMPLETIONS = "chat_completions"
LLM_API_FORMAT_RESPONSES = "responses"
LLM_API_FORMAT_ANTHROPIC_MESSAGES = "anthropic_messages"
LLM_WEB_SEARCH_API_FORMAT_AUTO = "auto"

ALL_LLM_API_FORMATS = {
    LLM_API_FORMAT_CHAT_COMPLETIONS,
    LLM_API_FORMAT_RESPONSES,
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
}

ALL_LLM_WEB_SEARCH_API_FORMATS = {
    LLM_WEB_SEARCH_API_FORMAT_AUTO,
    *ALL_LLM_API_FORMATS,
}

# Anthropic Messages 请求兼容档案。标准模式只发送官方协议要求的请求字段；
# claude_code_proxy 仅用于明确要求 Claude Code 兼容头的反代服务。
LLM_PROTOCOL_PROFILE_STANDARD = "standard"
LLM_PROTOCOL_PROFILE_CLAUDE_CODE_PROXY = "claude_code_proxy"

ALL_LLM_PROTOCOL_PROFILES = {
    LLM_PROTOCOL_PROFILE_STANDARD,
    LLM_PROTOCOL_PROFILE_CLAUDE_CODE_PROXY,
}


def normalize_protocol_profile(api_format: str | None, protocol_profile: str | None) -> str:
    """Return the only valid protocol profile for the effective API format."""
    if api_format != LLM_API_FORMAT_ANTHROPIC_MESSAGES:
        return LLM_PROTOCOL_PROFILE_STANDARD
    if protocol_profile in ALL_LLM_PROTOCOL_PROFILES:
        return str(protocol_profile)
    return LLM_PROTOCOL_PROFILE_STANDARD


# 客户端身份档案（0.57.0 阶段 A）：控制 UA 与身份相关的安全请求头；
# 与 protocol_profile 相互独立（后者控制协议语义 / beta 头）。
# 取值集中定义在 ``services.llm_identity``；这里保留 DB 层允许集合与默认值，
# 避免模型层反向依赖 service 层。
LLM_CLIENT_IDENTITY_AUTO = "auto"

ALL_LLM_CLIENT_IDENTITY_PROFILES = {
    "auto",
    "minimal",
    "openai_sdk",
    "codex_cli",
    "codex_desktop",
    "claude_code",
    "claude_desktop",
}


def normalize_client_identity_profile(value: str | None) -> str:
    """规范化客户端身份档案；未知值降级为 auto（不拒绝）。"""
    candidate = (value or "").strip().lower()
    if candidate in ALL_LLM_CLIENT_IDENTITY_PROFILES:
        return candidate
    return LLM_CLIENT_IDENTITY_AUTO


def default_api_format_for(provider_kind: str) -> str:
    """给定 provider 厂商，返回默认 API 格式。

    用于：alembic 迁移 0009 自动回填 + 创建 provider 时缺省值。
    """
    if (provider_kind or "").lower() == LLM_PROVIDER_ANTHROPIC:
        return LLM_API_FORMAT_ANTHROPIC_MESSAGES
    return LLM_API_FORMAT_CHAT_COMPLETIONS


# ── LLM 模态枚举（路由必备）──────────────────────────────
# - text         纯文本 LLM（最常见，GPT/Claude/GLM/Mimo 文本端点都属于这类）
# - vision       视觉多模态：能识图（图文输入 + 文本输出，如 GPT-4V/Claude Vision）
# - audio        音频多模态：能听语音 / 转写（如 Whisper / GPT-4o realtime audio）
# - multimodal   全模态：同时支持图、音、视频等多输入（GPT-4o / Gemini-Pro 类）
LLM_MODALITY_TEXT = "text"
LLM_MODALITY_VISION = "vision"
LLM_MODALITY_AUDIO = "audio"
LLM_MODALITY_MULTIMODAL = "multimodal"

ALL_LLM_MODALITIES = {
    LLM_MODALITY_TEXT,
    LLM_MODALITY_VISION,
    LLM_MODALITY_AUDIO,
    LLM_MODALITY_MULTIMODAL,
}


# ── 路由标签字典（前端做多选 chip，后端只校验集合） ────────
# 任何一条 LLMProvider.tags 是这些值的子集；路由器据 tag 选 provider。
# 标签维度按"擅长领域 / 上下文容量 / 速度档"三类组织。
LLM_TAG_CHAT = "chat"               # 通用闲聊 / 短问短答
LLM_TAG_CODE = "code"               # 代码生成 / 解释 / 调试
LLM_TAG_MATH = "math"               # 数学推导 / 计算
LLM_TAG_TRANSLATE = "translate"     # 多语种翻译
LLM_TAG_VISION = "vision"           # 看图说话 / 图像理解（与 modality 配合）
LLM_TAG_LONG_CONTEXT = "long_context"  # 大上下文（≥ 64K token）
LLM_TAG_REASON = "reason"           # 复杂推理 / 多步分析（旗舰模型）
LLM_TAG_SMART = "smart"             # 同 reason，强调"答主力"
LLM_TAG_CHEAP = "cheap"             # 量大优先（成本档 1）
LLM_TAG_FAST = "fast"               # 低延迟优先
LLM_TAG_CLASSIFY = "classify"       # 适合作"路由分类器"的轻量小模型

ALL_LLM_TAGS = {
    LLM_TAG_CHAT,
    LLM_TAG_CODE,
    LLM_TAG_MATH,
    LLM_TAG_TRANSLATE,
    LLM_TAG_VISION,
    LLM_TAG_LONG_CONTEXT,
    LLM_TAG_REASON,
    LLM_TAG_SMART,
    LLM_TAG_CHEAP,
    LLM_TAG_FAST,
    LLM_TAG_CLASSIFY,
}


class CommandTemplate(Base):
    """全局命令模板。

    ``name`` 是 ``,name`` 触发名，全表唯一；用户可在系统设置里 CRUD。
    每个账号通过 ``AccountCommandLink`` 选择是否启用某条模板。
    """

    __tablename__ = "command_template"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # ``,name`` 触发名；保持简洁仅允许 [a-zA-Z0-9_]
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 命令别名列表（JSON 数组，元素同 name 规则；跨模板唯一性由 service 层校验）
    aliases: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    # 取值见上方常量；schema 层做合法性校验
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 按 type 决定结构；统一存 JSON
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AccountCommandLink(Base):
    """[账号 × 命令模板] 启用关系表。

    联合主键；``enabled=False`` 表示曾经启用过但已关闭（保留记录便于 UI 显示历史）。
    实际派发时只看 ``enabled=True`` 的行。
    """

    __tablename__ = "account_command_link"

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), primary_key=True
    )
    template_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("command_template.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LLMProvider(Base):
    """LLM 供应商配置（AI 命令调用入口）。

    ``api_key_enc`` 是 Fernet 加密后的 base64 字符串（见 ``app/crypto.py``）；
    任何 GET 接口都不得返回明文，只返 ``has_api_key:bool``。

    路由相关字段（见 ``services.llm_router``）：
    - ``modality``  能力模态（text / vision / audio / multimodal）
    - ``tags``      路由标签数组；路由器根据用户消息特征匹配 tag 选 provider
    - ``cost_tier`` 1=便宜（量大优先）/ 2=中 / 3=旗舰（质量优先）
    - ``notes``     运维备注（不影响路由）
    """

    __tablename__ = "llm_provider"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 友好名称（前端展示用），全表唯一
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 厂商类型：openai / anthropic / ollama
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    # Fernet 加密 token；可空（如 ollama 本地部署可不填）
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 自定义 base_url；OpenAI 兼容代理 / 自托管 Ollama 都靠它
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 默认模型 ID（命令 config 里允许覆盖单条调用的 model）
    default_model: Mapped[str] = mapped_column(String(64), nullable=False)
    # API 协议：chat_completions / responses / anthropic_messages；和 provider 厂商解耦
    # 因为同一个反代 base_url 可能只支持其中某种（典型例子：anyrouter 只接 /responses）
    api_format: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=LLM_API_FORMAT_CHAT_COMPLETIONS,
    )
    # Anthropic Messages 请求兼容档案；其他 API 协议始终规范化为 standard。
    protocol_profile: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=LLM_PROTOCOL_PROFILE_STANDARD,
    )
    # 客户端身份档案（0.57.0 阶段 A）：控制 UA 与身份相关安全头，与 protocol_profile 独立。
    # auto = 按本次实际协议解析；minimal = 仅协议必需头；其余为具体产品身份。
    client_identity_profile: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=LLM_CLIENT_IDENTITY_AUTO,
    )
    # 联网搜索调用时的协议覆盖：
    # - auto：OpenAI/chat_completions 日常走 chat，web_search 时临时走 responses
    # - responses：显式用 Responses
    # - chat_completions / anthropic_messages：保留给兼容服务或后续扩展；不支持工具时会明确报错
    web_search_api_format: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=LLM_WEB_SEARCH_API_FORMAT_AUTO,
    )
    # 该 provider 下"已启用 + 自定义"的模型清单。
    # JSON 数组；每条形如：
    #   {"id": "gpt-5.5", "enabled": true, "custom": false, "label": null}
    # - ``id``       OpenAI / Anthropic 的模型 ID（fetch /v1/models 拿来 / 用户填的）
    # - ``enabled``  下游"自定义命令 ai 子表单"里是否会出现这条（ON 时会展开成
    #                ``Provider 名（提供商 · model_id）`` 一条候选）
    # - ``custom``   true = 用户手动添加；false = 从 GET /v1/models 拉的
    # - ``label``    可选展示名（默认就用 id）
    models: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    # ── 路由元数据 ────────────────────────────────
    # 模态：text/vision/audio/multimodal；默认 text
    modality: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=LLM_MODALITY_TEXT
    )
    # 标签数组；JSON list[str]；默认空列表
    tags: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    # 成本档：1=cheap / 2=mid / 3=premium；默认 2
    cost_tier: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="2"
    )
    # 运维备注（仅给自己看；路由不读）
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 出口代理（指向 proxy 表）；NULL = 直连（DIRECT），即不走任何代理。
    # mtproxy 类型仅给 Telegram 用，HTTP 客户端不支持——schema 层会拒绝。
    proxy_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("proxy.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── 命令别名（Sprint5）──────────────────────────────────────
class CommandAlias(Base):
    """命令别名：支持多词别名 + 参数透传。

    - ``alias`` 可含空格（多词别名），如 "fy zh"
    - ``target`` 是目标命令（可含预设参数），如 "translate zh"
    - ``account_id`` 为 NULL 时表示全局别名；有值时仅该账号生效
    """

    __tablename__ = "command_alias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
