import { useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, Download, Plus, Send, Square } from "lucide-react";

import {
  fetchProviderModelsPreview,
  streamQuickVerifyProvider,
} from "@/api/commands";
import type {
  LLMApiFormat,
  LLMClientIdentityProfile,
  LLMExecutionBackend,
  LLMProtocolProfile,
  LLMProviderKind,
  LLMRequestHeaderInput,
  ProviderModel,
  QuickVerifyProviderResult,
  QuickVerifyProviderStreamEvent,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Textarea } from "@/components/ui/textarea";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  ReasoningEffortSelect,
  type ReasoningEffortValue,
} from "@/components/ai/ReasoningEffortSelect";
import { StreamingText } from "@/components/ai/StreamingText";
import { useStreamingText } from "@/hooks/useStreamingText";

const DEFAULT_MESSAGE =
  "我准备开始一段需要专注的工作，但现在有些分心。请用两句话给我一个可以立刻执行的小建议。";

const DEFAULT_BASE_URLS: Record<LLMProviderKind, string> = {
  openai: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com/v1",
  ollama: "http://localhost:11434/v1",
};

type VerifyStatus = "idle" | "running" | "success" | "error";
export type ProviderCreateStage =
  | "empty"
  | "fetching"
  | "select"
  | "selected"
  | "verifying"
  | "verified";

