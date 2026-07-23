import assert from "node:assert/strict";
import test from "node:test";

import { stabilizeStreamingMarkdown, visibleConversationMessages } from "./conversationState.ts";

test("历史工具结果不作为第二条聊天消息渲染", () => {
  const messages = [
    { id: 1, role: "user", content: { text: "查询" } },
    { id: 2, role: "assistant", content: { text: "结果" } },
    { id: 3, role: "tool", content: { tool_name: "rules.list" } },
  ] as never[];

  assert.deepEqual(
    visibleConversationMessages(messages).map((message) => message.id),
    [1, 2],
  );
});

test("流式 Markdown 未闭合围栏会补临时闭合", () => {
  const open = "前言\n```js\nconst x = 1\n";
  const closed = stabilizeStreamingMarkdown(open);
  assert.equal((closed.match(/^```/gm) || []).length % 2, 0);
  assert.ok(closed.endsWith("```"));
  assert.equal(stabilizeStreamingMarkdown("```js\ncode\n```"), "```js\ncode\n```");
});
