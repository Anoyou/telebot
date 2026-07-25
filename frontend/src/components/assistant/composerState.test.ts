import assert from "node:assert/strict";
import test from "node:test";

import { composerEnterAction } from "./composerState.ts";

const base = {
  key: "Enter",
  shiftKey: false,
  nativeComposing: false,
  compositionActive: false,
  suppressAfterComposition: false,
};

test("普通 Enter 提交，Shift+Enter 保留换行", () => {
  assert.equal(composerEnterAction(base), "submit");
  assert.equal(composerEnterAction({ ...base, shiftKey: true }), "ignore");
});

test("IME 组合输入和 compositionend 后紧随的 Enter 都不会发送", () => {
  assert.equal(composerEnterAction({ ...base, nativeComposing: true }), "ignore");
  assert.equal(composerEnterAction({ ...base, compositionActive: true }), "ignore");
  assert.equal(composerEnterAction({ ...base, suppressAfterComposition: true }), "suppress");
});
