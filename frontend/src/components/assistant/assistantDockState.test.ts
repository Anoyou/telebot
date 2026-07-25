import assert from "node:assert/strict";
import test from "node:test";

import { shouldOpenAssistantDock } from "./assistantDockState.ts";

test("助手直达和会话深链在首次加载时展开 Dock", () => {
  assert.equal(shouldOpenAssistantDock("/assistant", ""), true);
  assert.equal(shouldOpenAssistantDock("/ai", "?session=session-1"), true);
  assert.equal(shouldOpenAssistantDock("/ai", ""), false);
});
