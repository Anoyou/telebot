/** System Agent API client. */
import { api, apiFetch } from "@/lib/api";

export interface SystemAgentConfig {
  enabled: boolean;
  provider_id: number | null;
  model: string | null;
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

export interface SystemAgentCapabilities {
  enabled: boolean;
  provider_id: number | null;
  model: string | null;
  ai_enabled: boolean;
  timezone: string;
  tools: SystemAgentCapability[];
  stage: number;
  write_tools_available: boolean;
}

export interface SystemAgentSession {
  id: string;
  web_user_id: number | null;
  bot_tg_user_id: number | null;
  account_id: number | null;
  channel: string;
  title: string | null;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface SystemAgentMessage {
  id: number;
  session_id: string;
  role: string;
  content: Record<string, unknown>;
  usage?: Record<string, unknown> | null;
  created_at: string | null;
}

export type SystemAgentStreamEvent = {
  type: string;
  run_id?: string;
  session_id?: string;
  seq?: number;
  ts?: string;
  content?: string;
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
  [key: string]: unknown;
};

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

function streamErrorMessage(value: unknown, fallback: string): string {
  if (!value || typeof value !== "object") return fallback;
  const obj = value as Record<string, unknown>;
  const error = obj.error as Record<string, unknown> | undefined;
  if (error?.message && typeof error.message === "string") return error.message;
  if (typeof obj.detail === "string") return obj.detail;
  return fallback;
}

/** 消费 NDJSON 对话流；任意分块边界安全。 */
export async function streamSystemAgentMessage(
  sessionId: string,
  payload: { content: string; account_id?: number | null },
  onEvent: (event: SystemAgentStreamEvent) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  const response = await apiFetch(`/api/system-agent/sessions/${sessionId}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
    body: JSON.stringify(payload),
    signal: opts?.signal,
  });
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
  const decoder = new TextDecoder();
  let terminalReceived = false;
  let streamFinished = false;
  let buffer = "";
  const consumeLine = (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const event = JSON.parse(trimmed) as SystemAgentStreamEvent;
    if (event.type === "done" || event.type === "error") terminalReceived = true;
    onEvent(event);
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      lines.forEach(consumeLine);
      if (done) break;
    }
    if (buffer.trim()) consumeLine(buffer);
    if (!terminalReceived) throw new Error("流式响应提前结束，没有返回最终状态。");
    streamFinished = true;
  } finally {
    if (!streamFinished) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
