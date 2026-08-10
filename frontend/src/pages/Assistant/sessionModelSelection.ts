/** 会话级本轮模型选择（localStorage，不写全局配置）。 */

import type { SystemAgentModelSelection } from "@/api/systemAgent";
import type {
  AgentClientIdentity,
  AgentExecutionBackend,
} from "@/lib/assistantClientSelection";

export type SessionModelSelection =
  | {
      mode: "auto";
      executionBackend: SessionExecutionBackend;
      clientIdentityProfile?: SessionClientIdentity;
    }
  | {
      mode: "pinned";
      providerId: number;
      model: string;
      executionBackend: SessionExecutionBackend;
      clientIdentityProfile?: SessionClientIdentity;
    };

export type SessionExecutionBackend = AgentExecutionBackend;
export type SessionClientIdentity = AgentClientIdentity;

export const DEFAULT_SESSION_MODEL_SELECTION: SessionModelSelection = {
  mode: "auto",
  executionBackend: "provider",
};

const CLIENT_IDENTITIES = new Set<SessionClientIdentity>([
  "auto",
  "minimal",
  "openai_sdk",
  "codex_tui",
  "codex_desktop",
  "claude_code",
  "claude_desktop",
  "grok_cli",
]);

const STORAGE_KEY = "telepilot.system-agent.session-model.v1";

type Store = Record<string, SessionModelSelection>;

function readStore(): Store {
  try {
    const raw = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}") as unknown;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    return raw as Store;
  } catch {
    return {};
  }
}

function writeStore(store: Store): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    // ignore
  }
}

export function loadSessionModelSelection(sessionId: string | null | undefined): SessionModelSelection {
  if (!sessionId) return DEFAULT_SESSION_MODEL_SELECTION;
  const value = readStore()[sessionId];
  if (!value || typeof value !== "object") return DEFAULT_SESSION_MODEL_SELECTION;
  const executionBackend = ["provider", "direct", "codex_gateway"].includes(value.executionBackend)
    ? value.executionBackend
    : "provider";
  const clientIdentityProfile = CLIENT_IDENTITIES.has(value.clientIdentityProfile)
    ? value.clientIdentityProfile
    : undefined;
  const common = {
    executionBackend,
    clientIdentityProfile,
  };
  if (value.mode === "pinned" && value.providerId > 0 && value.model) {
    return { mode: "pinned", providerId: value.providerId, model: value.model, ...common };
  }
  return { mode: "auto", ...common };
}

export function saveSessionModelSelection(
  sessionId: string,
  selection: SessionModelSelection,
): void {
  const store = readStore();
  if (
    selection.mode === "auto"
    && selection.executionBackend === "provider"
    && !selection.clientIdentityProfile
  ) {
    delete store[sessionId];
  } else {
    store[sessionId] = selection;
  }
  writeStore(store);
}

export function toApiModelSelection(
  selection: SessionModelSelection,
): SystemAgentModelSelection {
  const common = {
    execution_backend: selection.executionBackend,
    client_identity_profile: selection.clientIdentityProfile,
  };
  if (selection.mode === "pinned") {
    return {
      mode: "pinned",
      provider_id: selection.providerId,
      model: selection.model,
      ...common,
    };
  }
  return { mode: "auto", ...common };
}
