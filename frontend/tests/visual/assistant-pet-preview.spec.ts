import { expect, test, type Locator } from "@playwright/test";

test.use({ reducedMotion: "no-preference" });

type CanvasMetrics = {
  hash: number;
  alphaMass: number;
  centerX: number;
  lowerCenterX: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
};

async function canvasMetrics(canvas: Locator): Promise<CanvasMetrics> {
  return canvas.evaluate((element) => {
    const target = element as HTMLCanvasElement;
    const context = target.getContext("2d");
    if (!context) throw new Error("Agent 预览 Canvas 不可读");
    const pixels = context.getImageData(0, 0, target.width, target.height).data;
    let hash = 2166136261;
    let alphaMass = 0;
    let weightedX = 0;
    let lowerAlphaMass = 0;
    let lowerWeightedX = 0;
    let left = target.width;
    let right = 0;
    let top = target.height;
    let bottom = 0;
    for (let offset = 0; offset < pixels.length; offset += 4) {
      const alpha = pixels[offset + 3] / 255;
      hash ^= pixels[offset];
      hash = Math.imul(hash, 16777619);
      hash ^= pixels[offset + 1];
      hash = Math.imul(hash, 16777619);
      hash ^= pixels[offset + 2];
      hash = Math.imul(hash, 16777619);
      hash ^= pixels[offset + 3];
      hash = Math.imul(hash, 16777619);
      if (alpha <= 0) continue;
      const pixel = offset / 4;
      const x = pixel % target.width;
      const y = Math.floor(pixel / target.width);
      alphaMass += alpha;
      weightedX += x * alpha;
      if (y >= 120) {
        lowerAlphaMass += alpha;
        lowerWeightedX += x * alpha;
      }
      left = Math.min(left, x);
      right = Math.max(right, x + 1);
      top = Math.min(top, y);
      bottom = Math.max(bottom, y + 1);
    }
    return {
      hash: hash >>> 0,
      alphaMass,
      centerX: weightedX / alphaMass,
      lowerCenterX: lowerWeightedX / lowerAlphaMass,
      left,
      right,
      top,
      bottom,
    };
  });
}

async function expectCanvasReady(canvas: Locator) {
  await expect.poll(async () => (await canvasMetrics(canvas)).alphaMass).toBeGreaterThan(500);
}

