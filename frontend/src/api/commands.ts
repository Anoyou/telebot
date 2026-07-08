// 自定义命令 + LLM Provider API 包装（Sprint2 #2）
import { api } from "@/lib/api";
import type {
  AccountCommandItem,
  AICommandEnablementSummary,
  BuiltinCommandItem,
  ChatTestModelsRequest,
  ChatTestModelsResponse,
  CommandTemplateCreate,
  CommandTemplateOut,
  CommandTemplateUpdate,
  DetectProviderProtocolsRequest,
  DetectProviderProtocolsResponse,
  FetchModelsPreviewRequest,
  FetchModelsPreviewResponse,
  FetchModelsResponse,
  LLMProviderCreate,
  LLMProviderOut,
  LLMProviderUpdate,
  TestModelRequest,
  TestModelResponse,
} from "@/api/types";

const LLM_PROVIDER_OPERATION_TIMEOUT_MS = 120000;

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
): Promise<TestModelResponse> {
  const { data } = await api.post<TestModelResponse>(
    `/api/commands/llm-providers/${id}/test-model`,
    payload,
    { timeout: LLM_PROVIDER_OPERATION_TIMEOUT_MS },
  );
  return data;
}

/** 用真实聊天路径批量测试一个 provider 下的多个模型；可带临时历史上下文。 */
export async function chatTestProviderModels(
  id: number,
  payload: ChatTestModelsRequest,
): Promise<ChatTestModelsResponse> {
  const { data } = await api.post<ChatTestModelsResponse>(
    `/api/commands/llm-providers/${id}/chat-test-models`,
    payload,
    { timeout: LLM_PROVIDER_OPERATION_TIMEOUT_MS },
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
