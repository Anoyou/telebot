import assert from "node:assert/strict";
import test from "node:test";

import { filterTraceEvents, summarizeTraceEvents } from "./runTraceState.ts";

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
