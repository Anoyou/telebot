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
import { ArrowLeft, Plus, Trash2, KeyRound, Edit3, Download, CheckCircle2, XCircle, Star, ChevronDown, ChevronRight, Eye, EyeOff, Filter, X, Package, Save, MessageSquare, ArrowUpDown, GripVertical } from "lucide-react";

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
  deleteLLMProvider,
  detectClientIdentityVersions,
  detectProviderProtocols,
  fetchProviderModelsPreview,
  getClientIdentityVersions,
  listLLMProviders,
  patchLLMProvider,
  revealLLMProviderApiKey,
  testProviderModel,
  updateClientIdentityVersions,
} from "@/api/commands";
import { listProxies } from "@/api/proxies";
import { getSystemSettings, patchSystemSettings } from "@/api/system";
import type { ClientIdentityVersionDetectItem, ClientIdentityVersionItem, DetectProviderProtocolsResponse, LLMApiFormat, LLMClientIdentityProfile, LLMModality, LLMProtocolProfile, LLMProviderKind, LLMProviderOut, LLMTag, LLMWebSearchApiFormat, ProviderModel, ProtocolProbeResult, ProxyOut } from "@/api/types";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";
import { confirmDiscardChanges, useUnsavedChanges } from "@/lib/unsavedChanges";
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

