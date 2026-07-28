import type { SystemAgentAction } from "@/api/systemAgent";

const API_KEY_ERROR_CODES = new Set([
  "API_KEY_REQUIRED",
  "API_KEY_DECRYPT_FAILED",
  "API_KEY_REJECTED",
]);

function providerNeedsApiKey(action: SystemAgentAction): boolean {
  const previewMode = String(action.preview?.mode || "");
  const currentProvider = action.preview?.current as { has_api_key?: boolean } | undefined;
  return Boolean(
    API_KEY_ERROR_CODES.has(String(action.error_code || ""))
      || (action.tool_name === "providers.verify" && previewMode === "draft")
      || (action.tool_name === "providers.save" && previewMode === "create")
      || (
        action.tool_name === "providers.save"
        && previewMode === "update"
        && currentProvider?.has_api_key === false
      ),
  );
}

export function actionSecretInputFields(action: SystemAgentAction): string[] {
  // Provider 请求头和 API Key 会分开清理。即使请求头仍在加密暂存，
  // API Key 预检失败后也只能要求补 Key，不能把已有请求头误当成“密钥齐全”。
  if (providerNeedsApiKey(action)) return ["api_key"];
  return action.secret_fields?.length ? action.secret_fields : ["api_key"];
}

export function shouldShowActionSecretInput(action: SystemAgentAction): boolean {
  if (providerNeedsApiKey(action)) return true;
  if (action.has_secret) return false;
  return Boolean(action.secret_fields?.length);
}

export function shouldShowRuntimeRetry(action: SystemAgentAction): boolean {
  return action.runtime_sync_status === "failed" && action.runtime_retryable !== false;
}
