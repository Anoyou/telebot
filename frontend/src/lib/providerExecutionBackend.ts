export type LLMExecutionBackend = "direct" | "codex_gateway";

export interface ProviderBackendFields {
  execution_backend: LLMExecutionBackend;
  api_format: string;
  protocol_profile: string;
  web_search_api_format: string;
  client_identity_profile: string;
  direct_api_format?: string;
  direct_protocol_profile?: string;
  direct_web_search_api_format?: string;
}

function fallbackDirectFormat(clientIdentityProfile: string): string {
  return clientIdentityProfile === "claude_code" || clientIdentityProfile === "claude_desktop"
    ? "anthropic_messages"
    : "responses";
}

function fallbackDirectProtocolProfile(clientIdentityProfile: string): string {
  return clientIdentityProfile === "codex_tui" || clientIdentityProfile === "codex_desktop"
    ? "codex_responses"
    : "standard";
}

export function applyExecutionBackend<T extends ProviderBackendFields>(
  form: T,
  executionBackend: LLMExecutionBackend,
): T {
  if (executionBackend === "direct") {
    return {
      ...form,
      execution_backend: executionBackend,
      api_format: form.direct_api_format || fallbackDirectFormat(form.client_identity_profile),
      protocol_profile:
        form.direct_protocol_profile || fallbackDirectProtocolProfile(form.client_identity_profile),
      web_search_api_format: form.direct_web_search_api_format || "auto",
    };
  }
  return {
    ...form,
    execution_backend: executionBackend,
    ...(form.execution_backend === "direct"
      ? {
          direct_api_format: form.api_format,
          direct_protocol_profile: form.protocol_profile,
          direct_web_search_api_format: form.web_search_api_format,
        }
      : {}),
    api_format: "responses",
    protocol_profile: "codex_responses",
    web_search_api_format: "responses",
  };
}

export function executionBackendLabel(
  value?: string | null,
  missingLabel = "Provider 直连",
): string {
  if (value === "codex_gateway") return "Codex Gateway";
  if (value === "direct") return "Provider 直连";
  return missingLabel;
}

export function isGatewayBackend(value?: string | null): boolean {
  return value === "codex_gateway";
}
