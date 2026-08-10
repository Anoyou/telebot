// 自定义命令 + LLM Provider API 包装（Sprint2 #2）
import { api, apiFetch } from "@/lib/api";
import { NdjsonDecoder } from "@/lib/ndjsonStream";
import type {
  AccountCommandItem,
  AICommandEnablementSummary,
  BuiltinCommandItem,
  ChatTestModelResult,
  ChatTestModelsRequest,
  ChatTestModelsResponse,
  ClientIdentityVersionDetectResponse,
  ClientIdentityVersionsResponse,
  ClientIdentityVersionsUpdateRequest,
  CommandTemplateCreate,
  CommandTemplateOut,
  CommandTemplateUpdate,
  DetectProviderProtocolsRequest,
  DetectProviderProtocolsResponse,
  FetchModelsPreviewRequest,
  FetchModelsPreviewResponse,
  FetchModelsResponse,
  FullLivenessPreviewRequest,
  FullLivenessPreviewResponse,
  FullLivenessRunRequest,
  FullLivenessRunStartResponse,
  FullLivenessRunResponse,
  LLMProviderCreate,
  LLMProviderApiKeyReveal,
  LLMProviderOut,
  LLMProviderUpdate,
  QuickVerifyProviderRequest,
  QuickVerifyProviderStreamEvent,
  TestModelRequest,
  TestModelResponse,
} from "@/api/types";

const LLM_PROVIDER_OPERATION_TIMEOUT_MS = 120000;
// 测活类请求（test-model / chat-test）的前端超时 = 该请求实际 timeout_seconds + 余量，
// 保证前端不会比后端更早超时、把还在途的请求硬打断（后端 chat-test 上限 600s）。
const TEST_REQUEST_TIMEOUT_MARGIN_MS = 15000;
// test-model 后端固定用 timeout_seconds=90（见 backend/app/api/commands.py::test_model）。
const TEST_MODEL_BACKEND_TIMEOUT_SECONDS = 90;

// ===================== 内置命令（只读，0.4.1 加） =====================
export async function listBuiltinCommands(): Promise<BuiltinCommandItem[]> {
  const { data } = await api.get<BuiltinCommandItem[]>("/api/commands/builtin");
  return data;
}

// ===================== 命令模板 CRUD =====================
export async function listCommandTemplates(): Promise<CommandTemplateOut[]> {
  const { data } = await api.get<CommandTemplateOut[]>(
    "/api/commands/templates",
  );
  return data;
}

export async function createCommandTemplate(
  payload: CommandTemplateCreate,
): Promise<CommandTemplateOut> {
  const { data } = await api.post<CommandTemplateOut>(
    "/api/commands/templates",
    payload,
  );
  return data;
}

export async function patchCommandTemplate(
  id: number,
  payload: CommandTemplateUpdate,
): Promise<CommandTemplateOut> {
  const { data } = await api.patch<CommandTemplateOut>(
    `/api/commands/templates/${id}`,
    payload,
  );
  return data;
}

export async function deleteCommandTemplate(id: number): Promise<void> {
  await api.delete(`/api/commands/templates/${id}`);
}

// ===================== LLM Provider CRUD =====================
export async function listLLMProviders(): Promise<LLMProviderOut[]> {
  const { data } = await api.get<LLMProviderOut[]>(
    "/api/commands/llm-providers",
  );
  return data;
}

export async function createLLMProvider(
  payload: LLMProviderCreate,
): Promise<LLMProviderOut> {
  const { data } = await api.post<LLMProviderOut>(
    "/api/commands/llm-providers",
    payload,
  );
  return data;
}

export async function patchLLMProvider(
  id: number,
  payload: LLMProviderUpdate,
): Promise<LLMProviderOut> {
  const { data } = await api.patch<LLMProviderOut>(
    `/api/commands/llm-providers/${id}`,
    payload,
  );
  return data;
}

export async function deleteLLMProvider(id: number): Promise<void> {
  await api.delete(`/api/commands/llm-providers/${id}`);
}

export async function revealLLMProviderApiKey(
  id: number,
): Promise<LLMProviderApiKeyReveal> {
  const { data } = await api.get<LLMProviderApiKeyReveal>(
    `/api/commands/llm-providers/${id}/api-key`,
  );
  return data;
}

/** 调 GET {base_url}/models 拉模型列表，合并到 provider.models（保留已 enabled 状态）。
 *  Anthropic 不支持，会拿到 422。 */
export async function fetchProviderModels(
  id: number,
): Promise<FetchModelsResponse> {
  const { data } = await api.post<FetchModelsResponse>(
    `/api/commands/llm-providers/${id}/fetch-models`,
    undefined,
    { timeout: LLM_PROVIDER_OPERATION_TIMEOUT_MS },
  );
  return data;
}

