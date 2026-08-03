// 系统设置 → LLM Provider 管理
// 用于"AI 类自定义指令"的大模型供应商凭据配置；API Key 在后端 Fernet 加密落库
// 列表里只显示 has_api_key:✓/✗；编辑表单点击眼睛时才通过专用接口按需查看明文
//
// 路由元数据（modality / tags / cost_tier / notes）：决定"自动路由"模式下
// 一条 ,ai 指令该把请求送给哪个 provider；详见 backend/services/llm_router.py
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { ArrowLeft, Plus, Trash2, KeyRound, Edit3, Download, Check, CheckCircle2, XCircle, Star, ChevronDown, ChevronRight, Eye, EyeOff, Filter, X, Package, Save, Activity, ArrowUpDown, GripVertical, ServerCog, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CommandBadge } from "@/components/CommandBadge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Spinner } from "@/components/ui/misc";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

import {
  createLLMProvider,
  chatTestProviderModels,
  deleteLLMProvider,
  detectClientIdentityVersions,
  detectProviderProtocols,
  fetchProviderModelsPreview,
  getClientIdentityVersions,
  listLLMProviders,
  patchLLMProvider,
  revealLLMProviderApiKey,
  updateClientIdentityVersions,
} from "@/api/commands";
import { listProxies } from "@/api/proxies";
import { getHealthOverview, getSystemSettings, patchSystemSettings } from "@/api/system";
import type { ChatTestModelResult, ClientIdentityHeaderItem, ClientIdentityRequestProfile, ClientIdentityVersionDetectItem, ClientIdentityVersionItem, DetectProviderProtocolsResponse, LLMApiFormat, LLMClientIdentityProfile, LLMExecutionBackend, LLMModality, LLMProtocolProfile, LLMProviderKind, LLMProviderOut, LLMRequestHeaderInput, LLMRequestHeaderScope, LLMTag, LLMWebSearchApiFormat, ProviderModel, ProtocolProbeResult, ProxyOut } from "@/api/types";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";
import { confirmDiscardChanges, useUnsavedChanges } from "@/lib/unsavedChanges";
import { applyExecutionBackend, executionBackendLabel, isGatewayBackend } from "@/lib/providerExecutionBackend";
import {
  ProviderCreateVerification,
  type ProviderCreateStage,
} from "@/components/ai/ProviderCreateVerification";

// 各 provider 的默认 base_url 提示，仅作 placeholder
const DEFAULT_BASE_URLS: Record<LLMProviderKind, string> = {
  openai: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com/v1",
  ollama: "http://localhost:11434/v1",
};

// API Format 选项（与后端 ALL_LLM_API_FORMATS 对齐）
const API_FORMAT_OPTIONS: { value: LLMApiFormat; label: string; hint: string }[] = [
  {
    value: "chat_completions",
    label: "Chat Completions ( /chat/completions )",
    hint: "OpenAI 经典协议；最广为兼容；OpenAI 官方 / 大多数反代默认接这个",
  },
  {
    value: "responses",
    label: "Responses ( /responses )",
    hint: "OpenAI 兼容的新协议；DeepSeek 官方 deepseek-v4-flash 原生支持，Base URL 请填 https://api.deepseek.com（无需 /v1）",
  },
  {
    value: "anthropic_messages",
    label: "Anthropic Messages ( /v1/messages )",
    hint: "Anthropic 协议；走官方 https://api.anthropic.com 或兼容反代时选",
  },
];

const PROTOCOL_PROFILE_OPTIONS: Record<
  LLMApiFormat,
  Array<{ value: LLMProtocolProfile; label: string; hint: string }>
> = {
  chat_completions: [
    { value: "standard", label: "标准 Chat Completions", hint: "使用所选 API Format 的标准字段。" },
  ],
  responses: [
    { value: "standard", label: "通用 Responses", hint: "适合未声明特殊方言的兼容服务。" },
    { value: "openai_responses", label: "OpenAI Responses", hint: "启用 OpenAI 官方 Responses 字段与标准 SDK 身份。" },
    { value: "deepseek_responses", label: "DeepSeek Responses", hint: "按 DeepSeek V4 正式版约束移除 store、previous_response_id、include 等不支持字段。" },
    { value: "codex_responses", label: "Codex Responses", hint: "用于明确要求 Codex 身份及加密 reasoning 回传的 Responses 服务。" },
  ],
  anthropic_messages: [
    { value: "standard", label: "标准 Anthropic API", hint: "仅发送 Anthropic Messages 标准字段。" },
    { value: "claude_code_proxy", label: "Claude Code 反代兼容", hint: "仅用于明确要求 Claude Code beta 语义的反代。" },
  ],
};

function protocolProfileForFormat(
  apiFormat: LLMApiFormat,
  profile: LLMProtocolProfile,
): LLMProtocolProfile {
  return PROTOCOL_PROFILE_OPTIONS[apiFormat].some((option) => option.value === profile)
    ? profile
    : "standard";
}

const WEB_SEARCH_API_FORMAT_OPTIONS: { value: LLMWebSearchApiFormat; label: string; hint: string }[] = [
  {
    value: "auto",
    label: "自动（推荐）",
    hint: "日常按上方 API Format 调用；联网搜索时，OpenAI/chat_completions 会临时切到 Responses。",
  },
  {
    value: "responses",
    label: "Responses ( /responses )",
    hint: "联网搜索显式使用 Responses。适合官方 OpenAI 或同时支持两种协议的兼容站。",
  },
  {
    value: "chat_completions",
    label: "Chat Completions ( /chat/completions )",
    hint: "仅当你的兼容服务在 chat/completions 自行实现了搜索工具时使用；官方 OpenAI 搜索不走这里。",
  },
  {
    value: "anthropic_messages",
    label: "Anthropic Messages ( /v1/messages )",
    hint: "预留给未来 Anthropic 搜索能力；当前通常不建议用于 search 模式。",
  },
];

// 客户端身份档案选项（与后端 services.llm_identity 对齐）。
const CLIENT_IDENTITY_OPTIONS: {
  value: LLMClientIdentityProfile;
  label: string;
  hint: string;
  disabled?: boolean;
}[] = [
  {
    value: "auto",
    label: "自动（推荐）",
    hint: "按协议档案解析：标准 Responses / DeepSeek 使用 OpenAI SDK，Codex 档案仅使用兼容请求头，Anthropic 使用 Claude Code CLI。",
  },
  {
    value: "minimal",
    label: "最小（仅协议必需头）",
    hint: "不附加任何产品模拟头，仅发送协议必需头。上游不校验客户端身份时使用。",
  },
  {
    value: "openai_sdk",
    label: "OpenAI SDK（标准 API）",
    hint: "OpenAI 官方 Python SDK 身份，用于标准 Chat Completions / Responses API。",
  },
  {
    value: "codex_tui",
    label: "Codex 兼容请求头（非官方运行时）",
    hint: "仅附加可复核的 Codex 兼容请求头；不提供官方运行时、OAuth、账号或设备身份。",
  },
  {
    value: "codex_desktop",
    label: "Codex Desktop",
    hint: "Codex 桌面端身份（originator=Codex Desktop），用于 Responses。",
  },
  {
    value: "claude_code",
    label: "Claude Code CLI",
    hint: "Claude Code 身份（x-app=cli），用于 Anthropic Messages。",
  },
  {
    value: "grok_cli",
    label: "Grok CLI",
    hint: "Grok CLI 身份（grok-cli UA + x-grok-client-version），用于 Responses；不附加 OAuth、账号或设备字段。",
  },
];

// 模态选项 + 中文解释（与后端 ALL_LLM_MODALITIES 对齐）
const MODALITY_OPTIONS: { value: LLMModality; label: string; hint: string }[] = [
  { value: "text", label: "纯文本（text）", hint: "只支持文本输入输出（绝大多数 LLM）" },
  {
    value: "vision",
    label: "视觉多模态（vision）",
    hint: "支持图文输入 → 文本输出（如 GPT-4V、Claude Vision）",
  },
  {
    value: "audio",
    label: "音频多模态（audio）",
    hint: "支持语音转写 / TTS（如 Whisper、GPT-4o realtime）",
  },
  {
    value: "multimodal",
    label: "全模态（multimodal）",
    hint: "图、音、视频同时输入（如 GPT-4o、Gemini-Pro）",
  },
];

// 路由标签字典 + 解释（与后端 ALL_LLM_TAGS 对齐）
const TAG_OPTIONS: { value: LLMTag; label: string; hint: string }[] = [
  { value: "chat", label: "chat", hint: "通用闲聊 / 短问短答" },
  { value: "code", label: "code", hint: "代码生成 / 解释 / 调试" },
  { value: "math", label: "math", hint: "数学推导 / 计算" },
  { value: "translate", label: "translate", hint: "多语种翻译" },
  { value: "vision", label: "vision", hint: "看图说话 / 图像理解（需配合 modality=vision）" },
  { value: "long_context", label: "long_context", hint: "大上下文（≥ 64K token）" },
  { value: "reason", label: "reason", hint: "复杂推理 / 多步分析（旗舰）" },
  { value: "smart", label: "smart", hint: "答主力（同 reason，强调质量）" },
  { value: "cheap", label: "cheap", hint: "量大优先（成本档 1）" },
  { value: "fast", label: "fast", hint: "低延迟优先" },
  { value: "classify", label: "classify", hint: "适合做路由分类器的轻量小模型" },
];

const COST_TIER_OPTIONS = [
  { value: 1, label: "1 · 便宜（量大走它）" },
  { value: 2, label: "2 · 中（默认）" },
  { value: 3, label: "3 · 旗舰（贵但答主力）" },
];

const CLIENT_HEADER_GROUPS: Array<{
  value: ClientIdentityHeaderItem["management"];
  label: string;
  description: string;
  tone: "outline" | "info" | "warn" | "danger";
}> = [
  { value: "fixed", label: "固定发送", description: "由所选客户端档案生成。", tone: "info" },
  { value: "runtime", label: "动态生成", description: "每个客户端实例或请求自动生成。", tone: "outline" },
  { value: "protocol", label: "协议自动", description: "由鉴权、API 协议和响应模式决定。", tone: "warn" },
  { value: "transport", label: "传输自动", description: "由 HTTP 客户端在发出请求时计算。", tone: "outline" },
  { value: "excluded", label: "观察到但不复制", description: "涉及内部实验、设备、账号或鉴权语义，明确禁止配置。", tone: "danger" },
];

const MASKED_SECRET_PLACEHOLDER = "••••••••••••••••";

interface FormState {
  id?: number; // 编辑模式时存在
  hasApiKey?: boolean;
  name: string;
  provider: LLMProviderKind;
  api_key: string; // 编辑时初始为空 = 不动；填非空 = 替换
  base_url: string;
  default_model: string;
  // API Format（chat_completions / responses / anthropic_messages）
  api_format: LLMApiFormat;
  protocol_profile: LLMProtocolProfile;
  web_search_api_format: LLMWebSearchApiFormat;
  // 客户端身份档案；与 protocol_profile 相互独立
  client_identity_profile: LLMClientIdentityProfile;
  execution_backend: LLMExecutionBackend;
  direct_api_format?: LLMApiFormat;
  direct_protocol_profile?: LLMProtocolProfile;
  direct_web_search_api_format?: LLMWebSearchApiFormat;
  // 编辑模式下，是否要"清空已有 key"（按钮触发）
  clearKey: boolean;
  // ── 路由元数据 ──
  modality: LLMModality;
  tags: LLMTag[];
  cost_tier: number;
  notes: string;
  // ── 出口代理 ──
  // "" 表示 DIRECT（不走代理）；其它是 proxy.id 字符串
  proxy_id: string;
  // ── 候选模型清单 ──
  // toggle / 自定义添加 / fetch 都改这个；保存时整体 PATCH 给后端
  models: ProviderModel[];
  request_headers: FormRequestHeader[];
}

interface FormRequestHeader {
  name: string;
  value: string;
  scopes: LLMRequestHeaderScope[];
  hasValue?: boolean;
}

function requestHeadersPayload(headers: FormRequestHeader[]): LLMRequestHeaderInput[] {
  return headers.map((header) => ({
    name: header.name.trim(),
    value: header.value || (header.hasValue ? null : ""),
    scopes: header.scopes,
  }));
}

const EMPTY_FORM: FormState = {
  name: "",
  provider: "openai",
  api_key: "",
  base_url: "",
  default_model: "",
  api_format: "responses",
  protocol_profile: "standard",
  web_search_api_format: "auto",
  client_identity_profile: "auto",
  execution_backend: "direct",
  clearKey: false,
  modality: "text",
  tags: ["chat"],
  cost_tier: 2,
  notes: "",
  proxy_id: "",
  models: [],
  request_headers: [],
};

