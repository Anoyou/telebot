import assert from "node:assert/strict";
import test from "node:test";

import {
  nextAssistantOutcomeSignal,
  shouldOpenAssistantDock,
} from "./assistantDockState.ts";

test("助手直达和会话深链在首次加载时展开 Dock", () => {
  assert.equal(shouldOpenAssistantDock("/assistant", ""), true);
  assert.equal(shouldOpenAssistantDock("/ai", "?session=session-1"), true);
  assert.equal(shouldOpenAssistantDock("/ai", ""), false);
});

test("助手终态事件按发生顺序递增并保留最新状态", () => {
  const completed = nextAssistantOutcomeSignal(null, "complete");
  const failed = nextAssistantOutcomeSignal(completed, "failed");

  assert.deepEqual(completed, { id: 1, status: "complete" });
  assert.deepEqual(failed, { id: 2, status: "failed" });
});
