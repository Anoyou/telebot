/**
 * 测活统一结果状态词表（PLAN-conversation-deeix WP8）：
 * 正常 / 降级 / 失败 / 超时 / 限流 / 协议不匹配 / 缺少能力 / 跳过 / 未知
 */

export type LivenessStatusKey =
  | "normal"
  | "degraded"
  | "failed"
  | "timeout"
  | "rate_limited"
  | "protocol_mismatch"
  | "capability_missing"
  | "client_restricted"
  | "quota_exhausted"
  | "gateway_unavailable"
  | "permission_denied"
  | "skipped"
  | "unknown"
  | "pending";

export const LIVENESS_STATUS_LABEL: Record<LivenessStatusKey, string> = {
  normal: "正常",
  degraded: "降级",
  failed: "失败",
  timeout: "超时",
  rate_limited: "限流",
  protocol_mismatch: "协议不匹配",
  capability_missing: "缺少能力",
  client_restricted: "客户端受限",
  quota_exhausted: "额度耗尽",
  gateway_unavailable: "Gateway 不可用",
  permission_denied: "权限不足",
  skipped: "跳过",
  unknown: "未知",
  pending: "请求中",
};

export type StatusTone = "success" | "warn" | "danger" | "neutral" | "info" | undefined;

export function livenessStatusTone(key: LivenessStatusKey): StatusTone {
  switch (key) {
    case "normal":
      return "success";
    case "degraded":
    case "rate_limited":
    case "timeout":
    case "quota_exhausted":
    case "gateway_unavailable":
      return "warn";
    case "failed":
    case "protocol_mismatch":
    case "capability_missing":
    case "client_restricted":
    case "permission_denied":
      return "danger";
    case "skipped":
    case "unknown":
      return "neutral";
    case "pending":
      return "info";
    default:
      return undefined;
  }
}

export function livenessStatusLabel(key: LivenessStatusKey): string {
  return LIVENESS_STATUS_LABEL[key] || LIVENESS_STATUS_LABEL.unknown;
}

export function extractHttpStatusCode(
  statusCode?: number | null,
  error?: string | null,
): number | null {
  if (typeof statusCode === "number" && statusCode >= 100 && statusCode <= 599) {
    return statusCode;
  }
  const match = String(error || "").match(
    /(?:HTTP(?:\/\d(?:\.\d)?)?|接口返回|status(?:_code)?\s*[=:]?)\s*([1-5]\d{2})\b/i,
  );
  return match ? Number(match[1]) : null;
}

/** 从错误文案/分类推断状态 */
export function classifyErrorText(error?: string | null, errorCategory?: string | null): LivenessStatusKey {
  const cat = String(errorCategory || "").toLowerCase();
  const text = String(error || "").toLowerCase();
  if (cat === "client_rejected" || cat === "official_account_required") {
    return "client_restricted";
  }
  if (cat === "quota_exhausted") return "quota_exhausted";
  if (cat === "gateway_unavailable" || cat === "gateway_overloaded") return "gateway_unavailable";
  if (cat === "permission_denied" || cat === "account_policy") return "permission_denied";
  if (cat === "timeout" || text.includes("timeout") || text.includes("超时") || text.includes("timed out")) {
    return "timeout";
  }
  if (cat === "rate_limited" || text.includes("429") || text.includes("rate limit") || text.includes("限流")) {
    return "rate_limited";
  }
  if (
    cat === "protocol_rejected" ||
    cat === "endpoint_missing" ||
    text.includes("protocol") ||
    text.includes("api_format") ||
    text.includes("协议")
  ) {
    return "protocol_mismatch";
  }
  if (
    cat === "capability_mismatch" ||
    cat === "model_missing" ||
    cat === "context_limit" ||
    text.includes("not support") ||
    text.includes("unsupported") ||
    text.includes("capability") ||
    text.includes("tools are not")
  ) {
    return "capability_missing";
  }
  if (cat === "cancelled" || text.includes("取消") || text.includes("cancelled")) {
    return "skipped";
  }
  if (error || cat) return "failed";
  return "unknown";
}

/** 对话测活（chat-test）单条结果 */
export function classifyChatResult(result: {
  pending?: boolean;
  streaming?: boolean;
  ok?: boolean;
  empty_response?: boolean;
  error?: string | null;
  stream_fallback?: boolean;
  requested_model?: string;
  model?: string | null;
}): LivenessStatusKey {
  if (result.pending) return "pending";
  if (result.ok) {
    if (result.stream_fallback) return "degraded";
    if (
      result.model &&
      result.requested_model &&
      result.model !== result.requested_model
    ) {
      return "degraded";
    }
    return "normal";
  }
  if (result.empty_response && !result.error) return "failed";
  return classifyErrorText(result.error);
}

/** 全量测活后端 status 字段映射 */
export function classifyFullLivenessStatus(
  status: string,
  opts?: { skipped?: boolean },
): LivenessStatusKey {
  if (opts?.skipped) return "skipped";
  switch (status) {
    case "healthy":
      return "normal";
    case "empty_response":
      return "failed";
    case "rate_limited":
      return "rate_limited";
    case "timeout":
      return "timeout";
    case "protocol_rejected":
    case "endpoint_missing":
      return "protocol_mismatch";
    case "model_missing":
    case "context_limit":
      return "capability_missing";
    case "client_rejected":
    case "official_account_required":
      return "client_restricted";
    case "quota_exhausted":
      return "quota_exhausted";
    case "gateway_unavailable":
    case "gateway_overloaded":
      return "gateway_unavailable";
    case "permission_denied":
    case "account_policy":
      return "permission_denied";
    case "cancelled":
    case "skipped_disabled":
    case "skipped_provider_missing":
    case "no_enabled_models":
      return "skipped";
    case "auth_failed":
    case "request_invalid":
    case "invalid_response":
    case "upstream_error":
    case "config_error":
    case "network_error":
      return "failed";
    default:
      return status ? "failed" : "unknown";
  }
}

/** 将 chat/full 结果转成 ModelRunMeta 可用的 usage 形 */
export function livenessResultToUsage(result: {
  requested_model?: string;
  model?: string | null;
  provider_name?: string;
  input_tokens?: number;
  output_tokens?: number;
  latency_ms?: number;
  effective_api_format?: string | null;
  stream_fallback?: boolean;
}): Record<string, unknown> {
  return {
    schema_version: 2,
    provider_name: result.provider_name || undefined,
    model: result.model || result.requested_model || undefined,
    requested_model: result.requested_model || undefined,
    input_tokens: result.input_tokens ?? 0,
    output_tokens: result.output_tokens ?? 0,
    elapsed_ms: result.latency_ms ?? undefined,
    api_format: result.effective_api_format || undefined,
    stream_fallback: Boolean(result.stream_fallback),
    used_fallback: Boolean(
      result.model &&
        result.requested_model &&
        result.model !== result.requested_model,
    ),
    tool_calls: 0,
  };
}
