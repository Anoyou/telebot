import assert from "node:assert/strict";
import test from "node:test";

import {
  ASSISTANT_PET_COMPACT_CANVAS_HEIGHT,
  assistantPetCell,
  assistantPetDrawPlan,
  assistantPetFrameAt,
  assistantPetIntentForState,
  assistantPetLookDirection,
  assistantPetLookDirectionWithHysteresis,
  assistantPetLookPhase,
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

test("注视方向使用单帧并在相邻方向间保留迟滞", () => {
  const radius = 100;
  const radians = 168.75 * Math.PI / 180;
  const phase = assistantPetLookPhase(
    centerX + Math.sin(radians) * radius,
    centerY - Math.cos(radians) * radius,
    bounds,
  );
  assert.ok(phase != null && Math.abs(phase - 7.5) < 0.001);

  assert.equal(assistantPetLookDirectionWithHysteresis(4.55, 4), 4);
  assert.equal(assistantPetLookDirectionWithHysteresis(4.7, 4), 5);
  assert.equal(assistantPetLookDirectionWithHysteresis(3.45, 4), 4);
  assert.equal(assistantPetLookDirectionWithHysteresis(3.3, 4), 3);

  assert.equal(assistantPetLookDirectionWithHysteresis(15.55, 15), 15);
  assert.equal(assistantPetLookDirectionWithHysteresis(15.7, 15), 0);
  assert.equal(assistantPetLookDirectionWithHysteresis(0.45, 0), 0);
  assert.equal(assistantPetLookDirectionWithHysteresis(15.3, 0), 15);

  assert.deepEqual(assistantPetFrameAt("idle", 0, 7.5), {
    cell: { row: 10, column: 0 },
  });
  assert.deepEqual(assistantPetFrameAt("idle", 0, 15.5), {
    cell: { row: 9, column: 0 },
  });
});

test("工作和完成状态优先于鼠标注视", () => {
  assert.deepEqual(assistantPetCell("review", 0, 12), { row: 8, column: 0 });
  assert.deepEqual(assistantPetCell("jumping", 0, 12), { row: 4, column: 0 });
  assert.deepEqual(assistantPetCell("failed", 0, 12), { row: 5, column: 7 });
});

test("待机只在同一站姿中短暂眨眼", () => {
  assert.deepEqual(assistantPetFrameAt("idle", 0, null).cell, { row: 0, column: 4 });
  assert.deepEqual(assistantPetFrameAt("idle", 1_799, null).cell, { row: 0, column: 4 });
  assert.deepEqual(assistantPetFrameAt("idle", 1_800, null).cell, { row: 0, column: 2 });
  assert.deepEqual(assistantPetFrameAt("idle", 1_890, null).cell, { row: 0, column: 4 });
  assert.deepEqual(assistantPetFrameAt("idle", 1_980, null).cell, { row: 0, column: 4 });
});

test("失败动作剔除第六和第七格并按指定顺序播放", () => {
  const durations = [0, 140, 280, 420, 560, 700];
  assert.deepEqual(
    durations.map((elapsed) => assistantPetFrameAt("failed", elapsed, null).cell.column),
    [7, 0, 1, 4, 2, 3],
  );
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
  assert.deepEqual(assistantPetCell("running-right", 84, null), { row: 1, column: 1 });
  assert.deepEqual(assistantPetCell("running-left", 588, null), { row: 2, column: 7 });
  assert.deepEqual(assistantPetCell("running-right", 672, null), { row: 1, column: 0 });

  const leftPlan = assistantPetDrawPlan({ row: 2, column: 5 });
  assert.equal(leftPlan.layers[0]?.row, 2);
  assert.equal(leftPlan.layers[0]?.column, 5);
  assert.equal(leftPlan.layers[0]?.flipX, false);
  const rightPlan = assistantPetDrawPlan({ row: 1, column: 5 });
  assert.equal(rightPlan.layers[0]?.row, 2);
  assert.equal(rightPlan.layers[0]?.flipX, true);
});

test("跑动以稳定 12fps 逐格播放，不叠化两个姿势", () => {
  const cells = Array.from({ length: 8 }, (_, index) => (
    assistantPetFrameAt("running-right", index * 84, null).cell
  ));
  assert.deepEqual(cells, Array.from({ length: 8 }, (_, column) => ({ row: 1, column })));
  assert.deepEqual(assistantPetFrameAt("running-right", 672, null).cell, { row: 1, column: 0 });
});

test("跳跃使用同一动作行的五个阶段播放三次后站稳", () => {
  const plans = [0, 1, 2, 3, 4].map((column) => assistantPetDrawPlan({ row: 4, column }));
  assert.deepEqual(plans.map((plan) => plan.layers[0]?.destinationY), [-56, -21, -6, -25, -58]);
  assert.deepEqual(plans.map((plan) => plan.layers[0]?.row), [4, 4, 4, 4, 4]);
  assert.deepEqual(plans.map((plan) => plan.layers[0]?.column), [0, 1, 2, 3, 4]);
  assert.equal(plans.every((plan) => plan.layers.length === 1), true);
  assert.ok((plans[0]?.layers[0]?.destinationWidth ?? 0) > 192);

  const liftoff = assistantPetFrameAt("jumping", 100, null);
  assert.deepEqual(liftoff.cell, { row: 4, column: 1 });
  assert.deepEqual(assistantPetFrameAt("jumping", 190, null).cell, { row: 4, column: 2 });
  assert.deepEqual(assistantPetFrameAt("jumping", 579, null).cell, { row: 4, column: 4 });
  assert.deepEqual(assistantPetFrameAt("jumping", 580, null).cell, { row: 4, column: 0 });
  assert.deepEqual(assistantPetFrameAt("jumping", 680, null).cell, { row: 4, column: 1 });
  assert.deepEqual(assistantPetFrameAt("jumping", 1_160, null).cell, { row: 4, column: 0 });
  assert.deepEqual(assistantPetFrameAt("jumping", 1_739, null).cell, { row: 4, column: 4 });
  assert.deepEqual(assistantPetFrameAt("jumping", 1_740, null).cell, { row: 0, column: 4 });
  assert.deepEqual(assistantPetFrameAt("jumping", 0, null, true).cell, { row: 0, column: 4 });
});

test("注视帧统一人物高度、基线和下半身锚点", () => {
  const sourceMetrics = [
    [196, 95.099], [196, 94.052], [196, 94.23], [195, 94.749],
    [194, 94.02], [190, 94.001], [186, 93.618], [183, 94.552],
    [190, 95.057], [191, 94.538], [191, 94.791], [189, 94.842],
    [186, 94.502], [185, 95.287], [185, 95.002], [186, 94.727],
  ] as const;

  sourceMetrics.forEach(([height, lowerCenterX], direction) => {
    const layer = assistantPetDrawPlan({
      row: direction < 8 ? 9 : 10,
      column: direction % 8,
    }).layers[0];
    const scale = (layer?.destinationHeight ?? 0) / 208;
    assert.ok(Math.abs(height * scale - 190) < 0.001);
    assert.ok(Math.abs((layer?.destinationY ?? 0) + 202 * scale - 202) < 0.001);
    assert.ok(Math.abs((layer?.destinationX ?? 0) + lowerCenterX * scale - 95) < 0.001);
  });
});

test("摆手固定头身基准，只清除旧手臂并按肩部锚点绘制新手臂", () => {
  const plan = assistantPetDrawPlan({ row: 3, column: 2 });
  assert.equal(plan.layers.length, 3);
  assert.deepEqual(plan.layers[0], {
    row: 3,
    column: 0,
    sourceX: 0,
    sourceY: 0,
    sourceWidth: 192,
    sourceHeight: 208,
    destinationX: 0,
    destinationY: 0,
    destinationWidth: 192,
    destinationHeight: 208,
  });
  assert.deepEqual(plan.layers[1], {
    row: 3,
    column: 1,
    sourceX: 57,
    sourceY: 108,
    sourceWidth: 30,
    sourceHeight: 42,
    destinationX: 55,
    destinationY: 108,
    destinationWidth: 30,
    destinationHeight: 42,
    clearBeforeDraw: true,
  });
  const movingArm = plan.layers[2];
  assert.deepEqual({
    row: movingArm?.row,
    column: movingArm?.column,
    sourceX: movingArm?.sourceX,
    sourceY: movingArm?.sourceY,
    sourceWidth: movingArm?.sourceWidth,
    sourceHeight: movingArm?.sourceHeight,
    destinationX: movingArm?.destinationX,
    destinationY: movingArm?.destinationY,
    destinationWidth: movingArm?.destinationWidth,
    destinationHeight: movingArm?.destinationHeight,
  }, {
    row: 3,
    column: 2,
    sourceX: 0,
    sourceY: 0,
    sourceWidth: 192,
    sourceHeight: 208,
    destinationX: 8,
    destinationY: -4,
    destinationWidth: 192,
    destinationHeight: 208,
  });
  assert.ok((movingArm?.clipPath?.length ?? 0) >= 10);
  assert.equal(Math.max(...(movingArm?.clipPath ?? []).map((point) => point.x)), 82);

  const registrations = [1, 2].map((column) => {
    const layer = assistantPetDrawPlan({ row: 3, column }).layers[2];
    return [layer?.destinationX, layer?.destinationY];
  });
  assert.deepEqual(registrations, [
    [-2, 0],
    [8, -4],
  ]);
  assert.equal(assistantPetDrawPlan({ row: 3, column: 3 }).layers.length, 1);
  assert.deepEqual(assistantPetFrameAt("waving", 420, null).cell, { row: 3, column: 0 });
});

test("PWA 紧凑视图只绘制上半身", () => {
  const plan = assistantPetDrawPlan({ row: 0, column: 0 }, true);
  assert.equal(plan.viewportHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
  assert.equal(plan.layers.length, 1);
  assert.equal(plan.layers[0]?.destinationHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
  assert.equal(plan.layers[0]?.sourceHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
});

test("PWA 摆手保留完整可见的手臂动作", () => {
  const plan = assistantPetDrawPlan({ row: 3, column: 2 }, true);
  assert.equal(plan.viewportHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
  assert.equal(plan.layers.length, 3);
  assert.equal(plan.layers[0]?.destinationX, 0);
  assert.equal(plan.layers[0]?.sourceHeight, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT);
  assert.equal(plan.layers[1]?.clearBeforeDraw, true);
  assert.deepEqual([
    plan.layers[2]?.sourceX,
    plan.layers[2]?.sourceY,
    plan.layers[2]?.sourceWidth,
    plan.layers[2]?.sourceHeight,
    plan.layers[2]?.destinationX,
    plan.layers[2]?.destinationY,
    plan.layers[2]?.destinationHeight,
  ], [0, 0, 192, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT, 8, -4, ASSISTANT_PET_COMPACT_CANVAS_HEIGHT]);
  assert.equal(Math.max(...(plan.layers[2]?.clipPath ?? []).map((point) => point.x)), 82);
});

test("PWA 跳跃和失败使用完整单元格，不沿用上半身裁切和桌面放大", () => {
  for (const cell of [{ row: 4, column: 2 }, { row: 5, column: 3 }]) {
    const plan = assistantPetDrawPlan(cell, false, true);
    assert.equal(plan.viewportHeight, 208);
    const layer = plan.layers[0];
    assert.equal(layer?.row, cell.row);
    assert.equal(layer?.column, cell.column);
    assert.ok((layer?.sourceWidth ?? 192) < 192);
    assert.ok((layer?.sourceHeight ?? 208) < 208);
    assert.ok((layer?.destinationWidth ?? 193) <= 176);
    assert.ok((layer?.destinationHeight ?? 195) <= 194);
    assert.ok((layer?.destinationX ?? -1) >= 0);
    assert.ok((layer?.destinationY ?? -1) >= 0);
    assert.ok((layer?.destinationX ?? 0) + (layer?.destinationWidth ?? 193) <= 192);
    assert.ok((layer?.destinationY ?? 0) + (layer?.destinationHeight ?? 209) <= 208);
  }
});
