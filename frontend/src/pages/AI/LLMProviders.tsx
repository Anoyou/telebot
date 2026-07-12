// 系统设置 → LLM Provider 管理
// 用于"AI 类自定义指令"的大模型供应商凭据配置；API Key 在后端 Fernet 加密落库
// 列表里只显示 has_api_key:✓/✗，永远不会回显明文 key（与后端约定）
//
// 路由元数据（modality / tags / cost_tier / notes）：决定"自动路由"模式下
// 一条 ,ai 指令该把请求送给哪个 provider；详见 backend/services/llm_router.py
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { Plus, Trash2, KeyRound, Edit3, Download, Loader2, CheckCircle2, XCircle, Star, ChevronDown, ChevronRight, Filter, X, Package, Save, MessageSquare, Send, RotateCcw } from "lucide-react";

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
  chatTestProviderModels,
  createLLMProvider,
  deleteLLMProvider,
  detectProviderProtocols,
  fetchProviderModelsPreview,
  fullLivenessPreview,
  fullLivenessRun,
  listLLMProviders,
  patchLLMProvider,
  testProviderModel,
} from "@/api/commands";
import { listProxies } from "@/api/proxies";
import { getSystemSettings } from "@/api/system";
import type { ChatTestModelResult, ChatTestTurn, DetectProviderProtocolsResponse, FullLivenessPreviewResponse, FullLivenessRunResponse, LLMApiFormat, LLMClientIdentityProfile, LLMModality, LLMProtocolProfile, LLMProviderKind, LLMProviderOut, LLMTag, LLMWebSearchApiFormat, ProviderModel, ProtocolProbeResult, ProxyOut } from "@/api/types";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";
import { confirmDiscardChanges, useUnsavedChanges } from "@/lib/unsavedChanges";

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
  default_model: SUGGESTED_MODELS.openai,
  api_format: "chat_completions",
  protocol_profile: "standard",
  web_search_api_format: "auto",
  client_identity_profile: "auto",
  clearKey: false,
  modality: "text",
  tags: ["chat"],
  cost_tier: 2,
  notes: "",
  proxy_id: "",
  models: [],
};

export function LLMProviders({
  openCreateOnMount = false,
}: {
  openCreateOnMount?: boolean;
}) {
  const qc = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const didHandleCreateOnMount = useRef(false);
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

  const [editing, setEditing] = useState<FormState | null>(null);
  const [chatTestOpen, setChatTestOpen] = useState(false);
  const [fullLivenessOpen, setFullLivenessOpen] = useState(false);

  const visibleProviders = (listQ.data || []).filter((p) => {
    if (!isVisionFilter) return true;
    return p.modality === "vision" || p.modality === "multimodal";
  });

  const clearProviderFilter = () => {
    const next = new URLSearchParams(searchParams);
    next.delete("filter");
    setSearchParams(next, { replace: true });
  };

  useEffect(() => {
    const shouldOpenFromQuery = searchParams.get("newProvider") === "1";
    const shouldOpen = openCreateOnMount || shouldOpenFromQuery;

    if (shouldOpenFromQuery) {
      const next = new URLSearchParams(searchParams);
      next.delete("newProvider");
      setSearchParams(next, { replace: true });
    }

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
      setEditing(null);
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
      setEditing(null);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteLLMProvider(id),
    onSuccess: () => {
      toast.success("已删除");
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
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
      })),
    });
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <SectionHeader
              icon={Package}
              title="模型提供商"
              description={
                <>
                  一行 = 一个模型供应商凭据。配完 API Key + Base URL 后，在编辑里点
                  <strong>「Fetch 模型列表」</strong>就能自动拉取并可手动选择要启用的模型。<br />
                  <span className="text-muted-foreground/80">
                    modality（模态）+ tags（标签）+ cost_tier（成本档）这三项决定「自动路由」模式下该模型提供商所配置的模型是否被选中——详见 AI 帮助里的配置示例。
                  </span>
                </>
              }
              meta={
                <SignalPill
                  tone={visibleProviders.length > 0 ? "primary" : "warn"}
                  label="可见 Provider"
                  value={`${visibleProviders.length}`}
                />
              }
              className="flex-1"
            />
            <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:shrink-0 sm:justify-end">
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="flex-1 sm:flex-none"
                disabled={visibleProviders.length === 0}
                onClick={() => setChatTestOpen(true)}
              >
                <MessageSquare className="mr-1 h-4 w-4" /> 模型测活
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="flex-1 sm:flex-none"
                disabled={visibleProviders.length === 0}
                onClick={() => setFullLivenessOpen(true)}
              >
                <MessageSquare className="mr-1 h-4 w-4" /> 全量测活
              </Button>
              <Button size="sm" className="flex-1 sm:flex-none" onClick={() => setEditing({ ...EMPTY_FORM })}>
                <Plus className="mr-1 h-4 w-4" /> 新建
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
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

      {editing && (
        <ProviderEditDialog
          form={editing}
          onChange={setEditing}
          onCancel={() => setEditing(null)}
          onSave={() => {
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
          }}
          saving={createMut.isPending || updateMut.isPending}
        />
      )}
      <ProviderChatTestDialog
        open={chatTestOpen}
        onOpenChange={setChatTestOpen}
        providers={visibleProviders}
      />
      <FullLivenessDialog open={fullLivenessOpen} onOpenChange={setFullLivenessOpen} />
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
    <div className="rounded-xl border border-border/70 bg-background/70 p-3">
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

