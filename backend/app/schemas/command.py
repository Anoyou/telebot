"""自定义命令 + LLM Provider Pydantic schema（Sprint2 #2）。

字段约定：
- ``CommandTemplate.config`` 按 ``type`` 决定结构，schema 层做基础类型校验
- ``LLMProviderOut`` 永远不包含明文 ``api_key``；仅返回 ``has_api_key:bool``
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..db.models.command import (
    ALL_COMMAND_TYPES,
    ALL_LLM_MODALITIES,
    ALL_LLM_PROVIDERS,
    ALL_LLM_TAGS,
    COMMAND_TYPE_AI,
    COMMAND_TYPE_FORWARD_TO,
    COMMAND_TYPE_REPLY_TEXT,
    COMMAND_TYPE_RUN_PLUGIN,
    LLM_API_FORMAT_CHAT_COMPLETIONS,
    LLM_CLIENT_IDENTITY_AUTO,
    LLM_MODALITY_TEXT,
    LLM_PROTOCOL_PROFILE_STANDARD,
    LLM_WEB_SEARCH_API_FORMAT_AUTO,
)

# ── 命令名校验正则：与 worker/command.py 中的 \w+ 派发兼容 ─────
_COMMAND_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,64}$")
_COMMAND_ALIAS_RE = re.compile(r"^[a-zA-Z0-9_]{1,16}$")


# ════════════════════════════════════════════════════════════
# CommandTemplate
# ════════════════════════════════════════════════════════════


class CommandTemplateBase(BaseModel):
    """模板公共字段。"""

    name: str = Field(min_length=1, max_length=64)
    type: Literal["reply_text", "forward_to", "run_plugin", "ai"]
    config: dict[str, Any] = Field(default_factory=dict)
    description: str | None = Field(default=None, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        # 命令名只允许 [a-zA-Z0-9_]，与派发正则 \w+ 对齐
        if not _COMMAND_NAME_RE.match(v):
            raise ValueError("命令名只能包含字母 / 数字 / 下划线，1-64 字符")
        return v

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        # 双保险：防 enum 绕过
        if v not in ALL_COMMAND_TYPES:
            raise ValueError(f"未知命令类型：{v}")
        return v

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in v:
            alias = (raw or "").strip()
            if not _COMMAND_ALIAS_RE.match(alias):
                raise ValueError("aliases 只能包含字母 / 数字 / 下划线，1-16 字符")
            if alias in seen:
                continue
            seen.add(alias)
            out.append(alias)
        return out

    @field_validator("config")
    @classmethod
    def _check_config_shape(cls, v: dict[str, Any], info) -> dict[str, Any]:
        """按 type 做基础结构校验，避免 worker 拿到不完整 config 才崩。

        ``info.data`` 在 v2 下可拿到同一对象上其他已校验字段。
        """
        t = info.data.get("type")
        if t == COMMAND_TYPE_REPLY_TEXT:
            # text 必须存在；允许空串（用户可能只想清空原消息）
            if "text" not in v or not isinstance(v["text"], str):
                raise ValueError("reply_text 类型必须配置 text:str")
        elif t == COMMAND_TYPE_FORWARD_TO:
            # target_chat_id 允许留空 / 缺省 → 触发时转发到当前会话；
            # 给了就必须是 int / 数字字符串
            tgt = v.get("target_chat_id")
            if tgt is None or tgt == "":
                v.pop("target_chat_id", None)
            else:
                try:
                    v["target_chat_id"] = int(tgt)
                except (TypeError, ValueError) as exc:
                    raise ValueError("target_chat_id 必须是整数") from exc
            # delete_after：成功转发后多少秒删命令消息；0 / 缺省 = 不删
            da = v.get("delete_after")
            if da is None or da == "":
                v.pop("delete_after", None)
            else:
                try:
                    dai = int(da)
                except (TypeError, ValueError) as exc:
                    raise ValueError("delete_after 必须是整数秒") from exc
                if dai < 0 or dai > 3600:
                    raise ValueError("delete_after 必须在 0~3600 秒之间")
                if dai == 0:
                    v.pop("delete_after", None)
                else:
                    v["delete_after"] = dai
            mode = str(v.get("mode", "forward_native") or "forward_native").strip()
            if mode not in ("forward_native", "copy_text", "copy_media", "quote", "link_only"):
                raise ValueError("mode 只能是 forward_native / copy_text / copy_media / quote / link_only")
            if mode == "forward_native":
                v.pop("mode", None)
            else:
                v["mode"] = mode
        elif t == COMMAND_TYPE_RUN_PLUGIN:
            if not v.get("plugin_key"):
                raise ValueError("run_plugin 类型必须配置 plugin_key")
        elif t == COMMAND_TYPE_AI:
            mode = str(v.get("mode", "chat") or "chat").strip().lower()
            if mode not in ("chat", "search", "image", "video"):
                raise ValueError("mode 只能是 chat / search / image / video")
            v["mode"] = mode
            image_backend = str(v.get("image_backend", "codex_image") or "codex_image").strip()
            if image_backend not in ("codex_image", "llm"):
                raise ValueError("image_backend 只能是 codex_image / llm")
            if mode == "image":
                v["image_backend"] = image_backend
            video_backend = str(v.get("video_backend", "plugin") or "plugin").strip()
            if video_backend != "plugin":
                raise ValueError("video_backend 当前只能是 plugin")
            if mode == "video":
                plugin_key = str(v.get("video_plugin_key") or "video_bridge").strip()
                if not plugin_key:
                    raise ValueError("video 模式必须配置 video_plugin_key")
                v["video_backend"] = video_backend
                v["video_plugin_key"] = plugin_key
                plugin_method = str(v.get("video_plugin_method") or "").strip()
                if plugin_method:
                    v["video_plugin_method"] = plugin_method
                else:
                    v.pop("video_plugin_method", None)
            needs_provider = (mode != "image" or image_backend != "codex_image") and mode != "video"
            if not v.get("provider_id"):
                if needs_provider:
                    raise ValueError("ai 类型必须配置 provider_id（在系统设置 → LLM Provider 里建）")
            else:
                try:
                    provider_id = int(v.get("provider_id"))
                except (TypeError, ValueError) as exc:
                    raise ValueError("provider_id 必须是 LLM Provider 的整数 id") from exc
                if provider_id <= 0:
                    raise ValueError("provider_id 必须是正整数")
                v["provider_id"] = provider_id
            # 路由模式：fixed（默认）/ auto；其它值拒绝
            rm = v.get("routing_mode", "fixed")
            if rm not in ("fixed", "auto"):
                raise ValueError("routing_mode 只能是 'fixed' 或 'auto'")
            # auto 模式额外字段：fallback_provider_id 必须是正整数（缺省 = 用 provider_id 自身）
            for fld in ("routing_fallback_provider_id", "classifier_provider_id"):
                fv = v.get(fld)
                if fv is None:
                    continue
                try:
                    fvi = int(fv)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{fld} 必须是 LLM Provider 的整数 id") from exc
                if fvi <= 0:
                    raise ValueError(f"{fld} 必须是正整数")
                v[fld] = fvi
            # 输出格式 / 模板（全可选；默认走 HTML 预设）
            # 兼容老数据：'markdownv2' 不再支持（telethon 1.36 不识别），自动归一到 'html'
            of_raw = v.get("output_format", "html")
            of = "html" if of_raw == "markdownv2" else of_raw
            if of not in ("html", "markdown", "plain"):
                raise ValueError("output_format 只能是 'html' / 'markdown' / 'plain'")
            v["output_format"] = of  # 把归一后的写回（避免老 cfg 永远带着 markdownv2）
            tpl = v.get("output_template")
            if tpl is not None:
                if not isinstance(tpl, str):
                    raise ValueError("output_template 必须是字符串")
                if len(tpl) > 4000:
                    raise ValueError("output_template 长度不能超过 4000 字符")
            ev = v.get("escape_values", True)
            if not isinstance(ev, bool):
                raise ValueError("escape_values 必须是布尔值")
            ws = v.get("web_search", False)
            if not isinstance(ws, bool):
                raise ValueError("web_search 必须是布尔值")
            if mode == "search":
                ws = True
            v["web_search"] = ws
            wscs = v.get("web_search_context_size", "medium")
            if wscs not in ("low", "medium", "high"):
                raise ValueError("web_search_context_size 只能是 low / medium / high")
            v["web_search_context_size"] = wscs
            # 发送方式：edit（默认，原地编辑命令消息保留 reply 链）/ send_new
            # （删命令重发新消息，不带 reply_to——避免在被回复方那里留下回复痕迹）
            sm = v.get("send_mode", "edit")
            if sm not in ("edit", "send_new"):
                raise ValueError("send_mode 只能是 'edit' 或 'send_new'")
            v["send_mode"] = sm  # 归一回写
        return v


class CommandTemplateCreate(CommandTemplateBase):
    """新建模板入参。"""


class CommandTemplateUpdate(BaseModel):
    """PATCH 更新；所有字段可选。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    type: Literal["reply_text", "forward_to", "run_plugin", "ai"] | None = None
    config: dict[str, Any] | None = None
    description: str | None = Field(default=None, max_length=255)
    aliases: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _COMMAND_NAME_RE.match(v):
            raise ValueError("命令名只能包含字母 / 数字 / 下划线，1-64 字符")
        return v

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out: list[str] = []
        seen: set[str] = set()
        for raw in v:
            alias = (raw or "").strip()
            if not _COMMAND_ALIAS_RE.match(alias):
                raise ValueError("aliases 只能包含字母 / 数字 / 下划线，1-16 字符")
            if alias in seen:
                continue
            seen.add(alias)
            out.append(alias)
        return out


