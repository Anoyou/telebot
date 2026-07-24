// 系统设置 → 自定义指令模板（4 种类型：reply_text / forward_to / run_plugin / ai）
//
// 设计：
//   列表页：全表展示模板，name 徽章 type，编辑/删除按钮
//   编辑对话框：根据 type 切不同子表单
//   保存后后端会通知所有启用此模板的 worker 热加载
import { forwardRef, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ChevronDown, Plus, Save, Trash2, Edit3 } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { OutputFormatFields } from "@/components/ai/OutputFormatFields";
import {
  ReasoningEffortSelect,
  reasoningEffortsForModel,
  type ReasoningEffortValue,
} from "@/components/ai/ReasoningEffortSelect";
import { RoutingFields } from "@/components/ai/RoutingFields";
import { WebSearchFields } from "@/components/ai/WebSearchFields";
import { CommandBadge } from "@/components/CommandBadge";
import { Spinner } from "@/components/ui/misc";
import { TelegramHtmlPreview } from "@/components/TelegramHtmlPreview";
import { SignalPill } from "@/components/ui/status";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
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
  createCommandTemplate,
  deleteCommandTemplate,
  disableAccountCommand,
  enableAccountCommand,
  listAccountCommands,
  listBuiltinCommands,
  listCommandTemplates,
  listLLMProviders,
  patchCommandTemplate,
} from "@/api/commands";
import { listAccounts } from "@/api/accounts";
import { getSystemSettings, patchSystemSettings } from "@/api/system";
import type {
  AccountCommandItem,
  CommandTemplateOut,
  CommandTemplateType,
  LLMApiFormat,
  LLMProviderOut,
} from "@/api/types";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";

// 指令名仅允许 [a-zA-Z0-9_]，与后端正则对齐
const NAME_RE = /^[a-zA-Z0-9_]{1,64}$/;
const ALIAS_RE = /^[a-zA-Z0-9_]{1,16}$/;

const TYPE_LABELS: Record<CommandTemplateType, string> = {
  reply_text: "回复文本",
  forward_to: "转发到",
  run_plugin: "调插件",
  ai: "AI",
};

type AiCapability = "routing" | "search" | "output" | "params";
type AiCommandMode = "chat" | "search" | "image" | "video";
type AiReasoningEffort = ReasoningEffortValue;

const AI_MODE_DEFAULTS: Record<
  AiCommandMode,
  {
    temperature: string;
    reasoning_effort: AiReasoningEffort;
    timeout_seconds: string;
  }
> = {
  chat: { temperature: "0.7", reasoning_effort: "medium", timeout_seconds: "60" },
  search: { temperature: "0.2", reasoning_effort: "medium", timeout_seconds: "90" },
  image: { temperature: "0.8", reasoning_effort: "low", timeout_seconds: "180" },
  video: { temperature: "0.8", reasoning_effort: "low", timeout_seconds: "300" },
};

const DEFAULT_AI_TEMPERATURE = AI_MODE_DEFAULTS.chat.temperature;
const DEFAULT_AI_REASONING_EFFORT = AI_MODE_DEFAULTS.chat.reasoning_effort;
const DEFAULT_AI_TIMEOUT_SECONDS = AI_MODE_DEFAULTS.chat.timeout_seconds;
const REPLY_TEXT_PREVIEW_VALUES = {
  args: "帮我把这句话翻译成英文并保留语气",
  cmd: "translate",
};

interface FormState {
  id?: number;
  name: string;
  type: CommandTemplateType;
  description: string;
  aliases_text: string;
  // 各 type 的 config 字段散开存，按 type 切表单时拼回
  text: string;
  target_chat_id: string;
  /** forward_to：触发指令后多少秒删指令消息（空 / 0 = 不删） */
  forward_delete_after: string;
  /** forward_to：成功后立即删除指令消息（不等待） */
  forward_delete_immediately: boolean;
  /** forward_to：转发方式（forward_native/copy_text/copy_media/quote/link_only） */
  forward_mode: string;
  plugin_key: string;
  plugin_method: string;
  plugin_args: string; // JSON string
  ai_mode: AiCommandMode;
  ai_provider_id: string; // <select> value，转 number 后下发
  ai_model: string;
  ai_system_prompt: string;
  ai_max_tokens: string;
  ai_temperature: string;
  ai_reasoning_effort: AiReasoningEffort;
  ai_timeout_seconds: string;
  ai_quote_replied: boolean;
  // ── 路由（auto 模式才用到，fixed 留空即可）──
  ai_routing_mode: "fixed" | "auto";
  ai_routing_fallback_provider_id: string;  // <select> value
  ai_classifier_provider_id: string;        // <select> value，可空
  // ── 输出格式（消息编辑回 TG 时长什么样）──
  ai_output_format: "html" | "markdown" | "plain";
  ai_output_template: string;
  ai_escape_values: boolean;
  ai_web_search: boolean;
  ai_web_search_context_size: "low" | "medium" | "high";
  ai_image_backend: "codex_image" | "llm";
  ai_video_backend: "plugin";
  ai_video_plugin_key: string;
  ai_video_plugin_method: string;
  // ── 发送方式 ──
  // edit:    原地编辑 ,ai 指令消息（默认；保留 reply 链）
  // send_new: 删指令、发新消息（不带 reply_to）——避免在被回复方那里留下"你回复了我"痕迹
  ai_send_mode: "edit" | "send_new";
}

const EMPTY_FORM: FormState = {
  name: "",
  type: "reply_text",
  description: "",
  aliases_text: "",
  text: "",
  target_chat_id: "",
  forward_delete_after: "",
  forward_delete_immediately: false,
  forward_mode: "forward_native",
  plugin_key: "",
  plugin_method: "",
  plugin_args: "[]",
  ai_mode: "chat",
  ai_provider_id: "",
  ai_model: "",
  ai_system_prompt: "你是简洁有用的中文助手。回答控制在 100 字内。",
  ai_max_tokens: "512",
  ai_temperature: DEFAULT_AI_TEMPERATURE,
  ai_reasoning_effort: DEFAULT_AI_REASONING_EFFORT,
  ai_timeout_seconds: DEFAULT_AI_TIMEOUT_SECONDS,
  ai_quote_replied: true,
  ai_routing_mode: "fixed",
  ai_routing_fallback_provider_id: "",
  ai_classifier_provider_id: "",
  ai_output_format: "html",
  ai_output_template: "",
  ai_escape_values: true,
  ai_web_search: false,
  ai_web_search_context_size: "medium",
  ai_image_backend: "codex_image",
  ai_video_backend: "plugin",
  ai_video_plugin_key: "video_bridge",
  ai_video_plugin_method: "",
  ai_send_mode: "edit",
};

function normalizeAiMode(value: unknown): AiCommandMode {
  return value === "search" || value === "image" || value === "video" ? value : "chat";
}

function applyAiModeDefaults(form: FormState, nextMode: AiCommandMode): Partial<FormState> {
  const currentDefaults = AI_MODE_DEFAULTS[form.ai_mode];
  const nextDefaults = AI_MODE_DEFAULTS[nextMode];
  return {
    ai_temperature:
      !form.ai_temperature || form.ai_temperature === currentDefaults.temperature
        ? nextDefaults.temperature
        : form.ai_temperature,
    ai_reasoning_effort:
      !form.ai_reasoning_effort || form.ai_reasoning_effort === currentDefaults.reasoning_effort
        ? nextDefaults.reasoning_effort
        : form.ai_reasoning_effort,
    ai_timeout_seconds:
      !form.ai_timeout_seconds || form.ai_timeout_seconds === currentDefaults.timeout_seconds
        ? nextDefaults.timeout_seconds
        : form.ai_timeout_seconds,
  };
}

function renderReplyTextPreview(template: string): string {
  const source = template || "hello {args}";
  return source
    .replace(/\{args\}/g, REPLY_TEXT_PREVIEW_VALUES.args)
    .replace(/\{cmd\}/g, REPLY_TEXT_PREVIEW_VALUES.cmd);
}