function ApiKeyInput({
  id,
  value,
  disabled,
  placeholder,
  autoComplete,
  hasStoredValue = false,
  revealStoredValue,
  onChange,
}: {
  id?: string;
  value: string;
  disabled?: boolean;
  placeholder: string;
  autoComplete: string;
  hasStoredValue?: boolean;
  revealStoredValue?: () => Promise<string>;
  onChange: (value: string) => void;
}) {
  const [visible, setVisible] = useState(false);
  const [revealedValue, setRevealedValue] = useState("");
  const [revealing, setRevealing] = useState(false);
  const displayValue = value || revealedValue;
  const canReveal = !disabled && (Boolean(displayValue) || (hasStoredValue && Boolean(revealStoredValue)));

  useEffect(() => {
    if (!value) setVisible(false);
  }, [value]);

  useEffect(() => {
    if (!disabled) return;
    setRevealedValue("");
    setVisible(false);
  }, [disabled]);

  const toggleVisibility = async () => {
    if (visible) {
      setVisible(false);
      return;
    }
    if (displayValue) {
      setVisible(true);
      return;
    }
    if (!revealStoredValue) return;
    setRevealing(true);
    try {
      const storedValue = await revealStoredValue();
      setRevealedValue(storedValue);
      setVisible(true);
    } catch (error) {
      toast.error(getErrMsg(error));
    } finally {
      setRevealing(false);
    }
  };

  return (
    <div className="relative">
      <Input
        id={id}
        type={visible ? "text" : "password"}
        value={displayValue}
        maxLength={512}
        autoComplete={autoComplete}
        disabled={disabled}
        placeholder={placeholder}
        className="pr-10"
        onChange={(event) => {
          setRevealedValue("");
          onChange(event.target.value);
        }}
      />
      <button
        type="button"
        className="absolute right-1 top-1/2 grid h-7 w-7 -translate-y-1/2 place-items-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-35"
        disabled={!canReveal || revealing}
        aria-label={visible ? "隐藏 API Key" : "显示 API Key"}
        aria-pressed={visible}
        title={canReveal ? (visible ? "隐藏 API Key" : "显示 API Key") : "当前没有可查看的 API Key"}
        onClick={() => void toggleVisibility()}
      >
        {revealing ? <Spinner /> : visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );
}

const REQUEST_HEADER_SCOPES: Array<{
  value: LLMRequestHeaderScope;
  label: string;
}> = [
  { value: "inference", label: "推理" },
  { value: "liveness", label: "测活" },
  { value: "models", label: "模型发现" },
];

interface RequestHeaderGuideEntry {
  name: string;
  description: string;
  scopes: LLMRequestHeaderScope[];
  credential?: boolean;
}

interface RequestHeaderGuide {
  key: string;
  label: string;
  hosts: string[];
  note: string;
  headers: RequestHeaderGuideEntry[];
}

const REQUEST_HEADER_GUIDES: RequestHeaderGuide[] = [
  {
    key: "deepseek",
    label: "DeepSeek 官方",
    hosts: ["api.deepseek.com"],
    note: "官方 API 通常只需要系统生成的 Bearer 鉴权，不需要额外兼容头；deepseek-v4-flash 请优先选择 Responses（Base URL 无需 /v1）。",
    headers: [],
  },
  {
    key: "openai",
    label: "OpenAI 官方",
    hosts: ["api.openai.com"],
    note: "普通调用不需要额外头；仅多组织或多项目账号按需添加。",
    headers: [
      { name: "OpenAI-Organization", description: "指定请求归属的 OpenAI 组织。", scopes: ["inference", "liveness", "models"] },
      { name: "OpenAI-Project", description: "指定请求归属的 OpenAI 项目。", scopes: ["inference", "liveness", "models"] },
    ],
  },
  {
    key: "openrouter",
    label: "OpenRouter",
    hosts: ["openrouter.ai"],
    note: "以下字段用于应用归属与展示，不替代 API Key。",
    headers: [
      { name: "HTTP-Referer", description: "声明调用来源站点，用于 OpenRouter 应用归属。", scopes: ["inference", "liveness", "models"] },
      { name: "X-Title", description: "声明应用名称，供 OpenRouter 控制台和排行展示。", scopes: ["inference", "liveness", "models"] },
    ],
  },
  {
    key: "azure",
    label: "Azure OpenAI",
    hosts: ["openai.azure.com", "services.ai.azure.com"],
    note: "使用 Azure API Key 鉴权的部署可添加 api-key；Entra ID 模式不要添加。",
    headers: [
      { name: "api-key", description: "Azure OpenAI 部署密钥，属于敏感凭据。", scopes: ["inference", "liveness", "models"], credential: true },
    ],
  },
  {
    key: "anthropic",
    label: "Anthropic 官方",
    hosts: ["api.anthropic.com"],
    note: "anthropic-version、beta 与 Claude Code 身份头由系统管理，不应在这里重复添加。",
    headers: [],
  },
  {
    key: "xai",
    label: "xAI 官方",
    hosts: ["api.x.ai"],
    note: "官方 API 通常只需要系统鉴权；Grok CLI 身份头由客户端身份档案生成。",
    headers: [],
  },
  {
    key: "ollama",
    label: "Ollama",
    hosts: ["localhost", "127.0.0.1", "host.docker.internal"],
    note: "本地 Ollama 默认不需要额外请求头；前置网关有明确要求时再配置。",
    headers: [],
  },
  {
    key: "gateway",
    label: "常见 AI 网关",
    hosts: [],
    note: "只有对应网关文档明确要求时才添加，值会作为敏感配置加密保存。",
    headers: [
      { name: "cf-aig-authorization", description: "Cloudflare AI Gateway 的网关鉴权值。", scopes: ["inference", "liveness", "models"], credential: true },
      { name: "x-portkey-api-key", description: "Portkey 网关自身的访问凭据。", scopes: ["inference", "liveness", "models"], credential: true },
      { name: "x-portkey-virtual-key", description: "Portkey 中选择上游凭据的虚拟 Key。", scopes: ["inference", "liveness", "models"], credential: true },
      { name: "Helicone-Auth", description: "Helicone 网关鉴权，通常填写 Bearer 形式。", scopes: ["inference", "liveness", "models"], credential: true },
      { name: "Helicone-Property-Session", description: "给 Helicone 调用记录附加会话分组标签。", scopes: ["inference"] },
    ],
  },
];

function requestHeaderGuide(baseUrl: string, provider: LLMProviderKind): RequestHeaderGuide {
  let hostname = "";
  try {
    hostname = new URL(baseUrl).hostname.toLowerCase();
  } catch {
    hostname = baseUrl.toLowerCase();
  }
  const matched = REQUEST_HEADER_GUIDES.find((guide) =>
    guide.hosts.some((host) => hostname === host || hostname.endsWith(`.${host}`)),
  );
  if (matched) return matched;
  if (provider === "anthropic") return REQUEST_HEADER_GUIDES.find((guide) => guide.key === "anthropic")!;
  if (provider === "ollama") return REQUEST_HEADER_GUIDES.find((guide) => guide.key === "ollama")!;
  return REQUEST_HEADER_GUIDES.find((guide) => guide.key === "gateway")!;
}

function RequestHeadersEditor({
  headers,
  provider,
  baseUrl,
  disabled = false,
  onChange,
}: {
  headers: FormRequestHeader[];
  provider: LLMProviderKind;
  baseUrl: string;
  disabled?: boolean;
  onChange: (headers: FormRequestHeader[]) => void;
}) {
  const [visibleValues, setVisibleValues] = useState<Set<number>>(new Set());
  const currentGuide = requestHeaderGuide(baseUrl, provider);

  const update = (index: number, patch: Partial<FormRequestHeader>) => {
    onChange(headers.map((header, itemIndex) => (
      itemIndex === index ? { ...header, ...patch } : header
    )));
  };

  const toggleScope = (index: number, scope: LLMRequestHeaderScope) => {
    const current = headers[index].scopes;
    update(index, {
      scopes: current.includes(scope)
        ? current.filter((item) => item !== scope)
        : [...current, scope],
    });
  };

  const addSuggestedHeader = (entry: RequestHeaderGuideEntry) => {
    if (headers.some((header) => header.name.trim().toLowerCase() === entry.name.toLowerCase())) return;
    onChange([...headers, { name: entry.name, value: "", scopes: entry.scopes }]);
  };

  const renderGuideEntries = (guide: RequestHeaderGuide) => (
    <div className="divide-y rounded-md border bg-background/70">
      {guide.headers.map((entry) => {
        const added = headers.some((header) => header.name.trim().toLowerCase() === entry.name.toLowerCase());
        return (
          <div key={entry.name} className="flex min-w-0 flex-wrap items-start gap-2 px-3 py-2.5 sm:flex-nowrap">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5">
                <code className="break-all text-xs font-semibold">{entry.name}</code>
                {entry.credential ? <MetaBadge tone="warn">凭据</MetaBadge> : null}
              </div>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">{entry.description}</p>
            </div>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="shrink-0"
              disabled={disabled || added || headers.length >= 16}
              onClick={() => addSuggestedHeader(entry)}
            >
              {added ? <Check className="mr-1 h-3.5 w-3.5" /> : <Plus className="mr-1 h-3.5 w-3.5" />}
              {added ? "已添加" : "添加"}
            </Button>
          </div>
        );
      })}
    </div>
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <Label className="text-sm font-semibold">Provider 兼容请求头</Label>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            仅用于上游明确要求的租户、路由或兼容字段。值会加密保存，系统鉴权头和客户端身份头不能覆盖。
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled || headers.length >= 16}
          onClick={() => onChange([
            ...headers,
            { name: "", value: "", scopes: ["inference", "liveness", "models"] },
          ])}
        >
          <Plus className="mr-1 h-4 w-4" /> 添加请求头
        </Button>
      </div>

      <div className="rounded-md border bg-muted/20 p-3">
        <div className="text-xs font-semibold">当前接入：{currentGuide.label}</div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{currentGuide.note}</p>
        {currentGuide.headers.length > 0 ? <div className="mt-2">{renderGuideEntries(currentGuide)}</div> : null}
        <details className="mt-2 border-t pt-2">
          <summary className="cursor-pointer text-xs font-medium text-muted-foreground">查看其它常见 Provider 请求头</summary>
          <div className="mt-2 space-y-3">
            {REQUEST_HEADER_GUIDES.filter((guide) => guide.key !== currentGuide.key).map((guide) => (
              <div key={guide.key}>
                <div className="text-xs font-semibold">{guide.label}</div>
                <p className="mt-0.5 text-xs leading-5 text-muted-foreground">{guide.note}</p>
                {guide.headers.length > 0 ? <div className="mt-1.5">{renderGuideEntries(guide)}</div> : null}
              </div>
            ))}
          </div>
        </details>
      </div>

      {headers.length === 0 ? (
        <div className="rounded-md border border-dashed px-3 py-4 text-center text-xs text-muted-foreground">
          当前没有 Provider 专用请求头。
        </div>
      ) : (
        <div className="divide-y rounded-md border">
          {headers.map((header, index) => {
            const valueVisible = visibleValues.has(index);
            return (
              <div key={`${index}-${header.name}`} className="min-w-0 space-y-2 p-3">
                <div className="grid gap-2 sm:grid-cols-[minmax(150px,0.8fr)_minmax(200px,1.2fr)_32px]">
                  <Input
                    value={header.name}
                    maxLength={64}
                    disabled={disabled}
                    className="font-mono text-xs"
                    placeholder="X-Tenant-ID"
                    aria-label={`请求头 ${index + 1} 名称`}
                    onChange={(event) => update(index, {
                      name: event.target.value,
                      ...(header.hasValue && !header.value ? { hasValue: false } : {}),
                    })}
                  />
                  <div className="relative min-w-0">
                    <Input
                      type={valueVisible ? "text" : "password"}
                      value={header.value}
                      maxLength={2048}
                      disabled={disabled}
                      className="pr-9 font-mono text-xs"
                      placeholder={header.hasValue ? "已加密保存，留空保持不变" : "请求头值"}
                      autoComplete="off"
                      aria-label={`请求头 ${index + 1} 值`}
                      onChange={(event) => update(index, { value: event.target.value })}
                    />
                    <button
                      type="button"
                      className="absolute right-1 top-1/2 grid h-7 w-7 -translate-y-1/2 place-items-center text-muted-foreground hover:text-foreground disabled:opacity-40"
                      disabled={disabled || !header.value}
                      title={valueVisible ? "隐藏新值" : "显示新值"}
                      aria-label={valueVisible ? "隐藏请求头新值" : "显示请求头新值"}
                      onClick={() => setVisibleValues((current) => {
                        const next = new Set(current);
                        if (next.has(index)) next.delete(index);
                        else next.add(index);
                        return next;
                      })}
                    >
                      {valueVisible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    disabled={disabled}
                    title="删除请求头"
                    aria-label={`删除请求头 ${index + 1}`}
                    onClick={() => onChange(headers.filter((_, itemIndex) => itemIndex !== index))}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                  <span className="text-xs text-muted-foreground">生效范围</span>
                  {REQUEST_HEADER_SCOPES.map((scope) => (
                    <label key={scope.value} className="inline-flex items-center gap-1.5 text-xs">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 accent-primary"
                        checked={header.scopes.includes(scope.value)}
                        disabled={disabled}
                        onChange={() => toggleScope(index, scope.value)}
                      />
                      {scope.label}
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
      <p className="text-xs leading-5 text-muted-foreground">
        禁止 Authorization、Cookie、Host、Content-Type、User-Agent、originator、x-app、会话标识和 X-Forwarded-*。
      </p>
    </div>
  );
}

export function LLMProviders({
  openCreateOnMount = false,
}: {
  openCreateOnMount?: boolean;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const didHandleCreateOnMount = useRef(openCreateOnMount);
  const providerFilter = searchParams.get("filter");
  const isVisionFilter = providerFilter === "modality:vision";

  const listQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
  });

  // 顶层也拉一次代理表，用于列表里把 proxy_id 翻译成 "host:port" 显示
  const proxiesListQ = useQuery({
    queryKey: ["proxies-for-llm"],
    queryFn: listProxies,
  });
  const proxyById: Map<number, ProxyOut> = new Map(
    (proxiesListQ.data || []).map((p) => [p.id, p]),
  );

  const [editing, setEditing] = useState<FormState | null>(() =>
    openCreateOnMount ? { ...EMPTY_FORM } : null,
  );
  const [identityVersionsOpen, setIdentityVersionsOpen] = useState(false);
  const [gatewayStatusOpen, setGatewayStatusOpen] = useState(false);
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const [providerSort, setProviderSort] = useState<"custom" | "name" | "models">("custom");
  const [editingProviderOrder, setEditingProviderOrder] = useState(false);
  const [providerOrder, setProviderOrder] = useState<number[]>([]);
  const [draggingProviderId, setDraggingProviderId] = useState<number | null>(null);
  const providerDragRef = useRef<number | null>(null);

  useEffect(() => {
    if (editingProviderOrder) return;
    const ids = (listQ.data ?? []).map((provider) => provider.id);
    const stored = settingsQ.data?.ui_preferences?.provider_order ?? [];
    setProviderOrder([
      ...stored.filter((id) => ids.includes(id)),
      ...ids.filter((id) => !stored.includes(id)),
    ]);
  }, [editingProviderOrder, listQ.data, settingsQ.data?.ui_preferences?.provider_order]);

  const filteredProviders = (listQ.data || []).filter((p) => {
    if (!isVisionFilter) return true;
    return p.modality === "vision" || p.modality === "multimodal";
  });
  const visibleProviders = [...filteredProviders].sort((left, right) => {
    if (providerSort === "name") return left.name.localeCompare(right.name, "zh-CN");
    if (providerSort === "models") {
      const enabledLeft = (left.models || []).filter((model) => model.enabled).length;
      const enabledRight = (right.models || []).filter((model) => model.enabled).length;
      return enabledRight - enabledLeft || left.name.localeCompare(right.name, "zh-CN");
    }
    const leftIndex = providerOrder.indexOf(left.id);
    const rightIndex = providerOrder.indexOf(right.id);
    if (leftIndex < 0 && rightIndex < 0) return left.id - right.id;
    if (leftIndex < 0) return 1;
    if (rightIndex < 0) return -1;
    return leftIndex - rightIndex;
  });

  const reorderProvider = (sourceId: number, targetId: number) => {
    if (sourceId === targetId) return;
    setProviderOrder((current) => {
      const ids = (listQ.data ?? []).map((provider) => provider.id);
      const next = [...current.filter((id) => ids.includes(id)), ...ids.filter((id) => !current.includes(id))];
      const sourceIndex = next.indexOf(sourceId);
      const targetIndex = next.indexOf(targetId);
      if (sourceIndex < 0 || targetIndex < 0) return current;
      next.splice(sourceIndex, 1);
      next.splice(targetIndex, 0, sourceId);
      return next;
    });
  };
  const saveProviderOrder = useMutation({
    mutationFn: () => patchSystemSettings({
      ui_preferences: {
        provider_order: providerOrder,
      },
    }),
    onSuccess: () => {
      toast.success("Provider 自定义顺序已保存");
      setEditingProviderOrder(false);
      setProviderSort("custom");
      void qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (error) => toast.error(getErrMsg(error)),
  });

  const clearProviderFilter = () => {
    const next = new URLSearchParams(searchParams);
    next.delete("filter");
    setSearchParams(next, { replace: true });
  };

  const openCreate = () => {
    const next = new URLSearchParams(searchParams);
    next.set("newProvider", "1");
    setSearchParams(next);
  };

  const closeCreate = () => {
    setEditing(null);
    if (searchParams.get("newProvider") !== "1") return;
    const next = new URLSearchParams(searchParams);
    next.delete("newProvider");
    setSearchParams(next, { replace: true });
  };

  useEffect(() => {
    const shouldOpenFromQuery = searchParams.get("newProvider") === "1";
    const shouldOpen = openCreateOnMount || shouldOpenFromQuery;

    if (!shouldOpen || didHandleCreateOnMount.current) return;

    didHandleCreateOnMount.current = true;
    setEditing({ ...EMPTY_FORM });
  }, [openCreateOnMount, searchParams, setSearchParams]);

  const createMut = useMutation({
    mutationFn: (form: FormState) =>
      createLLMProvider({
        name: form.name.trim(),
        provider: form.provider,
        api_key: form.api_key || null,
        base_url: form.base_url || null,
        default_model: form.default_model.trim(),
        api_format: form.api_format,
        protocol_profile: form.protocol_profile,
        web_search_api_format: form.web_search_api_format,
        client_identity_profile: form.client_identity_profile,
        execution_backend: form.execution_backend,
        modality: form.modality,
        tags: form.tags,
        cost_tier: form.cost_tier,
        notes: form.notes || null,
        proxy_id: form.proxy_id ? Number(form.proxy_id) : null,
        models: form.models,
        request_headers: requestHeadersPayload(form.request_headers),
      }),
    onSuccess: () => {
      toast.success("已新建模型提供商");
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      qc.invalidateQueries({ queryKey: ["system-agent", "capabilities"] });
      qc.invalidateQueries({ queryKey: ["system", "health-overview"] });
      closeCreate();
    },
    onError: (err) => {
      toast.error(getErrMsg(err));
      void qc.invalidateQueries({ queryKey: ["system", "health-overview"] });
    },
  });

  const updateMut = useMutation({
    mutationFn: (form: FormState) => {
      if (!form.id) throw new Error("缺少 id");
      const apiKey = form.clearKey ? "" : form.api_key ? form.api_key : undefined;
      const proxyPatch =
        form.proxy_id === ""
          ? { clear_proxy: true, proxy_id: null }
          : { proxy_id: Number(form.proxy_id) };
      return patchLLMProvider(form.id, {
        name: form.name.trim(),
        provider: form.provider,
        api_key: apiKey,
        base_url: form.base_url || null,
        default_model: form.default_model.trim(),
        api_format: form.api_format,
        protocol_profile: form.protocol_profile,
        web_search_api_format: form.web_search_api_format,
        client_identity_profile: form.client_identity_profile,
        execution_backend: form.execution_backend,
        modality: form.modality,
        tags: form.tags,
        cost_tier: form.cost_tier,
        notes: form.notes || null,
        ...proxyPatch,
        models: form.models,
        request_headers: requestHeadersPayload(form.request_headers),
      });
    },
    onSuccess: () => {
      toast.success("已保存");
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      qc.invalidateQueries({ queryKey: ["system-agent", "capabilities"] });
      qc.invalidateQueries({ queryKey: ["system", "health-overview"] });
      setEditing(null);
    },
    onError: (err) => {
      toast.error(getErrMsg(err));
      void qc.invalidateQueries({ queryKey: ["system", "health-overview"] });
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteLLMProvider(id),
    onSuccess: () => {
      toast.success("已删除");
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      qc.invalidateQueries({ queryKey: ["system-agent", "capabilities"] });
      qc.invalidateQueries({ queryKey: ["system", "health-overview"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const onEdit = (p: LLMProviderOut) => {
    setEditing({
      id: p.id,
      hasApiKey: p.has_api_key,
      name: p.name,
      provider: (p.provider as LLMProviderKind) || "openai",
      // 编辑模式下永远不预填明文 key
      api_key: "",
      base_url: p.base_url || "",
      default_model: p.default_model,
      api_format: ((p.api_format as LLMApiFormat) || "chat_completions"),
      protocol_profile: protocolProfileForFormat(
        ((p.api_format as LLMApiFormat) || "chat_completions"),
        ((p.protocol_profile as LLMProtocolProfile) || "standard"),
      ),
      web_search_api_format: ((p.web_search_api_format as LLMWebSearchApiFormat) || "auto"),
      client_identity_profile: ((p.client_identity_profile as LLMClientIdentityProfile) || "auto"),
      execution_backend: ((p.execution_backend as LLMExecutionBackend) || "direct"),
      clearKey: false,
      modality: ((p.modality as LLMModality) || "text"),
      tags: ((p.tags as LLMTag[]) || []).filter((t) =>
        TAG_OPTIONS.some((opt) => opt.value === t),
      ),
      cost_tier: typeof p.cost_tier === "number" ? p.cost_tier : 2,
      notes: p.notes || "",
      proxy_id: p.proxy_id != null ? String(p.proxy_id) : "",
      models: (p.models || []).map((m) => ({
        id: m.id,
        enabled: !!m.enabled,
        custom: !!m.custom,
        label: m.label ?? null,
        supports_tools: m.supports_tools ?? null,
        supports_images: m.supports_images ?? null,
        supports_temperature: m.supports_temperature ?? null,
        reasoning_efforts: m.reasoning_efforts ?? null,
      })),
      request_headers: (p.request_headers || []).map((header) => ({
        name: header.name,
        value: "",
        scopes: header.scopes,
        hasValue: header.has_value,
      })),
    });
  };

  const saveEditing = () => {
    if (!editing) return;
    if (!editing.name.trim()) {
      toast.error("名称必填");
      return;
    }
    if (!editing.default_model.trim()) {
      toast.error("默认模型必填");
      return;
    }
    const invalidHeader = editing.request_headers.find(
      (header) =>
        !header.name.trim() ||
        (!header.value && !header.hasValue) ||
        header.scopes.length === 0,
    );
    if (invalidHeader) {
      toast.error("兼容请求头需要填写名称和值，并至少选择一个作用域");
      return;
    }
    if (editing.id) {
      updateMut.mutate(editing);
    } else {
      createMut.mutate(editing);
    }
  };

  if (editing) {
    return (
      <ProviderEditDialog
        form={editing}
        onChange={setEditing}
        onCancel={closeCreate}
        onSave={saveEditing}
        saving={editing.id ? updateMut.isPending : createMut.isPending}
      />
    );
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <div className="min-w-0 space-y-4">
            <div className="flex min-w-0 items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <Package className="h-4 w-4 shrink-0 text-primary" />
                  <div className="min-w-0 truncate text-base font-semibold tracking-tight">模型提供商</div>
                </div>
                <div className="mt-1 text-sm leading-5 text-muted-foreground">
                  每行对应一组供应商凭据。编辑 Provider 可拉取模型列表，并选择参与路由的模型。
                </div>
              </div>
              <div className="shrink-0">
                <SignalPill
                  tone={visibleProviders.length > 0 ? "primary" : "warn"}
                  label="可见 Provider"
                  value={`${visibleProviders.length}`}
                  className="h-8 px-2 sm:px-3"
                />
              </div>
            </div>
            <div className="grid grid-cols-3 items-center gap-2 rounded-md border border-primary/20 bg-primary/[0.04] p-2 shadow-sm sm:flex sm:flex-wrap">
              <Button
                type="button"
                size="sm"
                className="min-w-0 flex-1 shadow-sm sm:flex-none"
                disabled={visibleProviders.length === 0}
                onClick={() => navigate(`/ai/liveness?provider=${visibleProviders[0].id}`)}
              >
                <Activity className="mr-1 h-4 w-4" /> 模型测活
              </Button>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                className="min-w-0 flex-1 border border-border/80 shadow-sm sm:flex-none"
                onClick={() => setIdentityVersionsOpen(true)}
              >
                <KeyRound className="mr-1 h-4 w-4" />请求配置
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="min-w-0 flex-1 px-2 shadow-sm sm:flex-none sm:px-3"
                onClick={() => setGatewayStatusOpen(true)}
              >
                <ServerCog className="mr-1 h-4 w-4" />
                <span className="truncate">Gateway 状态</span>
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <div className="mb-3 flex flex-nowrap items-center justify-end gap-1.5 sm:gap-2">
            {visibleProviders.length > 1 ? (
              <>
              <ArrowUpDown className="hidden h-3.5 w-3.5 shrink-0 text-muted-foreground sm:block" />
              <Label htmlFor="provider-sort" className="shrink-0 text-xs text-muted-foreground">排序</Label>
              <Select id="provider-sort" value={providerSort} disabled={editingProviderOrder} onChange={(event) => setProviderSort(event.target.value as typeof providerSort)} className="h-11 min-w-0 flex-1 text-xs sm:h-9 sm:w-auto sm:min-w-32 sm:flex-none">
                <option value="custom">自定义顺序</option>
                <option value="name">名称</option>
                <option value="models">启用模型数</option>
              </Select>
              {editingProviderOrder ? (
                <>
                  <Button type="button" size="sm" variant="outline" className="min-h-11 shrink-0 px-2 text-xs sm:min-h-9" onClick={() => setEditingProviderOrder(false)}>取消</Button>
                  <Button type="button" size="sm" className="min-h-11 shrink-0 px-2 text-xs sm:min-h-9" loading={saveProviderOrder.isPending} onClick={() => saveProviderOrder.mutate()}>
                    {!saveProviderOrder.isPending ? <Save className="mr-1 h-4 w-4" /> : null}保存排序
                  </Button>
                </>
              ) : (
                <Button type="button" size="sm" variant="outline" className="min-h-11 shrink-0 px-2 text-xs sm:min-h-9" onClick={() => { setProviderSort("custom"); setEditingProviderOrder(true); }}>
                  <GripVertical className="mr-1 h-4 w-4" />编辑排序
                </Button>
              )}
              </>
            ) : null}
            <Button size="sm" className="min-h-11 shrink-0 px-2 text-xs sm:min-h-9" disabled={editingProviderOrder} onClick={openCreate}>
              <Plus className="mr-1 h-4 w-4" /> 新建
            </Button>
          </div>
          {isVisionFilter ? (
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1.5">
                <Filter className="h-3.5 w-3.5" />
                当前仅显示 modality=vision 或 multimodal 的模型提供商。
              </span>
              <Button type="button" variant="ghost" size="sm" onClick={clearProviderFilter}>
                <X className="mr-1 h-3.5 w-3.5" />
                清除筛选
              </Button>
            </div>
          ) : null}
          {listQ.isLoading ? (
            <div className="flex h-20 items-center justify-center">
              <Spinner className="text-primary" />
            </div>
          ) : editingProviderOrder && visibleProviders.length > 0 ? (
            <div className="space-y-2" aria-label="Provider 自定义排序">
              {visibleProviders.map((provider) => (
                <div
                  key={provider.id}
                  data-provider-sort-id={provider.id}
                  className={cn(
                    "flex min-h-14 items-center gap-3 rounded-lg border bg-background px-3 py-2",
                    draggingProviderId === provider.id && "border-primary/50 bg-primary/5 opacity-70",
                  )}
                >
                  <button
                    type="button"
                    className="grid h-11 w-11 shrink-0 touch-none place-items-center rounded-md text-muted-foreground active:scale-95 active:bg-muted motion-reduce:transform-none"
                    aria-label={`拖动 ${provider.name} 排序`}
                    onPointerDown={(event) => {
                      event.preventDefault();
                      event.currentTarget.setPointerCapture(event.pointerId);
                      providerDragRef.current = provider.id;
                      setDraggingProviderId(provider.id);
                    }}
                    onPointerMove={(event) => {
                      const sourceId = providerDragRef.current;
                      if (sourceId == null) return;
                      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>("[data-provider-sort-id]");
                      const targetId = Number(target?.dataset.providerSortId);
                      if (Number.isInteger(targetId)) reorderProvider(sourceId, targetId);
                    }}
                    onPointerUp={(event) => {
                      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
                      providerDragRef.current = null;
                      setDraggingProviderId(null);
                    }}
                    onPointerCancel={() => {
                      providerDragRef.current = null;
                      setDraggingProviderId(null);
                    }}
                  >
                    <GripVertical className="h-5 w-5" />
                  </button>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-semibold">{provider.name}</div>
                    <div className="truncate font-mono text-xs text-muted-foreground">{provider.default_model}</div>
                  </div>
                  <MetaBadge>{(provider.models || []).filter((model) => model.enabled).length} 个模型</MetaBadge>
                </div>
              ))}
              <p className="text-xs text-muted-foreground">按住左侧手柄拖动卡片，完成后点击“保存排序”。</p>
            </div>
          ) : visibleProviders.length > 0 ? (
            <>
            <div className="hidden overflow-x-auto md:block">
            <Table className="min-w-[1180px]">
              <TableHeader>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>提供商协议</TableHead>
                  <TableHead>API 协议</TableHead>
                  <TableHead>执行后端</TableHead>
                  <TableHead>联网搜索协议</TableHead>
                  <TableHead>默认模型 ID</TableHead>
                  <TableHead>已启用模型</TableHead>
                  <TableHead>模态 / 推理成本档</TableHead>
                  <TableHead>标签</TableHead>
                  <TableHead>代理</TableHead>
                  <TableHead>API Key</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleProviders.map((p) => {
                  const enabledModels = (p.models || []).filter((m) => m.enabled);
                  return (
                    <TableRow key={p.id}>
                      <TableCell className="font-medium">{p.name}</TableCell>
                      <TableCell className="font-mono text-xs">{p.provider}</TableCell>
                      <TableCell className="space-y-1 text-xs">
                        <MetaBadge mono>{p.api_format || "chat_completions"}</MetaBadge>
                        {(p.protocol_profile || "standard") !== "standard" ? (
                          <div>
                            <MetaBadge mono tone={p.protocol_profile === "claude_code_proxy" ? "warn" : "neutral"}>
                              {p.protocol_profile || "standard"}
                            </MetaBadge>
                          </div>
                        ) : null}
                      </TableCell>
                      <TableCell>
                        <MetaBadge mono tone={isGatewayBackend(p.execution_backend) ? "info" : "neutral"}>
                          {executionBackendLabel(p.execution_backend)}
                        </MetaBadge>
                      </TableCell>
                      <TableCell className="text-xs">
                        <MetaBadge mono tone={(p.web_search_api_format || "auto") === "auto" ? "neutral" : "outline"}>
                          {p.web_search_api_format || "auto"}
                        </MetaBadge>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{p.default_model}</TableCell>
                      <TableCell>
                        <MetaBadge tone={enabledModels.length > 0 ? "neutral" : "warn"}>
                          {enabledModels.length} / {(p.models || []).length}
                        </MetaBadge>
                      </TableCell>
                      <TableCell className="space-x-1 text-xs">
                        <MetaBadge>{p.modality || "text"}</MetaBadge>
                        <MetaBadge>tier {p.cost_tier ?? 2}</MetaBadge>
                      </TableCell>
                      <TableCell className="space-x-1">
                        {(p.tags || []).length > 0 ? (
                          (p.tags || []).slice(0, 4).map((t) => (
                            <MetaBadge key={t}>
                              {t}
                            </MetaBadge>
                          ))
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                        {(p.tags || []).length > 4 ? (
                          <span className="text-xs text-muted-foreground">
                            +{(p.tags || []).length - 4}
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell>
                        {p.proxy_id != null ? (
                          proxyById.has(p.proxy_id) ? (
                            <MetaBadge mono>
                              {proxyById.get(p.proxy_id)!.type}://
                              {proxyById.get(p.proxy_id)!.host}:
                              {proxyById.get(p.proxy_id)!.port}
                            </MetaBadge>
                          ) : (
                            <MetaBadge tone="warn">
                              #{p.proxy_id} 已删除
                            </MetaBadge>
                          )
                        ) : (
                          <MetaBadge>
                            DIRECT
                          </MetaBadge>
                        )}
                      </TableCell>
                      <TableCell>
                        {p.has_api_key ? (
                          <MetaBadge tone="success">
                            <KeyRound className="h-3 w-3" /> 已配置
                          </MetaBadge>
                        ) : (
                          <MetaBadge>未配置</MetaBadge>
                        )}
                      </TableCell>
                      <TableCell className="space-x-2 text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`编辑 ${p.name}`}
                          title={`编辑 ${p.name}`}
                          onClick={() => onEdit(p)}
                        >
                          <Edit3 className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`删除 ${p.name}`}
                          title={`删除 ${p.name}`}
                          disabled={deleteMut.isPending}
                          onClick={() => {
                            if (confirm(`确认删除模型提供商「${p.name}」？引用此模型提供商的 AI 指令将失败`)) {
                              deleteMut.mutate(p.id);
                            }
                          }}
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            </div>
            <div className="space-y-3 md:hidden">
              {visibleProviders.map((p) => (
                <ProviderMobileCard
                  key={p.id}
                  provider={p}
                  proxyById={proxyById}
                  deletePending={deleteMut.isPending}
                  onEdit={() => onEdit(p)}
                  onDelete={() => {
                    if (confirm(`确认删除模型提供商「${p.name}」？引用此模型提供商的 AI 指令将失败`)) {
                      deleteMut.mutate(p.id);
                    }
                  }}
                />
              ))}
            </div>
            </>
          ) : (
            <div className="rounded-md border border-dashed bg-muted/20 px-4 py-8 text-center">
              <p className="text-sm text-muted-foreground">
                {isVisionFilter
                  ? "当前没有视觉或多模态模型提供商。可新建一个，或编辑已有 provider 的 modality。"
                  : "尚未配置任何模型提供商。新建一个后，就能在「自定义指令」里创建 AI 类型指令"}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      <IdentityVersionsDialog open={identityVersionsOpen} onOpenChange={setIdentityVersionsOpen} />
      <GatewayStatusDialog open={gatewayStatusOpen} onOpenChange={setGatewayStatusOpen} />
    </div>
  );
}

function ProviderMobileCard({
  provider,
  proxyById,
  deletePending,
  onEdit,
  onDelete,
}: {
  provider: LLMProviderOut;
  proxyById: Map<number, ProxyOut>;
  deletePending: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const enabledModels = (provider.models || []).filter((m) => m.enabled);
  const proxy = provider.proxy_id != null ? proxyById.get(provider.proxy_id) : null;
  return (
    <div data-provider-card className="rounded-xl border border-border/70 bg-background/70 p-3">
      <div className="space-y-2">
        <div className="min-w-0 break-words text-sm font-semibold">{provider.name}</div>
        <div className="flex flex-wrap gap-1.5">
          <MetaBadge tone={provider.has_api_key ? "success" : "warn"}>
            <KeyRound className="h-3 w-3" />
            {provider.has_api_key ? "已配置" : "未配置"}
          </MetaBadge>
          <MetaBadge mono>{provider.provider}</MetaBadge>
          <MetaBadge mono>{provider.api_format || "chat_completions"}</MetaBadge>
          <MetaBadge mono tone={isGatewayBackend(provider.execution_backend) ? "info" : "neutral"}>
            {executionBackendLabel(provider.execution_backend)}
          </MetaBadge>
          {(provider.protocol_profile || "standard") !== "standard" ? (
            <MetaBadge mono tone={provider.protocol_profile === "claude_code_proxy" ? "warn" : "neutral"}>
              {provider.protocol_profile || "standard"}
            </MetaBadge>
          ) : null}
          <MetaBadge tone={(provider.web_search_api_format || "auto") === "auto" ? "neutral" : "outline"} mono>
            搜索 {provider.web_search_api_format || "auto"}
          </MetaBadge>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
        <MobileInfo label="默认模型" value={provider.default_model || "-"} mono />
        <MobileInfo label="启用模型" value={`${enabledModels.length} / ${(provider.models || []).length}`} />
        <MobileInfo label="模态 / 成本" value={`${provider.modality || "text"} · tier ${provider.cost_tier ?? 2}`} />
        <MobileInfo
          label="代理"
          value={provider.proxy_id == null ? "DIRECT" : proxy ? `${proxy.type}://${proxy.host}:${proxy.port}` : `#${provider.proxy_id} 已删除`}
          mono={provider.proxy_id != null}
        />
      </div>
      {(provider.tags || []).length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {(provider.tags || []).slice(0, 6).map((tag) => (
            <MetaBadge key={tag}>{tag}</MetaBadge>
          ))}
          {(provider.tags || []).length > 6 ? <MetaBadge>+{(provider.tags || []).length - 6}</MetaBadge> : null}
        </div>
      ) : null}
      <div className="mt-3 flex justify-end gap-2">
        <Button variant="outline" size="sm" onClick={onEdit}>
          <Edit3 className="mr-1 h-4 w-4" />
          编辑
        </Button>
        <Button variant="outline" size="sm" className="border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive" disabled={deletePending} onClick={onDelete}>
          <Trash2 className="mr-1 h-4 w-4" />
          删除
        </Button>
      </div>
    </div>
  );
}

function MobileInfo({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0 rounded-lg border border-border/70 bg-muted/30 px-3 py-2">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={cn("mt-1 break-words text-xs font-medium", mono && "font-mono")}>{value}</div>
    </div>
  );
}

const CREATE_STAGE_COPY: Record<ProviderCreateStage, string> = {
  empty: "待获取模型",
  fetching: "正在获取模型",
  select: "请选择模型",
  selected: "可以开始验证",
  verifying: "正在流式验证",
  verified: "验证通过",
};

function inferredProviderName(baseUrl: string, model: string) {
  const value = baseUrl.trim();
  if (value) {
    try {
      const hostname = new URL(value).hostname.replace(/^api\./, "");
      const brand = hostname.split(".")[0];
      if (brand) return `${brand}-${model.split("-")[0] || "provider"}`.slice(0, 64);
    } catch {
      // 非标准 URL 留给模型名兜底。
    }
  }
  return `${model.split("-").slice(0, 2).join("-") || "model"}-provider`.slice(0, 64);
}

function ProviderCreateWorkspace({
  form,
  onChange,
  onCancel,
  onSave,
  saving,
  verified,
  onVerificationChange,
  stage,
  onStageChange,
  proxies,
  proxiesLoading,
  protocolDetection,
  detectingProtocol,
  onDetectProtocol,
  commandPrefix,
}: {
  form: FormState;
  onChange: (form: FormState) => void;
  onCancel: () => void;
  onSave: () => void;
  saving: boolean;
  verified: boolean;
  onVerificationChange: (verified: boolean) => void;
  stage: ProviderCreateStage;
  onStageChange: (stage: ProviderCreateStage) => void;
  proxies: ProxyOut[];
  proxiesLoading: boolean;
  protocolDetection: DetectProviderProtocolsResponse | null;
  detectingProtocol: boolean;
  onDetectProtocol: () => void;
  commandPrefix: string;
}) {
  const isEdit = Boolean(form.id);
  const setField = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    onChange({ ...form, [key]: value });
  const connectionLocked = (!isEdit && (stage === "fetching" || stage === "verifying")) || saving;
  const [activeStep, setActiveStep] = useState(1);
  const connectSectionRef = useRef<HTMLElement>(null);
  const verifySectionRef = useRef<HTMLDivElement>(null);
  const saveSectionRef = useRef<HTMLElement>(null);
  const defaultBaseUrl = DEFAULT_BASE_URLS[form.provider];
  const endpoint = form.base_url.trim() || defaultBaseUrl;
  const enabledModelCount = form.models.filter((model) => model.enabled).length;
  const gatewayMode = isGatewayBackend(form.execution_backend);
  const gatewayHealthQ = useQuery({
    queryKey: ["system", "health-overview"],
    queryFn: getHealthOverview,
    enabled: gatewayMode,
    staleTime: 5_000,
    refetchOnWindowFocus: true,
  });
  const gatewayHealth = gatewayHealthQ.data?.codex_gateway;
  const gatewayHasApiKey = Boolean(form.api_key.trim())
    || (isEdit && Boolean(form.hasApiKey) && !form.clearKey);
  const gatewayBlocked = gatewayMode && (
    gatewayHealthQ.isLoading ||
    gatewayHealthQ.isError ||
    !gatewayHasApiKey
  );

  useEffect(() => {
    const root = document.querySelector<HTMLElement>("[data-app-main]");
    const sections = [connectSectionRef.current, verifySectionRef.current, saveSectionRef.current]
      .filter((section): section is HTMLElement => Boolean(section));
    if (!root || sections.length !== 3) return;
    const updateStepFromScroll = () => {
      const anchor = root.getBoundingClientRect().top + Math.min(160, root.clientHeight * 0.28);
      let nextStep = 1;
      sections.forEach((section, index) => {
        if (section.getBoundingClientRect().top <= anchor) nextStep = index + 1;
      });
      setActiveStep(nextStep);
    };
    root.addEventListener("scroll", updateStepFromScroll, { passive: true });
    window.addEventListener("resize", updateStepFromScroll);
    updateStepFromScroll();
    return () => {
      root.removeEventListener("scroll", updateStepFromScroll);
      window.removeEventListener("resize", updateStepFromScroll);
    };
  }, []);

  const setApiFormat = (apiFormat: LLMApiFormat) => {
    if (gatewayMode) return;
    const provider: LLMProviderKind =
      apiFormat === "anthropic_messages" ? "anthropic" : form.provider === "anthropic" ? "openai" : form.provider;
    onChange({
      ...form,
      provider,
      api_format: apiFormat,
      protocol_profile: protocolProfileForFormat(apiFormat, form.protocol_profile),
      client_identity_profile: "auto",
      ...(!isEdit ? { default_model: "", models: [] } : {}),
    });
    onVerificationChange(false);
  };

  const setExecutionBackend = (executionBackend: LLMExecutionBackend) => {
    onChange(applyExecutionBackend(form, executionBackend) as FormState);
    onVerificationChange(false);
  };

  return (
    <div className="mx-auto w-full max-w-7xl pb-6">
      <header className="pb-3">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="-ml-2 mb-3 text-muted-foreground"
          onClick={onCancel}
        >
          <ArrowLeft className="mr-1 h-4 w-4" /> 返回模型提供商
        </Button>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">{isEdit ? "编辑" : "新建"}模型提供商</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {isEdit
                ? "更新接入信息、批量管理模型，然后一次保存。"
                : "连接、选模型、验证，然后一次保存。"}
            </p>
          </div>
          <MetaBadge tone={!isEdit && verified ? "success" : !isEdit && (stage === "verifying" || stage === "fetching") ? "info" : "outline"}>
            {isEdit ? "编辑中" : verified ? "可保存" : CREATE_STAGE_COPY[stage]}
          </MetaBadge>
        </div>
      </header>
      <ol className="sticky top-0 z-20 grid grid-cols-3 gap-2 border-b bg-background/95 px-0.5 py-2 backdrop-blur" aria-label="创建步骤">
          {[
            { step: 1, label: "接入信息", compactLabel: "接入信息" },
            { step: 2, label: isEdit ? "管理模型" : "选择模型并验证", compactLabel: isEdit ? "模型管理" : "模型与验证" },
            { step: 3, label: "保存", compactLabel: "保存" },
          ].map(({ step, label, compactLabel }) => {
            const complete = isEdit ? step < activeStep : step < activeStep || verified;
            const active = step === activeStep;
            return (
              <li
                key={step}
                className={cn(
                  "flex min-w-0 flex-col gap-1 text-[10px] leading-3 transition-colors sm:text-[11px]",
                  active ? "text-foreground" : complete ? "text-foreground/80" : "text-muted-foreground",
                )}
              >
                <span
                  className={cn(
                    "h-0.5 w-full rounded-full bg-border transition-colors",
                    complete && "bg-primary/50",
                    active && "bg-primary",
                  )}
                />
                <span className="min-w-0 truncate px-0.5">
                  <span className="sm:hidden">{compactLabel}</span>
                  <span className="hidden sm:inline">{label}</span>
                </span>
              </li>
            );
          })}
      </ol>

      <div className="mt-5 rounded-lg border bg-card p-3 lg:hidden" aria-label="当前配置摘要">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <span className="font-medium">当前配置</span>
          <span className="text-muted-foreground">{form.api_format}</span>
          <span className="text-muted-foreground">{form.client_identity_profile}</span>
          <span className="max-w-full break-all font-mono">{form.default_model || "待选择模型"}</span>
        </div>
      </div>

      <div className="mt-5 grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_260px]">
        <main className="min-w-0">
          <section ref={connectSectionRef} className="pb-6" aria-labelledby="provider-connect-title">
            <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 id="provider-connect-title" className="text-base font-semibold">接入信息</h2>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  {isEdit
                    ? "修改会在保存后生效；模型列表使用当前表单里的接入参数读取。"
                    : "填写真实接入参数，先读取模型列表，不会自动挑选模型发起请求。"}
                </p>
              </div>
              <MetaBadge tone={!isEdit && stage === "verified" ? "success" : !isEdit && (stage === "fetching" || stage === "verifying") ? "info" : "outline"}>
                {isEdit ? "已载入当前配置" : CREATE_STAGE_COPY[stage]}
              </MetaBadge>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5 sm:col-span-2">
                <Label htmlFor="provider-create-execution-backend">执行后端</Label>
                <Select
                  id="provider-create-execution-backend"
                  value={form.execution_backend}
                  disabled={connectionLocked}
                  onChange={(event) => setExecutionBackend(event.target.value as LLMExecutionBackend)}
                >
                  <option value="direct">Provider 直连</option>
                  <option value="codex_gateway">内置 Codex Gateway</option>
                </Select>
                {gatewayMode ? (
                  <div className={cn(
                    "rounded-md border px-3 py-2 text-xs leading-5",
                    gatewayHealthQ.isError || gatewayHealth?.state === "degraded"
                      ? "border-destructive/40 bg-destructive/5 text-destructive"
                      : "border-primary/25 bg-primary/[0.04] text-muted-foreground",
                  )}>
                    {gatewayHealthQ.isLoading ? (
                      <span className="inline-flex items-center gap-2"><Spinner className="h-3.5 w-3.5 text-primary" />正在读取 Gateway 状态…</span>
                    ) : gatewayHealthQ.isError ? (
                      "无法读取 Gateway 状态，暂不能保存 Gateway Provider。"
                    ) : gatewayHealth?.state === "degraded" ? (
                      gatewayHealth.error || "内置 Gateway 当前不可用；Provider 直连不受影响。"
                    ) : gatewayHealth?.state === "ready" ? (
                      `Gateway 已就绪${gatewayHealth.version ? ` · ${gatewayHealth.version}` : ""}。身份由 Gateway 管理。`
                    ) : (
                      "当前无需运行 Gateway；保存时会验证内置二进制并按需启动。身份由 Gateway 管理。"
                    )}
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground">由 TelePilot 直接调用当前 Provider API，并使用所选身份档案与出口代理。</p>
                )}
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <Label htmlFor="provider-create-base-url">Base URL</Label>
                <Input
                  id="provider-create-base-url"
                  value={form.base_url}
                  maxLength={255}
                  disabled={connectionLocked}
                  placeholder={defaultBaseUrl}
                  onChange={(event) => setField("base_url", event.target.value)}
                />
                <p className="text-xs text-muted-foreground">留空使用当前服务类型的默认地址。</p>
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <Label htmlFor="provider-create-api-key">API Key</Label>
                <ApiKeyInput
                  id="provider-create-api-key"
                  value={form.api_key}
                  autoComplete="off"
                  disabled={connectionLocked || (isEdit && form.clearKey)}
                  placeholder={isEdit && form.hasApiKey && !form.api_key ? MASKED_SECRET_PLACEHOLDER : isEdit ? "留空，保持原 Key 不变" : "sk-..."}
                  onChange={(value) => setField("api_key", value)}
                  hasStoredValue={isEdit && Boolean(form.hasApiKey)}
                  revealStoredValue={
                    isEdit && form.id
                      ? async () => (await revealLLMProviderApiKey(form.id!)).api_key
                      : undefined
                  }
                />
                {isEdit ? (
                  <div className="flex items-center gap-2 pt-1 text-xs">
                    <Switch
                      id="provider-workspace-clear-key"
                      checked={form.clearKey}
                      disabled={connectionLocked}
                      onCheckedChange={(checked) =>
                        onChange({
                          ...form,
                          clearKey: checked,
                          api_key: checked ? "" : form.api_key,
                        })
                      }
                    />
                    <Label htmlFor="provider-workspace-clear-key" className="font-normal text-muted-foreground">
                      保存时清空已存 API Key
                    </Label>
                  </div>
                ) : null}
                <p className="text-xs text-muted-foreground">
                  {isEdit
                    ? "留空不会覆盖已保存的 Key；点击眼睛按需查看。"
                    : "保存时加密落库；点击右侧眼睛可临时查看当前填写内容。"}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="provider-create-api-format">协议</Label>
                <Select
                  id="provider-create-api-format"
                  value={form.api_format}
                  disabled={connectionLocked || gatewayMode}
                  onChange={(event) => setApiFormat(event.target.value as LLMApiFormat)}
                >
                  {API_FORMAT_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </Select>
                <p className="text-xs leading-5 text-muted-foreground">
                  {gatewayMode
                    ? "Gateway 固定使用 Responses 协议。"
                    : API_FORMAT_OPTIONS.find((option) => option.value === form.api_format)?.hint}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="provider-create-client">客户端身份</Label>
                <Select
                  id="provider-create-client"
                  value={form.client_identity_profile}
                  disabled={connectionLocked || gatewayMode}
                  onChange={(event) => setField("client_identity_profile", event.target.value as LLMClientIdentityProfile)}
                >
                  {CLIENT_IDENTITY_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>
                  ))}
                </Select>
                <p className="text-xs leading-5 text-muted-foreground">
                  {gatewayMode
                    ? "身份由 Gateway 管理；当前已保存的身份配置会保留，切回 Provider 直连后继续使用。"
                    : CLIENT_IDENTITY_OPTIONS.find((option) => option.value === form.client_identity_profile)?.hint}
                </p>
              </div>
            </div>

            <details className="group mt-4 rounded-md border bg-muted/20">
              <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2.5 text-sm font-medium [&::-webkit-details-marker]:hidden">
                <ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" />
                Provider 兼容请求头
                {form.request_headers.length > 0 ? (
                  <MetaBadge>{form.request_headers.length}</MetaBadge>
                ) : null}
              </summary>
              <div className="border-t p-3">
                <RequestHeadersEditor
                  headers={form.request_headers}
                  provider={form.provider}
                  baseUrl={form.base_url}
                  disabled={connectionLocked}
                  onChange={(headers) => setField("request_headers", headers)}
                />
              </div>
            </details>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                loading={detectingProtocol}
                disabled={gatewayMode || connectionLocked || (!form.api_key.trim() && !form.hasApiKey && form.provider !== "ollama")}
                onClick={onDetectProtocol}
              >
                {!detectingProtocol ? <Download className="mr-1 h-4 w-4" /> : null}
                自动检测协议
              </Button>
              <span className="text-xs text-muted-foreground">
                {gatewayMode ? "Gateway 已固定 Responses/Codex 档案，无需自动检测。" : "不确定兼容协议时再检测，默认使用 Responses + Codex CLI。"}
              </span>
            </div>
            {protocolDetection ? <div className="mt-3"><ProtocolDetectionPanel result={protocolDetection} /></div> : null}
          </section>

          <div ref={verifySectionRef}>
            {isEdit ? (
              <section className="border-t pt-6" aria-labelledby="provider-model-management-title">
                <div className="mb-4">
                  <h2 id="provider-model-management-title" className="text-base font-semibold">模型管理</h2>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    可同时启用多个模型，并单独指定默认模型；所有变更会随本次保存一起生效。
                  </p>
                </div>
                <ProviderModelsSection
                  providerId={form.id ?? null}
                  models={form.models}
                  requestHeaders={form.request_headers}
                  defaultModel={form.default_model}
                  onModelsChange={(models) => setField("models", models)}
                  onSetDefault={(model) => setField("default_model", model)}
                  providerKind={form.provider}
                  apiFormat={form.api_format}
                  protocolProfile={form.protocol_profile}
                  executionBackend={form.execution_backend}
                  baseUrl={form.base_url}
                  apiKey={form.api_key}
                  proxyId={form.proxy_id}
                />
              </section>
            ) : (
            <ProviderCreateVerification
              providerKind={form.provider}
              executionBackend={form.execution_backend}
              apiFormat={form.api_format}
              protocolProfile={form.protocol_profile}
              clientIdentityProfile={form.client_identity_profile}
              baseUrl={form.base_url}
              apiKey={form.api_key}
              proxyId={form.proxy_id}
              models={form.models}
              requestHeaders={form.request_headers}
              onModelsChange={(models) => setField("models", models)}
              onReset={() => onChange({ ...form, models: [], default_model: "" })}
              onVerified={(model, models) =>
                onChange({
                  ...form,
                  name: form.name.trim() || inferredProviderName(endpoint, model),
                  models,
                  default_model: model,
                })
              }
              onVerificationChange={onVerificationChange}
              onStageChange={onStageChange}
            />
            )}
          </div>

          <section ref={saveSectionRef} className="border-t py-6" aria-labelledby="provider-save-title">
            <div className="mb-4">
              <h2 id="provider-save-title" className="text-base font-semibold">保存信息</h2>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {isEdit
                  ? "名称和默认模型会与上方模型启用状态一起保存。"
                  : "验证通过会自动补全；验证未通过也可手动填写名称和默认模型后保存。"}
              </p>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="provider-create-name">Provider 名称 *</Label>
                <Input
                  id="provider-create-name"
                  value={form.name}
                  maxLength={64}
                  placeholder="验证后自动生成，也可以自行填写"
                  onChange={(event) => setField("name", event.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="provider-create-default-model">默认模型</Label>
                <Input
                  id="provider-create-default-model"
                  value={form.default_model}
                  placeholder="例如 gpt-5.6-sol"
                  className="font-mono"
                  onChange={(event) => setField("default_model", event.target.value)}
                />
              </div>
            </div>
          </section>

          <details className="group border-t py-5">
            <summary className="cursor-pointer list-none text-sm font-semibold [&::-webkit-details-marker]:hidden">
              <span className="inline-flex items-center gap-2">
                <ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" />
                高级设置与路由策略（可选）
              </span>
              <span className="mt-1 block pl-6 text-xs font-normal leading-5 text-muted-foreground">
                服务类型、联网协议、出口代理和自动路由标签，普通接入保持默认即可。
              </span>
            </summary>
            <div className="mt-4 space-y-4 rounded-lg border bg-muted/20 p-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label>服务类型</Label>
                  <Select disabled={connectionLocked} value={form.provider} onChange={(event) => setField("provider", event.target.value as LLMProviderKind)}>
                    <option value="openai">OpenAI 兼容服务</option>
                    <option value="anthropic">Anthropic</option>
                    <option value="ollama">Ollama 本地服务</option>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>出口代理</Label>
                  {proxiesLoading ? (
                    <div className="flex h-10 items-center gap-2 rounded-md border px-3 text-xs text-muted-foreground"><Spinner className="text-primary" />加载中…</div>
                  ) : (
                    <Select disabled={connectionLocked} value={form.proxy_id} onChange={(event) => setField("proxy_id", event.target.value)}>
                      <option value="">DIRECT，不走代理</option>
                      {proxies.map((proxy) => (
                        <option key={proxy.id} value={String(proxy.id)}>#{proxy.id} · {proxy.type} · {proxy.host}:{proxy.port}</option>
                      ))}
                    </Select>
                  )}
                </div>
                <div className="space-y-1.5">
                  <Label>联网搜索协议</Label>
                  <Select disabled={connectionLocked || gatewayMode} value={form.web_search_api_format} onChange={(event) => setField("web_search_api_format", event.target.value as LLMWebSearchApiFormat)}>
                    {WEB_SEARCH_API_FORMAT_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </Select>
                  {gatewayMode ? (
                    <p className="text-xs leading-5 text-muted-foreground">Gateway 的联网请求固定使用 Responses；切回 Provider 直连后恢复原设置。</p>
                  ) : null}
                </div>
                <div className="space-y-1.5">
                  <Label>模态</Label>
                  <Select value={form.modality} onChange={(event) => setField("modality", event.target.value as LLMModality)}>
                    {MODALITY_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>推理成本档</Label>
                  <Select value={String(form.cost_tier)} onChange={(event) => setField("cost_tier", Number(event.target.value))}>
                    {COST_TIER_OPTIONS.map((option) => (
                      <option key={option.value} value={String(option.value)}>{option.label}</option>
                    ))}
                  </Select>
                </div>
                {form.api_format !== "chat_completions" ? (
                  <div className="space-y-1.5">
                    <Label>协议档案</Label>
                    <Select disabled={connectionLocked || gatewayMode} value={form.protocol_profile} onChange={(event) => setField("protocol_profile", event.target.value as LLMProtocolProfile)}>
                      {PROTOCOL_PROFILE_OPTIONS[form.api_format].map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </Select>
                    <p className="text-xs leading-5 text-muted-foreground">
                      {gatewayMode
                        ? "Gateway 固定使用 Codex Responses 档案。"
                        : PROTOCOL_PROFILE_OPTIONS[form.api_format].find((option) => option.value === form.protocol_profile)?.hint}
                    </p>
                  </div>
                ) : null}
                {gatewayMode && !gatewayHasApiKey ? (
                  <p className="text-xs leading-5 text-destructive">Gateway Provider 必须配置 API Key。</p>
                ) : null}
              </div>
              <div className="space-y-2">
                <Label>自动路由标签</Label>
                <div className="flex flex-wrap gap-1.5">
                  {TAG_OPTIONS.map((option) => {
                    const selected = form.tags.includes(option.value);
                    return (
                      <button
                        key={option.value}
                        type="button"
                        title={option.hint}
                        className={cn(
                          "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
                          selected ? "border-primary bg-primary text-primary-foreground" : "border-border bg-background hover:bg-muted",
                        )}
                        onClick={() => setField("tags", selected ? form.tags.filter((tag) => tag !== option.value) : [...form.tags, option.value])}
                      >
                        {option.label}
                      </button>
                    );
                  })}
                </div>
                <p className="text-xs leading-5 text-muted-foreground">
                  只影响自动路由模式下的 {commandPrefix}ai 分配，fixed 模式不会读取这些标签。
                </p>
              </div>
              <div className="space-y-1.5">
                <Label>备注</Label>
                <Textarea value={form.notes} rows={2} maxLength={500} placeholder="仅自己可见，不参与路由判断" onChange={(event) => setField("notes", event.target.value)} />
              </div>
            </div>
          </details>

          <div className="sticky bottom-0 z-10 -mx-3 mt-2 flex items-center justify-end gap-2 border-t bg-background/95 px-3 py-3 backdrop-blur sm:static sm:mx-0 sm:bg-transparent sm:px-0 sm:pt-5 sm:backdrop-blur-none">
            {!isEdit && !verified ? <span className="mr-auto hidden text-xs text-warning sm:inline">尚未通过真实验证，保存后请尽快测活。</span> : null}
            <Button type="button" variant="outline" disabled={saving} onClick={onCancel}>取消</Button>
            <Button
              type="button"
              loading={saving}
              disabled={!form.name.trim() || !form.default_model.trim() || gatewayBlocked}
              onClick={() => {
                if (!isEdit && !verified && !window.confirm("当前 Provider 尚未通过真实模型验证。仍要保存吗？保存后建议立即进入模型测活确认可用性。")) return;
                onSave();
              }}
            >
              {!saving ? <Save className="mr-2 h-4 w-4" /> : null}
              {isEdit ? "保存修改" : "保存 Provider"}
            </Button>
          </div>
        </main>

        <aside className="sticky top-4 hidden space-y-4 rounded-lg border bg-card p-4 shadow-sm lg:block" aria-label="Provider 配置摘要">
          <div>
            <h2 className="text-sm font-semibold">{isEdit ? "当前编辑" : "即将创建"}</h2>
            <p className="mt-1 text-xs text-muted-foreground">{isEdit ? "保存后统一生效。" : "验证通过后自动补全。"}</p>
          </div>
          <dl className="space-y-3 text-xs">
            <div><dt className="text-muted-foreground">接入地址</dt><dd className="mt-1 break-all font-mono">{endpoint}</dd></div>
            <div><dt className="text-muted-foreground">实际协议</dt><dd className="mt-1 font-medium">{form.api_format}</dd></div>
            <div><dt className="text-muted-foreground">执行后端</dt><dd className="mt-1 font-medium">{executionBackendLabel(form.execution_backend)}</dd></div>
            <div><dt className="text-muted-foreground">客户端身份</dt><dd className="mt-1 font-medium">{gatewayMode ? "由 Gateway 管理" : form.client_identity_profile}</dd></div>
            <div><dt className="text-muted-foreground">默认模型</dt><dd className="mt-1 break-all font-mono">{form.default_model || "待选择"}</dd></div>
            <div><dt className="text-muted-foreground">启用模型</dt><dd className="mt-1 font-medium">{enabledModelCount} 个</dd></div>
          </dl>
          <p className="border-t pt-3 text-xs leading-5 text-muted-foreground">
            {isEdit ? "未保存前不会改变线上 Provider。" : "路由策略沿用安全默认值，不阻塞首次接入。"}
          </p>
        </aside>
      </div>
    </div>
  );
}

function ProviderEditDialog({
  form,
  onChange,
  onCancel,
  onSave,
  saving,
}: {
  form: FormState;
  onChange: (s: FormState) => void;
  onCancel: () => void;
  onSave: () => void;
  saving: boolean;
}) {
  const isEdit = !!form.id;
  const initialFormRef = useRef(JSON.stringify(form));
  const dirty = JSON.stringify(form) !== initialFormRef.current;
  useUnsavedChanges(dirty);
  const requestCancel = () => {
    if (saving || !confirmDiscardChanges(dirty)) return;
    onCancel();
  };
  const setField = <K extends keyof FormState>(k: K, v: FormState[K]) =>
    onChange({ ...form, [k]: v });

  // 列出所有代理；mtproxy 不能给 LLM 用，前端做硬过滤；
  // 后端 service 层有同样的拒绝逻辑兜底
  const proxiesQ = useQuery({
    queryKey: ["proxies-for-llm"],
    queryFn: listProxies,
  });
  const llmUsableProxies: ProxyOut[] = (proxiesQ.data || []).filter(
    (p) => (p.type || "").toLowerCase() !== "mtproxy",
  );
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const cmdPrefix = settingsQ.data?.command_prefix || ",";
  const [protocolDetection, setProtocolDetection] = useState<DetectProviderProtocolsResponse | null>(null);
  const [createVerified, setCreateVerified] = useState(isEdit);
  const [createStage, setCreateStage] = useState<ProviderCreateStage>("empty");

  const detectProtocolsMut = useMutation({
    mutationFn: () =>
      detectProviderProtocols({
        provider: form.provider,
        base_url: form.base_url ? form.base_url.trim() : null,
        api_key: form.api_key ? form.api_key : null,
        proxy_id: form.proxy_id ? Number(form.proxy_id) : null,
        pid: form.id ?? null,
        model: form.default_model.trim() || null,
        request_headers: requestHeadersPayload(form.request_headers),
      }),
    onSuccess: (resp) => {
      setProtocolDetection(resp);
      if (resp.recommended_api_format) {
        const recommendedApiFormat = resp.recommended_api_format as LLMApiFormat;
        onChange({
          ...form,
          provider: !isEdit
            ? recommendedApiFormat === "anthropic_messages"
              ? "anthropic"
              : form.provider === "anthropic"
                ? "openai"
                : form.provider
            : form.provider,
          api_format: recommendedApiFormat,
          protocol_profile: protocolProfileForFormat(
            recommendedApiFormat,
            (resp.recommended_protocol_profile as LLMProtocolProfile) || "standard",
          ),
          web_search_api_format: (resp.recommended_web_search_api_format || "auto") as LLMWebSearchApiFormat,
          client_identity_profile:
            (resp.recommended_client_identity_profile as LLMClientIdentityProfile) ||
            form.client_identity_profile,
          ...(!isEdit ? { default_model: "", models: [] } : {}),
        });
        toast.success("已检测并填入推荐协议与客户端身份");
      } else {
        toast.warning("没有检测到推荐协议，请查看探测详情");
      }
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  return (
    <ProviderCreateWorkspace
      form={form}
      onChange={onChange}
      onCancel={requestCancel}
      onSave={onSave}
      saving={saving}
      verified={createVerified}
      onVerificationChange={setCreateVerified}
      stage={createStage}
      onStageChange={setCreateStage}
      proxies={llmUsableProxies}
      proxiesLoading={proxiesQ.isLoading}
      protocolDetection={protocolDetection}
      detectingProtocol={detectProtocolsMut.isPending}
      onDetectProtocol={() => detectProtocolsMut.mutate()}
      commandPrefix={cmdPrefix}
    />
  );

}

function ProtocolDetectionPanel({ result }: { result: DetectProviderProtocolsResponse }) {
  return (
    <div className="rounded-md border bg-muted/30 p-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="font-medium text-foreground">协议检测结果</div>
        {result.recommended_api_format ? (
          <div className="text-muted-foreground">
            推荐：<MetaBadge mono>{result.recommended_api_format}</MetaBadge>
            {result.recommended_client_identity_profile ? (
              <>
                {" "}· 身份{" "}
                <MetaBadge mono>{result.recommended_client_identity_profile}</MetaBadge>
              </>
            ) : null}
            {" "}· 联网{" "}
            <MetaBadge mono>
              {result.recommended_web_search_api_format || "auto"}
            </MetaBadge>
          </div>
        ) : null}
      </div>
      {result.note ? <p className="mt-1 text-muted-foreground">{result.note}</p> : null}
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <ProbeRow label="models" probe={result.models} />
        <ProbeRow label="chat/completions" probe={result.chat_completions} />
        <ProbeRow label="responses" probe={result.responses} />
        <ProbeRow label="anthropic/messages" probe={result.anthropic_messages} />
      </div>
      {result.identity_attempts && result.identity_attempts.length > 0 ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-muted-foreground">
            身份尝试详情（{result.identity_attempts.length}）
          </summary>
          <div className="mt-1.5 space-y-1">
            {result.identity_attempts.map((a, i) => (
              <div
                key={`${a.api_format}-${a.client_identity_profile}-${i}`}
                className="flex flex-wrap items-center gap-1.5 rounded-md border bg-background px-2 py-1"
              >
                <MetaBadge mono>{a.api_format}</MetaBadge>
                <MetaBadge mono>{a.client_identity_profile}</MetaBadge>
                <MetaBadge mono tone={a.ok ? "success" : "warn"}>
                  {a.ok ? "OK" : a.status_code ? `HTTP ${a.status_code}` : "FAIL"}
                </MetaBadge>
                {a.error_category ? (
                  <span className="text-muted-foreground">{a.error_category}</span>
                ) : null}
                {a.suggestion ? (
                  <span className="w-full break-words text-muted-foreground">{a.suggestion}</span>
                ) : null}
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}

function ProbeRow({ label, probe }: { label: string; probe: ProtocolProbeResult }) {
  return (
    <div className="rounded-md border bg-background px-2 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono">{label}</span>
        <MetaBadge mono tone={probe.ok ? "success" : "warn"}>
          {probe.ok ? "OK" : probe.status_code ? `HTTP ${probe.status_code}` : "FAIL"}
        </MetaBadge>
      </div>
      <div className="mt-1 text-muted-foreground">{probe.latency_ms} ms</div>
      {probe.error ? <div className="mt-1 break-words text-muted-foreground">{probe.error}</div> : null}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// ProviderModelsSection：候选模型清单 + Fetch + 自定义添加 + 对话测活
// ═══════════════════════════════════════════════════════════
//
// 设计：
// - models 是 form 的本地状态；toggle / 删除 / 自定义添加都改本地，最终随"保存"PATCH 落库
// - "Fetch 模型列表"现在直接读编辑表单当前值（provider/base_url/api_key/api_format/proxy_id）
//   走 ``/fetch-models-preview`` 预览端点，不需要先保存；新增模型 merge 到 form.models 本地。
// - 单模型测活在后台复用真实对话接口并原地返回结果；未保存的 provider
//   （form.id 为空）按钮置灰 + 提示"先保存"。
// - 模型按 enabled 拆两段：启用的常驻显示；未启用的默认折叠隐藏，点击展开。
function ProviderModelsSection({
  providerId,
  models,
  defaultModel,
  onModelsChange,
  onSetDefault,
  providerKind,
  apiFormat,
  protocolProfile,
  executionBackend,
  baseUrl,
  apiKey,
  proxyId,
  requestHeaders,
}: {
  providerId: number | null;
  models: ProviderModel[];
  defaultModel: string;
  onModelsChange: (next: ProviderModel[]) => void;
  onSetDefault: (id: string) => void;
  providerKind: LLMProviderKind;
  apiFormat: LLMApiFormat;
  protocolProfile: LLMProtocolProfile;
  executionBackend: LLMExecutionBackend;
  baseUrl: string;
  apiKey: string;
  proxyId: string;
  requestHeaders: FormRequestHeader[];
}) {
  const [customId, setCustomId] = useState("");
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, ChatTestModelResult>>({});
  const testAbortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      testAbortRef.current?.abort();
      testAbortRef.current = null;
    };
  }, []);
  // 未启用模型组：默认折叠（仅当存在已启用模型时；如果一条都没启用，
  // 用户一进来就需要看到全部，强制展开避免"看着是空的"）
  const enabledCount = models.filter((m) => m.enabled).length;
  const [showDisabled, setShowDisabled] = useState<boolean>(false);

  const persisted = providerId !== null;

  // 把后端拉到的 ID 列表合并进 form.models，逻辑与后端 fetch_models 一致：
  // - 已存在的条目保留 enabled / label，custom 改 false（fetch 拿到了说明不是用户瞎填）
  // - 新条目默认 enabled=false
  // - 老的 fetch 来的（非 custom）但本次没拿到 → 视为已下架，丢弃
  // - 老的 custom 条目 → 永远保留
  const mergeFetched = (newIds: string[]) => {
    const existing = new Map(models.map((m) => [m.id, m]));
    const fetched = new Set(newIds);
    const merged: ProviderModel[] = [];
    for (const mid of newIds) {
      const old = existing.get(mid);
      if (old) {
        merged.push({
          ...old,
          id: mid,
          enabled: !!old.enabled,
          custom: false,
          label: old.label ?? null,
        });
      } else {
        merged.push({ id: mid, enabled: false, custom: false, label: null });
      }
    }
    for (const m of models) {
      if (!fetched.has(m.id) && m.custom) {
        merged.push(m);
      }
    }
    onModelsChange(merged);
  };

  const fetchMut = useMutation({
    mutationFn: () =>
      fetchProviderModelsPreview({
        provider: providerKind,
        api_format: apiFormat,
        protocol_profile: protocolProfile,
        execution_backend: executionBackend,
        base_url: baseUrl ? baseUrl.trim() : null,
        // 编辑模式下若用户没重填 api_key，让后端回落到 DB 已存的
        api_key: apiKey ? apiKey : null,
        proxy_id: proxyId ? Number(proxyId) : null,
        pid: providerId,
        request_headers: requestHeadersPayload(requestHeaders),
      }),
    onSuccess: (resp) => {
      mergeFetched(resp.ids);
      toast.success(
        `已拉取 ${resp.fetched} 个模型；本地共 ${
        // mergeFetched 是同步的，但 models 还是旧引用——直接用 resp.fetched 给提示
        resp.fetched +
        models.filter((m) => m.custom && !resp.ids.includes(m.id)).length
        } 条`,
      );
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const onTest = async (modelId: string) => {
    const controller = new AbortController();
    const startedAt = performance.now();
    testAbortRef.current = controller;
    setTestingId(modelId);
    try {
      const response = await chatTestProviderModels(
        providerId!,
        {
          models: [modelId],
          message: "请用一句简短中文回复：模型测活正常。",
          system_prompt: "你正在执行模型可用性检查。请直接、简短地回复用户，不要调用工具。",
          max_tokens: 128,
          timeout_seconds: 90,
          execution_backend_override: executionBackend,
        },
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      const result = response.results[0];
      if (!result) throw new Error("测活接口没有返回模型结果");
      setTestResults((current) => ({ ...current, [modelId]: result }));
      if (result.ok) {
        const reply = (result.response || result.preview || "模型已正常回复").trim();
        toast.success(`${modelId} 正常 · ${result.latency_ms} ms`, {
          description: reply.slice(0, 160),
        });
      } else {
        toast.error(`${modelId} 测活失败 · ${result.latency_ms} ms`, {
          description: result.error || "上游未返回有效文本",
        });
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      const message = getErrMsg(error);
      setTestResults((current) => ({
        ...current,
        [modelId]: {
          ok: false,
          requested_model: modelId,
          latency_ms: Math.max(0, Math.round(performance.now() - startedAt)),
          input_tokens: 0,
          output_tokens: 0,
          empty_response: false,
          error: message,
        },
      }));
      toast.error(`${modelId} 测活失败`, { description: message });
    } finally {
      if (testAbortRef.current === controller) {
        testAbortRef.current = null;
        if (mountedRef.current) setTestingId(null);
      }
    }
  };

  const toggleByIdx = (idx: number) => {
    const next = models.slice();
    next[idx] = { ...next[idx], enabled: !next[idx].enabled };
    onModelsChange(next);
  };

  const removeByIdx = (idx: number) => {
    const next = models.slice();
    next.splice(idx, 1);
    onModelsChange(next);
  };

  const addCustom = () => {
    const id = customId.trim();
    if (!id) return;
    if (models.some((m) => m.id === id)) {
      toast.error(`模型 ${id} 已存在`);
      return;
    }
    onModelsChange([...models, { id, enabled: true, custom: true, label: null }]);
    setCustomId("");
  };

  // Fetch 按钮可用性：新建模式使用表单内 Key；编辑模式可回落到 DB 已存 Key。
  const fetchDisabledHint =
    !persisted && !apiKey.trim() && providerKind !== "ollama"
      ? "新建模式下需先填 API Key 才能 Fetch；或先保存让后端用已存 key"
      : null;

  // 渲染单行模型；按用户要求保持**固定顺序**：
  //   [⭐(设默认) 或 默认徽章] / 测活 / 删除
  // 即第一个槽位永远是"设默认动作"——非默认显示 ⭐ 按钮、默认显示徽章占位；
  // 后两位永远是 测活 + 删除，避免列错位。
  const renderModelRow = (m: ProviderModel, idx: number) => {
    const isDefault = m.id === defaultModel;
    const result = testResults[m.id];
    const resultDetail = result?.ok
      ? (result.response || result.preview || "模型已正常回复")
      : result?.error;
    return (
      <div
        key={m.id}
        data-provider-model-id={m.id}
        className="flex items-center gap-2 border-b px-2 py-1.5 last:border-b-0 text-sm"
      >
        <Switch
          checked={m.enabled}
          onCheckedChange={() => toggleByIdx(idx)}
        />
        <span className="font-mono text-xs flex-1 truncate" title={m.id}>
          {m.id}
        </span>
        {m.custom ? (
          <MetaBadge tone="outline" className="text-[10px] leading-4">custom</MetaBadge>
        ) : null}
        {result ? (
          result.ok ? (
            <MetaBadge tone="success" className="text-[10px] leading-4" title={resultDetail || ""}>
              <CheckCircle2 className="h-3 w-3" />
              {result.latency_ms} ms
            </MetaBadge>
          ) : (
            <MetaBadge tone="danger" className="text-[10px] leading-4" title={resultDetail || ""}>
              <XCircle className="h-3 w-3" />
              失败
            </MetaBadge>
          )
        ) : null}
        {result?.execution_backend ? (
          <MetaBadge
            tone={isGatewayBackend(result.execution_backend) ? "info" : "neutral"}
            className="text-[10px] leading-4"
            title={isGatewayBackend(result.execution_backend)
              ? [result.gateway_version, result.gateway_stage, result.gateway_request_id].filter(Boolean).join(" · ")
              : "实际通过 Provider 直连调用"}
          >
            {executionBackendLabel(result.execution_backend)}
          </MetaBadge>
        ) : null}
        {/* 槽位 1：设默认动作（非默认 → ⭐ 按钮；默认 → 默认徽章） */}
        {isDefault ? (
          <MetaBadge tone="success" className="text-[10px] leading-4">默认</MetaBadge>
        ) : (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => onSetDefault(m.id)}
            title="设为默认模型 ID"
          >
            <Star className="h-3.5 w-3.5" />
          </Button>
        )}
        {/* 槽位 2：真实单模型对话测活 */}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          loading={testingId === m.id}
          disabled={!persisted || (testingId !== null && testingId !== m.id)}
          title={persisted ? "使用当前表单所选执行后端发起真实单模型对话测活" : "先保存 Provider 再测活"}
          onClick={() => onTest(m.id)}
        >
          {testingId !== m.id ? <Activity className="h-3.5 w-3.5" /> : null}
          测活
        </Button>
        {/* 槽位 3：删除 */}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => removeByIdx(idx)}
          title="移除"
        >
          <Trash2 className="h-3.5 w-3.5 text-destructive" />
        </Button>
      </div>
    );
  };

  // 把 models 拆成 [启用, 未启用]，但保留原 idx 以便按索引 toggle / remove
  const enabledRows: { m: ProviderModel; idx: number }[] = [];
  const disabledRows: { m: ProviderModel; idx: number }[] = [];
  models.forEach((m, idx) => {
    if (m.enabled) enabledRows.push({ m, idx });
    else disabledRows.push({ m, idx });
  });

  return (
    <div className="rounded-md border bg-muted/30 p-3 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <Label className="text-sm font-semibold">模型管理</Label>
          <p className="text-xs text-muted-foreground">
            点 <code>Fetch</code> 使用当前表单所选执行后端和接入参数拉模型列表，无需先保存；
            手动启用要用的几个；也能手动添加。
            启用的模型会在「自定义指令 → AI 子表单」的下拉里展开成
            <code> 名称（提供商 · 模型ID）</code>
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          loading={fetchMut.isPending}
          disabled={!!fetchDisabledHint}
          onClick={() => fetchMut.mutate()}
          title={fetchDisabledHint || "用当前表单字段拉模型列表（不必先保存）"}
        >
          {!fetchMut.isPending ? (
            <Download className="mr-1 h-4 w-4" />
          ) : null}
          Fetch 模型列表
        </Button>
      </div>

      {fetchDisabledHint && !fetchMut.isPending ? (
        <p className="rounded-md border px-3 py-1.5 text-xs alert-warning">
          {fetchDisabledHint}
        </p>
      ) : null}

      {/* 自定义添加 */}
      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <Label className="text-xs">自定义添加</Label>
          <Input
            value={customId}
            onChange={(e) => setCustomId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addCustom();
              }
            }}
            placeholder="例如：gpt-4o-mini / claude-haiku-4-5 / glm-4-air"
            maxLength={128}
          />
        </div>
        <Button type="button" size="sm" onClick={addCustom} disabled={!customId.trim()}>
          <Plus className="mr-1 h-4 w-4" /> 添加
        </Button>
      </div>

      {/* 模型列表 */}
      {models.length === 0 ? (
        <p className="rounded-md border border-dashed py-4 text-center text-xs text-muted-foreground">
          尚无候选模型。点 Fetch 自动拉，或在上面手动添加。
        </p>
      ) : (
        <div className="space-y-2">
          {enabledCount > 0 ? (
            <div className="rounded-md border overflow-hidden">
              {enabledRows.map(({ m, idx }) => renderModelRow(m, idx))}
            </div>
          ) : (
            <p className="rounded-md border border-dashed py-3 text-center text-xs text-muted-foreground">
              当前没有启用任何模型。展开下方未启用列表开启模型，或在上面 Fetch 后自定义添加
            </p>
          )}

          {disabledRows.length > 0 ? (
            <div className="rounded-md border bg-background">
              <button
                type="button"
                onClick={() => setShowDisabled((v) => !v)}
                className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs text-muted-foreground hover:bg-muted/40"
                aria-expanded={showDisabled}
              >
                {showDisabled ? (
                  <ChevronDown className="h-3.5 w-3.5" />
                ) : (
                  <ChevronRight className="h-3.5 w-3.5" />
                )}
                <span>
                  未启用模型（{disabledRows.length}）
                  {showDisabled ? " · 点击折叠" : " · 点击展开"}
                </span>
              </button>
              {showDisabled ? (
                <div className="border-t">
                  {disabledRows.map(({ m, idx }) => renderModelRow(m, idx))}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function GatewayStatusDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
}) {
  const healthQ = useQuery({
    queryKey: ["system", "health-overview"],
    queryFn: getHealthOverview,
    enabled: open,
    staleTime: 5_000,
    refetchOnWindowFocus: true,
  });
  const gateway = healthQ.data?.codex_gateway;
  const stateLabel = gateway?.state === "ready"
    ? "已就绪"
    : gateway?.state === "degraded"
      ? "异常"
      : gateway?.state === "not_required"
        ? "当前未启用"
        : "读取中";
  const stateTone = gateway?.state === "ready"
    ? "success"
    : gateway?.state === "degraded"
      ? "danger"
      : "outline";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>内置 Codex Gateway 状态</DialogTitle>
          <DialogDescription>
            Gateway 没有单独的用户配置表单。它会读取所有选择“内置 Codex Gateway”的 Provider，并在 Web 容器内按需启动。
          </DialogDescription>
        </DialogHeader>
        {healthQ.isLoading ? (
          <div className="flex min-h-32 items-center justify-center gap-2 text-sm text-muted-foreground">
            <Spinner className="text-primary" />
            正在读取 Gateway 状态…
          </div>
        ) : healthQ.isError ? (
          <p className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {getErrMsg(healthQ.error)}
          </p>
        ) : (
          <div className="space-y-4">
            <div className="flex items-center justify-between rounded-md border bg-muted/20 px-3 py-2.5">
              <span className="text-sm font-medium">运行状态</span>
              <MetaBadge tone={stateTone}>{stateLabel}</MetaBadge>
            </div>
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div className="rounded-md border p-3">
                <dt className="text-xs text-muted-foreground">Gateway 版本</dt>
                <dd className="mt-1 break-all font-mono text-xs">{gateway?.version || "暂无"}</dd>
              </div>
              <div className="rounded-md border p-3">
                <dt className="text-xs text-muted-foreground">Provider 数量</dt>
                <dd className="mt-1 font-semibold">{gateway?.provider_count ?? 0}</dd>
              </div>
              <div className="rounded-md border p-3">
                <dt className="text-xs text-muted-foreground">配置修订</dt>
                <dd className="mt-1 font-mono text-xs">{gateway?.revision ?? 0}</dd>
              </div>
              <div className="rounded-md border p-3">
                <dt className="text-xs text-muted-foreground">是否需要运行</dt>
                <dd className="mt-1 font-medium">{gateway?.required ? "是" : "否"}</dd>
              </div>
            </dl>
            {gateway?.error ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3">
                <div className="text-xs font-medium text-destructive">最近错误</div>
                <p className="mt-1 break-words text-xs leading-5 text-destructive">{gateway.error}</p>
              </div>
            ) : (
              <p className="rounded-md border border-dashed px-3 py-2 text-xs leading-5 text-muted-foreground">
                当前没有 Gateway 错误。新增、编辑或停用 Gateway Provider 后，配置会自动同步。
              </p>
            )}
          </div>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          <Button
            type="button"
            disabled={healthQ.isFetching}
            onClick={() => void healthQ.refetch()}
          >
            <RefreshCw className={cn("mr-1 h-4 w-4", healthQ.isFetching && "animate-spin")} />
            刷新状态
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ═══════════ AI 供应商请求配置 ═══════════
function IdentityVersionsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const [items, setItems] = useState<ClientIdentityVersionItem[]>([]);
  const [profiles, setProfiles] = useState<ClientIdentityRequestProfile[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [detected, setDetected] = useState<Record<string, ClientIdentityVersionDetectItem>>({});
  const [loading, setLoading] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setError(null);
    setDetected({});
    setLoading(true);
    getClientIdentityVersions()
      .then((resp) => {
        if (!alive) return;
        setItems(resp.items);
        setProfiles(resp.profiles);
        setDrafts(Object.fromEntries(resp.items.map((i) => [i.key, i.current])));
      })
      .catch((e) => {
        if (alive) setError(getErrMsg(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open]);

  const detect = async () => {
    setDetecting(true);
    setError(null);
    try {
      const resp = await detectClientIdentityVersions();
      setDetected(Object.fromEntries(resp.items.map((i) => [i.key, i])));
    } catch (e) {
      setError(getErrMsg(e));
    } finally {
      setDetecting(false);
    }
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // overrides 只保存偏离内置默认值的非空项；留空或填回默认值都会删除覆盖。
      const overrides: Record<string, string> = {};
      for (const it of items) {
        const v = (drafts[it.key] ?? "").trim();
        if (v && v !== it.default.trim()) overrides[it.key] = v;
      }
      const resp = await updateClientIdentityVersions({ overrides });
      setItems(resp.items);
      setProfiles(resp.profiles);
      setDrafts(Object.fromEntries(resp.items.map((i) => [i.key, i.current])));
      toast.success("已保存 AI 供应商请求配置");
    } catch (e) {
      setError(getErrMsg(e));
    } finally {
      setSaving(false);
    }
  };

  const detectedUpdates = items.filter((item) => {
    const result = detected[item.key];
    return Boolean(result?.latest && !result.error && result.latest !== (drafts[item.key] ?? "").trim());
  });

  const applyDetectedUpdates = () => {
    setDrafts((previous) => {
      const next = { ...previous };
      for (const item of detectedUpdates) {
        const latest = detected[item.key]?.latest;
        if (latest) next[item.key] = latest;
      }
      return next;
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>AI 供应商请求配置</DialogTitle>
          <DialogDescription>
            按客户端查看可配置版本、完整抓包请求头及每个字段的处理方式。Provider 专用兼容头请在对应 Provider 编辑页配置。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="rounded-md border bg-muted/25 px-3 py-2 text-xs leading-5 text-muted-foreground">
            自动策略：Chat Completions 使用 OpenAI SDK，Responses 使用 Codex CLI，Anthropic Messages 使用 Claude Code CLI。下方完整列出抓包观察到的请求头，并标明 TelePilot 是固定发送、动态生成、协议处理，还是因安全与语义风险明确不复制。
          </div>
          {error ? <p className="break-words text-sm text-destructive">{error}</p> : null}
          {loading ? (
            <p className="text-sm text-muted-foreground">
              <Spinner className="mr-1" /> 加载中…
            </p>
          ) : (
            <>
              <section className="overflow-hidden rounded-md border bg-background" aria-label="客户端版本配置">
                <div className="flex min-w-0 items-start justify-between gap-3 border-b bg-muted/20 px-3 py-2.5">
                  <div className="min-w-0">
                    <div className="text-sm font-semibold">客户端版本</div>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">
                      当前发送版本与检测结果始终可见；采用后点击底部保存才会生效。
                    </p>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="shrink-0"
                    disabled={detectedUpdates.length === 0}
                    onClick={applyDetectedUpdates}
                  >
                    <Download className="mr-1 h-4 w-4" />
                    填入全部更新
                  </Button>
                </div>
                <div className="divide-y">
                  {items.map((item) => {
                    const result = detected[item.key];
                    const canApply = Boolean(
                      result?.latest &&
                      !result.error &&
                      result.latest !== (drafts[item.key] ?? "").trim(),
                    );
                    return (
                      <div key={item.key} className="grid grid-cols-2 gap-2 px-3 py-3 sm:grid-cols-[minmax(0,1fr)_150px_150px_auto] sm:items-end">
                        <div className="col-span-2 min-w-0 sm:col-span-1">
                          <div className="text-sm font-medium">{item.label}</div>
                          <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">
                            内置基线 {item.default} · {item.registry ? `检测源 ${item.registry === "cli:grok-update-check" ? "Grok CLI / xAI stable" : item.registry}` : "仅手动填写"}
                          </p>
                        </div>
                        <label className="min-w-0 space-y-1">
                          <span className="block text-[11px] text-muted-foreground">当前版本</span>
                          <Input
                            className="font-mono text-xs"
                            value={drafts[item.key] ?? ""}
                            onChange={(event) => setDrafts((previous) => ({ ...previous, [item.key]: event.target.value }))}
                          />
                        </label>
                        <label className="min-w-0 space-y-1">
                          <span className="block text-[11px] text-muted-foreground">检测到的版本</span>
                          <Input
                            readOnly
                            className="font-mono text-xs"
                            value={result?.latest ?? ""}
                            placeholder={result?.error ? "检测失败" : "待检测"}
                            title={result?.error || result?.latest || "尚未检测"}
                          />
                        </label>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="col-span-2 w-full sm:col-span-1 sm:w-auto"
                          disabled={!canApply}
                          onClick={() => {
                            const latest = detected[item.key]?.latest;
                            if (latest) {
                              setDrafts((previous) => ({ ...previous, [item.key]: latest }));
                            }
                          }}
                        >
                          采用
                        </Button>
                        {result?.error ? (
                          <p className="col-span-2 break-words text-xs text-destructive sm:col-start-2 sm:col-end-5">
                            检测失败：{result.error}
                          </p>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              </section>

              <div className="divide-y rounded-md border">
                {profiles.map((profile) => (
                  <details key={profile.profile} className="group">
                    <summary className="flex cursor-pointer list-none items-start gap-3 px-3 py-3 [&::-webkit-details-marker]:hidden">
                      <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold">{profile.label}</span>
                          {profile.api_formats.map((format) => <MetaBadge key={format} mono>{format}</MetaBadge>)}
                        </div>
                        <p className="mt-1 text-xs leading-5 text-muted-foreground">{profile.description}</p>
                      </div>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {profile.version_keys.length} 项版本 · {profile.headers.length} 项请求头
                      </span>
                    </summary>
                    <div className="space-y-4 border-t bg-muted/15 px-4 py-4">
                      <div className="space-y-3">
                        <div>
                          <div className="text-xs font-semibold text-muted-foreground">抓包请求头清单</div>
                          <p className="mt-1 text-xs leading-5 text-muted-foreground">鉴权值、设备标识和内部元数据只展示字段名与处理规则，不回显真实内容。</p>
                        </div>
                        {CLIENT_HEADER_GROUPS.map((group) => {
                          const headers = profile.headers.filter((header) => header.management === group.value);
                          if (headers.length === 0) return null;
                          return (
                            <section key={group.value} className="overflow-hidden rounded-md border bg-background" aria-label={group.label}>
                              <div className="flex flex-wrap items-center gap-2 border-b bg-muted/20 px-3 py-2">
                                <MetaBadge tone={group.tone}>{group.label}</MetaBadge>
                                <span className="text-xs text-muted-foreground">{group.description}</span>
                              </div>
                              <div className="divide-y">
                                {headers.map((header) => (
                                  <div key={`${group.value}:${header.name}`} className="grid min-w-0 gap-1.5 px-3 py-2.5 sm:grid-cols-[150px_minmax(0,1fr)] sm:items-start sm:gap-3">
                                    <code className="break-all text-xs text-foreground">{header.name}</code>
                                    <div className="min-w-0">
                                      <code className="block break-all text-xs text-muted-foreground">{header.value}</code>
                                      <p className="mt-1 text-xs leading-5 text-muted-foreground">{header.description}</p>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            </section>
                          );
                        })}
                      </div>
                      <p className="border-t pt-3 text-xs leading-5 text-muted-foreground">证据：{profile.source}</p>
                    </div>
                  </details>
                ))}
              </div>
            </>
          )}
        </div>
        <DialogFooter className="!grid grid-cols-2 gap-2 sm:!flex">
          <Button className="w-full sm:w-auto" type="button" variant="outline" onClick={() => void detect()} loading={detecting} disabled={loading}>
            检测最新版本
          </Button>
          <Button className="w-full sm:w-auto" type="button" onClick={() => void save()} loading={saving} disabled={loading}>
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
