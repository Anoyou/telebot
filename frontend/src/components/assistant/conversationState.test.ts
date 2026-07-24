import assert from "node:assert/strict";
import test from "node:test";

import {
  extractStyleColor,
  resolveAssistantTextColor,
  stabilizeStreamingMarkdown,
  visibleConversationMessages,
} from "./conversationState.ts";

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

test("助手 HTML 颜色：命名色与中文别名映射 class", () => {
  assert.equal(resolveAssistantTextColor("orange").className, "assistant-html-text-orange");
  assert.equal(resolveAssistantTextColor("gold").className, "assistant-html-text-gold");
  assert.equal(resolveAssistantTextColor("pink").className, "assistant-html-text-pink");
  assert.equal(resolveAssistantTextColor("purple").className, "assistant-html-text-purple");
  assert.equal(resolveAssistantTextColor("橙色").className, "assistant-html-text-orange");
  assert.equal(resolveAssistantTextColor("金色").className, "assistant-html-text-gold");
});

test("助手 HTML 颜色：十六进制与 rgb 走安全 style", () => {
  assert.deepEqual(resolveAssistantTextColor("#e74c3c"), { style: "color: #e74c3c" });
  assert.deepEqual(resolveAssistantTextColor("#2ECC71"), { style: "color: #2ecc71" });
  assert.deepEqual(resolveAssistantTextColor("#3498db"), { style: "color: #3498db" });
  assert.deepEqual(resolveAssistantTextColor("#9b59b6"), { style: "color: #9b59b6" });
  assert.equal(resolveAssistantTextColor("rgb(155, 89, 182)").style, "color: rgb(155, 89, 182)");
  assert.deepEqual(resolveAssistantTextColor("expression(alert(1))"), {});
  assert.deepEqual(resolveAssistantTextColor("url(javascript:alert(1))"), {});
});

test("从 style 提取 color 值", () => {
  assert.equal(extractStyleColor("color: #9b59b6"), "#9b59b6");
  assert.equal(extractStyleColor("font-size:12px; color: purple;"), "purple");
  assert.equal(extractStyleColor("font-weight: bold"), null);
});