// 各 provider 常见模型示例（首次新建友好填充）
const SUGGESTED_MODELS: Record<LLMProviderKind, string> = {
  openai: "gpt-4o-mini",
  anthropic: "claude-haiku-4-5",
  ollama: "llama3:8b",
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
    hint: "OpenAI 2024 出的新协议；anyrouter 等部分反代只接这个；默认应该选这个解决 chat/completions 不通的问题",
  },
  {
    value: "anthropic_messages",
    label: "Anthropic Messages ( /v1/messages )",
    hint: "Anthropic 协议；走官方 https://api.anthropic.com 或兼容反代时选",
  },
];

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
// Desktop 档案暂无可复核证据，标记 disabled，前端不可选。
const CLIENT_IDENTITY_OPTIONS: {
  value: LLMClientIdentityProfile;
  label: string;
  hint: string;
  disabled?: boolean;
}[] = [
  {
    value: "auto",
    label: "自动（推荐）",
    hint: "按本次实际协议解析：chat_completions→OpenAI SDK / responses→Codex CLI / anthropic_messages→Claude Code。",
  },
  {
    value: "minimal",
    label: "最小（仅协议必需头）",
    hint: "不附加任何产品模拟头，仅发送协议必需头。上游不校验客户端身份时使用。",
  },
  {
    value: "openai_sdk",
    label: "OpenAI SDK",
    hint: "OpenAI 官方 Python SDK 身份，用于 Chat Completions。",
  },
  {
    value: "codex_cli",
    label: "Codex CLI",
    hint: "Codex CLI 身份（originator=codex_cli_rs），用于 Responses。",
  },
  {
    value: "claude_code",
    label: "Claude Code",
    hint: "Claude Code 身份（x-app=cli），用于 Anthropic Messages。",
  },
  {
    value: "codex_desktop",
    label: "Codex Desktop",
    hint: "Codex Desktop 身份（originator=Codex Desktop），用于 Responses。证据来自本机抓包的 alpha 预发布版，stable 版可能变化。",
  },
  {
    value: "grok_cli",
    label: "Grok CLI",
    hint: "Grok CLI 身份（grok-cli UA + x-grok-client-version），用于 Responses；不附加 OAuth、账号或设备字段。",
  },
  {
    value: "claude_desktop",
    label: "Claude Desktop（暂不可用）",
    hint: "缺少可复核的请求头证据，暂不可选。",
    disabled: true,
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
  client_identity_profile: "codex_cli",
  clearKey: false,
  modality: "text",
  tags: ["chat"],
  cost_tier: 2,
  notes: "",
  proxy_id: "",
  models: [],
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
        ...(form.api_format === "anthropic_messages"
          ? { protocol_profile: form.protocol_profile }
          : {}),
        web_search_api_format: form.web_search_api_format,
        client_identity_profile: form.client_identity_profile,
        modality: form.modality,
        tags: form.tags,
        cost_tier: form.cost_tier,
        notes: form.notes || null,
        proxy_id: form.proxy_id ? Number(form.proxy_id) : null,
        models: form.models,
      }),
    onSuccess: () => {
      toast.success("已新建模型提供商");
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      qc.invalidateQueries({ queryKey: ["system-agent", "capabilities"] });
      closeCreate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
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
        ...(form.api_format === "anthropic_messages"
          ? { protocol_profile: form.protocol_profile }
          : {}),
        web_search_api_format: form.web_search_api_format,
        client_identity_profile: form.client_identity_profile,
        modality: form.modality,
        tags: form.tags,
        cost_tier: form.cost_tier,
        notes: form.notes || null,
        ...proxyPatch,
        models: form.models,
      });
    },
    onSuccess: () => {
      toast.success("已保存");
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      qc.invalidateQueries({ queryKey: ["system-agent", "capabilities"] });
      setEditing(null);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteLLMProvider(id),
    onSuccess: () => {
      toast.success("已删除");
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      qc.invalidateQueries({ queryKey: ["system-agent", "capabilities"] });
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
      protocol_profile:
        p.api_format === "anthropic_messages" && p.protocol_profile === "claude_code_proxy"
          ? "claude_code_proxy"
          : "standard",
      web_search_api_format: ((p.web_search_api_format as LLMWebSearchApiFormat) || "auto"),
      client_identity_profile: ((p.client_identity_profile as LLMClientIdentityProfile) || "auto"),
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
    if (editing.id) {
      updateMut.mutate(editing);
    } else {
      createMut.mutate(editing);
    }
  };

  if (editing && !editing.id) {
    return (
      <ProviderEditDialog
        form={editing}
        onChange={setEditing}
        onCancel={closeCreate}
        onSave={saveEditing}
        saving={createMut.isPending}
      />
    );
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <div className="space-y-4">
            <SectionHeader
              icon={Package}
              title="模型提供商"
              description={
                <>
                  每行对应一组供应商凭据。编辑 Provider 可拉取模型列表，并选择参与路由的模型。
                </>
              }
              meta={
                <SignalPill
                  tone={visibleProviders.length > 0 ? "primary" : "warn"}
                  label="可见 Provider"
                  value={`${visibleProviders.length}`}
                />
              }
            />
            <div className="grid grid-cols-2 items-center gap-2 rounded-md border border-primary/20 bg-primary/[0.04] p-2 shadow-sm sm:flex sm:flex-wrap">
              <Button
                type="button"
                size="sm"
                className="min-w-0 flex-1 shadow-sm sm:flex-none"
                disabled={visibleProviders.length === 0}
                onClick={() => navigate(`/ai/liveness?provider=${visibleProviders[0].id}`)}
              >
                <MessageSquare className="mr-1 h-4 w-4" /> 对话测活
              </Button>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                className="min-w-0 flex-1 border border-border/80 shadow-sm sm:flex-none"
                onClick={() => setIdentityVersionsOpen(true)}
              >
                <KeyRound className="mr-1 h-4 w-4" />客户端身份版本
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <div className="mb-3 flex flex-wrap items-center justify-end gap-2">
            {visibleProviders.length > 1 ? (
              <>
              <ArrowUpDown className="h-3.5 w-3.5 text-muted-foreground" />
              <Label htmlFor="provider-sort" className="text-xs text-muted-foreground">排序</Label>
              <Select id="provider-sort" value={providerSort} disabled={editingProviderOrder} onChange={(event) => setProviderSort(event.target.value as typeof providerSort)} className="h-11 w-auto min-w-32 text-xs sm:h-9">
                <option value="custom">自定义顺序</option>
                <option value="name">名称</option>
                <option value="models">启用模型数</option>
              </Select>
              {editingProviderOrder ? (
                <>
                  <Button type="button" size="sm" variant="outline" className="min-h-11 sm:min-h-9" onClick={() => setEditingProviderOrder(false)}>取消</Button>
                  <Button type="button" size="sm" className="min-h-11 sm:min-h-9" loading={saveProviderOrder.isPending} onClick={() => saveProviderOrder.mutate()}>
                    {!saveProviderOrder.isPending ? <Save className="mr-1 h-4 w-4" /> : null}保存排序
                  </Button>
                </>
              ) : (
                <Button type="button" size="sm" variant="outline" className="min-h-11 sm:min-h-9" onClick={() => { setProviderSort("custom"); setEditingProviderOrder(true); }}>
                  <GripVertical className="mr-1 h-4 w-4" />编辑排序
                </Button>
              )}
              </>
            ) : null}
            <Button size="sm" className="min-h-11 sm:min-h-9" disabled={editingProviderOrder} onClick={openCreate}>
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
            <Table className="min-w-[1080px]">
              <TableHeader>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>提供商协议</TableHead>
                  <TableHead>API 协议</TableHead>
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
                        {p.api_format === "anthropic_messages" ? (
                          <div>
                            <MetaBadge mono tone={p.protocol_profile === "claude_code_proxy" ? "warn" : "neutral"}>
                              {p.protocol_profile || "standard"}
                            </MetaBadge>
                          </div>
                        ) : null}
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
                        <Button variant="ghost" size="sm" onClick={() => onEdit(p)}>
                          <Edit3 className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
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

      {editing?.id && (
        <ProviderEditDialog
          form={editing}
          onChange={setEditing}
          onCancel={() => setEditing(null)}
          onSave={saveEditing}
          saving={createMut.isPending || updateMut.isPending}
        />
      )}
      <IdentityVersionsDialog open={identityVersionsOpen} onOpenChange={setIdentityVersionsOpen} />
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
          {provider.api_format === "anthropic_messages" ? (
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
  const setField = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    onChange({ ...form, [key]: value });
  const connectionLocked = stage === "fetching" || stage === "verifying" || saving;
  const [activeStep, setActiveStep] = useState(1);
  const connectSectionRef = useRef<HTMLElement>(null);
  const verifySectionRef = useRef<HTMLDivElement>(null);
  const saveSectionRef = useRef<HTMLElement>(null);
  const defaultBaseUrl = DEFAULT_BASE_URLS[form.provider];
  const endpoint = form.base_url.trim() || defaultBaseUrl;
  const enabledModelCount = form.models.filter((model) => model.enabled).length;

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
    const provider: LLMProviderKind =
      apiFormat === "anthropic_messages" ? "anthropic" : form.provider === "anthropic" ? "openai" : form.provider;
    onChange({
      ...form,
      provider,
      api_format: apiFormat,
      protocol_profile: apiFormat === "anthropic_messages" ? form.protocol_profile : "standard",
      client_identity_profile:
        apiFormat === "responses"
          ? "codex_cli"
          : apiFormat === "anthropic_messages"
            ? "claude_code"
            : "openai_sdk",
      default_model: "",
      models: [],
    });
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
            <h1 className="text-xl font-semibold tracking-tight">新建模型提供商</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              连接、选模型、验证，然后一次保存。
            </p>
          </div>
          <MetaBadge tone={verified ? "success" : stage === "verifying" || stage === "fetching" ? "info" : "outline"}>
            {verified ? "可保存" : CREATE_STAGE_COPY[stage]}
          </MetaBadge>
        </div>
      </header>
      <ol className="sticky top-0 z-20 grid grid-cols-3 gap-1 rounded-lg border bg-background/95 p-1.5 shadow-sm backdrop-blur" aria-label="创建步骤">
          {[
            { step: 1, label: "接入信息", compactLabel: "接入信息" },
            { step: 2, label: "选择模型并验证", compactLabel: "模型与验证" },
            { step: 3, label: "保存", compactLabel: "保存" },
          ].map(({ step, label, compactLabel }) => {
            const complete = step < activeStep || verified;
            const active = step === activeStep;
            return (
              <li
                key={step}
                className={cn(
                  "flex min-w-0 items-center justify-center gap-1.5 rounded-md px-1.5 py-2 text-[11px] transition-colors sm:text-xs",
                  active ? "bg-primary text-primary-foreground" : complete ? "bg-primary/10 text-foreground" : "text-muted-foreground",
                )}
              >
                <span
                  className={cn(
                    "grid h-5 w-5 shrink-0 place-items-center rounded-full border text-[10px]",
                    complete && !active && "border-primary bg-primary text-primary-foreground",
                    active && "border-primary-foreground/60 text-primary-foreground",
                  )}
                >
                  {complete ? <CheckCircle2 className="h-3.5 w-3.5" /> : step}
                </span>
                <span className="min-w-0 leading-4">
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
                  填写真实接入参数，先读取模型列表，不会自动挑选模型发起请求。
                </p>
              </div>
              <MetaBadge tone={stage === "verified" ? "success" : stage === "fetching" || stage === "verifying" ? "info" : "outline"}>
                {CREATE_STAGE_COPY[stage]}
              </MetaBadge>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
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
                  autoComplete="new-password"
                  disabled={connectionLocked}
                  placeholder="sk-..."
                  onChange={(value) => setField("api_key", value)}
                />
                <p className="text-xs text-muted-foreground">保存时加密落库；点击右侧眼睛可临时查看当前填写内容。</p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="provider-create-api-format">协议</Label>
                <Select
                  id="provider-create-api-format"
                  value={form.api_format}
                  disabled={connectionLocked}
                  onChange={(event) => setApiFormat(event.target.value as LLMApiFormat)}
                >
                  {API_FORMAT_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </Select>
                <p className="text-xs leading-5 text-muted-foreground">
                  {API_FORMAT_OPTIONS.find((option) => option.value === form.api_format)?.hint}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="provider-create-client">客户端身份</Label>
                <Select
                  id="provider-create-client"
                  value={form.client_identity_profile}
                  disabled={connectionLocked}
                  onChange={(event) => setField("client_identity_profile", event.target.value as LLMClientIdentityProfile)}
                >
                  {CLIENT_IDENTITY_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>
                  ))}
                </Select>
                <p className="text-xs leading-5 text-muted-foreground">
                  {CLIENT_IDENTITY_OPTIONS.find((option) => option.value === form.client_identity_profile)?.hint}
                </p>
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                loading={detectingProtocol}
                disabled={connectionLocked || (!form.api_key.trim() && form.provider !== "ollama")}
                onClick={onDetectProtocol}
              >
                {!detectingProtocol ? <Download className="mr-1 h-4 w-4" /> : null}
                自动检测协议
              </Button>
              <span className="text-xs text-muted-foreground">不确定兼容协议时再检测，默认使用 Responses + Codex CLI。</span>
            </div>
            {protocolDetection ? <div className="mt-3"><ProtocolDetectionPanel result={protocolDetection} /></div> : null}
          </section>

          <div ref={verifySectionRef}>
            <ProviderCreateVerification
              providerKind={form.provider}
              apiFormat={form.api_format}
              protocolProfile={form.protocol_profile}
              clientIdentityProfile={form.client_identity_profile}
              baseUrl={form.base_url}
              apiKey={form.api_key}
              proxyId={form.proxy_id}
              models={form.models}
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
          </div>

          <section ref={saveSectionRef} className="border-t py-6" aria-labelledby="provider-save-title">
            <div className="mb-4">
              <h2 id="provider-save-title" className="text-base font-semibold">保存信息</h2>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">验证通过会自动补全；验证未通过也可手动填写名称和默认模型后保存。</p>
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
                  <Select value={form.web_search_api_format} onChange={(event) => setField("web_search_api_format", event.target.value as LLMWebSearchApiFormat)}>
                    {WEB_SEARCH_API_FORMAT_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </Select>
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
                {form.api_format === "anthropic_messages" ? (
                  <div className="space-y-1.5">
                    <Label>Anthropic 请求兼容模式</Label>
                    <Select disabled={connectionLocked} value={form.protocol_profile} onChange={(event) => setField("protocol_profile", event.target.value as LLMProtocolProfile)}>
                      <option value="standard">标准 Anthropic API</option>
                      <option value="claude_code_proxy">Claude Code 反代兼容</option>
                    </Select>
                  </div>
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
            {!verified ? <span className="mr-auto hidden text-xs text-warning sm:inline">尚未通过真实验证，保存后请尽快测活。</span> : null}
            <Button type="button" variant="outline" disabled={saving} onClick={onCancel}>取消</Button>
            <Button
              type="button"
              loading={saving}
              disabled={!form.name.trim() || !form.default_model.trim()}
              onClick={() => {
                if (!verified && !window.confirm("当前 Provider 尚未通过真实模型验证。仍要保存吗？保存后建议立即进入模型测活确认可用性。")) return;
                onSave();
              }}
            >
              {!saving ? <Save className="mr-2 h-4 w-4" /> : null}
              保存 Provider
            </Button>
          </div>
        </main>

        <aside className="sticky top-4 hidden space-y-4 rounded-lg border bg-card p-4 shadow-sm lg:block" aria-label="Provider 配置摘要">
          <div>
            <h2 className="text-sm font-semibold">即将创建</h2>
            <p className="mt-1 text-xs text-muted-foreground">验证通过后自动补全。</p>
          </div>
          <dl className="space-y-3 text-xs">
            <div><dt className="text-muted-foreground">接入地址</dt><dd className="mt-1 break-all font-mono">{endpoint}</dd></div>
            <div><dt className="text-muted-foreground">实际协议</dt><dd className="mt-1 font-medium">{form.api_format}</dd></div>
            <div><dt className="text-muted-foreground">客户端身份</dt><dd className="mt-1 font-medium">{form.client_identity_profile}</dd></div>
            <div><dt className="text-muted-foreground">默认模型</dt><dd className="mt-1 break-all font-mono">{form.default_model || "待选择"}</dd></div>
            <div><dt className="text-muted-foreground">启用模型</dt><dd className="mt-1 font-medium">{enabledModelCount} 个</dd></div>
          </dl>
          <p className="border-t pt-3 text-xs leading-5 text-muted-foreground">路由策略沿用安全默认值，不阻塞首次接入。</p>
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

  const toggleTag = (tag: LLMTag) => {
    const has = form.tags.includes(tag);
    setField("tags", has ? form.tags.filter((t) => t !== tag) : [...form.tags, tag]);
  };

  const detectProtocolsMut = useMutation({
    mutationFn: () =>
      detectProviderProtocols({
        provider: form.provider,
        base_url: form.base_url ? form.base_url.trim() : null,
        api_key: form.api_key ? form.api_key : null,
        proxy_id: form.proxy_id ? Number(form.proxy_id) : null,
        pid: form.id ?? null,
        model: form.default_model.trim() || null,
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
          protocol_profile:
            recommendedApiFormat === "anthropic_messages" ? form.protocol_profile : "standard",
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

  if (!isEdit) {
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

  return (
    <Dialog open onOpenChange={(o) => !o && requestCancel()}>
      <DialogContent
        className={cn(
          isEdit
            ? "max-h-[90vh] max-w-3xl overflow-y-auto"
            : "inset-0 left-0 top-0 h-dvh max-h-none w-screen max-w-none translate-x-0 translate-y-0 grid-rows-[auto_minmax(0,1fr)_auto] gap-0 overflow-hidden rounded-none border-0 p-0 [&>button.absolute]:hidden",
        )}
      >
        <DialogHeader
          className={cn(
            !isEdit && "border-b bg-background px-4 py-4 sm:px-6",
          )}
        >
          {!isEdit ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="mb-2 w-fit px-0 text-muted-foreground hover:bg-transparent hover:text-foreground"
              onClick={requestCancel}
            >
              <ArrowLeft className="mr-1 h-4 w-4" /> 返回模型提供商
            </Button>
          ) : null}
          <div className={cn(!isEdit && "flex flex-wrap items-start justify-between gap-3")}>
            <div>
              <DialogTitle>{isEdit ? "编辑" : "新建"}模型提供商</DialogTitle>
              {!isEdit ? (
                <p className="mt-1 text-sm text-muted-foreground">
                  连接、获取模型、选择档位并完成真实验证，然后一次保存。
                </p>
              ) : null}
            </div>
            {!isEdit ? <MetaBadge tone={createVerified ? "success" : "outline"}>{createVerified ? "可保存" : "草稿"}</MetaBadge> : null}
          </div>
          <DialogDescription>
            API Key 加密落库；列表只显示是否已配置，编辑时可点击眼睛按需查看。
          </DialogDescription>
          {!isEdit ? (
            <ol className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3" aria-label="创建步骤">
              {[
                ["1", "接入信息"],
                ["2", "选择模型并验证"],
                ["3", "保存"],
              ].map(([number, label], index) => {
                const active = createVerified ? index <= 2 : index === 0;
                return (
                  <li
                    key={number}
                    className={cn(
                      "flex min-w-0 items-center gap-2 border-t-2 pt-2 text-xs text-muted-foreground",
                      active && "border-primary text-foreground",
                      !active && "border-border",
                    )}
                  >
                    <span className={cn("grid h-5 w-5 shrink-0 place-items-center rounded-full border text-[10px]", active && "border-primary bg-primary text-primary-foreground")}>{number}</span>
                    <span className="min-w-0 leading-4">{label}</span>
                  </li>
                );
              })}
            </ol>
          ) : null}
        </DialogHeader>

        <div className={cn(isEdit ? "space-y-4" : "min-h-0 overflow-y-auto bg-muted/15 px-4 py-5 sm:px-6")}>
          <div className={cn(!isEdit && "mx-auto grid w-full max-w-6xl items-start gap-6 lg:grid-cols-[minmax(0,1fr)_260px]")}>
          <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>名称 *</Label>
            <Input
              value={form.name}
              maxLength={64}
              onChange={(e) => setField("name", e.target.value)}
              placeholder="例如：openai-main"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>提供商协议 *</Label>
              <Select
                value={form.provider}
                onChange={(e) => {
                  const p = e.target.value as LLMProviderKind;
                  onChange({
                    ...form,
                    provider: p,
                    ...(!isEdit ? { default_model: "", models: [] } : {}),
                  });
                }}
              >
                <option value="openai">OpenAI（兼容协议）</option>
                <option value="anthropic">Anthropic</option>
                <option value="ollama">Ollama（本地）</option>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>默认模型 ID *</Label>
              <Input
                value={form.default_model || (isEdit ? "" : "完成真实验证后自动填写")}
                maxLength={64}
                readOnly={!isEdit}
                onChange={(e) => {
                  if (isEdit) setField("default_model", e.target.value);
                }}
                placeholder={SUGGESTED_MODELS[form.provider]}
              />
              <p className="text-xs text-muted-foreground">
                {isEdit
                  ? "自动路由 fallback 时使用；可在模型管理区直接设置。"
                  : "从模型列表选择并验证成功后自动设置，不需要手填。"}
              </p>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>Base URL</Label>
            <Input
              value={form.base_url}
              maxLength={255}
              onChange={(e) => setField("base_url", e.target.value)}
              placeholder={DEFAULT_BASE_URLS[form.provider]}
            />
            <p className="text-xs text-muted-foreground">
              留空使用默认地址。OpenAI 兼容代理 / 自托管 Ollama 都填这里。
            </p>
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center justify-between gap-2">
              <Label>API Format（API 协议）*</Label>
              <Button
                type="button"
                variant="outline"
                size="sm"
                loading={detectProtocolsMut.isPending}
                disabled={!isEdit && !form.api_key.trim() && form.provider !== "ollama"}
                onClick={() => detectProtocolsMut.mutate()}
              >
                {!detectProtocolsMut.isPending ? (
                  <Download className="mr-1 h-4 w-4" />
                ) : null}
                检测协议
              </Button>
            </div>
            <Select
              value={form.api_format}
              onChange={(e) => {
                const apiFormat = e.target.value as LLMApiFormat;
                onChange({
                  ...form,
                  api_format: apiFormat,
                  protocol_profile:
                    apiFormat === "anthropic_messages" ? form.protocol_profile : "standard",
                  ...(!isEdit
                    ? {
                        client_identity_profile:
                          apiFormat === "responses"
                            ? "codex_cli"
                            : apiFormat === "anthropic_messages"
                              ? "claude_code"
                              : "openai_sdk",
                        default_model: "",
                        models: [],
                      }
                    : {}),
                });
              }}
            >
              {API_FORMAT_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </Select>
            <p className="text-xs text-muted-foreground">
              {API_FORMAT_OPTIONS.find((o) => o.value === form.api_format)?.hint}
            </p>
            {!isEdit && !form.api_key.trim() && form.provider !== "ollama" ? (
              <p className="text-xs text-muted-foreground">
                新建时检测协议需要先填 API Key；编辑已有 Provider 可复用已保存的 Key。
              </p>
            ) : null}
          </div>

          {form.api_format === "anthropic_messages" ? (
            <div className="space-y-1.5">
              <Label>Anthropic 请求兼容模式</Label>
              <Select
                value={form.protocol_profile}
                onChange={(e) => setField("protocol_profile", e.target.value as LLMProtocolProfile)}
              >
                <option value="standard">标准 Anthropic API（推荐）</option>
                <option value="claude_code_proxy">Claude Code 反代兼容</option>
              </Select>
              <p className="text-xs text-muted-foreground">
                标准模式遵循 Anthropic Messages 协议。仅当反代明确要求 Claude Code 专用兼容头时，才选择反代兼容模式；官方 Anthropic API 不需要开启。
              </p>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label>联网搜索 API Format</Label>
            <Select
              value={form.web_search_api_format}
              onChange={(e) => setField("web_search_api_format", e.target.value as LLMWebSearchApiFormat)}
            >
              {WEB_SEARCH_API_FORMAT_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </Select>
            <p className="text-xs text-muted-foreground">
              {WEB_SEARCH_API_FORMAT_OPTIONS.find((o) => o.value === form.web_search_api_format)?.hint}
            </p>
          </div>

          {protocolDetection ? (
            <ProtocolDetectionPanel result={protocolDetection} />
          ) : null}

          <div className="space-y-1.5">
            <Label>客户端身份</Label>
            <Select
              value={form.client_identity_profile}
              onChange={(e) =>
                setField(
                  "client_identity_profile",
                  e.target.value as LLMClientIdentityProfile,
                )
              }
            >
              {CLIENT_IDENTITY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value} disabled={opt.disabled}>
                  {opt.label}
                </option>
              ))}
            </Select>
            <p className="text-xs text-muted-foreground">
              {CLIENT_IDENTITY_OPTIONS.find((o) => o.value === form.client_identity_profile)?.hint}
            </p>
            {form.client_identity_profile === "auto" ? (
              <p className="text-xs text-muted-foreground">
                当前协议 <span className="font-mono">{form.api_format}</span> 将解析为{" "}
                <span className="font-mono">
                  {form.api_format === "responses"
                    ? "codex_cli"
                    : form.api_format === "anthropic_messages"
                      ? "claude_code"
                      : "openai_sdk"}
                </span>
                。标准模式不再发送 TelePilot 产品 UA。
              </p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label>API Key {isEdit ? "" : "*（建议）"}</Label>
            <ApiKeyInput
              value={form.api_key}
              autoComplete="off"
              onChange={(value) => setField("api_key", value)}
              placeholder={isEdit && form.hasApiKey && !form.api_key ? MASKED_SECRET_PLACEHOLDER : isEdit ? "留空 = 保持原 key 不变" : "sk-..."}
              disabled={form.clearKey}
              hasStoredValue={isEdit && Boolean(form.hasApiKey)}
              revealStoredValue={
                isEdit && form.id
                  ? async () => (await revealLLMProviderApiKey(form.id!)).api_key
                  : undefined
              }
            />
            {isEdit && (
              <div className="flex items-center gap-2 pt-1 text-xs">
                <input
                  id="clearKey"
                  type="checkbox"
                  checked={form.clearKey}
                  onChange={(e) =>
                    onChange({
                      ...form,
                      clearKey: e.target.checked,
                      api_key: e.target.checked ? "" : form.api_key,
                    })
                  }
                />
                <label htmlFor="clearKey" className="cursor-pointer text-muted-foreground">
                  勾选 = 清空已存的 api_key（提交后该 provider 标记为未配置）
                </label>
              </div>
            )}
            <p className="text-xs text-muted-foreground">
              点击右侧眼睛可查看已保存或新填写的 Key。Ollama 本地部署可不填。
            </p>
          </div>

          {/* 验证必须使用最终保存的出口，因此代理选择放在验证区之前。 */}
          <div className="space-y-2 rounded-md border bg-muted/30 p-3">
            <div>
              <Label className="text-sm font-semibold">出口代理</Label>
              <p className="text-xs text-muted-foreground">
                获取模型、真实验证和保存后的请求共用此出口；
                <code>DIRECT</code> = 直连不走代理。 <span className="text-muted-foreground/80">
                  代理库在「系统设置 → 代理」管理；mtproxy 不支持，已自动过滤。
                </span>
              </p>
            </div>
            {proxiesQ.isLoading ? (
              <div className="flex h-10 items-center gap-2 rounded-md border px-3 text-xs text-muted-foreground">
                <Spinner className="text-primary" /> 加载代理列表…
              </div>
            ) : (
              <Select
                value={form.proxy_id}
                onChange={(e) => setField("proxy_id", e.target.value)}
              >
                <option value="">DIRECT — 不走代理（直连）</option>
                {llmUsableProxies.map((p) => (
                  <option key={p.id} value={String(p.id)}>
                    #{p.id} · {p.type} · {p.host}:{p.port}
                    {p.username ? ` (${p.username})` : ""}
                  </option>
                ))}
              </Select>
            )}
            {!proxiesQ.isLoading &&
              llmUsableProxies.length === 0 &&
              form.proxy_id === "" && (
                <p className="rounded-md border px-3 py-2 text-xs alert-warning">
                  代理库为空。如果你在中国大陆访问 OpenAI / Anthropic，记得先到
                  「系统设置 → 代理」添加一条 socks5 / http 代理，再回来选上。
                </p>
              )}
          </div>

          {!isEdit ? (
            <ProviderCreateVerification
              providerKind={form.provider}
              apiFormat={form.api_format}
              protocolProfile={form.protocol_profile}
              clientIdentityProfile={form.client_identity_profile}
              baseUrl={form.base_url}
              apiKey={form.api_key}
              proxyId={form.proxy_id}
              models={form.models}
              onModelsChange={(next) => setField("models", next)}
              onReset={() => onChange({ ...form, models: [], default_model: "" })}
              onVerified={(model, nextModels) =>
                onChange({ ...form, models: nextModels, default_model: model })
              }
              onVerificationChange={setCreateVerified}
            />
          ) : null}

          {/* ── 路由元数据区 ─────────────────────────── */}
          <details className="rounded-md border bg-muted/30 p-3">
            <summary className="cursor-pointer text-sm font-semibold">
              高级设置与路由策略（可选）
            </summary>
            <div className="mt-3 space-y-3">
              <div>
              <p className="text-xs text-muted-foreground">
                这些字段决定「自动路由」模式下，一条 <CommandBadge>{cmdPrefix}ai</CommandBadge> 指令的请求是否会被分配给本 provider。
                普通接入或只用 fixed 模式可保持默认。
              </p>
              </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>模态（modality）</Label>
                <Select
                  value={form.modality}
                  onChange={(e) => setField("modality", e.target.value as LLMModality)}
                >
                  {MODALITY_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </Select>
                <p className="text-xs text-muted-foreground">
                  {MODALITY_OPTIONS.find((o) => o.value === form.modality)?.hint}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label>推理成本档（cost_tier）</Label>
                <Select
                  value={String(form.cost_tier)}
                  onChange={(e) => setField("cost_tier", Number(e.target.value))}
                >
                  {COST_TIER_OPTIONS.map((opt) => (
                    <option key={opt.value} value={String(opt.value)}>
                      {opt.label}
                    </option>
                  ))}
                </Select>
                <p className="text-xs text-muted-foreground">
                  同 tag 内有多个 provider 时，路由器据此挑（cheap=1 优先做闲聊，premium=3 优先做推理）。
                </p>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>路由标签（tags）</Label>
              <div className="flex flex-wrap gap-1.5">
                {TAG_OPTIONS.map((opt) => {
                  const active = form.tags.includes(opt.value);
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => toggleTag(opt.value)}
                      title={opt.hint}
                      className={
                        "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold leading-5 transition-colors " +
                        (active
                          ? "border-transparent bg-primary text-primary-foreground"
                          : "border-transparent bg-muted text-foreground hover:bg-muted/70")
                      }
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
              <p className="text-xs text-muted-foreground">
                点击切换。常用搭配：闲聊模型 = ['chat','cheap'] · 旗舰答主力 = ['smart','reason','code','long_context'] · 视觉模型 = ['vision'] +
                modality=vision · 路由分类器 = ['classify','cheap']
              </p>
            </div>

            <div className="space-y-1.5">
              <Label>备注（notes，可选）</Label>
              <Textarea
                value={form.notes}
                rows={2}
                maxLength={500}
                onChange={(e) => setField("notes", e.target.value)}
                placeholder="例如：GLM 4.7，做路由分类器+中文短问；速率好但长文偶尔翻车"
              />
              <p className="text-xs text-muted-foreground">
                仅给自己看；路由器不读这个字段。
              </p>
            </div>
            </div>
          </details>

          {/* ── 模型管理（Fetch + Toggle + 自定义 + 测试）──────── */}
          {isEdit ? (
            <ProviderModelsSection
              providerId={form.id ?? null}
              models={form.models}
              defaultModel={form.default_model}
              onModelsChange={(next) => setField("models", next)}
              onSetDefault={(id) => setField("default_model", id)}
              providerKind={form.provider}
              apiFormat={form.api_format}
              baseUrl={form.base_url}
              apiKey={form.api_key}
              proxyId={form.proxy_id}
            />
          ) : null}

          </div>
          {!isEdit ? (
            <aside className="sticky top-0 hidden space-y-4 rounded-md border bg-card p-4 shadow-sm lg:block" aria-label="Provider 配置摘要">
              <div>
                <h3 className="text-sm font-semibold">即将创建</h3>
                <p className="mt-1 text-xs text-muted-foreground">验证通过后自动补全。</p>
              </div>
              <dl className="space-y-3 text-xs">
                <div>
                  <dt className="text-muted-foreground">接入地址</dt>
                  <dd className="mt-1 break-all font-mono">{form.base_url.trim() || DEFAULT_BASE_URLS[form.provider]}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">实际协议</dt>
                  <dd className="mt-1 font-medium">{form.api_format}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">客户端身份</dt>
                  <dd className="mt-1 font-medium">{form.client_identity_profile}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">默认模型</dt>
                  <dd className="mt-1 break-all font-mono">{form.default_model || "待选择"}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">启用模型</dt>
                  <dd className="mt-1 font-medium">{form.models.filter((model) => model.enabled).length} 个</dd>
                </div>
              </dl>
              <p className="border-t pt-3 text-xs leading-5 text-muted-foreground">
                路由策略沿用安全默认值，不阻塞首次接入。
              </p>
            </aside>
          ) : null}
          </div>
        </div>

        <DialogFooter className={cn("!flex !flex-row gap-2 sm:space-x-0 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none", !isEdit && "border-t bg-background px-4 py-3 sm:px-6")}>
          <Button variant="outline" onClick={requestCancel} disabled={saving}>
            取消
          </Button>
          <Button onClick={onSave} loading={saving} disabled={!isEdit && !createVerified}>
            {!saving ? <Save className="mr-2 h-4 w-4" /> : null}
            {!isEdit && !createVerified ? "先验证后保存" : "保存"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
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
// ProviderModelsSection：候选模型清单 + Fetch + 自定义添加 + 测试
// ═══════════════════════════════════════════════════════════
//
// 设计：
// - models 是 form 的本地状态；toggle / 删除 / 自定义添加都改本地，最终随"保存"PATCH 落库
// - "Fetch 模型列表"现在直接读编辑表单当前值（provider/base_url/api_key/api_format/proxy_id）
//   走 ``/fetch-models-preview`` 预览端点，不需要先保存；新增模型 merge 到 form.models 本地。
// - "测试连通性"仍需 provider 已落库（要解密 api_key 用 LLMClient 走正常路径），
//   未保存的 provider（form.id 为空）按钮置灰 + 提示"先保存"。
// - 模型按 enabled 拆两段：启用的常驻显示；未启用的默认折叠隐藏，点击展开。
function ProviderModelsSection({
  providerId,
  models,
  defaultModel,
  onModelsChange,
  onSetDefault,
  providerKind,
  apiFormat,
  baseUrl,
  apiKey,
  proxyId,
}: {
  providerId: number | null;
  models: ProviderModel[];
  defaultModel: string;
  onModelsChange: (next: ProviderModel[]) => void;
  onSetDefault: (id: string) => void;
  providerKind: LLMProviderKind;
  apiFormat: LLMApiFormat;
  baseUrl: string;
  apiKey: string;
  proxyId: string;
}) {
  const [customId, setCustomId] = useState("");
  // 测试某条模型时，记当前正在测的 id（用来禁用按钮 + 显示 spinner）
  const [testingId, setTestingId] = useState<string | null>(null);
  // 测试结果按 id 缓存：{[id]: {ok, latency_ms, error?}}
  const [testResults, setTestResults] = useState<
    Record<string, { ok: boolean; latency_ms: number; error?: string | null; preview?: string | null; model?: string | null }>
  >({});
  // 在途 test-model 请求的 AbortController：组件卸载 / 关编辑弹窗时中断，中断后不再回写状态。
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      abortRef.current = null;
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
        base_url: baseUrl ? baseUrl.trim() : null,
        // 编辑模式下若用户没重填 api_key，让后端回落到 DB 已存的
        api_key: apiKey ? apiKey : null,
        proxy_id: proxyId ? Number(proxyId) : null,
        pid: providerId,
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
    abortRef.current = controller;
    setTestingId(modelId);
    try {
      const r = await testProviderModel(
        providerId!,
        { model: modelId },
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      setTestResults((prev) => ({
        ...prev,
        [modelId]: {
          ok: r.ok,
          latency_ms: r.latency_ms,
          error: r.error,
          preview: r.preview,
          model: r.model,
        },
      }));
      if (r.ok) {
        toast.success(`${modelId} 通：${r.latency_ms} ms`);
      } else {
        toast.error(`${modelId} 失败（${r.latency_ms} ms）：${r.error || "未知"}`);
      }
    } catch (e) {
      if (controller.signal.aborted) return; // 被中断：静默
      toast.error(getErrMsg(e));
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
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
  //   [⭐(设默认) 或 默认徽章] / 测试 / 删除
  // 即第一个槽位永远是"设默认动作"——非默认显示 ⭐ 按钮、默认显示徽章占位；
  // 后两位永远是 测试 + 删除，避免列错位。
  const renderModelRow = (m: ProviderModel, idx: number) => {
    const isDefault = m.id === defaultModel;
    const result = testResults[m.id];
    return (
      <div
        key={m.id}
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
            <MetaBadge tone="success" className="text-[10px] leading-4">
              <CheckCircle2 className="h-3 w-3" />
              {result.latency_ms} ms
            </MetaBadge>
          ) : (
            <MetaBadge
              tone="danger"
              className="text-[10px] leading-4"
              title={result.error || ""}
            >
              <XCircle className="h-3 w-3" />
              失败
            </MetaBadge>
          )
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
        {/* 槽位 2：连通性测试 + 对话测活深链 */}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          loading={testingId === m.id}
          disabled={!persisted || (testingId !== null && testingId !== m.id)}
          onClick={() => onTest(m.id)}
          title={persisted ? "测试连通性 + 延时" : "先保存 provider 再测"}
        >
          测试
        </Button>
        {persisted && providerId != null ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            asChild
            title="打开对话测活并预选此模型"
          >
            <Link to={`/ai/liveness?provider=${providerId}&model=${encodeURIComponent(m.id)}`}>
              对话
            </Link>
          </Button>
        ) : null}
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
            点 <code>Fetch</code> 用<strong>当前编辑表单的字段</strong>（提供商 / Base URL / API 协议 / API Key / 代理）拉模型列表，
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
              当前没有启用任何模型。展开下方未启用列表勾选 / 或在上面 Fetch + 自定义添加
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

// ═══════════ 客户端身份 UA 版本配置弹窗（0.57.0 收口） ═══════════
function IdentityVersionsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const [items, setItems] = useState<ClientIdentityVersionItem[]>([]);
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
      setDrafts(Object.fromEntries(resp.items.map((i) => [i.key, i.current])));
      toast.success("已保存客户端身份 UA 版本");
    } catch (e) {
      setError(getErrMsg(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>客户端身份 UA 版本</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            仅调整身份 UA 里的版本号；UA 结构与请求头字段由证据锁定，不随此处变化。检测按钮向公共
            registry 查询最新版本作为建议值，保存后对后续 AI 请求生效。
          </p>
          {error ? <p className="break-words text-sm text-destructive">{error}</p> : null}
          {loading ? (
            <p className="text-sm text-muted-foreground">
              <Spinner className="mr-1" /> 加载中…
            </p>
          ) : (
            <div className="space-y-3">
              {items.map((it) => {
                const det = detected[it.key];
                return (
                  <div key={it.key} className="rounded-md border p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium">{it.label}</p>
                        <p className="break-words text-xs text-muted-foreground">
                          当前默认 {(drafts[it.key] || it.current).trim() || it.current}
                          {(drafts[it.key] || it.current).trim() !== it.default.trim()
                            ? ` · 内置基线 ${it.default}`
                            : ""}
                          {it.registry
                            ? ` · 源 ${it.registry === "cli:grok-update-check" ? "grok update --check / xAI stable" : it.registry}`
                            : " · 仅手动填写"}
                        </p>
                      </div>
                      <Input
                        className="w-40 font-mono text-xs"
                        value={drafts[it.key] ?? ""}
                        onChange={(e) =>
                          setDrafts((prev) => ({ ...prev, [it.key]: e.target.value }))
                        }
                      />
                    </div>
                    {det ? (
                      <p className="mt-2 break-words text-xs text-muted-foreground">
                        {det.error
                          ? `检测失败：${det.error}`
                          : det.latest
                            ? det.up_to_date
                              ? `已是最新（${det.latest}）`
                              : `最新 ${det.latest}（当前 ${det.current}）`
                            : "无检测结果"}
                        {det.latest && !det.up_to_date && !det.error ? (
                          <button
                            type="button"
                            className="ml-2 underline"
                            onClick={() =>
                              setDrafts((prev) => ({ ...prev, [it.key]: det.latest ?? prev[it.key] }))
                            }
                          >
                            填入
                          </button>
                        ) : null}
                      </p>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => void detect()} loading={detecting} disabled={loading}>
            检测最新版本
          </Button>
          <Button type="button" onClick={() => void save()} loading={saving} disabled={loading}>
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