test("实装 Agent 的注视、跑动、贴边和跳跃保持连续", async ({ page }) => {
  test.setTimeout(45_000);
  await page.goto("/assistant-pet-states-preview.html");

  await page.getByRole("tab", { name: "注视", exact: true }).click();
  const pet = page.locator("[data-preview-production-pet]");
  const canvas = pet.locator("canvas");
  await expect(canvas).toBeVisible();
  await expectCanvasReady(canvas);
  const petBox = await pet.boundingBox();
  expect(petBox).not.toBeNull();

  await page.getByRole("tab", { name: "待机", exact: true }).click();
  await expectCanvasReady(canvas);
  const idleMetrics: CanvasMetrics[] = [];
  for (let sample = 0; sample < 14; sample += 1) {
    await page.waitForTimeout(90);
    idleMetrics.push(await canvasMetrics(canvas));
  }
  const idleMasses = idleMetrics.map((sample) => sample.alphaMass);
  const idleWidths = idleMetrics.map((sample) => sample.right - sample.left);
  expect(Math.max(...idleMasses) / Math.min(...idleMasses)).toBeLessThan(1.02);
  expect(Math.max(...idleWidths) / Math.min(...idleWidths)).toBeLessThan(1.01);

  await page.getByRole("tab", { name: "注视", exact: true }).click();
  const lookMetrics: CanvasMetrics[] = [];
  const centerX = (petBox?.x ?? 0) + (petBox?.width ?? 0) / 2;
  const centerY = (petBox?.y ?? 0) + (petBox?.height ?? 0) / 2;
  for (let direction = 0; direction < 16; direction += 1) {
    const radians = direction * 22.5 * Math.PI / 180;
    await page.mouse.move(
      centerX + Math.sin(radians) * 42,
      centerY - Math.cos(radians) * 42,
    );
    await page.waitForTimeout(40);
    lookMetrics.push(await canvasMetrics(canvas));
  }
  const lookMasses = lookMetrics.map((sample) => sample.alphaMass);
  const lookAnchors = lookMetrics.map((sample) => sample.lowerCenterX);
  const lookHeights = lookMetrics.map((sample) => sample.bottom - sample.top);
  const lookWidths = lookMetrics.map((sample) => sample.right - sample.left);
  expect(Math.min(...lookMasses)).toBeGreaterThan(9_500);
  expect(Math.max(...lookAnchors) - Math.min(...lookAnchors)).toBeLessThan(2.5);
  expect(Math.max(...lookHeights) / Math.min(...lookHeights)).toBeLessThan(1.015);
  expect(Math.max(...lookWidths) / Math.min(...lookWidths)).toBeLessThan(1.2);

  const boundaryMetrics: CanvasMetrics[] = [];
  for (const degrees of [157.5, 163.125, 168.75, 174.375, 180, 337.5, 343.125, 348.75, 354.375, 360]) {
    const radians = degrees * Math.PI / 180;
    await page.mouse.move(
      centerX + Math.sin(radians) * 42,
      centerY - Math.cos(radians) * 42,
    );
    await page.waitForTimeout(40);
    boundaryMetrics.push(await canvasMetrics(canvas));
  }
  for (const [start, end] of [[0, 4], [5, 9]] as const) {
    const samples = boundaryMetrics.slice(start, end + 1);
    for (let index = 1; index < samples.length; index += 1) {
      const previous = samples[index - 1];
      const current = samples[index];
      expect(Math.max(previous.alphaMass, current.alphaMass) / Math.min(previous.alphaMass, current.alphaMass)).toBeLessThan(1.04);
      expect(Math.abs(previous.centerX - current.centerX)).toBeLessThan(3.5);
    }
  }

  await page.getByRole("tab", { name: "右拖", exact: true }).click();
  await expect(pet.locator('[data-assistant-pet-intent="running-right"]')).toBeVisible();
  await expectCanvasReady(canvas);
  await page.waitForTimeout(90);
  const runningSamples: CanvasMetrics[] = [];
  for (let sample = 0; sample < 36; sample += 1) {
    await page.waitForTimeout(24);
    runningSamples.push(await canvasMetrics(canvas));
  }
  const runningHashes = new Set(runningSamples.map((sample) => sample.hash));
  expect(runningHashes.size).toBeGreaterThanOrEqual(7);
  expect(runningHashes.size).toBeLessThanOrEqual(8);
  expect(Math.min(...runningSamples.map((sample) => sample.alphaMass))).toBeGreaterThan(9_000);

  await page.getByRole("tab", { name: "贴边", exact: true }).click();
  await expect(canvas).toHaveAttribute("height", "150");
  const peekingFrame = pet.locator("[data-assistant-pet-peeking='true']");
  const peekingBox = await peekingFrame.boundingBox();
  expect(Math.round(peekingBox?.width || 0)).toBe(102);
  expect(Math.round(peekingBox?.height || 0)).toBe(80);
  const peekingTransform = await canvas.evaluate((element) => getComputedStyle(element).transform);
  expect(peekingTransform).not.toBe("none");

  await page.getByRole("tab", { name: "已完成", exact: true }).click();
  await expect(pet.locator('[data-assistant-pet-intent="jumping"]')).toBeVisible();
  await expect(canvas).toHaveAttribute("height", "208");
  await expectCanvasReady(canvas);
  await page.waitForTimeout(30);
  const jumpSamples: CanvasMetrics[] = [];
  for (let sample = 0; sample < 40; sample += 1) {
    await page.waitForTimeout(24);
    jumpSamples.push(await canvasMetrics(canvas));
  }
  const jumpHashes = new Set(jumpSamples.map((sample) => sample.hash));
  expect(jumpHashes.size).toBeGreaterThanOrEqual(5);
  expect(jumpHashes.size).toBeLessThanOrEqual(6);
  expect(Math.min(...jumpSamples.map((sample) => sample.alphaMass))).toBeGreaterThan(8_000);
  expect(Math.min(...jumpSamples.map((sample) => sample.top))).toBeLessThan(8);
  expect(Math.max(...jumpSamples.map((sample) => sample.bottom))).toBeGreaterThan(195);
});
