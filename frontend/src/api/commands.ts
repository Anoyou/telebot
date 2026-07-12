// 自定义命令 + LLM Provider API 包装（Sprint2 #2）
import { api } from "@/lib/api";
import type {
  AccountCommandItem,
  AICommandEnablementSummary,
  BuiltinCommandItem,
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
  LLMProviderOut,
  LLMProviderUpdate,
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

/** 用编辑表单当前值预览 fetch /models（不落库），让用户不必先保存即可拉模型列表。
 *  Anthropic 不支持，会拿到 422。 */
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
