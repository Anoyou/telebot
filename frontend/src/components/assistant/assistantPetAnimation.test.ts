import assert from "node:assert/strict";
import test from "node:test";

import { assistantPetCell, assistantPetLookDirection } from "./assistantPetAnimation.ts";

const bounds = { left: 100, top: 100, width: 68, height: 76 };
const centerX = bounds.left + bounds.width / 2;
const centerY = bounds.top + bounds.height / 2;

test("鼠标 8 点到 11 点映射到精灵图第十行的左向弧", () => {
  const radius = 100;
  const cases = [
    { clock: 8, degrees: 240, expected: 11 },
    { clock: 9, degrees: 270, expected: 12 },
    { clock: 10, degrees: 300, expected: 13 },
    { clock: 11, degrees: 330, expected: 15 },
  ];

  for (const item of cases) {
    const radians = item.degrees * Math.PI / 180;
    const direction = assistantPetLookDirection(
      centerX + Math.sin(radians) * radius,
      centerY - Math.cos(radians) * radius,
      bounds,
    );
    assert.equal(direction, item.expected, `${item.clock} 点方向索引错误`);
    assert.deepEqual(assistantPetCell("idle", 0, direction), {
      row: 10,
      column: item.expected % 8,
    });
  }
});

test("鼠标角度以桌宠实际位置为中心并保留中心死区", () => {
  assert.equal(assistantPetLookDirection(centerX, centerY, bounds), null);
  assert.equal(assistantPetLookDirection(centerX + 100, centerY, bounds), 4);
  assert.equal(assistantPetLookDirection(centerX - 100, centerY, bounds), 12);
});

test("工作和完成状态优先于鼠标注视", () => {
  assert.deepEqual(assistantPetCell("working", 0, 12), { row: 7, column: 0 });
  assert.deepEqual(assistantPetCell("complete", 0, 12), { row: 8, column: 0 });
});