class CommandTemplateOut(CommandTemplateBase):
    """模板出参，比 base 多 id/created_at。"""

    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ════════════════════════════════════════════════════════════
# 账号 × 模板 关联
# ════════════════════════════════════════════════════════════


class AccountCommandItem(BaseModel):
    """账号详情 → 命令 tab 一行：模板内容 + 该账号是否启用。"""

    template: CommandTemplateOut
    enabled: bool

    model_config = ConfigDict(from_attributes=True)


class AICommandEnablementSummary(BaseModel):
    """AI 命令在账号上的启用统计。"""

    total_accounts: int
    enabled_accounts: int
    ai_templates: int


class BuiltinCommandItem(BaseModel):
    """``GET /api/commands/builtin`` 返回的一条。

    内置命令静态注册在 worker.command 的 ``_BUILTIN`` 字典里；前端用本接口
    把 name/alias/doc 列出来，避免用户配自定义模板时撞名字（API 校验也会拒）。
    """

    name: str
    aliases: list[str] = Field(default_factory=list)
    doc: str = ""


# ════════════════════════════════════════════════════════════
# LLM Provider
# ════════════════════════════════════════════════════════════


class ProviderModel(BaseModel):
    """LLMProvider 下挂的一个候选模型。

    - ``id``       模型 ID（如 ``gpt-5.5`` / ``claude-haiku-4-5``）
    - ``enabled``  下游"自定义命令 ai 子表单"展开式 select 里是否会出现这条
    - ``custom``   true = 用户手动添加；false = 从 fetch /v1/models 拉的
    - ``label``    可选展示名（默认就用 id）
    """

    id: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    custom: bool = False
    label: str | None = Field(default=None, max_length=128)
    supports_tools: bool | None = None
    supports_images: bool | None = None
    supports_temperature: bool | None = None
    reasoning_efforts: list[Literal["minimal", "low", "medium", "high", "xhigh"]] | None = None

    @field_validator("id")
    @classmethod
    def _strip(cls, v: str) -> str:
        v2 = v.strip()
        if not v2:
            raise ValueError("model id 不能为空")
        return v2


