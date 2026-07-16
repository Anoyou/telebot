import { useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, Loader2, Send, Square, X, Zap } from "lucide-react";
import { toast } from "sonner";

import { createLLMProvider, streamQuickVerifyProvider } from "@/api/commands";
import type {
  LLMApiFormat,
  LLMProviderKind,
  LLMProviderOut,
  QuickVerifyProviderResult,
  QuickVerifyProviderStreamEvent,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { getErrMsg } from "@/lib/api";

const DEFAULT_MESSAGE = "你怎么又不行了？继续。";
const DEFAULT_SYSTEM_PROMPT =
  "你是一个自然、简洁的中文聊天助手。请像真实聊天一样直接回复用户，不要只返回 ping/pong。";

const PROTOCOL_OPTIONS: { value: LLMApiFormat; label: string }[] = [
  { value: "chat_completions", label: "Chat Completions" },
  { value: "responses", label: "Responses" },
  { value: "anthropic_messages", label: "Anthropic Messages" },
];

const DEFAULT_PROVIDER_BASE_URLS: Record<LLMProviderKind, string> = {
  openai: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com/v1",
  ollama: "http://localhost:11434/v1",
};

type VerifyStatus = "idle" | "running" | "success" | "error";

function comparableBaseUrl(value: string | null | undefined): string {
  const raw = (value || "").trim();
  if (!raw) return "";
  try {
    const parsed = new URL(raw);
    return `${parsed.protocol}//${parsed.host.toLowerCase()}${parsed.pathname.replace(/\/+$/, "")}`;
  } catch {
    return raw.replace(/\/+$/, "").toLowerCase();
  }
}

function comparableProviderBaseUrl(provider: LLMProviderOut): string {
  return comparableBaseUrl(
    provider.base_url || DEFAULT_PROVIDER_BASE_URLS[provider.provider],
  );
}

function uniqueProviderName(suggested: string, providers: LLMProviderOut[]): string {
  const base = suggested.trim().slice(0, 58) || "快速验证 Provider";
  const names = new Set(providers.map((provider) => provider.name.toLowerCase()));
  if (!names.has(base.toLowerCase())) return base;
  for (let index = 2; index < 100; index += 1) {
    const candidate = `${base} ${index}`.slice(0, 64);
    if (!names.has(candidate.toLowerCase())) return candidate;
  }
  return `${base}-${Date.now().toString().slice(-4)}`.slice(0, 64);
}

export function QuickVerifyProviderPanel({
  providers,
  onClose,
  onImported,
}: {
  providers: LLMProviderOut[];
  onClose: () => void;
  onImported: () => void;
}) {
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiFormat, setApiFormat] = useState<LLMApiFormat>("chat_completions");
  const [message, setMessage] = useState(DEFAULT_MESSAGE);
  const [manualModel, setManualModel] = useState("");
  const [showManualModel, setShowManualModel] = useState(false);
  const [status, setStatus] = useState<VerifyStatus>("idle");
  const [reply, setReply] = useState("");
  const [error, setError] = useState("");
  const [activeModel, setActiveModel] = useState("");
  const [result, setResult] = useState<QuickVerifyProviderResult | null>(null);
  const [importing, setImporting] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const resetResult = () => {
    if (status === "running") return;
    setStatus("idle");
    setReply("");
    setError("");
    setActiveModel("");
    setResult(null);
  };

  const matchingEndpointProvider = useMemo(() => {
    if (!result?.base_url) return null;
    const normalized = comparableBaseUrl(result.base_url);
    return providers.find(
      (provider) =>
        comparableProviderBaseUrl(provider) === normalized
        && (provider.api_format || "chat_completions") === result.api_format,
    ) || null;
  }, [providers, result]);

  const validate = () => {
    if (!baseUrl.trim()) return "请填写 Base URL。";
    try {
      const url = new URL(baseUrl.trim());
      if (!["http:", "https:"].includes(url.protocol)) return "Base URL 只支持 HTTP(S)。";
      if (url.username || url.password) return "Base URL 不能包含用户名或密码。";
    } catch {
      return "Base URL 必须是完整的 HTTP(S) 地址。";
    }
    if (!message.trim()) return "请填写测活语。";
    if (showManualModel && !manualModel.trim()) return "请填写模型 ID 后重试。";
    return "";
  };

  const runVerify = async () => {
    const validationError = validate();
    if (validationError) {
      setError(validationError);
      setStatus("error");
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setStatus("running");
    setReply("");
    setError("");
    setResult(null);
    setActiveModel(manualModel.trim());

    try {
      await streamQuickVerifyProvider(
        {
          base_url: baseUrl.trim(),
          api_key: apiKey.trim() || null,
          api_format: apiFormat,
          model: showManualModel ? manualModel.trim() : null,
          system_prompt: DEFAULT_SYSTEM_PROMPT,
          message: message.trim(),
          max_tokens: 400,
          timeout_seconds: 90,
        },
        (event: QuickVerifyProviderStreamEvent) => {
          if (event.type === "discovery" || event.type === "start") {
            setActiveModel(event.model);
            return;
          }
          if (event.type === "delta") {
            setActiveModel(event.model);
            setReply((current) => current + event.delta);
            return;
          }
          if (event.type === "done") {
            setResult(event);
            setReply(event.response || "");
            setActiveModel(event.model || event.requested_model || "");
            setStatus("success");
            setError("");
            return;
          }
          setResult(event);
          setReply(event.response || "");
          setError(event.error || "快速验证失败。");
          setStatus("error");
          if (event.requires_model) setShowManualModel(true);
        },
        { signal: controller.signal },
      );
    } catch (caught) {
      if (controller.signal.aborted) {
        setStatus("idle");
        setError("已停止本次验证。可调整参数后重新发送。");
      } else {
        setStatus("error");
        setError(getErrMsg(caught));
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const stopVerify = () => abortRef.current?.abort();

  const importProvider = async () => {
    if (!result?.ok || !result.base_url || !result.provider) return;
    const verifiedModel = (result.requested_model || result.model || "").trim();
    if (!verifiedModel) {
      toast.error("验证结果缺少可导入的模型 ID。");
      return;
    }
    if (verifiedModel.length > 64) {
      toast.error("模型 ID 超过当前 Provider 的 64 字符上限，暂时无法直接导入。");
      return;
    }
    const modelIds = Array.from(new Set([verifiedModel, ...(result.models || [])])).slice(0, 200);
    setImporting(true);
    try {
      await createLLMProvider({
        name: uniqueProviderName(result.suggested_name || "快速验证 Provider", providers),
        provider: result.provider,
        api_key: apiKey.trim() || null,
        base_url: result.base_url,
        default_model: verifiedModel,
        api_format: result.api_format,
        protocol_profile: "standard",
        web_search_api_format: "auto",
        client_identity_profile: "auto",
        modality: "text",
        tags: ["chat"],
        cost_tier: 2,
        notes: "由快速验证导入",
        proxy_id: null,
        models: modelIds.map((id) => ({
          id,
          enabled: id === verifiedModel,
          custom: !result.models.includes(id),
          label: null,
        })),
      });
      toast.success("已导入模型提供商");
      onImported();
    } catch (caught) {
      toast.error(getErrMsg(caught));
    } finally {
      setImporting(false);
    }
  };

  return (
    <section className="rounded-lg border border-border/80 bg-background/80 p-3 shadow-sm sm:p-4" aria-label="快速验证模型">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <span className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-primary/10 text-primary">
              <Zap className="h-4 w-4" />
            </span>
            快速验证
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            临时凭据只用于本次真实对话，验证成功后才会在导入时加密保存。
          </p>
        </div>
        <Button type="button" variant="ghost" size="icon" className="h-10 w-10 shrink-0" aria-label="关闭快速验证" onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-[minmax(0,1.4fr)_minmax(0,1.15fr)_minmax(180px,0.8fr)]">
        <div className="space-y-1.5">
          <Label htmlFor="quick-base-url">Base URL</Label>
          <Input
            id="quick-base-url"
            value={baseUrl}
            disabled={status === "running"}
            placeholder="https://api.example.com/v1"
            inputMode="url"
            onChange={(event) => {
              setBaseUrl(event.target.value);
              setShowManualModel(false);
              setManualModel("");
              resetResult();
            }}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="quick-api-key">API Key</Label>
          <Input
            id="quick-api-key"
            type="password"
            autoComplete="new-password"
            value={apiKey}
            disabled={status === "running"}
            placeholder="本地服务可留空"
            onChange={(event) => {
              setApiKey(event.target.value);
              setShowManualModel(false);
              setManualModel("");
              resetResult();
            }}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="quick-api-format">协议</Label>
          <Select
            id="quick-api-format"
            value={apiFormat}
            disabled={status === "running"}
            onChange={(event) => {
              setApiFormat(event.target.value as LLMApiFormat);
              setShowManualModel(false);
              setManualModel("");
              resetResult();
            }}
          >
            {PROTOCOL_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </Select>
        </div>
      </div>

      {showManualModel ? (
        <div className="mt-3 rounded-md border border-warning/30 bg-warning/[0.06] p-3">
          <Label htmlFor="quick-model">模型 ID</Label>
          <Input
            id="quick-model"
            className="mt-1.5"
            value={manualModel}
            disabled={status === "running"}
            placeholder="自动发现失败，请填写可调用的模型 ID"
            onChange={(event) => {
              setManualModel(event.target.value);
              resetResult();
            }}
          />
        </div>
      ) : null}

      <div className="mt-4 overflow-hidden rounded-lg border border-border/80 bg-muted/20">
        <div className="flex flex-wrap items-center gap-2 border-b border-border/70 px-3 py-2">
          <span className="text-xs font-semibold">真实对话</span>
          {activeModel ? <MetaBadge mono tone="outline">{activeModel}</MetaBadge> : null}
          {status === "running" ? <MetaBadge tone="info">流式接收中</MetaBadge> : null}
          {status === "success" ? <MetaBadge tone="success">验证可用</MetaBadge> : null}
          {result?.latency_ms != null ? <MetaBadge>{result.latency_ms} ms</MetaBadge> : null}
        </div>

        <div className="min-h-28 space-y-3 p-3 sm:p-4">
          {(status !== "idle" || reply || error) ? (
            <>
              <div className="ml-auto max-w-[88%] rounded-xl rounded-br-sm bg-primary px-3 py-2 text-sm leading-6 text-primary-foreground sm:max-w-[72%]">
                {message.trim()}
              </div>
              <div className="max-w-[92%] rounded-xl rounded-bl-sm bg-background px-3 py-2 text-sm leading-6 shadow-sm sm:max-w-[80%]">
                {reply ? <p className="whitespace-pre-wrap break-words">{reply}</p> : status === "running" ? (
                  <span className="inline-flex items-center gap-2 text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" /> 等待模型回复
                  </span>
                ) : (
                  <span className="text-muted-foreground">模型没有返回可显示的内容。</span>
                )}
              </div>
            </>
          ) : (
            <div className="grid min-h-20 place-items-center text-center text-xs leading-5 text-muted-foreground">
              填写连接信息后发送测活语，回复会在这里实时出现。
            </div>
          )}
        </div>

        {error ? (
          <div className="border-t border-destructive/20 bg-destructive/[0.05] px-3 py-2 text-xs leading-5 text-destructive">
            {error}
          </div>
        ) : null}

        <div className="flex flex-col gap-2 border-t border-border/70 p-3 sm:flex-row sm:items-end">
          <div className="min-w-0 flex-1 space-y-1.5">
            <Label htmlFor="quick-message">测活语</Label>
            <Textarea
              id="quick-message"
              className="min-h-[68px] resize-none sm:min-h-[62px]"
              value={message}
              disabled={status === "running"}
              maxLength={2000}
              onChange={(event) => {
                setMessage(event.target.value);
                resetResult();
              }}
            />
          </div>
          {status === "running" ? (
            <Button type="button" variant="outline" className="h-10 shrink-0" onClick={stopVerify}>
              <Square className="h-3.5 w-3.5 fill-current" /> 停止
            </Button>
          ) : (
            <Button type="button" className="h-10 shrink-0" onClick={runVerify}>
              <Send className="h-4 w-4" /> 发送验证
            </Button>
          )}
        </div>
      </div>

      <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0 text-xs leading-5 text-muted-foreground">
          {matchingEndpointProvider ? (
            <span>
              已有相同接入地址与协议的「{matchingEndpointProvider.name}」。如果这是另一组账号凭据，可继续导入为独立 Provider；否则建议直接编辑现有配置。
            </span>
          ) : result?.ok ? (
            <span className="inline-flex items-center gap-1.5 text-success">
              <CheckCircle2 className="h-4 w-4" /> 已确认当前凭据可以调用 {result.requested_model || result.model}
            </span>
          ) : (
            <span>验证成功后可直接导入，其他发现模型会保留为未启用候选项。</span>
          )}
        </div>
        <Button
          type="button"
          className="h-10 shrink-0"
          disabled={!result?.ok || importing || status === "running"}
          onClick={importProvider}
        >
          {importing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
          {matchingEndpointProvider ? "仍导入为独立 Provider" : "导入模型提供商"}
        </Button>
      </div>
    </section>
  );
}
