import { expect, test, type Locator } from "@playwright/test";

test.use({ reducedMotion: "no-preference" });

type CanvasMetrics = {
  hash: number;
  alphaMass: number;
  centerX: number;
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
      top = Math.min(top, y);
      bottom = Math.max(bottom, y + 1);
    }
    return {
      hash: hash >>> 0,
      alphaMass,
      centerX: weightedX / alphaMass,
      top,
      bottom,
    };
  });
}

test("实装 Agent 的注视、跑动、贴边和跳跃保持连续", async ({ page }) => {
  test.setTimeout(45_000);
  await page.goto("/assistant-pet-states-preview.html");

  await page.getByRole("tab", { name: "注视", exact: true }).click();
  const pet = page.locator("[data-preview-production-pet]");
  const canvas = pet.locator("canvas");
  await expect(canvas).toBeVisible();
  const petBox = await pet.boundingBox();
  expect(petBox).not.toBeNull();

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
  const lookCenters = lookMetrics.map((sample) => sample.centerX);
  expect(Math.max(...lookMasses) / Math.min(...lookMasses)).toBeLessThan(1.025);
  expect(Math.max(...lookCenters) - Math.min(...lookCenters)).toBeLessThan(1.5);

  await page.getByRole("tab", { name: "右拖", exact: true }).click();
  const runningSamples: CanvasMetrics[] = [];
  for (let sample = 0; sample < 36; sample += 1) {
    await page.waitForTimeout(24);
    runningSamples.push(await canvasMetrics(canvas));
  }
  expect(new Set(runningSamples.map((sample) => sample.hash)).size).toBeGreaterThan(16);

  await page.getByRole("tab", { name: "贴边", exact: true }).click();
  await expect(canvas).toHaveAttribute("height", "208");
  const wall = await pet.locator("[data-assistant-pet-peeking='true']").evaluate((element) => {
    const style = getComputedStyle(element, "::after");
    return { content: style.content, top: style.top, width: style.width };
  });
  expect(wall.content).not.toBe("none");
  expect(wall.top).toBe("70px");
  expect(wall.width).toBe("1px");

  await page.getByRole("tab", { name: "已完成", exact: true }).click();
  const jumpSamples: CanvasMetrics[] = [];
  for (let sample = 0; sample < 40; sample += 1) {
    await page.waitForTimeout(24);
    jumpSamples.push(await canvasMetrics(canvas));
  }
  expect(new Set(jumpSamples.map((sample) => sample.hash)).size).toBeGreaterThan(14);
  expect(Math.min(...jumpSamples.map((sample) => sample.top))).toBeLessThan(8);
  expect(Math.max(...jumpSamples.map((sample) => sample.bottom))).toBeGreaterThan(195);
});
