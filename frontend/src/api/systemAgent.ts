/** System Agent API client. */
import { api, apiFetch } from "@/lib/api";
import { NdjsonDecoder } from "@/lib/ndjsonStream";

export interface SystemAgentConfig {
  enabled: boolean;
  provider_id: number | null;
  model: string | null;
  fallback_provider_ids: number[];
  require_tool_approval: boolean;
  max_steps: number;
  max_tool_calls: number;
  session_token_limit: number;
}

export interface SystemAgentCapability {
  name: string;
  description: string;
  read_only: boolean;
  min_role: string;
  risk: string;
  channels: string[];
  available: boolean;
  unavailable_reason?: string | null;
}

export interface SystemAgentModelMatrixItem {
  provider_id: number;
  provider_name: string;
  model: string;
  enabled?: boolean;
  declared_supports_tools?: boolean | null;
  declared_supports_images?: boolean | null;
  declared_reasoning_efforts?: unknown;
  probed_supports_tools?: boolean | null;
  probed_status?: string | null;
  health?: {
    state?: string;
    cooldown_remaining_seconds?: number;
    last_error_class?: string | null;
    last_error_message?: string | null;
  };
}

export interface SystemAgentCapabilities {
  enabled: boolean;
  provider_id: number | null;
  model: string | null;
  provider_name?: string | null;
  resolved_model?: string | null;
  ai_enabled: boolean;
  timezone: string;
  tools: SystemAgentCapability[];
  stage: number;
  write_tools_available: boolean;
  model_matrix?: SystemAgentModelMatrixItem[];
}

