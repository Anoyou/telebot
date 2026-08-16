import assert from "node:assert/strict";
import test from "node:test";

import {
  loadSessionModelSelection,
  saveSessionModelSelection,
} from "./sessionModelSelection.ts";

function installLocalStorage(): void {
  const values = new Map<string, string>();
  const localStorage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
    clear: () => { values.clear(); },
    key: (index: number) => [...values.keys()][index] ?? null,
    get length() { return values.size; },
  };
  (globalThis as unknown as { window: { localStorage: typeof localStorage } }).window = {
    localStorage,
  };
}

test("刷新或切换会话时沿用最近一次模型和调用方式", () => {
  installLocalStorage();
  saveSessionModelSelection("session-a", {
    mode: "pinned",
    providerId: 7,
    model: "grok-4.5",
    executionBackend: "direct",
    clientIdentityProfile: "codex_tui",
  });

  assert.deepEqual(loadSessionModelSelection("session-b"), {
    mode: "pinned",
    providerId: 7,
    model: "grok-4.5",
    executionBackend: "direct",
    clientIdentityProfile: "codex_tui",
  });

  saveSessionModelSelection("session-b", {
    mode: "auto",
    executionBackend: "provider",
  });
  assert.deepEqual(loadSessionModelSelection("session-a"), {
    mode: "auto",
    executionBackend: "provider",
  });
});

test("尚未创建会话时也会记住最近一次选择", () => {
  installLocalStorage();
  saveSessionModelSelection(null, {
    mode: "auto",
    executionBackend: "codex_gateway",
    clientIdentityProfile: "codex_desktop",
  });

  assert.deepEqual(loadSessionModelSelection(null), {
    mode: "auto",
    executionBackend: "codex_gateway",
    clientIdentityProfile: "codex_desktop",
  });
});
