import assert from "node:assert/strict";
import test from "node:test";

import {
  formatDirectPassthroughRankLabel,
  formatDirectPassthroughRankTitle,
} from "./pluginContract.ts";

test("直通优先级标签使用 TypeScript 对象参数", () => {
  assert.equal(
    formatDirectPassthroughRankLabel(2, {
      secondaryEnabled: true,
      totalEnabled: 3,
    }),
    "第2优先 · 共3个",
  );
  assert.equal(
    formatDirectPassthroughRankLabel(null, { secondaryEnabled: true }),
    "待排序",
  );
  assert.equal(
    formatDirectPassthroughRankLabel(1, { secondaryEnabled: false }),
    "直通未开",
  );
});

test("直通优先级说明与开关和名次一致", () => {
  assert.equal(
    formatDirectPassthroughRankTitle(1, { secondaryEnabled: true }),
    "本账号已开直通插件中最先调用",
  );
  assert.equal(
    formatDirectPassthroughRankTitle(3, { secondaryEnabled: true }),
    "本账号已开直通插件中第 3 个调用；更前的插件成功后不会轮到本插件",
  );
  assert.match(
    formatDirectPassthroughRankTitle(null, { secondaryEnabled: false }),
    /不参与直通调度/,
  );
});