/** 用编辑表单当前值预览 fetch /models（不落库），让用户不必先保存即可拉模型列表。 */
export async function fetchProviderModelsPreview(
  payload: FetchModelsPreviewRequest,
): Promise<FetchModelsPreviewResponse> {
  const { data } = await api.post<FetchModelsPreviewResponse>(
    `/api/commands/llm-providers/fetch-models-preview`,
    payload,
    { timeout: LLM_PROVIDER_OPERATION_TIMEOUT_MS },
  );
  return data;
}

export async function detectProviderProtocols(
  payload: DetectProviderProtocolsRequest,
): Promise<DetectProviderProtocolsResponse> {
  const { data } = await api.post<DetectProviderProtocolsResponse>(
    "/api/commands/llm-providers/detect-protocols",
    payload,
    { timeout: LLM_PROVIDER_OPERATION_TIMEOUT_MS },
  );
  return data;
}

/** 用 max_tokens=4 的最小调用测某个 model 通不通；返延时和返回片段。 */
export async function testProviderModel(
  id: number,
  payload: TestModelRequest,
  opts?: { signal?: AbortSignal },
): Promise<TestModelResponse> {
  const { data } = await api.post<TestModelResponse>(
    `/api/commands/llm-providers/${id}/test-model`,
    payload,
    {
      timeout: TEST_MODEL_BACKEND_TIMEOUT_SECONDS * 1000 + TEST_REQUEST_TIMEOUT_MARGIN_MS,
      signal: opts?.signal,
    },
  );
  return data;
}

/** 用真实聊天路径批量测试一个 provider 下的多个模型；可带临时历史上下文。 */
export async function chatTestProviderModels(
  id: number,
  payload: ChatTestModelsRequest,
  opts?: { signal?: AbortSignal },
): Promise<ChatTestModelsResponse> {
  const { data } = await api.post<ChatTestModelsResponse>(
    `/api/commands/llm-providers/${id}/chat-test-models`,
    payload,
    {
      timeout: (payload.timeout_seconds ?? 90) * 1000 + TEST_REQUEST_TIMEOUT_MARGIN_MS,
      signal: opts?.signal,
    },
  );
  return data;
}

export type ChatTestStreamEvent =
  | {
      type: "start";
      requested_model: string;
      streaming: true;
      effective_api_format?: string | null;
      client_identity_profile?: string | null;
      execution_backend?: string | null;
      gateway_version?: string | null;
      gateway_request_id?: string | null;
      gateway_stage?: string | null;
    }
  | {
      type: "delta";
      requested_model: string;
      delta: string;
      model?: string | null;
      stream_fallback?: boolean;
    }
  | {
      type: "done" | "error";
      requested_model: string;
      result: ChatTestModelResult;
    };

function streamErrorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const value = payload as {
    error?: { message?: string };
    detail?: string | { message?: string } | Array<{ message?: string; msg?: string }>;
  };
  if (value.error?.message) return value.error.message;
  if (typeof value.detail === "string") return value.detail;
  if (Array.isArray(value.detail)) {
    const message = value.detail.map((item) => item.message || item.msg).filter(Boolean).join("；");
    if (message) return message;
  }
  if (value.detail && !Array.isArray(value.detail) && value.detail.message) {
    return value.detail.message;
  }
  return fallback;
}

