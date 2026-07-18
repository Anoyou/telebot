import assert from "node:assert/strict";
import test from "node:test";

import { removeSessionAndChooseNext } from "./sessionState.ts";

test("删除当前会话后不会从旧列表重新选回已删除会话", () => {
  const sessions = [
    { id: "deleted", title: "刚删除" },
    { id: "next", title: "下一个会话" },
  ];

  assert.deepEqual(
    removeSessionAndChooseNext(sessions, "deleted", "deleted"),
    {
      sessions: [{ id: "next", title: "下一个会话" }],
      activeId: "next",
    },
  );
});

test("删除非当前会话不会改变当前会话", () => {
  const sessions = [{ id: "active" }, { id: "other" }];

  assert.deepEqual(removeSessionAndChooseNext(sessions, "active", "other"), {
    sessions: [{ id: "active" }],
    activeId: "active",
  });
});