export function ProviderCreateVerification({
  providerKind,
  executionBackend,
  apiFormat,
  protocolProfile,
  clientIdentityProfile,
  baseUrl,
  apiKey,
  proxyId,
  models,
  requestHeaders,
  onModelsChange,
  onReset,
  onVerified,
  onVerificationChange,
  onStageChange,
}: {
  providerKind: LLMProviderKind;
  executionBackend: LLMExecutionBackend;
  apiFormat: LLMApiFormat;
  protocolProfile: LLMProtocolProfile;
  clientIdentityProfile: LLMClientIdentityProfile;
  baseUrl: string;
  apiKey: string;
  proxyId: string;
  models: ProviderModel[];
  requestHeaders: LLMRequestHeaderInput[];
  onModelsChange: (models: ProviderModel[]) => void;
  onReset: () => void;
  onVerified: (model: string, models: ProviderModel[]) => void;
  onVerificationChange: (verified: boolean) => void;
  onStageChange?: (stage: ProviderCreateStage) => void;
}) {
  const [fetching, setFetching] = useState(false);
  const [selectedModel, setSelectedModel] = useState("");
  const [modelFilter, setModelFilter] = useState("");
  const [manualModel, setManualModel] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffortValue>("");
  const [message, setMessage] = useState(DEFAULT_MESSAGE);
  const [status, setStatus] = useState<VerifyStatus>("idle");
  const [error, setError] = useState("");
  const [result, setResult] = useState<QuickVerifyProviderResult | null>(null);
  const [streamFallback, setStreamFallback] = useState(false);
  const liveReply = useStreamingText();
  const abortRef = useRef<AbortController | null>(null);
  const fingerprint = [
    providerKind,
    executionBackend,
    apiFormat,
    protocolProfile,
    clientIdentityProfile,
    baseUrl.trim(),
    apiKey,
    proxyId,
    JSON.stringify(requestHeaders),
  ].join("\u0000");
  const previousFingerprint = useRef(fingerprint);

  useEffect(() => {
    if (fetching) {
      onStageChange?.("fetching");
    } else if (status === "running") {
      onStageChange?.("verifying");
    } else if (status === "success") {
      onStageChange?.("verified");
    } else if (models.length === 0) {
      onStageChange?.("empty");
    } else if (!selectedModel) {
      onStageChange?.("select");
    } else {
      onStageChange?.("selected");
    }
  }, [fetching, models.length, onStageChange, selectedModel, status]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (previousFingerprint.current === fingerprint) return;
    previousFingerprint.current = fingerprint;
    abortRef.current?.abort();
    setSelectedModel("");
    setModelFilter("");
    setReasoningEffort("");
    setStatus("idle");
    liveReply.clear();
    setError("");
    setResult(null);
    setStreamFallback(false);
    onReset();
    onVerificationChange(false);
  }, [fingerprint]);

  const selectedMetadata = useMemo(
    () => models.find((model) => model.id === selectedModel),
    [models, selectedModel],
  );
  const enabledModelCount = useMemo(
    () => models.filter((model) => model.enabled).length,
    [models],
  );
  const visibleModels = useMemo(() => {
    const query = modelFilter.trim().toLowerCase();
    if (!query) return models;
    return models.filter((model) =>
      `${model.id} ${model.label || ""}`.toLowerCase().includes(query),
    );
  }, [modelFilter, models]);

  const resetVerification = () => {
    if (status === "running") abortRef.current?.abort();
    setStatus("idle");
    liveReply.clear();
    setError("");
    setResult(null);
    setStreamFallback(false);
    onVerificationChange(false);
  };

  const fetchModels = async () => {
    if (!apiKey.trim() && providerKind !== "ollama") {
      setError("请先填写 API Key，再获取模型列表。");
      setStatus("error");
      return;
    }
    setFetching(true);
    resetVerification();
    try {
      const response = await fetchProviderModelsPreview({
        provider: providerKind,
        execution_backend: executionBackend,
        api_format: apiFormat,
        base_url: baseUrl.trim() || DEFAULT_BASE_URLS[providerKind],
        api_key: apiKey.trim() || null,
        proxy_id: proxyId ? Number(proxyId) : null,
        pid: null,
        request_headers: requestHeaders,
      });
      const existing = new Map(models.map((model) => [model.id, model]));
      const discovered = response.ids.slice(0, 200).map((id) =>
        existing.get(id) || {
          id,
          enabled: false,
          custom: false,
          label: null,
        },
      );
      const customOnly = models.filter(
        (model) => model.custom && !response.ids.includes(model.id),
      );
      const merged = [...discovered, ...customOnly].slice(0, 200);
      onModelsChange(merged);
      setSelectedModel("");
      setReasoningEffort("");
      setError(
        merged.length > 0
          ? ""
          : "模型列表为空，可以在下方手动添加模型 ID。",
      );
      setStatus(merged.length > 0 ? "idle" : "error");
    } catch (caught) {
      setError(`${getErrMsg(caught)} 可以在下方手动添加模型 ID。`);
      setStatus("error");
    } finally {
      setFetching(false);
    }
  };

  const addManualModel = () => {
    const id = manualModel.trim();
    if (!id) return;
    if (models.some((model) => model.id === id)) {
      setSelectedModel(id);
      setManualModel("");
      setReasoningEffort("");
      resetVerification();
      return;
    }
    const next = [
      ...models,
      { id, enabled: true, custom: true, label: null },
    ].slice(0, 200);
    onModelsChange(next);
    setSelectedModel(id);
    setManualModel("");
    setReasoningEffort("");
    resetVerification();
  };

  const setModelEnabled = (id: string, enabled: boolean) => {
    onModelsChange(
      models.map((model) => (model.id === id ? { ...model, enabled } : model)),
    );
  };

  const setVisibleModelsEnabled = (enabled: boolean) => {
    const visibleIds = new Set(visibleModels.map((model) => model.id));
    onModelsChange(
      models.map((model) =>
        visibleIds.has(model.id) ? { ...model, enabled } : model,
      ),
    );
  };

  const runVerify = async () => {
    if (!selectedModel) {
      setError("请先选择一个模型。");
      setStatus("error");
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setStatus("running");
    liveReply.clear();
    setError("");
    setResult(null);
    setStreamFallback(false);
    onVerificationChange(false);
    try {
      await streamQuickVerifyProvider(
        {
          base_url: baseUrl.trim() || DEFAULT_BASE_URLS[providerKind],
          execution_backend: executionBackend,
          api_key: apiKey.trim() || null,
          api_format: apiFormat,
          protocol_profile: protocolProfile,
          client_identity_profile: clientIdentityProfile,
          model: selectedModel,
          reasoning_effort: reasoningEffort || null,
          proxy_id: proxyId ? Number(proxyId) : null,
          message: message.trim(),
          timeout_seconds: 90,
          request_headers: requestHeaders,
        },
        (event: QuickVerifyProviderStreamEvent) => {
          if (event.type === "delta") {
            liveReply.append(event.delta);
            if (event.stream_fallback) setStreamFallback(true);
            return;
          }
          if (event.type === "done") {
            setResult(event);
            setStreamFallback(Boolean(event.stream_fallback));
            liveReply.syncSnapshot(event.response || "");
            setStatus("success");
            const nextModels = models.map((model) => {
              if (model.id !== selectedModel) return model;
              const verifiedEfforts = reasoningEffort
                ? Array.from(
                    new Set([...(model.reasoning_efforts || []), reasoningEffort]),
                  )
                : model.reasoning_efforts;
              return {
                ...model,
                enabled: true,
                reasoning_efforts: verifiedEfforts,
              };
            });
            onVerified(selectedModel, nextModels);
            onVerificationChange(true);
            return;
          }
          if (event.type === "error") {
            setResult(event);
            setStreamFallback(Boolean(event.stream_fallback));
            liveReply.syncSnapshot(event.response || "");
            setError(event.error || "模型验证失败。");
            setStatus("error");
          }
        },
        { signal: controller.signal },
      );
    } catch (caught) {
      if (controller.signal.aborted) {
        setStatus("idle");
        setError("已停止本次验证。");
      } else {
        setStatus("error");
        setError(getErrMsg(caught));
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  return (
    <section className="space-y-4 border-t pt-6" aria-label="模型发现与真实验证">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold">模型与真实验证</h2>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            先多选要启用的模型，再单独指定一个模型做真实验证并设为默认；验证失败时仍可保存，但会在提交前提醒风险。
          </p>
        </div>
        <Button type="button" size="sm" variant="outline" loading={fetching} disabled={status === "running"} onClick={() => void fetchModels()}>
          {!fetching ? <Download className="mr-1 h-4 w-4" /> : null}
          {models.length > 0 ? "重新获取模型" : "获取模型列表"}
        </Button>
      </div>

      <div className="flex flex-col gap-2 rounded-lg border border-dashed bg-muted/20 p-3 sm:flex-row sm:items-end">
        <div className="flex min-w-0 flex-1 items-end gap-2">
          <div className="min-w-0 flex-1 space-y-1.5">
            <Label htmlFor="create-manual-model">手动模型 ID</Label>
            <Input
              id="create-manual-model"
              value={manualModel}
              maxLength={128}
              disabled={status === "running"}
              placeholder="模型列表不可用时填写"
              onChange={(event) => setManualModel(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  addManualModel();
                }
              }}
            />
          </div>
          <Button type="button" variant="outline" disabled={!manualModel.trim() || status === "running"} onClick={addManualModel}>
            <Plus className="h-4 w-4" /> 添加
          </Button>
        </div>
      </div>

      {models.length > 0 ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div className="min-w-0">
              <Label htmlFor="create-model-filter">启用模型</Label>
              <p className="mt-1 text-xs text-muted-foreground">
                已启用 {enabledModelCount} / {models.length} 个。复选框控制启用，右侧单选用于指定验证目标。
              </p>
            </div>
            <Input
              id="create-model-filter"
              className="h-9 w-full sm:w-64"
              value={modelFilter}
              disabled={status === "running"}
              placeholder="筛选模型 ID"
              onChange={(event) => setModelFilter(event.target.value)}
            />
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/20 px-3 py-2">
            <span className="text-xs text-muted-foreground">
              当前显示 {visibleModels.length} 个模型
            </span>
            <div className="flex flex-wrap gap-1.5">
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-8 px-2 text-xs"
                disabled={status === "running" || visibleModels.length === 0}
                onClick={() => setVisibleModelsEnabled(true)}
              >
                全选当前结果
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-8 px-2 text-xs"
                disabled={status === "running" || visibleModels.length === 0}
                onClick={() => setVisibleModelsEnabled(false)}
              >
                清空当前结果
              </Button>
            </div>
          </div>
          <div className="max-h-72 overflow-y-auto rounded-lg border bg-background">
            {visibleModels.length > 0 ? visibleModels.map((model) => {
              const selected = model.id === selectedModel;
              return (
                <div
                  key={model.id}
                  className={cn(
                    "flex min-h-12 w-full min-w-0 items-center gap-3 border-b px-3 py-2.5 text-left text-sm transition-colors last:border-b-0 hover:bg-muted/60",
                    selected && "bg-primary/[0.08] ring-1 ring-inset ring-primary/25",
                  )}
                >
                  <label className="grid h-8 w-8 shrink-0 cursor-pointer place-items-center" title={model.enabled ? "停用此模型" : "启用此模型"}>
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-primary"
                      checked={model.enabled}
                      disabled={status === "running"}
                      aria-label={`${model.enabled ? "停用" : "启用"}模型 ${model.id}`}
                      onChange={(event) => setModelEnabled(model.id, event.target.checked)}
                    />
                  </label>
                  <span className="min-w-0 flex-1 break-all font-mono text-xs">{model.id}</span>
                  <button
                    type="button"
                    className={cn(
                      "inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border px-2 text-xs transition-colors",
                      selected
                        ? "border-primary bg-primary/10 text-foreground"
                        : "border-border bg-background text-muted-foreground hover:bg-muted",
                    )}
                    aria-pressed={selected}
                    disabled={status === "running"}
                    onClick={() => {
                      setSelectedModel(model.id);
                      setReasoningEffort("");
                      resetVerification();
                    }}
                  >
                    <span className={cn("grid h-3.5 w-3.5 shrink-0 place-items-center rounded-full border", selected && "border-primary")}>
                      {selected ? <span className="h-1.5 w-1.5 rounded-full bg-primary" /> : null}
                    </span>
                    {selected ? "验证目标" : "选为验证目标"}
                  </button>
                </div>
              );
            }) : (
              <p className="px-3 py-6 text-center text-xs text-muted-foreground">没有匹配的模型。</p>
            )}
          </div>
        </div>
      ) : (
        <div className="rounded-md border border-dashed bg-background px-3 py-6 text-center text-xs text-muted-foreground">
          尚未获取模型列表，也可以先手动添加模型 ID。
        </div>
      )}

      {selectedModel ? (
        <div className="space-y-1.5">
          <Label htmlFor="create-reasoning-effort">推理强度（仅本次验证）</Label>
          <ReasoningEffortSelect
            id="create-reasoning-effort"
            value={reasoningEffort}
            onChange={(value) => {
              setReasoningEffort(value);
              resetVerification();
            }}
            declaredEfforts={selectedMetadata?.reasoning_efforts}
            apiFormat={apiFormat}
            modelId={selectedModel}
            disabled={status === "running"}
          />
        </div>
      ) : null}

      <div className="overflow-hidden rounded-md border bg-background">
        <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
          <span className="text-xs font-semibold">真实对话</span>
          {selectedModel ? <MetaBadge mono tone="outline">{selectedModel}</MetaBadge> : null}
          {reasoningEffort ? <MetaBadge mono>{reasoningEffort}</MetaBadge> : <MetaBadge>自动</MetaBadge>}
          {status === "running" && !streamFallback ? (
            <MetaBadge tone="info">流式接收中</MetaBadge>
          ) : null}
          {status === "running" && streamFallback ? (
            <MetaBadge tone="outline">完整响应</MetaBadge>
          ) : null}
          {status === "success" ? <MetaBadge tone="success">验证可用</MetaBadge> : null}
          {result?.latency_ms != null ? <MetaBadge>{result.latency_ms} ms</MetaBadge> : null}
        </div>
        <div className="min-h-24 space-y-3 p-3">
          {status !== "idle" || liveReply.text || error ? (
            <>
              <div className="ml-auto max-w-[88%] rounded-xl rounded-br-sm bg-primary px-3 py-2 text-sm leading-6 text-primary-foreground sm:max-w-[72%]">
                {message.trim()}
              </div>
              <div className="max-w-[92%] rounded-xl rounded-bl-sm bg-muted px-3 py-2 text-sm leading-6 sm:max-w-[80%]">
                {liveReply.text ? (
                  <StreamingText
                    text={liveReply.text}
                    active={status === "running"}
                    fallback={streamFallback}
                  />
                ) : status === "running" ? (
                  <StreamingText text="" active waitingLabel="正在等待上游返回首段内容" />
                ) : <span className="text-muted-foreground">模型没有返回可显示的内容。</span>}
              </div>
            </>
          ) : (
            <div className="grid min-h-16 place-items-center text-center text-xs leading-5 text-muted-foreground">
              选择模型后发送默认测活语，回复会在这里实时出现。
            </div>
          )}
        </div>
        {error ? <div className="border-t border-destructive/20 bg-destructive/[0.05] px-3 py-2 text-xs leading-5 text-destructive">{error}</div> : null}
        <div className="flex flex-col gap-2 border-t p-3 sm:flex-row sm:items-end">
          <div className="min-w-0 flex-1 space-y-1.5">
            <Label htmlFor="create-verify-message">测活语</Label>
            <Textarea
              id="create-verify-message"
              className="min-h-[62px] resize-none"
              value={message}
              maxLength={2000}
              disabled={status === "running"}
              onChange={(event) => {
                setMessage(event.target.value);
                resetVerification();
              }}
            />
          </div>
          {status === "running" ? (
            <Button type="button" variant="outline" className="h-10 shrink-0" onClick={() => abortRef.current?.abort()}>
              <Square className="h-3.5 w-3.5 fill-current" /> 停止
            </Button>
          ) : (
            <Button type="button" className="h-10 shrink-0" disabled={!selectedModel || !message.trim()} onClick={() => void runVerify()}>
              <Send className="h-4 w-4" /> 验证所选模型
            </Button>
          )}
        </div>
      </div>

      {status === "success" ? (
        <p className="inline-flex items-center gap-1.5 text-xs text-success">
          <CheckCircle2 className="h-4 w-4" /> 当前模型和档位已通过真实对话验证，将启用并作为默认模型保存；其它模型的启用状态保持不变。
        </p>
      ) : null}
    </section>
  );
}
