import assert from "node:assert/strict";
import test from "node:test";

import {
  buildAgentTraceOverview,
  defaultPerspectiveEvent,
  filterPerspectiveEvents,
  filterTraceEvents,
  summarizeTraceEvents,
  traceEventCategory,
  traceEventHint,
} from "./runTraceState.ts";

test("统计唯一工具与重试", () => {
  const events = [
    { type: "provider_selected", reason: "configured", seq: 1 },
    { type: "tool_started", call_id: "c1", tool_name: "logs.recent", seq: 2 },
    { type: "tool_finished", call_id: "c1", seq: 3 },
    { type: "tool_started", call_id: "c2", tool_name: "scheduler.list", seq: 4 },
    { type: "retry_scheduled", retry_number: 1, seq: 5 },
    { type: "provider_selected", reason: "provider_fallback", seq: 6 },
    { type: "done", ok: true, seq: 7 },
    { type: "heartbeat", seq: 8 },
  ];
  assert.equal(filterTraceEvents(events).length, 7);
  const summary = summarizeTraceEvents(events);
  assert.equal(summary.toolCount, 2);
  assert.equal(summary.retryCount, 1);
  assert.equal(summary.fallbackCount, 1);
  assert.equal(summary.failed, false);
  assert.match(summary.headline, /2 个工具/);
});

test("失败时保持失败摘要", () => {
  const summary = summarizeTraceEvents([
    { type: "error", code: "PROVIDER_UNAVAILABLE", message: "原模型不可用" },
    { type: "done", ok: false },
  ]);
  assert.equal(summary.failed, true);
  assert.match(summary.headline, /执行失败/);
});

test("Agent 视角汇总路由、模型、工具、Token 与耗时", () => {
  const overview = buildAgentTraceOverview([
    { type: "provider_selected", provider_name: "DeepSeek", model: "deepseek-chat", reason: "configured" },
    { type: "route_selected", domains: ["logs", "system"], tool_count: 3 },
    { type: "skill_selected", skill_names: ["日志诊断"] },
    { type: "tool_started", call_id: "call-1", tool_name: "logs.recent" },
    { type: "tool_finished", call_id: "call-1", tool_name: "logs.recent", is_error: false },
    { type: "retry_scheduled", retry_number: 1 },
    { type: "assistant_message", usage: {
      provider_name: "DeepSeek",
      model: "deepseek-chat",
      input_tokens: 1320,
      output_tokens: 284,
      total_tokens: 1604,
      available_tools: 3,
      used_fallback: true,
      stage_timings: { verify_ms: 18, route_ms: 31, first_token_ms: 420, total_ms: 1870 },
    } },
    { type: "done", ok: true },
  ]);

  assert.equal(overview.status, "succeeded");
  assert.equal(overview.providerName, "DeepSeek");
  assert.equal(overview.model, "deepseek-chat");
  assert.deepEqual(overview.domains, ["logs", "system"]);
  assert.deepEqual(overview.skills, ["日志诊断"]);
  assert.equal(overview.toolCount, 1);
  assert.equal(overview.availableTools, 3);
  assert.equal(overview.retryCount, 1);
  assert.equal(overview.fallbackCount, 1);
  assert.equal(overview.totalTokens, 1604);
  assert.equal(overview.firstTokenMs, 420);
  assert.equal(overview.totalMs, 1870);
});

test("Agent 视角隐藏流式噪声并默认定位最后一个问题", () => {
  const events = [
    { type: "run_started", seq: 1 },
    { type: "heartbeat", seq: 2 },
    { type: "assistant_delta", seq: 3, delta: "处理中" },
    { type: "tool_started", seq: 4, call_id: "call-1" },
    { type: "tool_finished", seq: 5, call_id: "call-1", is_error: true },
    { type: "done", seq: 6, ok: false },
  ];

  assert.deepEqual(filterPerspectiveEvents(events, "all").map((event) => event.seq), [1, 4, 5, 6]);
  assert.deepEqual(filterPerspectiveEvents(events, "tool").map((event) => event.seq), [4, 5]);
  assert.deepEqual(filterPerspectiveEvents(events, "issue").map((event) => event.seq), [5, 6]);
  assert.equal(defaultPerspectiveEvent(events)?.seq, 5);
  assert.equal(traceEventCategory(events[4]!), "issue");
});

test("模型筛选保留同时属于异常的重试事件", () => {
  const events = [
    { type: "provider_selected", seq: 1 },
    { type: "retry_scheduled", seq: 2 },
    { type: "model_exhausted", seq: 3 },
    { type: "tool_started", seq: 4 },
  ];
  assert.deepEqual(filterPerspectiveEvents(events, "model").map((event) => event.seq), [1, 2, 3]);
  assert.deepEqual(filterPerspectiveEvents(events, "issue").map((event) => event.seq), [2, 3]);
});

test("同一次模型重试不会被计划与尝试事件重复计数", () => {
  const summary = summarizeTraceEvents([
    { type: "retry_scheduled", provider_name: "DeepSeek", model: "deepseek-chat", retry_number: 1 },
    { type: "model_attempt", provider_name: "DeepSeek", model: "deepseek-chat", attempt: 2 },
    { type: "done", ok: true },
  ]);
  assert.equal(summary.retryCount, 1);
});

test("不同工具步骤各自安排的重试会分别计数", () => {
  const summary = summarizeTraceEvents([
    { type: "tool_started", call_id: "call-1" },
    { type: "retry_scheduled", provider_name: "DeepSeek", model: "deepseek-chat", retry_number: 1 },
    { type: "model_attempt", provider_name: "DeepSeek", model: "deepseek-chat", attempt: 2 },
    { type: "tool_finished", call_id: "call-1" },
    { type: "tool_started", call_id: "call-2" },
    { type: "retry_scheduled", provider_name: "DeepSeek", model: "deepseek-chat", retry_number: 1 },
    { type: "model_attempt", provider_name: "DeepSeek", model: "deepseek-chat", attempt: 2 },
    { type: "done", ok: true },
  ]);
  assert.equal(summary.retryCount, 2);
});

test("旧事件缺少重试计划时按模型重试尝试回退计数", () => {
  const summary = summarizeTraceEvents([
    { type: "model_attempt", attempt: 1 },
    { type: "model_attempt", attempt: 2 },
    { type: "model_attempt", attempt: 1 },
    { type: "model_attempt", attempt: 2 },
    { type: "done", ok: true },
  ]);
  assert.equal(summary.retryCount, 2);
});

test("缺少结构化错误时不擅自提示可重试", () => {
  const summary = summarizeTraceEvents([
    { type: "done", ok: false },
  ]);
  assert.equal(summary.headline, "执行失败 · 请查看错误详情");
});

test("错误 hint 同时兼容文本与导航对象", () => {
  assert.equal(traceEventHint("检查 Provider 配置"), "检查 Provider 配置");
  assert.equal(
    traceEventHint({ message: "前往 AI 中心配置", web_path: "/ai?tab=providers" }),
    "前往 AI 中心配置 · /ai?tab=providers",
  );
  assert.equal(traceEventHint(null), "");
});