function formFromTemplate(t: CommandTemplateOut): FormState {
  const cfg = t.config || {};
  const aiMode = normalizeAiMode(cfg.mode);
  const modeDefaults = AI_MODE_DEFAULTS[aiMode];
  return {
    id: t.id,
    name: t.name,
    type: t.type,
    description: t.description || "",
    aliases_text: (t.aliases || []).join(", "),
    text: typeof cfg.text === "string" ? (cfg.text as string) : "",
    target_chat_id:
      cfg.target_chat_id !== undefined && cfg.target_chat_id !== null
        ? String(cfg.target_chat_id)
        : "",
    forward_delete_after:
      cfg.delete_after !== undefined && cfg.delete_after !== null
        ? String(cfg.delete_after)
        : "",
    forward_delete_immediately: !!cfg.delete_immediately,
    forward_mode: typeof cfg.mode === "string" ? (cfg.mode as string) : "forward_native",
    plugin_key: typeof cfg.plugin_key === "string" ? (cfg.plugin_key as string) : "",
    plugin_method: typeof cfg.method === "string" ? (cfg.method as string) : "",
    plugin_args: cfg.args ? JSON.stringify(cfg.args) : "[]",
    ai_mode: aiMode,
    ai_provider_id:
      cfg.provider_id !== undefined && cfg.provider_id !== null
        ? String(cfg.provider_id)
        : "",
    ai_model: typeof cfg.model === "string" ? (cfg.model as string) : "",
    ai_system_prompt:
      typeof cfg.system_prompt === "string"
        ? (cfg.system_prompt as string)
        : EMPTY_FORM.ai_system_prompt,
    ai_max_tokens:
      cfg.max_tokens !== undefined && cfg.max_tokens !== null
        ? String(cfg.max_tokens)
        : "512",
    ai_temperature:
      cfg.temperature !== undefined && cfg.temperature !== null
        ? String(cfg.temperature)
        : modeDefaults.temperature,
    ai_reasoning_effort:
      cfg.reasoning_effort === "minimal" ||
        cfg.reasoning_effort === "low" ||
      cfg.reasoning_effort === "medium" ||
      cfg.reasoning_effort === "high" ||
      cfg.reasoning_effort === "xhigh" ||
      cfg.reasoning_effort === "max"
        ? cfg.reasoning_effort
        : modeDefaults.reasoning_effort,
    ai_timeout_seconds:
      cfg.timeout_seconds !== undefined && cfg.timeout_seconds !== null
        ? String(cfg.timeout_seconds)
        : modeDefaults.timeout_seconds,
    ai_quote_replied: cfg.quote_replied !== false, // 默认 true
    ai_routing_mode:
      cfg.routing_mode === "auto" ? "auto" : "fixed",
    ai_routing_fallback_provider_id:
      cfg.routing_fallback_provider_id !== undefined &&
        cfg.routing_fallback_provider_id !== null
        ? String(cfg.routing_fallback_provider_id)
        : "",
    ai_classifier_provider_id:
      cfg.classifier_provider_id !== undefined &&
        cfg.classifier_provider_id !== null
        ? String(cfg.classifier_provider_id)
        : "",
    ai_output_format:
      cfg.output_format === "html" ||
        cfg.output_format === "markdown" ||
        cfg.output_format === "plain"
        ? cfg.output_format
        : "html", // 老 'markdownv2' / 缺省 → 默认 html
    ai_output_template: typeof cfg.output_template === "string" ? cfg.output_template : "",
    ai_escape_values: cfg.escape_values !== false,
    ai_web_search: cfg.web_search === true,
    ai_web_search_context_size:
      cfg.web_search_context_size === "low" ||
        cfg.web_search_context_size === "medium" ||
        cfg.web_search_context_size === "high"
        ? cfg.web_search_context_size
        : "medium",
    ai_image_backend: cfg.image_backend === "llm" ? "llm" : "codex_image",
    ai_video_backend: "plugin",
    ai_video_plugin_key:
      typeof cfg.video_plugin_key === "string" ? cfg.video_plugin_key : EMPTY_FORM.ai_video_plugin_key,
    ai_video_plugin_method:
      typeof cfg.video_plugin_method === "string" ? cfg.video_plugin_method : "",
    ai_send_mode: cfg.send_mode === "send_new" ? "send_new" : "edit",
  };
}

// 根据 type 拼出 config 对象 + 入参校验
function buildPayload(form: FormState): {
  ok: boolean;
  errMsg?: string;
  config?: Record<string, unknown>;
  aliases?: string[];
} {
  const aliases = form.aliases_text
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  for (const alias of aliases) {
    if (!ALIAS_RE.test(alias)) {
      return { ok: false, errMsg: `别名不合法：${alias}` };
    }
  }
  const t = form.type;
  if (t === "reply_text") {
    return { ok: true, aliases, config: { text: form.text } };
  }
  if (t === "forward_to") {
    const cfg: Record<string, unknown> = {};
    const v = form.target_chat_id.trim();
    if (v) {
      const n = Number(v);
      if (!Number.isInteger(n)) return { ok: false, errMsg: "target_chat_id 必须是整数" };
      cfg.target_chat_id = n;
    }
    if (form.forward_mode && form.forward_mode !== "forward_native") {
      cfg.mode = form.forward_mode;
    }
    if (form.forward_delete_immediately) {
      cfg.delete_immediately = true;
    } else {
      const da = form.forward_delete_after.trim();
      if (da) {
        const n = Number(da);
        if (!Number.isInteger(n) || n < 0 || n > 3600) {
          return { ok: false, errMsg: "自动删除秒数必须是 0~3600 的整数" };
        }
        if (n > 0) cfg.delete_after = n;
      }
    }
    return { ok: true, aliases, config: cfg };
  }
  if (t === "run_plugin") {
    if (!form.plugin_key.trim())
      return { ok: false, errMsg: "plugin_key 必填" };
    let args: unknown = [];
    try {
      args = form.plugin_args ? JSON.parse(form.plugin_args) : [];
    } catch {
      return { ok: false, errMsg: "args 不是合法 JSON" };
    }
    return {
      ok: true, aliases,
      config: {
        plugin_key: form.plugin_key.trim(),
        method: form.plugin_method.trim() || undefined,
        args,
      },
    };
  }
  // ai
  const pid = Number(form.ai_provider_id);
  const usesCodexImage = form.ai_mode === "image" && form.ai_image_backend === "codex_image";
  const usesVideoPlugin = form.ai_mode === "video" && form.ai_video_backend === "plugin";
  if (!usesCodexImage && !usesVideoPlugin && (!Number.isInteger(pid) || pid <= 0))
    return { ok: false, errMsg: "AI 类型必须选择模型提供商" };
  const mt = form.ai_max_tokens.trim();
  const temperature = form.ai_temperature.trim();
  const timeoutSeconds = form.ai_timeout_seconds.trim();
  const cfg: Record<string, unknown> = {
    mode: form.ai_mode,
    quote_replied: form.ai_quote_replied,
    system_prompt: form.ai_system_prompt,
    routing_mode: form.ai_routing_mode,
  };
  if (Number.isInteger(pid) && pid > 0) {
    cfg.provider_id = pid;
  }
  if (form.ai_mode === "image") {
    cfg.image_backend = form.ai_image_backend;
  }
  if (form.ai_mode === "video") {
    const pluginKey = form.ai_video_plugin_key.trim();
    if (!pluginKey) return { ok: false, errMsg: "视频模式必须填写插件 key" };
    cfg.video_backend = "plugin";
    cfg.video_plugin_key = pluginKey;
    if (form.ai_video_plugin_method.trim()) {
      cfg.video_plugin_method = form.ai_video_plugin_method.trim();
    }
  }
  if (form.ai_model.trim()) cfg.model = form.ai_model.trim();
  if (mt) cfg.max_tokens = Number(mt) || 512;
  if (temperature) {
    const n = Number(temperature);
    if (!Number.isFinite(n) || n < 0 || n > 2) {
      return { ok: false, errMsg: "温度 temperature 必须是 0~2 之间的数字" };
    }
    cfg.temperature = n;
  }
  if (form.ai_reasoning_effort) {
    cfg.reasoning_effort = form.ai_reasoning_effort;
  }
  if (timeoutSeconds) {
    const n = Number(timeoutSeconds);
    if (!Number.isInteger(n) || n < 5 || n > 600) {
      return { ok: false, errMsg: "超时时间必须是 5~600 秒之间的整数" };
    }
    cfg.timeout_seconds = n;
  }
  // 路由字段：只在 auto 模式下下发，避免 fixed 留脏数据
  if (form.ai_routing_mode === "auto") {
    if (form.ai_routing_fallback_provider_id.trim()) {
      const fb = Number(form.ai_routing_fallback_provider_id);
      if (!Number.isInteger(fb) || fb <= 0)
        return { ok: false, errMsg: "兜底模型提供商必须有效" };
      cfg.routing_fallback_provider_id = fb;
    }
    if (form.ai_classifier_provider_id.trim()) {
      const cls = Number(form.ai_classifier_provider_id);
      if (!Number.isInteger(cls) || cls <= 0)
        return { ok: false, errMsg: "分类器模型提供商必须有效" };
      cfg.classifier_provider_id = cls;
    }
  }
  // 输出格式（默认 html + 空模板 = 用后端的 PRESET_SIMPLE）
  cfg.output_format = form.ai_output_format;
  if (form.ai_output_template.trim()) {
    cfg.output_template = form.ai_output_template;
  }
  // escape_values 默认 true；非默认值才下发
  if (!form.ai_escape_values) {
    cfg.escape_values = false;
  }
  if (form.ai_mode === "search" || form.ai_web_search) {
    cfg.web_search = true;
    cfg.web_search_context_size = form.ai_web_search_context_size;
  }
  // 发送方式：edit 是默认，仅 send_new 才下发
  if (form.ai_send_mode === "send_new") {
    cfg.send_mode = "send_new";
  }
  return { ok: true, aliases, config: cfg };
}

