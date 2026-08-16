/** Agent 最近一次本轮模型选择（localStorage，不写全局 Provider 配置）。 */

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
const LAST_SELECTION_KEY = "telepilot.system-agent.last-model-selection.v1";

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

function normalizeSelection(value: unknown): SessionModelSelection | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Partial<SessionModelSelection>;
  const executionBackend = ["provider", "direct", "codex_gateway"].includes(
    String(candidate.executionBackend || ""),
  )
    ? candidate.executionBackend as SessionExecutionBackend
    : "provider";
  const clientIdentityProfile = CLIENT_IDENTITIES.has(candidate.clientIdentityProfile)
    ? candidate.clientIdentityProfile
    : undefined;
  // Agent 快速选择器不再暴露 minimal / openai_sdk。它们仍是 Provider
  // 配置和后端兼容契约的一部分；旧选择恢复时统一回到标准 API 的自动身份。
  const visibleClientIdentityProfile = clientIdentityProfile === "minimal"
    || clientIdentityProfile === "openai_sdk"
    ? "auto"
    : clientIdentityProfile;
  const common = visibleClientIdentityProfile
    ? { executionBackend, clientIdentityProfile: visibleClientIdentityProfile }
    : { executionBackend };
  if (
    candidate.mode === "pinned"
    && Number(candidate.providerId) > 0
    && typeof candidate.model === "string"
    && candidate.model
  ) {
    return {
      mode: "pinned",
      providerId: Number(candidate.providerId),
      model: candidate.model,
      ...common,
    };
  }
  if (candidate.mode === "auto") return { mode: "auto", ...common };
  return null;
}

function readLastSelection(): SessionModelSelection | null {
  try {
    return normalizeSelection(
      JSON.parse(window.localStorage.getItem(LAST_SELECTION_KEY) || "null") as unknown,
    );
  } catch {
    return null;
  }
}

function writeLastSelection(selection: SessionModelSelection): void {
  try {
    window.localStorage.setItem(LAST_SELECTION_KEY, JSON.stringify(selection));
  } catch {
    // ignore
  }
}

export function loadSessionModelSelection(sessionId: string | null | undefined): SessionModelSelection {
  const recent = readLastSelection();
  if (recent) return recent;
  if (!sessionId) return DEFAULT_SESSION_MODEL_SELECTION;
  return normalizeSelection(readStore()[sessionId]) || DEFAULT_SESSION_MODEL_SELECTION;
}

export function saveSessionModelSelection(
  sessionId: string | null | undefined,
  selection: SessionModelSelection,
): void {
  writeLastSelection(selection);
  if (!sessionId) return;
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