class LLMProviderCreate(BaseModel):
    """新建 LLM provider 入参；``api_key`` 可空（如本地 Ollama）。"""

    name: str = Field(min_length=1, max_length=64)
    provider: Literal["openai", "anthropic", "ollama"]
    api_key: str | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, max_length=255)
    default_model: str = Field(min_length=1, max_length=64)

    api_format: Literal["chat_completions", "responses", "anthropic_messages"] = (
        LLM_API_FORMAT_CHAT_COMPLETIONS
    )
    """API 协议；和 provider 厂商解耦——同一个反代 base_url 可能只支持其中某种。"""

    protocol_profile: Literal["standard", "claude_code_proxy"] = (
        LLM_PROTOCOL_PROFILE_STANDARD
    )
    """Anthropic Messages 请求兼容档案；其他 API 协议会在服务层规范化为 standard。"""

    web_search_api_format: Literal["auto", "chat_completions", "responses", "anthropic_messages"] = (
        LLM_WEB_SEARCH_API_FORMAT_AUTO
    )
    """联网搜索时的协议覆盖；auto 会让 OpenAI/chat_completions 在联网时临时走 responses。"""

    client_identity_profile: Literal[
        "auto",
        "minimal",
        "openai_sdk",
        "codex_cli",
        "codex_desktop",
        "claude_code",
        "claude_desktop",
        "grok_cli",
    ] = LLM_CLIENT_IDENTITY_AUTO
    """客户端身份档案；auto 按本次实际协议解析。与 protocol_profile 相互独立。"""

    # ── 路由元数据（全可选；不填走默认）───────────────────────
    modality: Literal["text", "vision", "audio", "multimodal"] = Field(
        default=LLM_MODALITY_TEXT
    )
    """能力模态。决定该 provider 是否会被视觉路由命中。"""

    tags: list[str] = Field(default_factory=list, max_length=20)
    """路由标签。前端用 chips 编辑；空列表 = 不参与"按 tag 分类"路由（可作纯 fallback）。"""

    cost_tier: int = Field(default=2, ge=1, le=3)
    """1=便宜（量大走它）/ 2=中 / 3=旗舰；路由器据此在同 tag 里挑。"""

    notes: str | None = Field(default=None, max_length=500)
    """运维备注；路由不读。"""

    proxy_id: int | None = Field(default=None, ge=1)
    """出口代理 id（指向 proxy 表）；None = 直连（DIRECT）。mtproxy 类型的 proxy 不能给
    LLM 调用用——HTTP 客户端不支持 MTProto；service 层在校验时拒绝。"""

    models: list[ProviderModel] = Field(default_factory=list, max_length=200)
    """该 provider 下挂的候选模型清单。新建时通常留空；建完 provider 后用前端的
    ``Fetch 模型列表`` 按钮自动拉取，再 toggle 启用要用的几个。"""

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, v: str) -> str:
        if v not in ALL_LLM_PROVIDERS:
            raise ValueError(f"未知 provider：{v}")
        return v

    @field_validator("modality")
    @classmethod
    def _check_modality(cls, v: str) -> str:
        if v not in ALL_LLM_MODALITIES:
            raise ValueError(f"未知 modality：{v}")
        return v

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: list[str]) -> list[str]:
        # 大小写不敏感 + 去重；非法标签拒绝
        normalized: list[str] = []
        seen: set[str] = set()
        for t in v:
            if not isinstance(t, str):
                raise ValueError("tags 必须是字符串数组")
            tag = t.strip().lower()
            if not tag:
                continue
            if tag not in ALL_LLM_TAGS:
                raise ValueError(
                    f"未知 tag：{tag}（合法：{sorted(ALL_LLM_TAGS)}）"
                )
            if tag in seen:
                continue
            seen.add(tag)
            normalized.append(tag)
        return normalized