export function CommandTemplates() {
  const nav = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const qc = useQueryClient();
  const listQ = useQuery({
    queryKey: ["cmd-tpl"],
    queryFn: listCommandTemplates,
  });
  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  // 实时拉系统指令前缀，用在编辑器的"`,name` 触发"那行提示——避免硬编码逗号
  // 跟系统设置改了不一致
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const cmdPrefix = settingsQ.data?.command_prefix || ",";
  const aiEnabled = settingsQ.data?.ai_enabled ?? true;
  const providersQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
    enabled: !settingsQ.isLoading && aiEnabled,
    retry: false,
  });
  const providerIds = useMemo(
    () => new Set((providersQ.data || []).map((p) => p.id)),
    [providersQ.data],
  );
  const hasProviders = (providersQ.data?.length || 0) > 0;
  const providerUnavailable = providersQ.isSuccess && !hasProviders;
  const typeFilterParam = searchParams.get("type");
  const typeFilter = typeFilterParam && typeFilterParam in TYPE_LABELS
    ? (typeFilterParam as CommandTemplateType)
    : null;
  const visibleTemplates = (listQ.data || []).filter((t) =>
    typeFilter ? t.type === typeFilter : true,
  );

  const [editing, setEditing] = useState<FormState | null>(null);
  const [enableTargetTemplate, setEnableTargetTemplate] = useState<CommandTemplateOut | null>(null);
  const [focusCapability, setFocusCapability] = useState<AiCapability | null>(null);
  const returnToRef = useRef<string | null>(null);

  const closeEditor = (shouldReturn = false) => {
    setEditing(null);
    setFocusCapability(null);
    const returnTo = returnToRef.current;
    returnToRef.current = null;
    if (shouldReturn && returnTo) {
      nav(returnTo);
    }
  };
  const clearFocusCapability = useCallback(() => {
    setFocusCapability(null);
  }, []);

  const openEnableFlow = (tpl: CommandTemplateOut) => {
    setEnableTargetTemplate(tpl);
  };

  const accountCommandQueries = useQueries({
    queries: (accountsQ.data ?? []).map((account) => ({
      queryKey: ["account", account.id, "commands"],
      queryFn: () => listAccountCommands(account.id),
      enabled: enableTargetTemplate !== null,
    })),
  });

  const accountCommandToggleMut = useMutation({
    mutationFn: async ({ aid, templateId, enabled }: { aid: number; templateId: number; enabled: boolean }) => {
      if (enabled) await enableAccountCommand(aid, templateId);
      else await disableAccountCommand(aid, templateId);
    },
    onSuccess: (_data, variables) => {
      qc.setQueryData<AccountCommandItem[]>(["account", variables.aid, "commands"], (current) =>
        current?.map((item) => item.template.id === variables.templateId
          ? { ...item, enabled: variables.enabled }
          : item),
      );
      toast.success(variables.enabled ? "已为该账号启用指令" : "已为该账号停用指令");
    },
    onError: (error) => toast.error(getErrMsg(error)),
  });

  useEffect(() => {
    const editId = searchParams.get("edit");
    const newType = searchParams.get("new");
    const providerId = searchParams.get("provider_id");
    const capabilityParam = searchParams.get("aiCapability");
    const returnTo = searchParams.get("returnTo");
    const capability =
      capabilityParam === "routing" ||
      capabilityParam === "search" ||
      capabilityParam === "output" ||
      capabilityParam === "params"
        ? capabilityParam
        : null;
    const shouldOpenNewAi = newType === "ai" || (!!capability && !editId);
    const hasConsumableQuery =
      !!editId || shouldOpenNewAi || !!providerId || !!capability || !!returnTo;

    if (!hasConsumableQuery) return;
    if (shouldOpenNewAi && settingsQ.isLoading) return;
    if (editId && !listQ.isSuccess) return;

    if (returnTo) {
      returnToRef.current = returnTo;
    }
    if (capability) {
      setFocusCapability(capability);
    }

    if (editId) {
      const id = Number(editId);
      const target = Number.isInteger(id)
        ? listQ.data?.find((t) => t.id === id)
        : null;
      if (target) {
        const next = formFromTemplate(target);
        if (providerId) {
          next.ai_provider_id = providerId;
          next.ai_model = "";
        }
        setEditing(next);
      } else {
        toast.error("未找到指定模板");
      }
    } else if (shouldOpenNewAi) {
      if (!aiEnabled) {
        toast.error("AI 能力已关闭，请先到系统设置启用");
        if (returnTo) {
          nav(returnTo);
        }
      } else {
        setEditing({
          ...EMPTY_FORM,
          type: "ai",
          ai_provider_id: providerId || EMPTY_FORM.ai_provider_id,
        });
      }
    }

    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("edit");
    nextParams.delete("new");
    nextParams.delete("provider_id");
    nextParams.delete("aiCapability");
    nextParams.delete("returnTo");
    setSearchParams(nextParams, { replace: true });
  }, [aiEnabled, listQ.data, listQ.isSuccess, nav, searchParams, setSearchParams, settingsQ.isLoading]);

  const createMut = useMutation({
    mutationFn: (form: FormState) => {
      const r = buildPayload(form);
      if (!r.ok) throw new Error(r.errMsg || "config 校验失败");
      return createCommandTemplate({
        name: form.name.trim(),
        aliases: r.aliases || [],
        type: form.type,
        config: r.config!,
        description: form.description || null,
      });
    },
    onSuccess: () => {
      toast.success("已新建模板");
      qc.invalidateQueries({ queryKey: ["cmd-tpl"] });
      closeEditor(true);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const updateMut = useMutation({
    mutationFn: (form: FormState) => {
      if (!form.id) throw new Error("缺少 id");
      const r = buildPayload(form);
      if (!r.ok) throw new Error(r.errMsg || "config 校验失败");
      return patchCommandTemplate(form.id, {
        name: form.name.trim(),
        aliases: r.aliases || [],
        type: form.type,
        config: r.config!,
        description: form.description || null,
      });
    },
    onSuccess: () => {
      toast.success("已保存");
      qc.invalidateQueries({ queryKey: ["cmd-tpl"] });
      closeEditor(true);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteCommandTemplate(id),
    onSuccess: () => {
      toast.success("已删除");
      qc.invalidateQueries({ queryKey: ["cmd-tpl"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const echoGuardLimit = settingsQ.data?.command_echo_guard_previous_messages ?? 8;
  const echoGuardEnabled = echoGuardLimit > 0;
  const [echoGuardInput, setEchoGuardInput] = useState("8");
  useEffect(() => {
    setEchoGuardInput(String(echoGuardLimit > 0 ? echoGuardLimit : 8));
  }, [echoGuardLimit]);
  const parsedEchoGuardLimit = Math.trunc(Number(echoGuardInput));
  const nextEchoGuardLimit =
    Number.isFinite(parsedEchoGuardLimit)
      ? Math.max(1, Math.min(50, parsedEchoGuardLimit))
      : 8;
  const echoGuardMut = useMutation({
    mutationFn: (limit: number) =>
      patchSystemSettings({
        command_echo_guard_previous_messages: limit,
      }),
    onSuccess: () => {
      toast.success("指令防误触设置已保存，worker 将热加载");
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  return (
    <div className="space-y-6">
      <BuiltinCommandsPanel cmdPrefix={cmdPrefix} />
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="max-w-3xl">
              <CardTitle className="text-base">指令防误触</CardTitle>
              <CardDescription>
                群聊里自己发送纯指令时，如果前 N 条消息内有人发过完全相同内容，会视为抽奖、接龙或复读场景并静默跳过，避免误触模板指令。设为 0 表示关闭。
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-3">
              <div className="flex items-center gap-2">
                <Label htmlFor="echo-guard-limit" className="whitespace-nowrap text-xs text-muted-foreground">
                  检查前
                </Label>
                <Input
                  id="echo-guard-limit"
                  type="number"
                  min={1}
                  max={50}
                  step={1}
                  value={echoGuardInput}
                  disabled={!echoGuardEnabled || settingsQ.isLoading || echoGuardMut.isPending}
                  onChange={(e) => setEchoGuardInput(e.target.value)}
                  onBlur={() => setEchoGuardInput(String(nextEchoGuardLimit))}
                  className="h-8 w-20"
                />
                <span className="whitespace-nowrap text-xs text-muted-foreground">条</span>
              </div>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={!echoGuardEnabled || settingsQ.isLoading || echoGuardMut.isPending}
                onClick={() => echoGuardMut.mutate(nextEchoGuardLimit)}
              >
                <Save className="mr-1 h-4 w-4" />
                保存条数
              </Button>
              <span className="text-xs text-muted-foreground">
                {echoGuardEnabled ? "已开启" : "已关闭"}
              </span>
              <Switch
                checked={echoGuardEnabled}
                disabled={settingsQ.isLoading || echoGuardMut.isPending}
                onCheckedChange={(checked) => echoGuardMut.mutate(checked ? nextEchoGuardLimit : 0)}
              />
            </div>
          </div>
        </CardHeader>
      </Card>
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-3">
            <div>
              <CardTitle className="text-base">自定义指令模板</CardTitle>
              <CardDescription className="space-y-1">
                <span className="block font-medium text-foreground">作用：定义能做什么</span>
                <span className="block">全局模板库，每条 = 一个 <CommandBadge>{cmdPrefix}name</CommandBadge> 指令的"配方"。</span>
              </CardDescription>
            </div>
            <Button
              size="sm"
              onClick={() => {
                returnToRef.current = null;
                setFocusCapability(null);
                setEditing({ ...EMPTY_FORM });
              }}
            >
              <Plus className="mr-1 h-4 w-4" /> 新建
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {aiEnabled && providerUnavailable ? (
            <div className="mb-3 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
              chat/search 类 AI 指令需要先添加模型提供商；image + codex_image 与 video + 插件后端可先使用插件配置。
              <Button
                type="button"
                variant="link"
                className="h-auto px-1 py-0 text-xs"
                onClick={() => nav("/ai?tab=providers")}
              >
                前往模型提供商
              </Button>
            </div>
          ) : null}
          {typeFilter ? (
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
              <span>
                当前仅显示 <Badge variant="secondary">{TYPE_LABELS[typeFilter]}</Badge> 类型模板。
              </span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  const next = new URLSearchParams(searchParams);
                  next.delete("type");
                  setSearchParams(next, { replace: true });
                }}
              >
                清除筛选
              </Button>
            </div>
          ) : null}
          {listQ.isLoading ? (
            <div className="flex h-20 items-center justify-center">
              <Spinner className="text-primary" />
            </div>
          ) : visibleTemplates.length > 0 ? (
            <>
            <div className="hidden overflow-x-auto md:block">
            <Table className="min-w-[760px]">
              <TableHeader>
                <TableRow>
                  <TableHead>指令</TableHead>
                  <TableHead>指令类型</TableHead>
                  <TableHead>别名（短指令）</TableHead>
                  <TableHead>说明</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleTemplates.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell className="font-mono text-sm">{cmdPrefix}{t.name}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap items-center gap-1">
                        <Badge variant="secondary">{TYPE_LABELS[t.type] || t.type}</Badge>
                        {t.type === "ai" &&
                        providersQ.isSuccess &&
                        typeof t.config?.provider_id === "number" &&
                        !providerIds.has(t.config.provider_id) ? (
                          <Badge variant="destructive">模型提供商缺失</Badge>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell className="max-w-[260px]">
                      <div className="flex flex-wrap gap-1">
                        {(t.aliases || []).length === 0 ? (
                          <span className="text-xs text-muted-foreground">—</span>
                        ) : (
                          (t.aliases || []).map((a) => (
                            <Badge key={a} variant="outline" className="font-mono text-[11px]">
                              {cmdPrefix}{a}
                            </Badge>
                          ))
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="max-w-[420px] truncate text-xs text-muted-foreground">
                      {t.description || "—"}
                    </TableCell>
                    <TableCell className="space-x-2 text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => openEnableFlow(t)}
                      >
                        启用
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          returnToRef.current = null;
                          setFocusCapability(null);
                          setEditing(formFromTemplate(t));
                        }}
                      >
                        <Edit3 className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={deleteMut.isPending}
                        onClick={() => {
                          if (
                            confirm(
                              `确认删除模板「${t.name}」？所有启用此模板的账号都会失去这个指令`,
                            )
                          ) {
                            deleteMut.mutate(t.id);
                          }
                        }}
                      >
                        <Trash2 className="h-4 w-4 text-destructive" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            </div>
            <div className="space-y-3 md:hidden">
              {visibleTemplates.map((t) => {
                const providerMissing =
                  t.type === "ai" &&
                  providersQ.isSuccess &&
                  typeof t.config?.provider_id === "number" &&
                  !providerIds.has(t.config.provider_id);
                return (
                  <div key={t.id} className="rounded-xl border border-border/70 bg-background/70 p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="break-all font-mono text-sm font-semibold">{cmdPrefix}{t.name}</div>
                        <div className="mt-1 text-xs leading-5 text-muted-foreground">
                          {t.description || "未填写说明"}
                        </div>
                      </div>
                      <div className="flex shrink-0 flex-col items-end gap-1.5">
                        <Badge variant="secondary">{TYPE_LABELS[t.type] || t.type}</Badge>
                        {providerMissing ? <Badge variant="destructive">模型缺失</Badge> : null}
                      </div>
                    </div>
                    <div className="mt-3 rounded-lg border border-border/70 bg-muted/30 px-3 py-2">
                      <div className="text-[11px] text-muted-foreground">别名</div>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {(t.aliases || []).length === 0 ? (
                          <span className="text-xs text-muted-foreground">无别名</span>
                        ) : (
                          (t.aliases || []).map((a) => (
                            <Badge key={a} variant="outline" className="font-mono text-[11px]">
                              {cmdPrefix}{a}
                            </Badge>
                          ))
                        )}
                      </div>
                    </div>
                    <div className="mt-3 grid grid-cols-3 gap-2">
                      <Button variant="outline" size="sm" onClick={() => openEnableFlow(t)}>
                        启用
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          returnToRef.current = null;
                          setFocusCapability(null);
                          setEditing(formFromTemplate(t));
                        }}
                      >
                        编辑
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive"
                        disabled={deleteMut.isPending}
                        onClick={() => {
                          if (
                            confirm(
                              `确认删除模板「${t.name}」？所有启用此模板的账号都会失去这个指令`,
                            )
                          ) {
                            deleteMut.mutate(t.id);
                          }
                        }}
                      >
                        删除
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
            </>
          ) : (
            <p className="rounded-md border border-dashed py-8 text-center text-xs text-muted-foreground">
              {typeFilter ? "当前筛选下没有模板。" : "尚无模板。新建一个后即可在账号详情中开启"}
            </p>
          )}
        </CardContent>

        {editing && (
          <CommandEditDialog
            form={editing}
            cmdPrefix={cmdPrefix}
            onChange={setEditing}
            focusCapability={focusCapability}
            onCapabilityFocused={clearFocusCapability}
            onCancel={() => closeEditor(true)}
            onSave={() => {
              const trimName = editing.name.trim();
              if (!NAME_RE.test(trimName)) {
                toast.error("指令名只能包含字母 / 数字 / 下划线，1-64 字符");
                return;
              }
              if (
                editing.type === "ai" &&
                !aiEnabled
              ) {
                toast.error("AI 能力已关闭，请先到系统设置启用");
                return;
              }
              if (
                editing.type === "ai" &&
                providerUnavailable &&
                !(editing.ai_mode === "image" && editing.ai_image_backend === "codex_image") &&
                !(editing.ai_mode === "video" && editing.ai_video_backend === "plugin")
              ) {
                toast.error("chat/search 模式需要先添加模型提供商");
                return;
              }
              if (editing.id) {
                updateMut.mutate(editing);
              } else {
                createMut.mutate(editing);
              }
            }}
            saving={createMut.isPending || updateMut.isPending}
            hasProviders={hasProviders}
            aiEnabled={aiEnabled}
            providerUnavailable={providerUnavailable}
            onGoProviders={() => nav("/ai?tab=providers")}
          />
        )}
        <Dialog open={!!enableTargetTemplate} onOpenChange={(v) => !v && setEnableTargetTemplate(null)}>
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle>选择要启用的账号</DialogTitle>
              <DialogDescription>
                直接设置模板{enableTargetTemplate ? `「${enableTargetTemplate.name}」` : ""}在各账号的启用状态，保存后 worker 会热加载。
              </DialogDescription>
            </DialogHeader>
            <div className="max-h-[min(55dvh,28rem)] space-y-2 overflow-y-auto pr-1">
              {accountsQ.isLoading ? (
                <div className="flex h-24 items-center justify-center"><Spinner className="text-primary" /></div>
              ) : (accountsQ.data ?? []).length > 0 ? (
                (accountsQ.data ?? []).map((account, index) => {
                  const commandQ = accountCommandQueries[index];
                  const command = commandQ.data?.find((item) => item.template.id === enableTargetTemplate?.id);
                  const accountLabel = account.display_name || account.phone || `账号 #${account.id}`;
                  return (
                    <div key={account.id} className="flex min-h-12 items-center justify-between gap-3 rounded-lg border bg-background px-3 py-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">{accountLabel}</div>
                        <div className="text-xs text-muted-foreground">账号 #{account.id}</div>
                      </div>
                      {commandQ.isError ? (
                        <Button type="button" size="sm" variant="ghost" onClick={() => void commandQ.refetch()}>重试</Button>
                      ) : (
                        <Switch
                          checked={command?.enabled ?? false}
                          disabled={commandQ.isLoading || accountCommandToggleMut.isPending || !command}
                          aria-label={`${accountLabel}启用${enableTargetTemplate?.name || "当前指令"}`}
                          onCheckedChange={(enabled) => {
                            if (!enableTargetTemplate) return;
                            accountCommandToggleMut.mutate({ aid: account.id, templateId: enableTargetTemplate.id, enabled });
                          }}
                        />
                      )}
                    </div>
                  );
                })
              ) : (
                <div className="rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">暂无可用账号，请先绑定账号。</div>
              )}
            </div>
            <DialogFooter className="!flex !flex-row gap-2 sm:space-x-0 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
              <Button onClick={() => setEnableTargetTemplate(null)}>完成</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </Card>
    </div>
  );
}

// ── 内置指令面板（只读） ──────────────────────────────────────────
// 让用户起自定义模板名时知道哪些已被占用；防撞名（API 校验也会拒）。
function BuiltinCommandsPanel({ cmdPrefix }: { cmdPrefix: string }) {
  const [expanded, setExpanded] = useState(false);
  const builtinQ = useQuery({
    queryKey: ["cmd-builtin"],
    queryFn: listBuiltinCommands,
    enabled: expanded,
  });

  return (
    <Card className="overflow-hidden">
      <button
        type="button"
        className="flex w-full items-center justify-between gap-3 p-5 text-left hover:bg-muted/25"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="min-w-0">
          <span className="block text-base font-semibold">内置指令（只读）</span>
          <span className="mt-1 block text-sm text-muted-foreground">系统注册在 worker 里的指令；自定义模板的 name/aliases 不能与此重复</span>
        </span>
        <ChevronDown className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform ${expanded ? "rotate-180" : ""}`} />
      </button>
      {expanded ? <CardContent className="border-t pt-5">
        {builtinQ.isLoading ? (
          <div className="flex h-16 items-center justify-center">
            <Spinner className="text-primary" />
          </div>
        ) : builtinQ.data && builtinQ.data.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {builtinQ.data.map((c) => (
              <span
                key={c.name}
                title={c.doc || c.name}
                className="inline-flex items-center gap-1.5 rounded-md border bg-muted/50 px-2 py-1 text-xs"
              >
                <code className="font-mono font-medium">
                  {cmdPrefix}
                  {c.name}
                </code>
                {c.aliases.length > 0 ? (
                  <span className="text-muted-foreground">
                    ({c.aliases.map((a) => `${cmdPrefix}${a}`).join(", ")})
                  </span>
                ) : null}
                {c.doc ? (
                  <span className="text-muted-foreground">— {c.doc}</span>
                ) : null}
              </span>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">未读取到内置指令</p>
        )}
      </CardContent> : null}
    </Card>
  );
}

function CommandEditDialog({
  form,
  cmdPrefix,
  onChange,
  focusCapability,
  onCapabilityFocused,
  onCancel,
  onSave,
  saving,
  hasProviders,
  aiEnabled,
  providerUnavailable,
  onGoProviders,
}: {
  form: FormState;
  cmdPrefix: string;
  onChange: (s: FormState) => void;
  focusCapability: AiCapability | null;
  onCapabilityFocused: () => void;
  onCancel: () => void;
  onSave: () => void;
  saving: boolean;
  hasProviders: boolean;
  aiEnabled: boolean;
  providerUnavailable: boolean;
  onGoProviders: () => void;
}) {
  const isEdit = !!form.id;
  const setField = <K extends keyof FormState>(k: K, v: FormState[K]) =>
    onChange({ ...form, [k]: v });
  // 一次性设置多个字段；用于"必须同步更新"的字段对（比如 provider_id + model）。
  // 不要拆成两次 setField——`form` 是闭包里的旧值，第二次 setField 会用旧 form
  // 把第一次的改动盖掉，结果两个字段都没改成。
  const setFields = (patch: Partial<FormState>) =>
    onChange({ ...form, ...patch });

  // ai 类型才需要拉 provider 列表
  const providersQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
    enabled: aiEnabled && form.type === "ai",
    retry: false,
  });
  const selectedReasoningProvider = useMemo(
    () =>
      (providersQ.data || []).find(
        (provider) => String(provider.id) === form.ai_provider_id,
      ) || null,
    [form.ai_provider_id, providersQ.data],
  );
  const selectedReasoningModelId =
    form.ai_model.trim() || selectedReasoningProvider?.default_model || "";
  const selectedReasoningModel =
    selectedReasoningProvider?.models?.find(
      (model) => model.id === selectedReasoningModelId,
    ) || null;

  // 切类型时清相邻字段，避免上次填的脏数据落到 config
  const typeOptions = useMemo(
    () =>
      (Object.entries(TYPE_LABELS) as [CommandTemplateType, string][]).filter(
        ([type]) => aiEnabled || type !== "ai" || form.type === "ai",
      ),
    [aiEnabled, form.type],
  );
  const [openAiSections, setOpenAiSections] = useState<AiCapability[]>(() => {
    const defaults: AiCapability[] = [];
    if (form.ai_routing_mode === "auto") defaults.push("routing");
    if (form.ai_web_search) defaults.push("search");
    if (form.ai_output_template.trim() || form.ai_output_format !== "html" || !form.ai_escape_values) {
      defaults.push("output");
    }
    if (form.ai_temperature || form.ai_reasoning_effort || form.ai_timeout_seconds) defaults.push("params");
    return defaults;
  });
  const routingSectionRef = useRef<HTMLDivElement | null>(null);
  const searchSectionRef = useRef<HTMLDivElement | null>(null);
  const outputSectionRef = useRef<HTMLDivElement | null>(null);
  const paramsSectionRef = useRef<HTMLDivElement | null>(null);
  const getSectionRef = (section: AiCapability) => {
    if (section === "routing") return routingSectionRef;
    if (section === "search") return searchSectionRef;
    if (section === "params") return paramsSectionRef;
    return outputSectionRef;
  };

  useEffect(() => {
    if (!focusCapability || form.type !== "ai") return;
    setOpenAiSections((prev) =>
      prev.includes(focusCapability) ? prev : [...prev, focusCapability],
    );
    window.setTimeout(() => {
      getSectionRef(focusCapability).current?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
      onCapabilityFocused();
    }, 80);
  }, [focusCapability, form.type, onCapabilityFocused]);

  const toggleAiSection = (section: AiCapability) => {
    setOpenAiSections((prev) =>
      prev.includes(section)
        ? prev.filter((item) => item !== section)
        : [...prev, section],
    );
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onCancel()}>
      <DialogContent className="dialog-center !flex w-[calc(100vw-1.5rem)] max-w-2xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="shrink-0 border-b border-border/70 px-4 py-4 pr-12 sm:px-6">
          <DialogTitle>{isEdit ? "编辑" : "新建"} 自定义指令</DialogTitle>
          <DialogDescription>
            根据类型不同，下方表单会切到对应字段，*为必填项
          </DialogDescription>
        </DialogHeader>

        <div className="safe-scrollbar min-h-0 flex-1 touch-pan-y space-y-4 overflow-y-auto overscroll-contain px-4 py-4 sm:px-6">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>指令 *</Label>
              <Input
                value={form.name}
                maxLength={64}
                placeholder="hi / ai / forward_to_devs"
                onChange={(e) => setField("name", e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                只允许字母 / 数字 / 下划线；当前为发送 <CommandBadge>{cmdPrefix}{form.name || "name"}</CommandBadge> 触发
              </p>
            </div>
            <div className="space-y-1.5">
              <Label>类型 *</Label>
              <Select
                value={form.type}
                onChange={(e) =>
                  setField("type", e.target.value as CommandTemplateType)
                }
              >
                {typeOptions.map(([k, label]) => (
                  <option key={k} value={k}>
                    {label}（{k}）
                  </option>
                ))}
              </Select>
              {!aiEnabled ? (
                <p className="text-xs text-muted-foreground">
                  AI 能力已关闭，暂不提供 AI 类型；已有 AI 模板会保留。
                </p>
              ) : providerUnavailable ? (
                <p className="text-xs text-warning">
                  chat/search 需要模型提供商；image 和 video 可先桥接账号插件。
                  <Button
                    type="button"
                    variant="link"
                    className="h-auto px-1 py-0 text-xs"
                    onClick={onGoProviders}
                  >
                    前往模型提供商
                  </Button>
                </p>
              ) : null}
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>说明（可选）</Label>
              <Input
                value={form.description}
                maxLength={255}
                placeholder={`便于 ${cmdPrefix}help 显示`}
                onChange={(e) => setField("description", e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label>别名 / 短指令（可选）</Label>
              <Input
                value={form.aliases_text}
                maxLength={255}
                placeholder="t, trans, tr"
                onChange={(e) => setField("aliases_text", e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                逗号或空格分隔，例如 <code>t, trans</code>
              </p>
            </div>
          </div>

          {/* 按 type 切不同子表单 */}
          {form.type === "reply_text" && (
            <div className="space-y-3">
              <Label>回复文本 *</Label>
              <Textarea
                value={form.text}
                rows={4}
                placeholder="hello {args}"
                onChange={(e) => setField("text", e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                支持 `{"{args}"}` 占位符，会被指令后跟的参数替换
              </p>
              <div className="flex flex-wrap gap-1.5">
                <SignalPill tone="neutral" label="可用占位符" value="{args}" />
                <SignalPill tone="neutral" label="示例参数" value={REPLY_TEXT_PREVIEW_VALUES.args} />
              </div>
              <TelegramHtmlPreview
                value={renderReplyTextPreview(form.text)}
                mode="html"
                title={`${
                  cmdPrefix
                }${form.name || "reply_text"}`}
                caption="最终会按 Telegram 消息样式发送"
                hints={[
                  { label: "{args}", value: REPLY_TEXT_PREVIEW_VALUES.args },
                ]}
              />
            </div>
          )}

          {form.type === "forward_to" && (
            <div className="space-y-3">
              <div className="space-y-1.5">
                <Label>目标 chat_id（可选）</Label>
                <Input
                  inputMode="numeric"
                  value={form.target_chat_id}
                  onChange={(e) =>
                    setField(
                      "target_chat_id",
                      e.target.value.replace(/[^\d-]/g, ""),
                    )
                  }
                  placeholder="留空 = 转到当前会话；填如 -1001234567890"
                />
                <p className="text-xs text-muted-foreground">
                  留空 = 触发指令时<strong>默认转发到指令消息所在的 chat</strong>。
                  填了就强制转到这个 chat_id；在该群里执行 <CommandBadge>{cmdPrefix}id</CommandBadge> 可获得 chat_id（超级群以 -100 开头）。
                </p>
              </div>
              <div className="space-y-1.5">
                <Label>转发方式</Label>
                <Select
                  value={form.forward_mode}
                  onChange={(e) => setField("forward_mode", e.target.value)}
                >
                  <option value="forward_native">原生转发（携带原作者）</option>
                  <option value="copy_text">复制文本（不显示原作者）</option>
                  <option value="copy_media">复读媒体（贴纸/图片/文件）</option>
                  <option value="quote">引用包装（带"来自 X"前缀）</option>
                  <option value="link_only">仅发链接（公开群可点）</option>
                </Select>
              </div>
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label>成功后立即删除指令消息</Label>
                  <Switch
                    checked={form.forward_delete_immediately}
                    onCheckedChange={(v) =>
                      setField("forward_delete_immediately", v)
                    }
                  />
                </div>
                <p className="text-xs text-muted-foreground">
                  开启后，指令触发成功后立即删除你发的 <CommandBadge>{cmdPrefix}{form.name || "name"}</CommandBadge> 指令消息（不影响转发/回复的内容）。
                </p>
              </div>
              {!form.forward_delete_immediately && (
                <div className="space-y-1.5">
                  <Label>触发后自动删除指令消息（秒，可选）</Label>
                  <Input
                    inputMode="numeric"
                    value={form.forward_delete_after}
                    maxLength={5}
                    onChange={(e) =>
                      setField(
                        "forward_delete_after",
                        e.target.value.replace(/[^\d]/g, ""),
                      )
                    }
                    placeholder="留空或 0 = 不删；如 5 = 5 秒后删指令消息"
                  />
                  <p className="text-xs text-muted-foreground">
                    转发成功后等待 N 秒，删除你刚发的 <CommandBadge>{cmdPrefix}{form.name || "name"}</CommandBadge> 指令消息（不影响转过去的内容）。范围 0–3600；留空或 0 = 不删，并会编辑原指令消息显示 ✓ 成功提示。
                  </p>
                </div>
              )}
            </div>
          )}

          {form.type === "run_plugin" && (
            <div className="space-y-3">
              <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                调用已加载插件注册的指令；method 留空时默认使用 plugin_key 同名指令。
              </div>
              <div className="space-y-1.5">
                <Label>plugin_key *</Label>
                <Input
                  value={form.plugin_key}
                  maxLength={64}
                  onChange={(e) => setField("plugin_key", e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>method（可选）</Label>
                <Input
                  value={form.plugin_method}
                  maxLength={64}
                  onChange={(e) => setField("plugin_method", e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>args（JSON 数组）</Label>
                <Input
                  value={form.plugin_args}
                  onChange={(e) => setField("plugin_args", e.target.value)}
                  placeholder='[]'
                />
              </div>
            </div>
          )}

          {form.type === "ai" && (
            <div className="space-y-3">
              <div className="space-y-1.5">
                <Label>AI 模式</Label>
                <Select
                  value={form.ai_mode}
                  onChange={(e) => {
                    const mode = normalizeAiMode(e.target.value);
                    setFields({
                      ai_mode: mode,
                      ai_web_search: mode === "search" ? true : form.ai_web_search,
                      ai_image_backend: mode === "image" ? form.ai_image_backend : EMPTY_FORM.ai_image_backend,
                      ...applyAiModeDefaults(form, mode),
                    });
                  }}
                >
                  <option value="chat">chat · 普通问答</option>
                  <option value="search">search · 联网搜索</option>
                  <option value="image">image · 图片生成</option>
                  <option value="video">video · 视频生成</option>
                </Select>
                <p className="text-xs text-muted-foreground">
                  可创建 <CommandBadge>{cmdPrefix}ai image 提示词</CommandBadge> 这类二级指令，也可新建名为
                  <CommandBadge className="mx-1">image</CommandBadge> 的 AI 模板作为直接指令。
                </p>
                <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                  <p>
                    <span className="font-medium text-foreground">chat</span>：普通问答、总结、翻译；若打开“允许联网搜索”，后端会把搜索工具交给模型，是否实际搜索由模型/provider 决定。
                  </p>
                  <p className="mt-1">
                    <span className="font-medium text-foreground">search</span>：专门搜索指令，保存时固定启用联网搜索；需要支持 OpenAI Responses API（api_format=responses）的 Provider，适合 <CommandBadge>{cmdPrefix}search</CommandBadge> 或 <CommandBadge>{cmdPrefix}ai search</CommandBadge>。
                  </p>
                  <p className="mt-1">
                    <span className="font-medium text-foreground">image</span>：图片生成；可桥接 codex_image 插件，也可使用 LLM Provider 原生生图，适合 <CommandBadge>{cmdPrefix}image</CommandBadge>。
                  </p>
                  <p className="mt-1">
                    <span className="font-medium text-foreground">video</span>：视频生成；转发到账号已启用的视频插件，适合 <CommandBadge>{cmdPrefix}video</CommandBadge>。
                  </p>
                </div>
              </div>

              {form.ai_mode === "image" && (
                <div className="space-y-1.5">
                  <Label>图片生成后端</Label>
                  <Select
                    value={form.ai_image_backend}
                    onChange={(e) =>
                      setField(
                        "ai_image_backend",
                        e.target.value === "llm" ? "llm" : "codex_image",
                      )
                    }
                  >
                    <option value="codex_image">codex_image 插件（当前推荐）</option>
                    <option value="llm">LLM Provider 原生生图</option>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    {form.ai_image_backend === "codex_image" ? (
                      <>
                        codex_image 使用账号插件里的 Codex Access Token、尺寸和发送配置；本模板只负责把
                        <CommandBadge className="mx-1">{cmdPrefix}{form.name || "image"}</CommandBadge>
                        转到该插件。
                      </>
                    ) : (
                      <>
                        原生生图会优先走 Responses 的 image_generation 工具；OpenAI-compatible chat 协议会尝试
                        <code className="mx-1">/images/generations</code>。建议选择支持图片生成的模型，并把超时时间调到 180 秒以上。
                      </>
                    )}
                  </p>
                </div>
              )}

              {form.ai_mode === "video" && (
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label>视频插件 key *</Label>
                    <Input
                      value={form.ai_video_plugin_key}
                      maxLength={64}
                      onChange={(e) => setField("ai_video_plugin_key", e.target.value)}
                      placeholder="video_bridge"
                    />
                    <p className="text-xs text-muted-foreground">
                      运行时会把提示词转给该插件；插件负责调用视频服务并发送视频。
                    </p>
                  </div>
                  <div className="space-y-1.5">
                    <Label>视频插件 method（可选）</Label>
                    <Input
                      value={form.ai_video_plugin_method}
                      maxLength={64}
                      onChange={(e) => setField("ai_video_plugin_method", e.target.value)}
                      placeholder="留空 = 插件默认指令"
                    />
                  </div>
                </div>
              )}

              {!(form.ai_mode === "image" && form.ai_image_backend === "codex_image") &&
                !(form.ai_mode === "video" && form.ai_video_backend === "plugin") && (
                <div className="space-y-1.5">
                  <Label>
                    {form.ai_routing_mode === "auto"
                      ? "默认 / 兜底模型 *"
                      : "提供商 + 模型 *"}
                  </Label>
                  <ProviderModelSelect
                    value={
                      form.ai_provider_id && form.ai_model
                        ? `${form.ai_provider_id}|${form.ai_model}`
                        : form.ai_provider_id
                          ? `${form.ai_provider_id}|`
                          : ""
                    }
                    providers={providersQ.data}
                    loading={providersQ.isLoading}
                    onChange={(v) => {
                      // 选项 value 形如 "<pid>|<model>"
                      // 必须用 setFields 一次性写两个字段——拆成两次 setField 会
                      // 因为闭包里的旧 form 互相覆盖，最终两个字段都回到空值
                      const sep = v.indexOf("|");
                      if (sep < 0) {
                        setFields({ ai_provider_id: "", ai_model: "" });
                        return;
                      }
                      const pid = v.slice(0, sep);
                      const model = v.slice(sep + 1);
                      const provider = (providersQ.data || []).find(
                        (item) => String(item.id) === pid,
                      );
                      const modelId = model || provider?.default_model || "";
                      const metadata = provider?.models?.find(
                        (item) => item.id === modelId,
                      );
                      const capability = reasoningEffortsForModel({
                        declared: metadata?.reasoning_efforts,
                        apiFormat: (provider?.api_format as LLMApiFormat) || "responses",
                        modelId,
                      });
                      const currentEffort = form.ai_reasoning_effort;
                      setFields({
                        ai_provider_id: pid,
                        ai_model: model,
                        ai_reasoning_effort:
                          currentEffort && !capability.efforts.includes(currentEffort)
                            ? ""
                            : currentEffort,
                      });
                    }}
                  />
                  <p className="text-xs text-muted-foreground">
                    下拉里每条 = 一个已启用的 (提供商 × 模型) 组合。要新增/启用模型去
                    <span className="mx-1 font-medium">AI → 模型提供商</span>编辑。
                    {form.ai_routing_mode === "auto"
                      ? " auto 模式下，规则未命中且未设独立兜底时走这条"
                      : ""}
                  </p>
                </div>
              )}
              <div className="space-y-1.5">
                <Label>System Prompt 系统默认提示词</Label>
                <Textarea
                  value={form.ai_system_prompt}
                  rows={3}
                  onChange={(e) => setField("ai_system_prompt", e.target.value)}
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>最大输出 tokens（max_tokens）</Label>
                  <Input
                    inputMode="numeric"
                    value={form.ai_max_tokens}
                    onChange={(e) =>
                      setField(
                        "ai_max_tokens",
                        e.target.value.replace(/[^\d]/g, ""),
                      )
                    }
                  />
                  <p className="text-xs text-muted-foreground">
                    限制单次回答长度；Responses API 会自动映射为 max_output_tokens。
                  </p>
                </div>
                <div className="flex items-center gap-2 self-end pb-2">
                  <Switch
                    checked={form.ai_quote_replied}
                    onCheckedChange={(v) => setField("ai_quote_replied", v)}
                    id="quoteReplied"
                  />
                  <Label htmlFor="quoteReplied" className="cursor-pointer">
                    引用被回复消息的内容作为提示词的一部分
                  </Label>
                </div>
              </div>

              {/* ── 发送方式 ─────────────────────────────── */}
              <div className="space-y-1.5">
                <Label>发送方式</Label>
                <Select
                  value={form.ai_send_mode}
                  onChange={(e) =>
                    setField(
                      "ai_send_mode",
                      e.target.value === "send_new" ? "send_new" : "edit",
                    )
                  }
                >
                  <option value="edit">原地编辑指令消息（默认）</option>
                  <option value="send_new">删指令、发新消息（不带 reply_to）</option>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {form.ai_send_mode === "send_new" ? (
                    <>
                      用于"我回复某人后用 <CommandBadge>{cmdPrefix}{form.name || "ai"}</CommandBadge> 提问"的场景：
                      被回复消息**只作为模型上下文**，发出去的回答**不再是 reply**，
                      也不会在被回复方那里留下"你回复了我"的痕迹（首次发指令的 ping 仍无法避免）。
                      此模式下 <code>{"{display_input}"}</code> 自动回退为你打的字（不复读对方原文）。
                    </>
                  ) : (
                    <>
                      原地把指令消息编辑成 AI 回答，保留 reply 链 — 在群里能让上下文清晰可追溯。
                    </>
                  )}
                </p>
              </div>

              <div className="space-y-2">
                <CollapsibleAiSection
                  ref={paramsSectionRef}
                  title="模型参数"
                  description={
                    form.ai_temperature || form.ai_reasoning_effort || form.ai_timeout_seconds
                      ? "已设置采样 / 推理 / 超时"
                      : "使用系统默认参数"
                  }
                  open={openAiSections.includes("params")}
                  onToggle={() => toggleAiSection("params")}
                >
                  <div className="grid gap-3 md:grid-cols-3">
                    <div className="space-y-1.5">
                      <Label>温度（temperature）</Label>
                      <Input
                        inputMode="decimal"
                        placeholder={AI_MODE_DEFAULTS[form.ai_mode].temperature}
                        value={form.ai_temperature}
                        onChange={(e) =>
                          setField(
                            "ai_temperature",
                            e.target.value.replace(/[^\d.]/g, ""),
                          )
                        }
                      />
                      <p className="text-xs text-muted-foreground">
                        当前模式默认 {AI_MODE_DEFAULTS[form.ai_mode].temperature}；0 更稳定，适合搜索、总结、分类；最高 2，更适合创作。
                      </p>
                    </div>
                    <div className="space-y-1.5">
                      <Label>推理强度（reasoning_effort）</Label>
                      <ReasoningEffortSelect
                        value={form.ai_reasoning_effort}
                        onChange={(effort) => setField("ai_reasoning_effort", effort)}
                        declaredEfforts={selectedReasoningModel?.reasoning_efforts}
                        apiFormat={(selectedReasoningProvider?.api_format as LLMApiFormat) || "responses"}
                        modelId={selectedReasoningModelId}
                      />
                      <p className="text-xs text-muted-foreground">
                        当前模式建议 {AI_MODE_DEFAULTS[form.ai_mode].reasoning_effort || "Off"}；切换模型时会按该模型声明的档位过滤，不兼容值自动回到 Off。
                      </p>
                    </div>
                    <div className="space-y-1.5">
                      <Label>超时时间（秒）</Label>
                      <Input
                        inputMode="numeric"
                        placeholder={AI_MODE_DEFAULTS[form.ai_mode].timeout_seconds}
                        value={form.ai_timeout_seconds}
                        onChange={(e) =>
                          setField(
                            "ai_timeout_seconds",
                            e.target.value.replace(/[^\d]/g, ""),
                          )
                        }
                      />
                      <p className="text-xs text-muted-foreground">
                        当前模式默认 {AI_MODE_DEFAULTS[form.ai_mode].timeout_seconds} 秒；单次 API 调用等待时间，5~600 秒；长推理或本地桥接可适当调高。
                      </p>
                    </div>
                  </div>
                </CollapsibleAiSection>

                <CollapsibleAiSection
                  ref={routingSectionRef}
                  title="路由策略"
                  description={
                    form.ai_routing_mode === "auto"
                      ? "已开启自动路由"
                      : "默认固定使用上方模型"
                  }
                  open={openAiSections.includes("routing")}
                  onToggle={() => toggleAiSection("routing")}
                >
                  <RoutingFields
                    value={{
                      routing_mode: form.ai_routing_mode,
                      fallback_provider_id: form.ai_routing_fallback_provider_id,
                      classifier_provider_id: form.ai_classifier_provider_id,
                    }}
                    providers={providersQ.data}
                    loading={providersQ.isLoading}
                    onChange={(patch) =>
                      setFields({
                        ai_routing_mode: patch.routing_mode ?? form.ai_routing_mode,
                        ai_routing_fallback_provider_id:
                          patch.fallback_provider_id ??
                          form.ai_routing_fallback_provider_id,
                        ai_classifier_provider_id:
                          patch.classifier_provider_id ??
                          form.ai_classifier_provider_id,
                      })
                    }
                  />
                </CollapsibleAiSection>

                <CollapsibleAiSection
                  ref={searchSectionRef}
                  title="工具 / 联网搜索"
                  description={form.ai_web_search ? "已开启联网搜索" : "默认不联网"}
                  open={openAiSections.includes("search")}
                  onToggle={() => toggleAiSection("search")}
                >
                  <WebSearchFields
                    value={{
                      web_search: form.ai_web_search,
                      web_search_context_size: form.ai_web_search_context_size,
                    }}
                    onChange={(patch) =>
                      setFields({
                        ai_web_search: patch.web_search ?? form.ai_web_search,
                        ai_web_search_context_size:
                          patch.web_search_context_size ??
                          form.ai_web_search_context_size,
                      })
                    }
                  />
                </CollapsibleAiSection>

                <CollapsibleAiSection
                  ref={outputSectionRef}
                  title="回复样式"
                  description={
                    form.ai_output_template.trim()
                      ? "已自定义消息模板"
                      : "默认使用简洁模板"
                  }
                  open={openAiSections.includes("output")}
                  onToggle={() => toggleAiSection("output")}
                >
                  <OutputFormatFields
                    value={{
                      output_format: form.ai_output_format,
                      output_template: form.ai_output_template,
                      escape_values: form.ai_escape_values,
                    }}
                    onChange={(patch) =>
                      setFields({
                        ai_output_format:
                          patch.output_format ?? form.ai_output_format,
                        ai_output_template:
                          patch.output_template ?? form.ai_output_template,
                        ai_escape_values:
                          patch.escape_values ?? form.ai_escape_values,
                      })
                    }
                  />
                </CollapsibleAiSection>
              </div>

              <p className="text-xs text-muted-foreground">
                调用流程：用户在 TG 中回复某消息并发 <CommandBadge>{cmdPrefix}{form.name || "ai"} 问题</CommandBadge>，worker 将「被回复消息正文 + 问题」拼成用户提示词，把回答编辑回原消息
              </p>
              <div className="flex flex-wrap gap-1.5">
                <SignalPill tone="primary" label="输出模式" value={form.ai_output_format} />
                <SignalPill
                  tone={form.ai_output_template.trim() ? "success" : "neutral"}
                  label="模板"
                  value={form.ai_output_template.trim() ? "已自定义" : "默认简洁"}
                />
              </div>
            </div>
          )}
        </div>

        <DialogFooter className="!flex shrink-0 !flex-row gap-2 border-t border-border/70 px-4 py-3 sm:px-6 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
          <Button variant="outline" onClick={onCancel} disabled={saving}>
            取消
          </Button>
          <Button onClick={onSave} loading={saving}>
            {!saving ? <Save className="mr-2 h-4 w-4" /> : null}
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const CollapsibleAiSection = forwardRef<
  HTMLDivElement,
  {
    title: string;
    description: string;
    open: boolean;
    onToggle: () => void;
    children: ReactNode;
  }
>(({ title, description, open, onToggle, children }, ref) => (
  <section ref={ref} className="rounded-md border bg-muted/30">
    <button
      type="button"
      className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left"
      onClick={onToggle}
      aria-expanded={open}
    >
      <span className="min-w-0">
        <span className="block text-sm font-semibold">{title}</span>
        <span className="block truncate text-xs text-muted-foreground">
          {description}
        </span>
      </span>
      <ChevronDown
        className={
          "h-4 w-4 shrink-0 text-muted-foreground transition-transform " +
          (open ? "rotate-180" : "")
        }
      />
    </button>
    {open ? <div className="border-t p-3">{children}</div> : null}
  </section>
));
CollapsibleAiSection.displayName = "CollapsibleAiSection";

/**
 * 展开式 provider × model 选择器：
 *
 * 每个启用的 (provider, model) 组合 = 一个候选选项，形如
 * ``Any（OpenAI · gpt-5.5）``。选项 value 编码为 ``{provider_id}|{model}``，
 * 上层在 ``buildPayload`` 里拆开写到 ``cfg.provider_id`` + ``cfg.model``。
 *
 * 如果某 provider 还没启用任何模型，会自动展开成"用 default_model"那条
 * 选项（向后兼容老配置：以前 provider.default_model 直接作为模型）。
 *
 * value 是 ``"<pid>|<model>"`` 的形式；onChange 回传同样格式。
 */
function ProviderModelSelect({
  value,
  providers,
  loading,
  onChange,
}: {
  value: string;
  providers?: LLMProviderOut[];
  loading: boolean;
  onChange: (v: string) => void;
}) {
  if (loading) {
    return (
      <div className="flex h-10 items-center gap-2 rounded-md border px-3 text-xs text-muted-foreground">
        <Spinner className="text-primary" /> 加载中…
      </div>
    );
  }
  if (!providers || providers.length === 0) {
    return (
      <div className="rounded-md border px-3 py-2 text-xs alert-warning">
        尚未配置模型提供商。先到「AI → 模型提供商」新建一个，并在编辑里拉取并启用至少一个模型
      </div>
    );
  }

  // 把每个 provider 展开成"启用的模型"列表
  // 每个 provider 都额外加一条 "用提供商默认（→ default_model）" 行；选了它后保存时
  // cfg.model 不下发，worker 调用时 build_client 会按 provider.default_model 走——
  // 这样用户改 default_model 后所有这种"默认"模板自动跟着变，不用一个个改模板
  type Row = {
    pid: number;
    providerName: string;
    providerKind: string;
    /** 空字符串 = "用提供商默认"；非空 = 具体 model id */
    modelId: string;
    /** 仅"用提供商默认"行才有值；UI 展示成 → gpt-5.5 让用户知道当前默认是啥 */
    defaultModelHint: string | null;
    custom: boolean;
    hasKey: boolean;
  };
  const rows: Row[] = [];
  for (const p of providers) {
    if (!p.has_api_key && (p.provider !== "ollama")) {
      // 没配 key 的 provider 不展开（除了 ollama 本地不需要 key）
      // 但仍想让用户看到，所以加一条 disabled 提示行——这里简单跳过
      // 没有 key 的还是要展示，让用户知道这条 provider 没法用，他能去配
    }
    // (1) 顶部一行：用提供商默认
    rows.push({
      pid: p.id,
      providerName: p.name,
      providerKind: String(p.provider),
      modelId: "",
      defaultModelHint: p.default_model || null,
      custom: false,
      hasKey: !!p.has_api_key,
    });
    // (2) 已启用的具体模型一一展开
    const enabled = (p.models || []).filter((m) => m.enabled);
    for (const m of enabled) {
      rows.push({
        pid: p.id,
        providerName: p.name,
        providerKind: String(p.provider),
        modelId: m.id,
        defaultModelHint: null,
        custom: !!m.custom,
        hasKey: !!p.has_api_key,
      });
    }
  }

  if (rows.length === 0) {
    return (
      <div className="rounded-md border px-3 py-2 text-xs alert-warning">
        所有提供商都没启用任何模型。在「模型提供商」编辑某个提供商，启用至少一条模型再来
      </div>
    );
  }

  return (
    <Select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">— 请选择 —</option>
      {rows.map((r) => {
        // value 形如 "<pid>|<model>"；"用默认"那条 model 部分空
        const v = `${r.pid}|${r.modelId}`;
        const label =
          r.modelId === ""
            ? `${r.providerName}（${r.providerKind} · 用提供商默认${r.defaultModelHint ? ` → ${r.defaultModelHint}` : ""}）` +
            (r.hasKey ? "" : " · ⚠ 未配置 key")
            : `${r.providerName}（${r.providerKind} · ${r.modelId}）` +
            (r.hasKey ? "" : " · ⚠ 未配置 key");
        return (
          <option key={v} value={v}>
            {label}
          </option>
        );
      })}
    </Select>
  );
}
