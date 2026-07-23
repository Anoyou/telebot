import assert from "node:assert/strict";
import test from "node:test";

import { normalizeSchedulerTarget } from "./schedulerTarget.ts";

test("normalizes numeric IDs and @username", () => {
  assert.equal(normalizeSchedulerTarget("8395686237"), 8395686237);
  assert.equal(normalizeSchedulerTarget(" -1001234567890 "), -1001234567890);
  assert.equal(normalizeSchedulerTarget(" @qingbaobu "), "@qingbaobu");
});

test("rejects unsupported Telegram target formats", () => {
  assert.throws(() => normalizeSchedulerTarget("qingbaobu"), /格式无效/);
  assert.throws(() => normalizeSchedulerTarget("https://t.me/qingbaobu"), /格式无效/);
  assert.throws(() => normalizeSchedulerTarget("@abc"), /格式无效/);
});

test("allows an omitted optional target", () => {
  assert.equal(normalizeSchedulerTarget(0, false), undefined);
  assert.equal(normalizeSchedulerTarget("", false), undefined);
});