interface ChatTestDisplayResult extends ChatTestModelResult {
  pending?: boolean;
}

interface ChatTestRound {
  id: string;
  providerId: number;
  providerName: string;
  message: string;
  createdAt: number;
  results: ChatTestDisplayResult[];
}

const DEFAULT_CHAT_TEST_MESSAGE = "我想知道你现在在想啥呢？我好无聊";
const DEFAULT_CHAT_TEST_SYSTEM_PROMPT =
  "你是一个自然、简洁的中文聊天助手。请像真实聊天一样直接回复用户，不要只返回 ping/pong。";

function providerModelChoices(provider: LLMProviderOut | null | undefined): ProviderModel[] {
  if (!provider) return [];
  const seen = new Set<string>();
  const out: ProviderModel[] = [];
  const add = (id: string, enabled = true, custom = false, label: string | null = null) => {
    const modelId = String(id || "").trim();
    if (!modelId || seen.has(modelId)) return;
    seen.add(modelId);
    out.push({ id: modelId, enabled, custom, label });
  };
  for (const item of provider.models || []) {
    add(item.id, !!item.enabled, !!item.custom, item.label ?? null);
  }
  add(provider.default_model, true, false, "默认模型");
  return out.sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.id.localeCompare(b.id));
}

function ProviderChatTestDialog({
  open,
  onOpenChange,
  providers,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  providers: LLMProviderOut[];
}) {
  const [providerId, setProviderId] = useState<number | null>(providers[0]?.id ?? null);
  const selectedProvider = providers.find((item) => item.id === providerId) || providers[0] || null;
  const modelChoices = providerModelChoices(selectedProvider);
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [message, setMessage] = useState(DEFAULT_CHAT_TEST_MESSAGE);
  const [systemPrompt, setSystemPrompt] = useState(DEFAULT_CHAT_TEST_SYSTEM_PROMPT);
  const [maxTokens, setMaxTokens] = useState(1200);
  const [timeoutSeconds, setTimeoutSeconds] = useState(90);
  const [rounds, setRounds] = useState<ChatTestRound[]>([]);
  const [histories, setHistories] = useState<Record<string, ChatTestTurn[]>>({});
  const [running, setRunning] = useState(false);

  // 在途测活请求的 AbortController：关弹窗 / 组件卸载 / 切 provider 时统一中断；
  // 中断后其回调不再回写 state（见 sendTest 里的 signal.aborted 判定）。
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

  // 关弹窗 / 切 provider：中断在途请求，复位 running 并把仍 pending 的结果收尾为"已取消"，
  // 保持 loading 状态机自洽（不让 running=false 时卡片还在转圈）。无在途请求时是空操作。
  const abortInFlight = () => {
    const controller = abortRef.current;
    if (!controller) return;
    controller.abort();
    abortRef.current = null;
    if (!mountedRef.current) return;
    setRunning(false);
    setRounds((prev) =>
      prev.map((round) => ({
        ...round,
        results: round.results.map((item) =>
          item.pending
            ? { ...item, pending: false, ok: false, empty_response: false, error: "已取消" }
            : item,
        ),
      })),
    );
  };

  useEffect(() => {
    if (!open) abortInFlight();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    if (!selectedProvider && providers[0]) {
      setProviderId(providers[0].id);
    }
  }, [open, selectedProvider, providers]);

  useEffect(() => {
    if (!open || !selectedProvider) return;
    const enabled = providerModelChoices(selectedProvider).filter((item) => item.enabled).map((item) => item.id);
    setSelectedModels((prev) => {
      const valid = prev.filter((id) => modelChoices.some((item) => item.id === id));
      if (valid.length > 0) return valid;
      return enabled.length > 0 ? enabled.slice(0, 8) : modelChoices.slice(0, 8).map((item) => item.id);
    });
  }, [open, selectedProvider?.id]);

  const toggleModel = (modelId: string) => {
    setSelectedModels((prev) =>
      prev.includes(modelId) ? prev.filter((item) => item !== modelId) : [...prev, modelId],
    );
  };

  const setEnabledModels = () => {
    const enabled = modelChoices.filter((item) => item.enabled).map((item) => item.id);
    setSelectedModels(enabled.length > 0 ? enabled : modelChoices.map((item) => item.id));
  };

  const resetConversation = () => {
    setRounds([]);
    setHistories({});
  };

  const updateRoundResult = (roundId: string, modelId: string, result: ChatTestDisplayResult) => {
    setRounds((prev) =>
      prev.map((round) =>
        round.id === roundId
          ? {
              ...round,
              results: round.results.map((item) =>
                item.requested_model === modelId ? result : item,
              ),
            }
          : round,
      ),
    );
  };

  const sendTest = async () => {
    const provider = selectedProvider;
    const text = message.trim();
    if (!provider) {
      toast.error("请先选择模型提供商");
      return;
    }
    if (selectedModels.length === 0) {
      toast.error("请至少选择一个模型");
      return;
    }
    if (!text) {
      toast.error("测试语不能为空");
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setRunning(true);
    const createdAt = Date.now();
    const roundId = `${createdAt}`;
    const modelsToTest = [...selectedModels];
    const historiesForRequest = modelsToTest.reduce<Record<string, ChatTestTurn[]>>((acc, modelId) => {
      const key = `${provider.id}:${modelId}`;
      acc[modelId] = histories[key] || [];
      return acc;
    }, {});
    setRounds((prev) => [
      ...prev,
      {
        id: roundId,
        providerId: provider.id,
        providerName: provider.name,
        message: text,
        createdAt,
        results: modelsToTest.map((modelId) => ({
          ok: false,
          requested_model: modelId,
          latency_ms: 0,
          input_tokens: 0,
          output_tokens: 0,
          empty_response: false,
          pending: true,
        })),
      },
    ]);
    setHistories((prev) => {
      const next = { ...prev };
      const userTurn: ChatTestTurn = { role: "user", content: text };
      for (const modelId of modelsToTest) {
        const key = `${provider.id}:${modelId}`;
        next[key] = [...(next[key] || []), userTurn].slice(-16);
      }
      return next;
    });
    try {
      await Promise.all(
        modelsToTest.map(async (modelId) => {
          try {
            const response = await chatTestProviderModels(
              provider.id,
              {
                models: [modelId],
                message: text,
                history: historiesForRequest[modelId] || [],
                system_prompt: systemPrompt.trim() || DEFAULT_CHAT_TEST_SYSTEM_PROMPT,
                max_tokens: maxTokens,
                timeout_seconds: timeoutSeconds,
              },
              { signal: controller.signal },
            );
            if (controller.signal.aborted) return;
            const result = response.results[0] || {
              ok: false,
              requested_model: modelId,
              latency_ms: 0,
              input_tokens: 0,
              output_tokens: 0,
              empty_response: true,
              error: "后端没有返回该模型的测活结果。",
            };
            updateRoundResult(roundId, modelId, result);
            if (result.ok && result.response) {
              setHistories((prev) => {
                const key = `${provider.id}:${modelId}`;
                const next = { ...prev };
                const assistantTurn: ChatTestTurn = { role: "assistant", content: result.response };
                next[key] = [...(next[key] || []), assistantTurn].slice(-16);
                return next;
              });
            }
          } catch (err) {
            if (controller.signal.aborted) return; // 被中断：静默，不当作失败回写
            updateRoundResult(roundId, modelId, {
              ok: false,
              requested_model: modelId,
              latency_ms: 0,
              input_tokens: 0,
              output_tokens: 0,
              empty_response: false,
              error: getErrMsg(err),
            });
          }
        }),
      );
    } finally {
      // 仅当这批仍是"当前批"时收尾；被 abort / 切换后 abortRef 已换人，避免误改 running
      if (abortRef.current === controller) {
        abortRef.current = null;
        if (mountedRef.current) setRunning(false);
      }
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[88vh] max-w-5xl flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>模型测活</DialogTitle>
          <DialogDescription>
            选择一个 Provider 和多个模型，用真实聊天请求并发获取回复；本窗口内会临时保留上下文，关闭后不落库。
          </DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
          <div className="min-h-0 space-y-4 overflow-y-auto rounded-md border bg-muted/20 p-3">
            <div className="space-y-1.5">
              <Label>模型提供商</Label>
              <Select
                value={selectedProvider ? String(selectedProvider.id) : ""}
                onChange={(event) => {
                  abortInFlight();
                  const nextId = Number(event.target.value);
                  setProviderId(Number.isFinite(nextId) ? nextId : null);
                  setSelectedModels([]);
                }}
              >
                {providers.map((provider) => (
                  <option key={provider.id} value={String(provider.id)}>
                    {provider.name} · {provider.provider}
                  </option>
                ))}
              </Select>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <Label>选择模型</Label>
                <div className="flex items-center gap-1">
                  <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={setEnabledModels}>
                    已启用
                  </Button>
                  <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => setSelectedModels(modelChoices.map((item) => item.id))}>
                    全选
                  </Button>
                  <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => setSelectedModels([])}>
                    清空
                  </Button>
                </div>
              </div>
              <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border bg-background p-1">
                {modelChoices.length > 0 ? (
                  modelChoices.map((model) => (
                    <label
                      key={model.id}
                      className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-muted/60"
                    >
                      <input
                        type="checkbox"
                        checked={selectedModels.includes(model.id)}
                        onChange={() => toggleModel(model.id)}
                      />
                      <span className="min-w-0 flex-1 truncate font-mono" title={model.id}>
                        {model.id}
                      </span>
                      {model.id === selectedProvider?.default_model ? <MetaBadge tone="success">默认</MetaBadge> : null}
                      {model.enabled ? <MetaBadge>启用</MetaBadge> : null}
                    </label>
                  ))
                ) : (
                  <div className="px-2 py-4 text-center text-xs text-muted-foreground">
                    当前 Provider 没有模型清单，请先 Fetch 或手动添加。
                  </div>
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div className="space-y-1.5">
                <Label>最大输出 Token</Label>
                <Input
                  type="number"
                  min={64}
                  max={8000}
                  value={maxTokens}
                  onChange={(event) => setMaxTokens(Number(event.target.value) || 1200)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>超时秒数</Label>
                <Input
                  type="number"
                  min={10}
                  max={600}
                  value={timeoutSeconds}
                  onChange={(event) => setTimeoutSeconds(Number(event.target.value) || 90)}
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>系统提示词</Label>
              <Textarea
                value={systemPrompt}
                rows={4}
                maxLength={2000}
                onChange={(event) => setSystemPrompt(event.target.value)}
              />
            </div>
          </div>

          <div className="flex min-h-0 flex-col overflow-hidden rounded-md border bg-background">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
              <div className="text-sm font-medium">测试对话</div>
              <Button type="button" variant="ghost" size="sm" onClick={resetConversation} disabled={running || rounds.length === 0}>
                <RotateCcw className="mr-1 h-4 w-4" />
                清空上下文
              </Button>
            </div>
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-muted/20 p-3">
              {rounds.length === 0 ? (
                <div className="flex h-full min-h-64 items-center justify-center rounded-md border border-dashed bg-background px-4 text-center text-sm text-muted-foreground">
                  输入一句自然测试语并发送后，这里会显示每个模型的实时回复、耗时和 token 统计。
                </div>
              ) : (
                rounds.map((round) => (
                  <div key={round.id} className="space-y-3">
                    <div className="flex justify-end">
                      <div className="max-w-[82%] rounded-md bg-primary px-3 py-2 text-sm leading-6 text-primary-foreground">
                        <div className="mb-1 text-[11px] opacity-80">
                          {round.providerName} · {new Date(round.createdAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                        </div>
                        {round.message}
                      </div>
                    </div>
                    <div className="grid gap-2 xl:grid-cols-2">
                      {round.results.map((result) => (
                        <ChatTestResultCard key={`${round.id}:${result.requested_model}`} result={result} />
                      ))}
                    </div>
                  </div>
                ))
              )}
              {running ? (
                <div className="flex items-center gap-2 rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin text-primary" />
                  正在并发请求 {selectedModels.length} 个模型，结果会逐个更新
                </div>
              ) : null}
            </div>
            <div className="border-t bg-background p-3">
              <div className="flex gap-2">
                <Textarea
                  value={message}
                  rows={2}
                  maxLength={2000}
                  onChange={(event) => setMessage(event.target.value)}
                  onKeyDown={(event) => {
                    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                      event.preventDefault();
                      if (!running) void sendTest();
                    }
                  }}
                  placeholder="输入一条真实聊天测试语"
                  className="min-h-12"
                />
                <Button type="button" className="h-auto self-stretch" disabled={running} onClick={() => void sendTest()}>
                  {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                  <span className="sr-only">发送</span>
                </Button>
              </div>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ChatTestResultCard({ result }: { result: ChatTestDisplayResult }) {
  return (
    <div className="rounded-md border bg-background px-3 py-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-mono text-xs font-semibold" title={result.requested_model}>
            {result.requested_model}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {result.pending ? "等待响应" : `${result.latency_ms} ms`}
            {result.model ? ` · ${result.model}` : ""}
            {result.input_tokens || result.output_tokens ? ` · ${result.input_tokens}/${result.output_tokens} tokens` : ""}
          </div>
        </div>
        <MetaBadge tone={result.pending ? undefined : result.ok ? "success" : "danger"}>
          {result.pending ? "请求中" : result.ok ? "可用" : result.empty_response ? "空返回" : "失败"}
        </MetaBadge>
      </div>
      {result.pending ? (
        <div className="mt-2 flex items-center gap-2 rounded-md bg-muted/40 px-3 py-2 text-sm leading-6 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin text-primary" />
          正在等待模型返回
        </div>
      ) : result.ok && result.response ? (
        <div className="mt-2 whitespace-pre-wrap break-words rounded-md bg-muted/40 px-3 py-2 text-sm leading-6">
          {result.response}
        </div>
      ) : (
        <div className="mt-2 break-words rounded-md bg-destructive/10 px-3 py-2 text-sm leading-6 text-destructive">
          {result.error || "没有拿到可展示文本。"}
        </div>
      )}
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
          api_format: recommendedApiFormat,
          protocol_profile:
            recommendedApiFormat === "anthropic_messages" ? form.protocol_profile : "standard",
          web_search_api_format: (resp.recommended_web_search_api_format || "auto") as LLMWebSearchApiFormat,
          client_identity_profile:
            (resp.recommended_client_identity_profile as LLMClientIdentityProfile) ||
            form.client_identity_profile,
        });
        toast.success("已检测并填入推荐协议与客户端身份");
      } else {
        toast.warning("没有检测到推荐协议，请查看探测详情");
      }
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  return (
    <Dialog open onOpenChange={(o) => !o && requestCancel()}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? "编辑" : "新建"}模型提供商</DialogTitle>
          <DialogDescription>
            API Key 加密落库；列表中只显示是否已配置，永远不回显明文。
          </DialogDescription>
        </DialogHeader>

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
                  setField("provider", p);
                  // 切提供商时给出建议默认模型 ID（若用户没改过）
                  if (
                    !form.default_model ||
                    Object.values(SUGGESTED_MODELS).includes(form.default_model)
                  ) {
                    onChange({
                      ...form,
                      provider: p,
                      default_model: SUGGESTED_MODELS[p],
                    });
                  }
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
                value={form.default_model}
                maxLength={64}
                onChange={(e) => setField("default_model", e.target.value)}
                placeholder={SUGGESTED_MODELS[form.provider]}
              />
              <p className="text-xs text-muted-foreground">
                自动路由 fallback 时用；可在下方"模型管理"区点 ✓ 直接设为此值
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
                disabled={detectProtocolsMut.isPending || (!isEdit && !form.api_key.trim() && form.provider !== "ollama")}
                onClick={() => detectProtocolsMut.mutate()}
              >
                {detectProtocolsMut.isPending ? (
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                ) : (
                  <Download className="mr-1 h-4 w-4" />
                )}
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
            <Input
              type="password"
              value={form.api_key}
              maxLength={512}
              autoComplete="off"
              onChange={(e) => setField("api_key", e.target.value)}
              placeholder={isEdit && form.hasApiKey && !form.api_key ? MASKED_SECRET_PLACEHOLDER : isEdit ? "留空 = 保持原 key 不变" : "sk-..."}
              disabled={form.clearKey}
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
              Ollama 本地部署可不填。其它厂商请到对应控制台获取。
            </p>
          </div>

          {/* ── 路由元数据区 ─────────────────────────── */}
          <div className="rounded-md border bg-muted/30 p-3 space-y-3">
            <div>
              <Label className="text-sm font-semibold">路由元数据</Label>
              <p className="text-xs text-muted-foreground">
                这些字段决定「自动路由」模式下，一条 <CommandBadge>{cmdPrefix}ai</CommandBadge> 指令的请求是否会被分配给本 provider。
                只用 fixed 模式可以全留默认。
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

          {/* ── 模型管理（Fetch + Toggle + 自定义 + 测试）──────── */}
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

          {/* ── 出口代理 ───────────────────────────── */}
          <div className="rounded-md border bg-muted/30 p-3 space-y-2">
            <div>
              <Label className="text-sm font-semibold">出口代理</Label>
              <p className="text-xs text-muted-foreground">
                调 LLM API 的 HTTP 流量走哪个代理。各 provider 可独立选；
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
        </div>

        <DialogFooter className="!flex !flex-row gap-2 sm:space-x-0 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
          <Button variant="outline" onClick={requestCancel} disabled={saving}>
            取消
          </Button>
          <Button onClick={onSave} disabled={saving}>
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
            保存
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

  // Fetch 按钮可用性：anthropic 不支持；新建模式下也允许（用户手填的 api_key 直接用）；
  // 编辑模式下用户没改 api_key 时后端会回落到 DB 已存的——也允许。
  const fetchDisabledHint =
    providerKind === "anthropic"
      ? "Anthropic 不支持列出模型接口，请手动添加"
      : !persisted && !apiKey.trim() && providerKind !== "ollama"
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
        {/* 槽位 2：测试 */}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={!persisted || testingId !== null}
          onClick={() => onTest(m.id)}
          title={persisted ? "测试连通性 + 延时" : "先保存 provider 再测"}
        >
          {testingId === m.id ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            "测试"
          )}
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
          disabled={providerKind === "anthropic" || fetchMut.isPending || !!fetchDisabledHint}
          onClick={() => fetchMut.mutate()}
          title={fetchDisabledHint || "用当前表单字段拉模型列表（不必先保存）"}
        >
          {fetchMut.isPending ? (
            <Loader2 className="mr-1 h-4 w-4 animate-spin" />
          ) : (
            <Download className="mr-1 h-4 w-4" />
          )}
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

// ════════════════════════════════════════════════════════════
// 阶段 C：全量已启用模型测活弹窗
// ════════════════════════════════════════════════════════════

function livenessStatusTone(status: string): "success" | "warn" | "neutral" {
  if (status === "healthy") return "success";
  if (status === "cancelled" || status === "skipped_disabled") return "neutral";
  return "warn";
}

const LIVENESS_STATUS_LABEL: Record<string, string> = {
  healthy: "正常",
  empty_response: "空响应",
  rate_limited: "限流(429)",
  auth_failed: "鉴权失败(401)",
  client_rejected: "身份被拒",
  protocol_rejected: "协议不符",
  model_missing: "模型缺失",
  timeout: "超时",
  upstream_error: "上游错误",
  config_error: "配置缺失",
  cancelled: "已取消",
  network_error: "网络异常",
  skipped_provider_missing: "Provider 缺失",
};

function livenessStatusLabel(status: string): string {
  return LIVENESS_STATUS_LABEL[status] ?? status;
}

function FullLivenessDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const [preview, setPreview] = useState<FullLivenessPreviewResponse | null>(null);
  const [result, setResult] = useState<FullLivenessRunResponse | null>(null);
  const [maxTokens, setMaxTokens] = useState(256);
  const [globalConcurrency, setGlobalConcurrency] = useState(8);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open) {
      setPreview(null);
      setResult(null);
      abortRef.current?.abort();
      abortRef.current = null;
    }
  }, [open]);

  const previewMut = useMutation({
    mutationFn: () => fullLivenessPreview({ max_tokens: maxTokens, global_concurrency: globalConcurrency }),
    onSuccess: (resp) => {
      setPreview(resp);
      setResult(null);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const runMut = useMutation({
    mutationFn: () => {
      const controller = new AbortController();
      abortRef.current = controller;
      return fullLivenessRun(
        { max_tokens: maxTokens, global_concurrency: globalConcurrency },
        { signal: controller.signal },
      );
    },
    onSuccess: (resp) => {
      setResult(resp);
      abortRef.current = null;
    },
    onError: (err) => {
      abortRef.current = null;
      toast.error(getErrMsg(err));
    },
  });

  const running = runMut.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>全量模型测活</DialogTitle>
          <DialogDescription>
            按各 Provider 已启用（enabled）的模型做真实调用探活。仅返回脱敏诊断结果，不修改生产健康状态、不自动禁用模型。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label className="text-xs">输出上限 (max_tokens)</Label>
              <Input
                type="number"
                className="w-32"
                min={64}
                max={8000}
                value={maxTokens}
                onChange={(e) => setMaxTokens(Math.max(64, Math.min(8000, Number(e.target.value) || 256)))}
                disabled={running}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">全局并发</Label>
              <Select
                className="w-24"
                value={String(globalConcurrency)}
                onChange={(e) => setGlobalConcurrency(Number(e.target.value))}
                disabled={running}
              >
                {[2, 4, 8, 12].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </Select>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => previewMut.mutate()}
              disabled={previewMut.isPending || running}
            >
              {previewMut.isPending ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : null}
              生成预览
            </Button>
          </div>

          {preview ? (
            <div className="rounded-md border bg-muted/30 p-3 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <MetaBadge>可执行 Provider {preview.executable_provider_total}/{preview.provider_total}</MetaBadge>
                <MetaBadge>任务 {preview.task_total}</MetaBadge>
                <MetaBadge>已启用模型 {preview.enabled_model_total}</MetaBadge>
                <MetaBadge mono>最多输出 ~{preview.max_output_tokens} tok</MetaBadge>
                {preview.needs_confirmation ? (
                  <MetaBadge tone="warn">任务较多，请确认</MetaBadge>
                ) : null}
              </div>
              <div className="mt-2 space-y-1">
                {preview.providers.map((p) => (
                  <div key={p.provider_id} className="flex flex-wrap items-center justify-between gap-2 rounded border bg-background px-2 py-1">
                    <span className="font-medium">{p.provider_name}</span>
                    <span className="flex flex-wrap items-center gap-1">
                      {p.executable ? (
                        <MetaBadge tone="success">{p.enabled_models.length} 个已启用模型</MetaBadge>
                      ) : (
                        <MetaBadge tone="warn">跳过：{livenessStatusLabel(p.skipped_reason || "no_enabled_models")}</MetaBadge>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">先生成预览，确认执行范围与成本后再运行。</p>
          )}

          {result ? (
            <div className="rounded-md border bg-muted/30 p-3 text-xs">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <MetaBadge tone="success">正常 {result.healthy}</MetaBadge>
                <MetaBadge tone="warn">失败 {result.failed}</MetaBadge>
                {result.skipped ? <MetaBadge>跳过 {result.skipped}</MetaBadge> : null}
                {result.cancelled ? <MetaBadge>取消 {result.cancelled}</MetaBadge> : null}
                <MetaBadge>共 {result.task_total}</MetaBadge>
              </div>
              <div className="max-h-72 space-y-1 overflow-y-auto">
                {result.results.map((r, i) => (
                  <div key={`${r.provider_id}-${r.model_id}-${i}`} className="rounded border bg-background px-2 py-1.5">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="min-w-0 break-all font-mono text-[11px]">{r.provider_name} · {r.model_id}</span>
                      <MetaBadge mono tone={livenessStatusTone(r.status)}>
                        {livenessStatusLabel(r.status)}{r.latency_ms ? ` · ${r.latency_ms}ms` : ""}
                      </MetaBadge>
                    </div>
                    {r.preview ? <div className="mt-1 break-words text-muted-foreground">{r.preview}</div> : null}
                    {r.error ? <div className="mt-1 break-words text-amber-600 dark:text-amber-400">{r.error}</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>

        <DialogFooter>
          {running ? (
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                abortRef.current?.abort();
                abortRef.current = null;
              }}
            >
              停止
            </Button>
          ) : null}
          <Button
            type="button"
            onClick={() => runMut.mutate()}
            disabled={running || !preview || preview.task_total === 0}
          >
            {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : null}
            {running ? "测活中…" : "开始测活"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
