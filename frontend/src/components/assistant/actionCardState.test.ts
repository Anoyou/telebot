import assert from "node:assert/strict";
import test from "node:test";

import type { SystemAgentAction } from "../../api/systemAgent.ts";
import {
  actionSecretInputFields,
  shouldShowActionSecretInput,
  shouldShowRuntimeRetry,
} from "./actionCardState.ts";

function action(overrides: Partial<SystemAgentAction> = {}): SystemAgentAction {
  return {
    id: "action-1",
    channel: "web",
    tool_name: "providers.probe_and_add",
    summary: "测活成功，是否添加 Provider？",
    preview: { mode: "verified_create" },
    risk: "normal",
    status: "pending",
    ...overrides,
  };
}

test("测活成功且密钥已加密暂存时不显示输入框", () => {
  assert.equal(
    shouldShowActionSecretInput(action({ has_secret: true, secret_fields: ["api_key"] })),
    false,
  );
});

test("创建 Provider 缺少密钥时显示输入框", () => {
  assert.equal(
    shouldShowActionSecretInput(action({
      tool_name: "providers.save",
      preview: { mode: "create" },
      has_secret: false,
    })),
    true,
  );
});

test("鉴权失败清除密钥后重新显示输入框", () => {
  assert.equal(
    shouldShowActionSecretInput(action({
      has_secret: false,
      error_code: "API_KEY_REJECTED",
    })),
    true,
  );
});

test("请求头仍暂存时 API Key 失败仍只要求补 Key", () => {
  const current = action({
    has_secret: true,
    secret_fields: ["request_headers"],
    error_code: "API_KEY_REJECTED",
  });
  assert.equal(shouldShowActionSecretInput(current), true);
  assert.deepEqual(actionSecretInputFields(current), ["api_key"]);
});

test("只有可安全重复的运行时副作用显示重新同步", () => {
  assert.equal(
    shouldShowRuntimeRetry(action({
      status: "executed",
      runtime_sync_status: "failed",
      runtime_retryable: true,
    })),
    true,
  );
  assert.equal(
    shouldShowRuntimeRetry(action({
      status: "executed",
      runtime_sync_status: "failed",
      runtime_retryable: false,
    })),
    false,
  );
});
