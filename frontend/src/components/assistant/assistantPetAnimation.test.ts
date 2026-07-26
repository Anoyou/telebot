import assert from "node:assert/strict";
import test from "node:test";

import {
  ASSISTANT_PET_COMPACT_CANVAS_HEIGHT,
  assistantPetCell,
  assistantPetDrawPlan,
  assistantPetFrameAt,
  assistantPetIntentForState,
  assistantPetLookRegistration,
  assistantPetLookDirection,
} from "./assistantPetAnimation.ts";

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
  assert.deepEqual(assistantPetCell("review", 0, 12), { row: 8, column: 0 });
  assert.deepEqual(assistantPetCell("jumping", 0, 12), { row: 4, column: 0 });
  assert.deepEqual(assistantPetCell("failed", 0, 12), { row: 5, column: 0 });
});

test("产品状态映射使用新工作和完成动作", () => {
  const base = {
    failed: false,
    celebrating: false,
    dragDirection: null,
    streaming: false,
    active: false,
  } as const;
  assert.equal(assistantPetIntentForState(base), "idle");
  assert.equal(assistantPetIntentForState({ ...base, active: true }), "waving");
  assert.equal(assistantPetIntentForState({ ...base, streaming: true }), "review");
  assert.equal(assistantPetIntentForState({ ...base, celebrating: true }), "jumping");
  assert.equal(assistantPetIntentForState({ ...base, dragDirection: "left" }), "running-left");
  assert.equal(assistantPetIntentForState({ ...base, dragDirection: "right" }), "running-right");
  assert.equal(assistantPetIntentForState({ ...base, failed: true, celebrating: true }), "failed");
});

test("拖拽方向使用左右奔跑行", () => {
  assert.deepEqual(assistantPetCell("running-right", 0, null), { row: 1, column: 0 });
  assert.deepEqual(assistantPetCell("running-left", 0, null), { row: 2, column: 0 });
  assert.deepEqual(assistantPetCell("running-right", 120, null), { row: 1, column: 1 });
  assert.deepEqual(assistantPetCell("running-left", 840, null), { row: 2, column: 7 });
  assert.deepEqual(assistantPetCell("running-right", 960, null), { row: 1, column: 0 });

  const leftPlan = assistantPetDrawPlan({ row: 2, column: 5 });
  assert.equal(leftPlan.layers[0]?.row, 1);
  assert.equal(leftPlan.layers[0]?.column, 5);
  assert.equal(leftPlan.layers[0]?.flipX, true);
});

test("跑动在相邻帧和循环边界都使用缓动过渡", () => {
  const middle = assistantPetFrameAt("running-right", 96, null);
  assert.deepEqual(middle.cell, { row: 1, column: 0 });
  assert.deepEqual(middle.nextCell, { row: 1, column: 1 });
  assert.ok(middle.blend > 0 && middle.blend < 1);

  const wrap = assistantPetFrameAt("running-right", 936, null);
  assert.deepEqual(wrap.cell, { row: 1, column: 7 });
  assert.deepEqual(wrap.nextCell, { row: 1, column: 0 });
  assert.ok(wrap.blend > 0 && wrap.blend < 1);
});

test("跳跃使用同一动作行的完整阶段并回到站姿", () => {
  const plans = [0, 1, 2, 3, 4, 5, 6].map((column) => assistantPetDrawPlan({ row: 4, column }));
  assert.deepEqual(plans.map((plan) => plan.layers[0]?.destinationY), [0, -30, -25, -6, -27, -30, 0]);
  assert.deepEqual(plans.map((plan) => plan.layers[0]?.row), [0, 4, 4, 4, 4, 4, 0]);
  assert.deepEqual(plans.map((plan) => plan.layers[0]?.column), [3, 0, 1, 2, 3, 4, 3]);
  assert.equal(plans.every((plan) => plan.layers.length === 1), true);
  assert.ok((plans[1]?.layers[0]?.destinationWidth ?? 0) > 192);

  const liftoff = assistantPetFrameAt("jumping", 172, null);
  assert.deepEqual(liftoff.cell, { row: 4, column: 1 });
  assert.deepEqual(liftoff.nextCell, { row: 4, column: 2 });
  assert.ok(liftoff.blend > 0 && liftoff.blend < 1);
});