/** 优先消费后端 NDJSON 流；每个模型的增量与最终结果通过回调即时交给页面。 */
export async function streamChatTestProviderModels(
  id: number,
  payload: ChatTestModelsRequest,
  onEvent: (event: ChatTestStreamEvent) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  const response = await apiFetch(
    `/api/commands/llm-providers/${id}/chat-test-models/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify(payload),
      signal: opts?.signal,
    },
  );
  if (!response.ok) {
    let payloadError: unknown;
    try {
      payloadError = await response.json();
    } catch {
      payloadError = null;
    }
    throw new Error(streamErrorMessage(payloadError, `测活请求失败（HTTP ${response.status}）`));
  }
  if (!response.body) throw new Error("浏览器没有提供可读取的流式响应。");

  const reader = response.body.getReader();
  const decoder = new NdjsonDecoder<ChatTestStreamEvent>();
  const completedModels = new Set<string>();
  let streamFinished = false;
  const consumeEvent = (event: ChatTestStreamEvent) => {
    if (event.type === "done" || event.type === "error") {
      completedModels.add(event.requested_model);
    }
    onEvent(event);
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      decoder.push(value).forEach(consumeEvent);
      if (done) break;
    }
    decoder.finish().forEach(consumeEvent);
    const incompleteModels = payload.models.filter((model) => !completedModels.has(model));
    if (incompleteModels.length > 0) {
      throw new Error(`流式响应提前结束，${incompleteModels.length} 个模型没有返回最终状态。`);
    }
    streamFinished = true;
  } finally {
    if (!streamFinished) {
      await reader.cancel().catch(() => undefined);
    }
    reader.releaseLock();
  }
}

/** 用未落库凭据发现模型并执行一次真实流式对话。 */
export async function streamQuickVerifyProvider(
  payload: QuickVerifyProviderRequest,
  onEvent: (event: QuickVerifyProviderStreamEvent) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  const response = await apiFetch(
    "/api/commands/llm-providers/quick-verify/stream",
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify(payload),
      signal: opts?.signal,
    },
  );
  if (!response.ok) {
    let payloadError: unknown;
    try {
      payloadError = await response.json();
    } catch {
      payloadError = null;
    }
    throw new Error(streamErrorMessage(payloadError, `快速验证失败（HTTP ${response.status}）`));
  }
  if (!response.body) throw new Error("浏览器没有提供可读取的流式响应。");

  const reader = response.body.getReader();
  const decoder = new NdjsonDecoder<QuickVerifyProviderStreamEvent>();
  let terminalReceived = false;
  let streamFinished = false;
  const consumeEvent = (event: QuickVerifyProviderStreamEvent) => {
    if (event.type === "done" || event.type === "error") terminalReceived = true;
    onEvent(event);
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      decoder.push(value).forEach(consumeEvent);
      if (done) break;
    }
    decoder.finish().forEach(consumeEvent);
    if (!terminalReceived) throw new Error("流式响应提前结束，没有返回最终验证状态。");
    streamFinished = true;
  } finally {
    if (!streamFinished) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export interface ProviderRuntimeHealth {
  provider_id: number;
  model: string;
  state: string;
  consecutive_failures?: number;
  last_error_class?: string | null;
  last_error_message?: string | null;
  cooldown_remaining_seconds?: number;
  cooldown_until?: number | null;
}

/** 运行时健康只读（Agent 业务调用写入；测活不改写）。 */
export async function listProviderRuntimeHealth(): Promise<ProviderRuntimeHealth[]> {
  const { data } = await api.get<ProviderRuntimeHealth[]>(
    "/api/commands/llm-providers/runtime-health",
  );
  return data;
}

/** 全量已启用模型测活执行预览（只读，不调用上游、不消耗 quota）。 */
export async function fullLivenessPreview(
  payload: FullLivenessPreviewRequest,
): Promise<FullLivenessPreviewResponse> {
  const { data } = await api.post<FullLivenessPreviewResponse>(
    "/api/commands/llm-providers/liveness/preview",
    payload,
    { timeout: LLM_PROVIDER_OPERATION_TIMEOUT_MS },
  );
  return data;
}

/** 按已启用模型执行全量 / 范围测活；返回脱敏诊断结果汇总。 */
export async function fullLivenessRun(
  payload: FullLivenessRunRequest,
): Promise<FullLivenessRunStartResponse> {
  const { data } = await api.post<FullLivenessRunStartResponse>(
    "/api/commands/llm-providers/liveness/run",
    payload,
    { timeout: LLM_PROVIDER_OPERATION_TIMEOUT_MS },
  );
  return data;
}

export async function fullLivenessStatus(runId: string): Promise<FullLivenessRunResponse> {
  const { data } = await api.get<FullLivenessRunResponse>(
    `/api/commands/llm-providers/liveness/${runId}`,
  );
  return data;
}

export async function cancelFullLiveness(runId: string): Promise<FullLivenessRunResponse> {
  const { data } = await api.post<FullLivenessRunResponse>(
    `/api/commands/llm-providers/liveness/${runId}/cancel`,
  );
  return data;
}

// ===================== 账号 × 模板 关联 =====================
export async function listAccountCommands(
  aid: number,
): Promise<AccountCommandItem[]> {
  const { data } = await api.get<AccountCommandItem[]>(
    `/api/accounts/${aid}/commands`,
  );
  return data;
}

export async function enableAccountCommand(
  aid: number,
  templateId: number,
): Promise<void> {
  await api.post(`/api/accounts/${aid}/commands/${templateId}`);
}

export async function disableAccountCommand(
  aid: number,
  templateId: number,
): Promise<void> {
  await api.delete(`/api/accounts/${aid}/commands/${templateId}`);
}

export async function getAICommandEnablementSummary(): Promise<AICommandEnablementSummary> {
  const { data } = await api.get<AICommandEnablementSummary>(
    "/api/commands/ai/enablement-summary",
  );
  return data;
}

// ===================== 客户端身份 UA 版本配置（0.57.0） =====================
export async function getClientIdentityVersions(): Promise<ClientIdentityVersionsResponse> {
  const { data } = await api.get<ClientIdentityVersionsResponse>(
    "/api/commands/llm-providers/identity-versions",
  );
  return data;
}

export async function detectClientIdentityVersions(): Promise<ClientIdentityVersionDetectResponse> {
  const { data } = await api.post<ClientIdentityVersionDetectResponse>(
    "/api/commands/llm-providers/identity-versions/detect",
    {},
    { timeout: 30000 },
  );
  return data;
}

export async function updateClientIdentityVersions(
  payload: ClientIdentityVersionsUpdateRequest,
): Promise<ClientIdentityVersionsResponse> {
  const { data } = await api.put<ClientIdentityVersionsResponse>(
    "/api/commands/llm-providers/identity-versions",
    payload,
  );
  return data;
}
