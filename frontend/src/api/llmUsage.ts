import { api } from "@/lib/api";

export interface LLMUsageRecord {
  id: number;
  account_id: number | null;
  provider_id: number | null;
  provider_name?: string | null;
  model: string | null;
  client_identity_profile?: string | null;
  execution_backend?: string | null;
  gateway_version?: string | null;
  gateway_request_id?: string | null;
  gateway_stage?: string | null;
  source?: string | null;
  input_tokens: number;
  output_tokens: number;
  latency_ms?: number | null;
  success: boolean;
  error_type?: string | null;
  used_fallback?: boolean;
  request_preview?: string | null;
  response_preview?: string | null;
  created_at: string;
}

export interface LLMUsageSummary {
  request_count: number;
  success_count: number;
  failed_count: number;
  fallback_count: number;
  total_tokens: number;
  avg_latency_ms: number;
}

export interface LLMUsageRecentResponse {
  items: LLMUsageRecord[];
  summary: LLMUsageSummary;
}

export interface PluginLLMUsageSummaryItem {
  plugin_key: string;
  source: string;
  request_count: number;
  success_count: number;
  failed_count: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  avg_latency_ms: number;
  last_used_at?: string | null;
}

export interface PluginLLMUsageSummaryResponse {
  items: PluginLLMUsageSummaryItem[];
}

export interface LLMUsageResetResponse {
  deleted: number;
}

function buildSummaryFromItems(items: LLMUsageRecord[]): LLMUsageSummary {
  const requestCount = items.length;
  const successCount = items.filter((r) => r.success).length;
  const fallbackCount = items.filter((r) => !!r.used_fallback).length;
  const totalTokens = items.reduce((sum, r) => sum + (r.input_tokens || 0) + (r.output_tokens || 0), 0);
  const totalLatency = items.reduce((sum, r) => sum + (r.latency_ms || 0), 0);

  return {
    request_count: requestCount,
    success_count: successCount,
    failed_count: requestCount - successCount,
    fallback_count: fallbackCount,
    total_tokens: totalTokens,
    avg_latency_ms: requestCount > 0 ? Math.round(totalLatency / requestCount) : 0,
  };
}

/**
 * 向后兼容：旧后端可能仅返回数组或仅返回 items。
 * 新后端返回 { items, summary }。
 */
export async function listRecentLLMUsage(limit = 20): Promise<LLMUsageRecentResponse> {
  const { data } = await api.get<LLMUsageRecentResponse | LLMUsageRecord[] | { items: LLMUsageRecord[] }>(
    "/api/llm/usage/recent",
    { params: { limit } },
  );

  if (Array.isArray(data)) {
    return { items: data, summary: buildSummaryFromItems(data) };
  }

  const items = data.items || [];
  return {
    items,
    summary: "summary" in data && data.summary ? data.summary : buildSummaryFromItems(items),
  };
}

export async function listPluginLLMUsageSummary(params?: {
  plugin_key?: string;
  limit?: number;
}): Promise<PluginLLMUsageSummaryResponse> {
  const { data } = await api.get<PluginLLMUsageSummaryResponse>("/api/llm/usage/plugins/summary", {
    params,
  });
  return data;
}

export async function resetRecentLLMUsage(): Promise<LLMUsageResetResponse> {
  const { data } = await api.delete<LLMUsageResetResponse>("/api/llm/usage/recent");
  return data;
}