test("注视帧按可见面积和重心统一注册", () => {
  const right = { alphaMass: 10_316, centerX: 89, baselineY: 202 };
  const left = { alphaMass: 9_728, centerX: 102, baselineY: 202 };
  const target = 10_000;
  const rightRegistration = assistantPetLookRegistration(right, target);
  const leftRegistration = assistantPetLookRegistration(left, target);
  assert.ok(Math.abs(right.alphaMass * rightRegistration.scale ** 2 - target) < 1);
  assert.ok(Math.abs(left.alphaMass * leftRegistration.scale ** 2 - target) < 1);
  assert.ok(Math.abs(rightRegistration.destinationX + right.centerX * rightRegistration.scale - 96) < 0.001);
  assert.ok(Math.abs(leftRegistration.destinationX + left.centerX * leftRegistration.scale - 96) < 0.001);

  const plan = assistantPetDrawPlan({ row: 9, column: 4 }, false, rightRegistration);
  assert.equal(plan.layers[0]?.destinationX, rightRegistration.destinationX);
  assert.equal(plan.layers[0]?.destinationY, rightRegistration.destinationY);
  assert.equal(plan.layers[0]?.destinationWidth, 192 * rightRegistration.scale);
});

test("摆手只替换上半身并固定下半身基准帧", () => {
  const plan = assistantPetDrawPlan({ row: 3, column: 2 });
  assert.equal(plan.layers.length, 4);
  assert.deepEqual(plan.layers[0], {
    row: 3,
    column: 2,
    sourceX: 0,
    sourceY: 0,
    sourceWidth: 192,
    sourceHeight: 208,
    destinationX: 10,
    destinationY: 0,
    destinationWidth: 192,
    destinationHeight: 208,
  });
  assert.deepEqual(plan.layers.slice(1).map((layer) => ({
    column: layer.column,
    sourceX: layer.sourceX,
    sourceY: layer.sourceY,
    sourceWidth: layer.sourceWidth,
    sourceHeight: layer.sourceHeight,
  })), [
    { column: 0, sourceX: 35, sourceY: 0, sourceWidth: 125, sourceHeight: 100 },
    { column: 0, sourceX: 64, sourceY: 82, sourceWidth: 66, sourceHeight: 68 },
    { column: 0, sourceX: 0, sourceY: 150, sourceWidth: 192, sourceHeight: 58 },
  ]);
  assert.equal(plan.layers.slice(1).every((layer) => layer.clearBeforeDraw), true);
});

test("PWA 紧凑视图只绘制上半身", () => {
  const plan = assistantPetDrawPlan({ row: 0, column: 0 }, true);
  assert.equal(plan.viewportHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
  assert.equal(plan.layers.length, 1);
  assert.equal(plan.layers[0]?.destinationHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
  assert.equal(plan.layers[0]?.sourceHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
});

test("PWA 摆手沿用固定头部和躯干的新版合成", () => {
  const plan = assistantPetDrawPlan({ row: 3, column: 2 }, true);
  assert.equal(plan.viewportHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
  assert.equal(plan.layers.length, 3);
  assert.equal(plan.layers[0]?.destinationX, 10);
  assert.equal(plan.layers[0]?.sourceHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
  assert.deepEqual(plan.layers.slice(1).map((layer) => [layer.sourceX, layer.sourceY, layer.sourceWidth, layer.sourceHeight]), [
    [35, 0, 125, 100],
    [64, 82, 66, 68],
  ]);
  assert.equal(plan.layers.slice(1).every((layer) => layer.clearBeforeDraw), true);
});
