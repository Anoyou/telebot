import assert from "node:assert/strict";
import test from "node:test";

import {
  classifyChatResult,
  classifyErrorText,
  classifyFullLivenessStatus,
  extractHttpStatusCode,
  livenessResultToUsage,
  livenessStatusLabel,
} from "./livenessStatus.ts";

test("对话测活：正常 / 降级 / 失败分类", () => {
  assert.equal(classifyChatResult({ ok: true, requested_model: "a", model: "a" }), "normal");
  assert.equal(
    classifyChatResult({ ok: true, stream_fallback: true, requested_model: "a", model: "a" }),
    "degraded",
  );
  assert.equal(
    classifyChatResult({ ok: true, requested_model: "a", model: "b" }),
    "degraded",
  );
  assert.equal(classifyChatResult({ ok: false, error: "timeout waiting" }), "timeout");
  assert.equal(classifyChatResult({ ok: false, error: "HTTP 429 rate limit" }), "rate_limited");
  assert.equal(classifyChatResult({ pending: true }), "pending");
});

test("全量测活 status 映射到九态词表", () => {
  assert.equal(classifyFullLivenessStatus("healthy"), "normal");
  assert.equal(classifyFullLivenessStatus("timeout"), "timeout");
  assert.equal(classifyFullLivenessStatus("rate_limited"), "rate_limited");
  assert.equal(classifyFullLivenessStatus("protocol_rejected"), "protocol_mismatch");
  assert.equal(classifyFullLivenessStatus("model_missing"), "capability_missing");
  assert.equal(classifyFullLivenessStatus("cancelled", { skipped: true }), "skipped");
  assert.equal(livenessStatusLabel("normal"), "正常");
  assert.equal(livenessStatusLabel("degraded"), "降级");
});

test("extracts structured and legacy HTTP status codes", () => {
  assert.equal(extractHttpStatusCode(429, "ignored 503"), 429);
  assert.equal(extractHttpStatusCode(null, "Responses streaming 接口返回 503"), 503);
  assert.equal(extractHttpStatusCode(null, "模型 gpt-404 暂不可用"), null);
  assert.equal(extractHttpStatusCode(null, "网络连接失败"), null);
});

test("错误文案分类", () => {
  assert.equal(classifyErrorText("tools are not supported"), "capability_missing");
  assert.equal(classifyErrorText(null, "timeout"), "timeout");
});

test("usage 转换含请求/实际模型", () => {
  const usage = livenessResultToUsage({
    requested_model: "req",
    model: "act",
    latency_ms: 120,
    input_tokens: 1,
    output_tokens: 2,
    effective_api_format: "responses",
  });
  assert.equal(usage.requested_model, "req");
  assert.equal(usage.model, "act");
  assert.equal(usage.used_fallback, true);
  assert.equal(usage.elapsed_ms, 120);
});
