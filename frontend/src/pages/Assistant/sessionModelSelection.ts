/** 会话级本轮模型选择（localStorage，不写全局配置）。 */

export type SessionModelSelection =
  | { mode: "auto" }
  | { mode: "pinned"; providerId: number; model: string };

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
  if (!sessionId) return { mode: "auto" };
  const value = readStore()[sessionId];
  if (!value || typeof value !== "object") return { mode: "auto" };
  if (value.mode === "pinned" && value.providerId > 0 && value.model) {
    return { mode: "pinned", providerId: value.providerId, model: value.model };
  }
  return { mode: "auto" };
}

export function saveSessionModelSelection(
  sessionId: string,
  selection: SessionModelSelection,
): void {
  const store = readStore();
  if (selection.mode === "auto") {
    delete store[sessionId];
  } else {
    store[sessionId] = selection;
  }
  writeStore(store);
}

export function toApiModelSelection(
  selection: SessionModelSelection,
): { mode: "auto" } | { mode: "pinned"; provider_id: number; model: string } {
  if (selection.mode === "pinned") {
    return {
      mode: "pinned",
      provider_id: selection.providerId,
      model: selection.model,
    };
  }
  return { mode: "auto" };
}