class LLMProviderUpdate(BaseModel):
    """PATCH 更新；``api_key`` 给 None = 不动；空串 = 清空；非空字符串 = 替换。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    provider: Literal["openai", "anthropic", "ollama"] | None = None
    api_key: str | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, max_length=255)
    default_model: str | None = Field(default=None, min_length=1, max_length=64)
    api_format: Literal["chat_completions", "responses", "anthropic_messages"] | None = None
    protocol_profile: Literal["standard", "claude_code_proxy"] | None = None
    client_identity_profile: (
        Literal[
            "auto",
            "minimal",
            "openai_sdk",
            "codex_cli",
            "codex_desktop",
            "claude_code",
            "claude_desktop",
            "grok_cli",
        ]
        | None
    ) = None
    web_search_api_format: Literal["auto", "chat_completions", "responses", "anthropic_messages"] | None = None

    # 路由元数据（全可选；None / 缺省 = 不动）
    modality: Literal["text", "vision", "audio", "multimodal"] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    cost_tier: int | None = Field(default=None, ge=1, le=3)
    notes: str | None = Field(default=None, max_length=500)
    # proxy：要支持显式置 None（前端切回 DIRECT）；用 sentinel 区分"没传"和"传了 None"
    # 简化做法：另一个布尔 ``clear_proxy``（前端切到 DIRECT 时同时下发 proxy_id=None +
    # clear_proxy=True；切到具体 proxy 时下发 proxy_id=<id>）
    proxy_id: int | None = Field(default=None, ge=1)
    clear_proxy: bool = False
    """显式 True 表示「切回直连」；为 False 时如果 proxy_id 也是 None 则视为"不动"。"""

    models: list[ProviderModel] | None = Field(default=None, max_length=200)
    """整体替换式的 PATCH——None 表示不动；给 list（含空 list）则覆盖。
    fetch-models / test-model 等独立 endpoint 不通过这条字段，那些直接改 DB。"""

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        # 复用 Create 的校验逻辑
        return LLMProviderCreate._check_tags.__func__(cls, v)  # type: ignore[attr-defined]


class LLMProviderOut(BaseModel):
    """LLM provider 出参；**绝不含明文 api_key**。"""

    id: int
    name: str
    provider: str
    has_api_key: bool
    base_url: str | None = None
    default_model: str
    api_format: str = LLM_API_FORMAT_CHAT_COMPLETIONS
    protocol_profile: str = LLM_PROTOCOL_PROFILE_STANDARD
    client_identity_profile: str = LLM_CLIENT_IDENTITY_AUTO
    web_search_api_format: str = LLM_WEB_SEARCH_API_FORMAT_AUTO
    # 路由元数据（出参始终带，便于前端展示）
    modality: str = LLM_MODALITY_TEXT
    tags: list[str] = Field(default_factory=list)
    cost_tier: int = 2
    notes: str | None = None
    # 出口代理：None = 直连
    proxy_id: int | None = None
    # 候选模型清单（带启用状态）
    models: list[ProviderModel] = Field(default_factory=list)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FetchModelsResponse(BaseModel):
    """``POST /api/commands/llm-providers/{pid}/fetch-models`` 出参。"""

    fetched: int
    """从 ``GET {base_url}/models`` 拉到的模型条数（不含已有 enabled 状态合并前的差异）。"""
    provider: LLMProviderOut
    """合并后最新 provider 出参——前端可以直接用它替换缓存。"""


class FetchModelsPreviewRequest(BaseModel):
    """``POST /api/commands/llm-providers/fetch-models-preview`` 入参。

    用于"未保存的 provider 也想 Fetch 模型列表"场景：
    前端把当前编辑表单里的字段（provider / api_format / base_url / api_key / proxy_id）
    直接送过来，后端发一次 ``GET {base_url}/models`` 后**只返 ID 列表**，不落库，
    避免用户为了 Fetch 还得先保存。

    若 ``pid`` 给了且 ``api_key`` 留空 / None，则用 DB 里已存的 api_key——
    前端"编辑模式下不预填明文 key"的约定下，用户只想换 base_url 重新 Fetch 也能跑。
    """

    provider: Literal["openai", "anthropic", "ollama"]
    api_format: Literal["chat_completions", "responses", "anthropic_messages"] = (
        LLM_API_FORMAT_CHAT_COMPLETIONS
    )
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)
    proxy_id: int | None = Field(default=None, ge=1)
    pid: int | None = Field(default=None, ge=1)


class FetchModelsPreviewResponse(BaseModel):
    """``POST /api/commands/llm-providers/fetch-models-preview`` 出参。

    只返从 ``/models`` 拉到的模型 ID 列表；前端自己负责合并到 form.models
    （保留已勾选的 enabled 状态 / custom 条目）。
    """

    fetched: int
    ids: list[str]


class DetectProviderProtocolsRequest(BaseModel):
    """``POST /api/commands/llm-providers/detect-protocols`` 入参。"""

    provider: Literal["openai", "anthropic", "ollama"]
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)
    proxy_id: int | None = Field(default=None, ge=1)
    pid: int | None = Field(default=None, ge=1)
    model: str | None = Field(default=None, max_length=128)
    # 阶段 B：可选自然提示词；不传则用稳定默认。探测使用自然语言而非字面量 ping。
    system_prompt: str | None = Field(default=None, max_length=2000)
    message: str | None = Field(default=None, max_length=2000)


class ProtocolProbeResult(BaseModel):
    """单个 API 协议探测结果。

    阶段 B 起补充身份、阶段与结构化错误分类字段（全部可选，向后兼容）。
    """

    ok: bool
    status_code: int | None = None
    latency_ms: int
    error: str | None = None
    # 阶段 B：本次探测使用的客户端身份档案（openai_sdk / codex_cli / claude_code / minimal ...）。
    client_identity_profile: str | None = None
    # 探测阶段：network / credentials / protocol / identity。
    stage: str | None = None
    # 结构化错误分类（见 llm_identity / 诊断状态枚举）。
    error_category: str | None = None
    # 面向用户的修复建议（脱敏）。
    suggestion: str | None = None


class ProtocolIdentityAttempt(BaseModel):
    """某协议下按身份顺序的单次身份尝试结果。"""

    api_format: str
    client_identity_profile: str
    ok: bool
    status_code: int | None = None
    latency_ms: int = 0
    error_category: str | None = None
    error: str | None = None
    suggestion: str | None = None


class DetectProviderProtocolsResponse(BaseModel):
    """协议探测结果与推荐配置。"""

    chat_completions: ProtocolProbeResult
    responses: ProtocolProbeResult
    anthropic_messages: ProtocolProbeResult
    models: ProtocolProbeResult
    recommended_api_format: str | None = None
    # 阶段 B：推荐客户端身份 + 每协议身份尝试列表。
    recommended_client_identity_profile: str | None = None
    identity_attempts: list[ProtocolIdentityAttempt] = Field(default_factory=list)
    recommended_web_search_api_format: str = LLM_WEB_SEARCH_API_FORMAT_AUTO
    note: str | None = None


class TestModelRequest(BaseModel):
    """``POST /api/commands/llm-providers/{pid}/test-model`` 入参。"""

    model: str = Field(min_length=1, max_length=128)
    """要测的模型 ID。后端会用它做一次 max_tokens=4 的最小调用，回延时和返回片段。"""


class TestModelResponse(BaseModel):
    """``POST /api/commands/llm-providers/{pid}/test-model`` 出参。"""

    ok: bool
    """是否成功（HTTP 200 + 有正常 text 输出）。"""
    latency_ms: int
    """从发请求到收到响应的总耗时（毫秒）。"""
    model: str | None = None
    """API 实际返回的模型名（可能与请求的 model 略有差异，如带日期后缀）。"""
    preview: str | None = None
    """返回 text 前 80 字符；用于让用户在 UI 一眼看出"这个模型确实回话了"。"""
    error: str | None = None
    """失败时的错误消息（已脱敏，不含 api_key）。"""


class ChatTestTurn(BaseModel):
    """模型真实聊天测活的临时上下文，不落库。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatTestModelsRequest(BaseModel):
    """``POST /api/commands/llm-providers/{pid}/chat-test-models`` 入参。"""

    models: list[str] = Field(min_length=1, max_length=20)
    """要并发测试的模型 ID 列表。"""
    message: str = Field(min_length=1, max_length=2000)
    """本轮模拟用户消息。"""
    history: list[ChatTestTurn] = Field(default_factory=list, max_length=20)
    """同一测试窗口里的临时历史，用于模拟连续对话。"""
    system_prompt: str = Field(
        default="你是一个自然、简洁的中文聊天助手。请像真实聊天一样直接回复用户，不要只返回 ping/pong。",
        min_length=1,
        max_length=2000,
    )
    """测试用 system prompt。"""
    max_tokens: int = Field(default=1200, ge=64, le=8000)
    timeout_seconds: int = Field(default=90, ge=10, le=600)

    @field_validator("models")
    @classmethod
    def _strip_models(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            model = str(value or "").strip()
            if not model:
                continue
            if len(model) > 128:
                raise ValueError("模型 ID 不能超过 128 字符")
            if model in seen:
                continue
            seen.add(model)
            out.append(model)
        if not out:
            raise ValueError("至少选择一个模型")
        return out


class ChatTestModelResult(BaseModel):
    """单个模型真实聊天测活结果。"""

    ok: bool
    requested_model: str
    model: str | None = None
    latency_ms: int
    response: str | None = None
    preview: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    empty_response: bool = False
    error: str | None = None
    client_identity_profile: str | None = None
    effective_api_format: str | None = None


class ChatTestModelsResponse(BaseModel):
    """真实聊天测活批量结果。"""

    provider_id: int
    provider_name: str
    results: list[ChatTestModelResult]


# ── 阶段 C：全量已启用模型测活 ────────────────────────────────
class FullLivenessPreviewRequest(BaseModel):
    """``POST /api/commands/llm-providers/liveness/preview`` 入参。"""

    max_tokens: int = Field(default=256, ge=64, le=8000)
    global_concurrency: int = Field(default=8)


class LivenessProviderPlan(BaseModel):
    """预览中单个 Provider 的目标模型与可执行性。"""

    provider_id: int
    provider_name: str
    enabled_models: list[str] = Field(default_factory=list)
    executable: bool
    skipped_reason: str | None = None


class FullLivenessPreviewResponse(BaseModel):
    """全量测活执行预览（权威快照）。"""

    provider_total: int
    executable_provider_total: int
    enabled_model_total: int
    task_total: int
    max_tokens: int
    max_output_tokens: int
    global_concurrency: int
    provider_concurrency: int
    needs_confirmation: bool
    providers: list[LivenessProviderPlan] = Field(default_factory=list)


class FullLivenessRunRequest(BaseModel):
    """``POST /api/commands/llm-providers/liveness/run`` 入参。

    全量测活不携带历史，确保所有模型收到完全相同的输入并可横向比较。
    可选 ``only_provider_ids`` / ``only_models`` 用于失败重测等范围过滤。
    """

    system_prompt: str = Field(
        default="你是一个自然、简洁的中文聊天助手。请像真实聊天一样直接回复用户，不要只返回 ping/pong。",
        min_length=1,
        max_length=2000,
    )
    message: str = Field(
        default="用一句话自我介绍，并说明你现在能做什么。",
        min_length=1,
        max_length=2000,
    )
    max_tokens: int = Field(default=256, ge=64, le=8000)
    timeout_seconds: int = Field(default=90, ge=10, le=600)
    global_concurrency: int = Field(default=8)
    # 范围过滤（失败重测 / 只测某 Provider / 只测新启用模型）。
    only_provider_ids: list[int] | None = Field(default=None)
    only_models: list[str] | None = Field(default=None, max_length=500)


class LivenessResultItem(BaseModel):
    """单个测活任务结果（已脱敏）。"""

    provider_id: int
    provider_name: str
    model_id: str
    status: str
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    preview: str | None = None
    error: str | None = None
    error_category: str | None = None
    suggestion: str | None = None
    client_identity_profile: str | None = None
    effective_api_format: str | None = None
    skipped: bool = False


class FullLivenessRunStartResponse(BaseModel):
    """``POST …/liveness/run`` 立即返回：创建异步 Job，前端凭 run_id 轮询。"""

    run_id: str
    status: str = "queued"
    task_total: int = 0


class FullLivenessRunResponse(BaseModel):
    """全量测活结果汇总（轮询 status 或同步兼容）。

    ``status``：queued | running | completed | cancelled。
    ``completed``：已出结果的任务数（含成功/失败/跳过/取消），用于逐项进度。
    """

    run_id: str | None = None
    status: str = "completed"
    task_total: int
    completed: int = 0
    healthy: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    results: list[LivenessResultItem] = Field(default_factory=list)
    error: str | None = None


# ═══════════════ 客户端身份 UA 版本配置（0.57.0 收口） ═══════════════
class ClientIdentityVersionItem(BaseModel):
    """单个客户端身份档案的版本信息（用于 UA 版本段）。"""

    key: str
    """版本键，如 codex_cli / claude_code / openai_sdk / codex_desktop_core。"""

    label: str
    """前端展示名。"""

    current: str
    """当前生效版本（含已应用的覆盖）。"""

    default: str
    """采集时核对过的默认版本（覆盖缺失时回落值）。"""

    registry: str | None = None
    """远端版本查询源标识（npm 包名 / pypi 包名）；None 表示无公共源、仅手动。"""

    detectable: bool = False
    """是否支持"检测最新版本"按钮（有公共 registry 才为 True）。"""


class ClientIdentityVersionsResponse(BaseModel):
    """客户端身份 UA 版本配置总览。"""

    items: list[ClientIdentityVersionItem] = Field(default_factory=list)


class ClientIdentityVersionDetectItem(BaseModel):
    """单个版本键的远端检测结果。"""

    key: str
    current: str
    latest: str | None = None
    up_to_date: bool | None = None
    error: str | None = None


class ClientIdentityVersionDetectResponse(BaseModel):
    """远端最新版本检测结果（只读查询，不落库）。"""

    items: list[ClientIdentityVersionDetectItem] = Field(default_factory=list)


class ClientIdentityVersionsUpdateRequest(BaseModel):
    """保存版本覆盖（只改 UA 版本号，不动 UA 结构与请求头）。"""

    overrides: dict[str, str] = Field(default_factory=dict)