export interface SystemAgentSession {
  id: string;
  web_user_id: number | null;
  bot_tg_user_id: number | null;
  account_id: number | null;
  channel: string;
  title: string | null;
  status: string;
  memory_summary?: string;
  memory_state?: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface SystemAgentMessage {
  id: number;
  session_id: string;
  role: string;
  content: Record<string, unknown>;
  usage?: Record<string, unknown> | null;
  run_status?: "pending" | "succeeded" | "failed" | "completed" | string;
  error_code?: string | null;
  error_message?: string | null;
  retry_count?: number;
  created_at: string | null;
}

export interface SystemAgentProviderSwitchCandidate {
  provider_id: number;
  provider_name: string;
  model: string;
}

export interface SystemAgentProviderSwitch {
  from_provider_name?: string;
  candidates: SystemAgentProviderSwitchCandidate[];
}

export interface SystemAgentToolApprovalItem {
  name: string;
  description: string;
  read_only: boolean;
  risk: string;
}

export interface SystemAgentToolApproval {
  domains?: string[];
  tools: SystemAgentToolApprovalItem[];
}

export interface SystemAgentAction {
  id: string;
  session_id?: string | null;
  account_id?: number | null;
  channel: string;
  tool_name: string;
  arguments?: Record<string, unknown>;
  secret_fields?: string[] | null;
  has_secret?: boolean;
  summary: string;
  preview: Record<string, unknown>;
  risk: string;
  status: string;
  result?: Record<string, unknown> | null;
  error_code?: string | null;
  error_message?: string | null;
  runtime_sync_status?: string;
  runtime_sync_error?: string | null;
  expires_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  executed_at?: string | null;
}

export type SystemAgentStreamEvent = {
  type: string;
  run_id?: string;
  session_id?: string;
  seq?: number;
  ts?: string;
  content?: string;
  delta?: string;
  message?: string;
  code?: string;
  tool_name?: string;
  call_id?: string;
  is_error?: boolean;
  result_summary?: unknown;
  arguments_summary?: unknown;
  usage?: Record<string, unknown>;
  ok?: boolean;
  hint?: { web_path?: string; message?: string };
  provider_id?: number;
  provider_name?: string;
  model?: string;
  reason?: string;
  provider_switch?: SystemAgentProviderSwitch;
  tool_approval?: SystemAgentToolApproval;
  action?: SystemAgentAction;
  stream_fallback?: boolean;
  [key: string]: unknown;
};

export interface SystemAgentRun {
  id: string;
  run_id: string;
  session_id: string;
  web_user_id: number | null;
  user_message_id: number | null;
  client_request_id: string;
  kind: "message" | "retry" | string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | string;
  last_seq: number;
  cancel_requested: boolean;
  error_code?: string | null;
  error_message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export async function getSystemAgentConfig(): Promise<SystemAgentConfig> {
  const { data } = await api.get<SystemAgentConfig>("/api/system-agent/config");
  return data;
}

export async function patchSystemAgentConfig(
  patch: Partial<SystemAgentConfig>,
): Promise<SystemAgentConfig> {
  const { data } = await api.patch<SystemAgentConfig>("/api/system-agent/config", patch);
  return data;
}

export async function getSystemAgentCapabilities(): Promise<SystemAgentCapabilities> {
  const { data } = await api.get<SystemAgentCapabilities>("/api/system-agent/capabilities");
  return data;
}

export async function listSystemAgentRunEvents(
  runId: string,
  afterSeq = 0,
): Promise<SystemAgentStreamEvent[]> {
  const { data } = await api.get<Array<{ run_id: string; seq: number; event: Record<string, unknown> }>>(
    `/api/system-agent/runs/${runId}/events`,
    { params: { after_seq: afterSeq } },
  );
  return (data || []).map((row) => ({
    ...(row.event || {}),
    type: String(row.event?.type || ""),
    run_id: row.run_id,
    seq: row.seq,
  })) as SystemAgentStreamEvent[];
}

export async function listSystemAgentRuns(params?: {
  status?: string;
  since?: string;
  until?: string;
  limit?: number;
}): Promise<SystemAgentRun[]> {
  const { data } = await api.get<SystemAgentRun[]>("/api/system-agent/runs", { params });
  return data;
}

export interface SystemAgentUserMemory {
  id: number;
  scope_type: string;
  scope_id: number;
  content: string;
  source: string;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export async function listSystemAgentUserMemory(): Promise<SystemAgentUserMemory[]> {
  const { data } = await api.get<SystemAgentUserMemory[]>("/api/system-agent/memory");
  return data;
}

export async function createSystemAgentUserMemory(payload: {
  content: string;
  enabled?: boolean;
}): Promise<SystemAgentUserMemory> {
  const { data } = await api.post<SystemAgentUserMemory>("/api/system-agent/memory", payload);
  return data;
}

export async function patchSystemAgentUserMemory(
  id: number,
  payload: { content?: string; enabled?: boolean },
): Promise<SystemAgentUserMemory> {
  const { data } = await api.patch<SystemAgentUserMemory>(`/api/system-agent/memory/${id}`, payload);
  return data;
}

export async function deleteSystemAgentUserMemory(id: number): Promise<void> {
  await api.delete(`/api/system-agent/memory/${id}`);
}

export async function listSystemAgentSessions(
  params?: { status?: string; limit?: number },
): Promise<SystemAgentSession[]> {
  const { data } = await api.get<SystemAgentSession[]>("/api/system-agent/sessions", { params });
  return data;
}

export async function createSystemAgentSession(payload?: {
  account_id?: number | null;
  title?: string;
}): Promise<SystemAgentSession> {
  const { data } = await api.post<SystemAgentSession>("/api/system-agent/sessions", payload || {});
  return data;
}

export async function getSystemAgentSession(sessionId: string): Promise<SystemAgentSession> {
  const { data } = await api.get<SystemAgentSession>(`/api/system-agent/sessions/${sessionId}`);
  return data;
}

export async function updateSystemAgentSession(
  sessionId: string,
  payload: { title?: string; status?: string; account_id?: number | null },
): Promise<SystemAgentSession> {
  const { data } = await api.patch<SystemAgentSession>(
    `/api/system-agent/sessions/${sessionId}`,
    payload,
  );
  return data;
}

export async function deleteSystemAgentSession(sessionId: string): Promise<void> {
  await api.delete(`/api/system-agent/sessions/${sessionId}`);
}

export async function listSystemAgentMessages(
  sessionId: string,
  params?: { limit?: number; before_id?: number },
): Promise<SystemAgentMessage[]> {
  const { data } = await api.get<SystemAgentMessage[]>(
    `/api/system-agent/sessions/${sessionId}/messages`,
    { params },
  );
  return data;
}

/** 本轮模型选择：auto 走全局配置；pinned 固定 provider+model（不改全局） */
export type SystemAgentModelSelection =
  | { mode: "auto" }
  | { mode: "pinned"; provider_id: number; model: string };

export async function startSystemAgentRun(
  sessionId: string,
  payload: {
    content: string;
    account_id?: number | null;
    client_request_id: string;
    model_selection?: SystemAgentModelSelection | null;
  },
): Promise<SystemAgentRun> {
  const { data } = await api.post<SystemAgentRun>(
    `/api/system-agent/sessions/${sessionId}/runs`,
    payload,
  );
  return data;
}

export async function startSystemAgentRetryRun(
  sessionId: string,
  messageId: number,
  payload: {
    account_id?: number | null;
    fallback_provider_id?: number | null;
    approved_tools?: string[];
    client_request_id: string;
    model_selection?: SystemAgentModelSelection | null;
  },
): Promise<SystemAgentRun> {
  const { data } = await api.post<SystemAgentRun>(
    `/api/system-agent/sessions/${sessionId}/messages/${messageId}/retry/runs`,
    payload,
  );
  return data;
}

export async function getSystemAgentRun(runId: string): Promise<SystemAgentRun> {
  const { data } = await api.get<SystemAgentRun>(`/api/system-agent/runs/${runId}`);
  return data;
}

export async function cancelSystemAgentRun(runId: string): Promise<SystemAgentRun> {
  const { data } = await api.post<SystemAgentRun>(`/api/system-agent/runs/${runId}/cancel`);
  return data;
}

export async function streamSystemAgentRun(
  runId: string,
  afterSeq: number,
  onEvent: (event: SystemAgentStreamEvent) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  const params = new URLSearchParams({ after_seq: String(Math.max(0, afterSeq)) });
  const response = await apiFetch(`/api/system-agent/runs/${runId}/stream?${params}`, {
    method: "GET",
    headers: { Accept: "application/x-ndjson" },
    signal: opts?.signal,
  });
  return consumeSystemAgentStream(response, onEvent);
}

function streamErrorMessage(value: unknown, fallback: string): string {
  if (!value || typeof value !== "object") return fallback;
  const obj = value as Record<string, unknown>;
  const error = obj.error as Record<string, unknown> | undefined;
  const detail = obj.detail as Record<string, unknown> | string | undefined;
  if (error?.message && typeof error.message === "string") return error.message;
  if (detail && typeof detail === "object" && typeof detail.message === "string") {
    return detail.message;
  }
  if (typeof detail === "string") return detail;
  return fallback;
}

export async function listSystemAgentActions(params?: {
  session_id?: string;
  status?: string;
  limit?: number;
}): Promise<SystemAgentAction[]> {
  const { data } = await api.get<SystemAgentAction[]>("/api/system-agent/actions", { params });
  return data;
}

export async function getSystemAgentAction(actionId: string): Promise<SystemAgentAction> {
  const { data } = await api.get<SystemAgentAction>(`/api/system-agent/actions/${actionId}`);
  return data;
}

export async function confirmSystemAgentAction(actionId: string): Promise<{
  ok: boolean;
  already_final?: boolean;
  error_code?: string | null;
  error_message?: string | null;
  action?: SystemAgentAction | null;
}> {
  const { data } = await api.post(`/api/system-agent/actions/${actionId}/confirm`);
  return data;
}

export async function rejectSystemAgentAction(actionId: string): Promise<SystemAgentAction> {
  const { data } = await api.post<SystemAgentAction>(`/api/system-agent/actions/${actionId}/reject`);
  return data;
}

export async function retrySystemAgentRuntimeSync(actionId: string): Promise<{
  ok: boolean;
  error_code?: string | null;
  error_message?: string | null;
  action?: SystemAgentAction | null;
}> {
  const { data } = await api.post(`/api/system-agent/actions/${actionId}/retry-runtime-sync`);
  return data;
}

/** 消费 NDJSON 对话流；任意分块边界安全。 */
export async function streamSystemAgentMessage(
  sessionId: string,
  payload: { content: string; account_id?: number | null },
  onEvent: (event: SystemAgentStreamEvent) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  return streamSystemAgentRequest(
    `/api/system-agent/sessions/${sessionId}/messages/stream`,
    payload,
    onEvent,
    opts,
  );
}

export async function retrySystemAgentMessage(
  sessionId: string,
  messageId: number,
  payload: {
    account_id?: number | null;
    fallback_provider_id?: number | null;
    approved_tools?: string[];
  },
  onEvent: (event: SystemAgentStreamEvent) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  return streamSystemAgentRequest(
    `/api/system-agent/sessions/${sessionId}/messages/${messageId}/retry/stream`,
    payload,
    onEvent,
    opts,
  );
}

async function streamSystemAgentRequest(
  path: string,
  payload: Record<string, unknown>,
  onEvent: (event: SystemAgentStreamEvent) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  const response = await apiFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
    body: JSON.stringify(payload),
    signal: opts?.signal,
  });
  return consumeSystemAgentStream(response, onEvent);
}

async function consumeSystemAgentStream(
  response: Response,
  onEvent: (event: SystemAgentStreamEvent) => void,
): Promise<void> {
  if (!response.ok) {
    let payloadError: unknown;
    try {
      payloadError = await response.json();
    } catch {
      payloadError = null;
    }
    throw new Error(streamErrorMessage(payloadError, `助手请求失败（HTTP ${response.status}）`));
  }
  if (!response.body) throw new Error("浏览器没有提供可读取的流式响应。");

  const reader = response.body.getReader();
  const decoder = new NdjsonDecoder<SystemAgentStreamEvent>();
  let doneReceived = false;
  let streamFinished = false;
  const consumeEvent = (event: SystemAgentStreamEvent) => {
    if (event.type === "done") doneReceived = true;
    onEvent(event);
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      decoder.push(value).forEach(consumeEvent);
      if (done) break;
    }
    decoder.finish().forEach(consumeEvent);
    if (!doneReceived) throw new Error("流式响应提前结束，没有返回最终状态。");
    streamFinished = true;
  } finally {
    if (!streamFinished) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
