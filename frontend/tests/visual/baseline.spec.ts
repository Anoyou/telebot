import { test, expect } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";
import { installApiFixture } from "./fixtures";
import { applyVisualMasks } from "./masks";
import { screenshotDiffRatio } from "./compare";

const pages = [
  ["dashboard", "/overview"],
  ["accounts", "/?accounts=1"],
  ["ledger", "/ledger"],
  ["logs", "/logs"],
  ["extensions", "/plugins/manage"],
  ["ai", "/ai"],
  ["llm-providers", "/ai?tab=providers"],
  ["bot", "/accounts/1?tab=bot"],
] as const;

// 仓库截图由开发机生成，跨 macOS 大版本时字体与 Chromium 栅格化会产生稳定的
// 像素差。CI 仍检查基线存在、页面行为和同一 Runner 内两次渲染一致；需要在
// CI 上强制核对仓库截图时可显式设置 VERIFY_STORED_VISUAL_BASELINES=1。
const verifyStoredVisualBaselines =
  process.env.CI !== "true" || process.env.VERIFY_STORED_VISUAL_BASELINES === "1";

for (const [name, path] of pages) {
  for (const theme of ["light", "dark"] as const) {
    test(`${name} ${theme} 页面基线可重复渲染`, async ({ page }, testInfo) => {
      const pageErrors: string[] = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      const fixture = await installApiFixture(page);
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.goto(path, { waitUntil: "networkidle" });
      await applyVisualMasks(page);
      await expect(page.locator("body")).toBeVisible();
      await expect(page.locator("main")).toBeVisible();
      expect(pageErrors, "页面渲染期间出现未捕获异常").toEqual([]);
      fixture.assertClean();
      if (testInfo.project.name === "mobile") {
        const overflow = await page.evaluate(() => {
          const viewportWidth = document.documentElement.clientWidth;
          const offenders = Array.from(document.querySelectorAll<HTMLElement>("body *"))
            .filter((element) => {
              const rect = element.getBoundingClientRect();
              return rect.right > viewportWidth + 1 || rect.left < -1;
            })
            .slice(0, 12)
            .map((element) => ({
              tag: element.tagName.toLowerCase(),
              className: typeof element.className === "string" ? element.className : "",
              left: Math.round(element.getBoundingClientRect().left),
              right: Math.round(element.getBoundingClientRect().right),
            }));
          return {
            viewportWidth,
            scrollWidth: document.documentElement.scrollWidth,
            offenders,
          };
        });
        expect(overflow.scrollWidth, JSON.stringify(overflow, null, 2)).toBeLessThanOrEqual(overflow.viewportWidth + 1);
      }
      const baselinePath = fileURLToPath(new URL(`../../../docs/frontend/baseline/screenshots/${name}-${testInfo.project.name}-${theme}.png`, import.meta.url));
      const first = await page.screenshot({ fullPage: true });
      if (process.env.UPDATE_VISUAL_BASELINES === "1") {
        mkdirSync(dirname(baselinePath), { recursive: true });
        writeFileSync(baselinePath, first);
      } else {
        expect(existsSync(baselinePath), `缺少视觉基线 ${baselinePath}，请先运行 pnpm --dir frontend test:visual:update`).toBe(true);
        if (verifyStoredVisualBaselines) {
          const baseline = readFileSync(baselinePath);
          expect(screenshotDiffRatio(baseline, first), `视觉回归超过 0.1%: ${baselinePath}`).toBeLessThanOrEqual(0.001);
        }
      }
      await page.reload({ waitUntil: "networkidle" });
      await applyVisualMasks(page);
      await expect(page.locator("main")).toBeVisible();
      expect(pageErrors, "页面重载期间出现未捕获异常").toEqual([]);
      const second = await page.screenshot({ fullPage: true });
      expect(screenshotDiffRatio(first, second)).toBeLessThanOrEqual(0.001);
      fixture.assertClean();
    });
  }
}
