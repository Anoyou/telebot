import assert from "node:assert/strict";
import test from "node:test";

import {
  applyExecutionBackend,
  executionBackendLabel,
} from "./providerExecutionBackend.ts";

const direct = {
  execution_backend: "direct" as const,
  api_format: "anthropic_messages",
  protocol_profile: "claude_code_proxy",
  web_search_api_format: "anthropic_messages",
  client_identity_profile: "claude_code",
  api_key: "secret",
  request_headers: [{ name: "X-Tenant", value: "one" }],
};

test("切换到 Gateway 时锁定协议但保留身份和凭据配置", () => {
  const gateway = applyExecutionBackend(direct, "codex_gateway");

  assert.equal(gateway.execution_backend, "codex_gateway");
  assert.equal(gateway.api_format, "responses");
  assert.equal(gateway.protocol_profile, "codex_responses");
  assert.equal(gateway.web_search_api_format, "responses");
  assert.equal(gateway.client_identity_profile, "claude_code");
  assert.equal(gateway.api_key, "secret");
  assert.deepEqual(gateway.request_headers, direct.request_headers);
});

test("切回标准 API 直连时恢复原协议、联网覆盖和身份配置", () => {
  const gateway = applyExecutionBackend(direct, "codex_gateway");
  const restored = applyExecutionBackend(gateway, "direct");

  assert.equal(restored.execution_backend, "direct");
  assert.equal(restored.api_format, "anthropic_messages");
  assert.equal(restored.protocol_profile, "claude_code_proxy");
  assert.equal(restored.web_search_api_format, "anthropic_messages");
  assert.equal(restored.client_identity_profile, "claude_code");
  assert.equal(restored.api_key, "secret");
});

test("已保存 Gateway 切回 direct 时按休眠身份选择兼容协议", () => {
  const restored = applyExecutionBackend(
    {
      ...direct,
      execution_backend: "codex_gateway" as const,
      api_format: "responses",
      protocol_profile: "codex_responses",
      web_search_api_format: "responses",
    },
    "direct",
  );

  assert.equal(restored.api_format, "anthropic_messages");
  assert.equal(restored.protocol_profile, "standard");
  assert.equal(restored.web_search_api_format, "auto");
  assert.equal(restored.client_identity_profile, "claude_code");
});

test("调用方式标签使用面向用户的稳定文案", () => {
  assert.equal(executionBackendLabel("direct"), "标准 API 直连");
  assert.equal(executionBackendLabel("codex_gateway"), "Codex 客户端兼容模式（Gateway）");
  assert.equal(executionBackendLabel(null, "未记录"), "未记录");
});
