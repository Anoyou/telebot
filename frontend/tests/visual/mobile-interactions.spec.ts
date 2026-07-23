import { expect, test } from "@playwright/test";

import { APP_VERSION } from "../../src/lib/version";
import { installApiFixture, installProviderFixture } from "./fixtures";

test.describe("移动端交互细节", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "只在 Chromium 项目运行");

  test("滚动边界版本提示不会占用页面高度并会自动回收", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/ledger", { waitUntil: "networkidle" });
    const main = page.locator("[data-app-main]");
    const topEdge = page.locator('[data-edge="top"]');
    const topLabel = topEdge.locator(".mobile-scroll-edge-label");
    const initialHeight = await topEdge.evaluate((element) => element.getBoundingClientRect().height);
    expect(initialHeight).toBe(0);
    await expect(topLabel).toHaveCSS("border-top-width", "0px");

    await main.evaluate((element) => { element.scrollTop = 180; });
    await main.evaluate((element) => { element.scrollTop = 0; });
    await expect(topEdge).toHaveAttribute("data-visible", "true");
    await expect(topEdge).toHaveAttribute("data-visible", "false", { timeout: 2_000 });
    fixture.assertClean();
  });

  test("侧栏明确展示更新日志入口并可打开内容", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/ledger", { waitUntil: "networkidle" });

    await page.getByRole("button", { name: "打开导航菜单" }).click();
    const changelogButton = page.getByRole("button", { name: "更新日志" });
    await expect(changelogButton).toBeVisible();
    await expect(changelogButton).toContainText("更新日志");
    await expect(changelogButton).toContainText(`v${APP_VERSION}`);
    const bounds = await changelogButton.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return { left: rect.left, right: rect.right, viewport: document.documentElement.clientWidth };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(0);
    expect(bounds.right).toBeLessThanOrEqual(bounds.viewport);

    await changelogButton.click();
    await expect(page.getByText("最近版本的主要变化，完整记录见仓库 CHANGELOG.md。"))
      .toBeVisible();
    const menuBounds = await page.getByRole("menu").evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return {
        top: rect.top,
        left: rect.left,
        right: rect.right,
        bottom: rect.bottom,
        viewportWidth: document.documentElement.clientWidth,
        viewportHeight: document.documentElement.clientHeight,
      };
    });
    expect(menuBounds.top).toBeGreaterThanOrEqual(0);
    expect(menuBounds.left).toBeGreaterThanOrEqual(0);
    expect(menuBounds.right).toBeLessThanOrEqual(menuBounds.viewportWidth);
    expect(menuBounds.bottom).toBeLessThanOrEqual(menuBounds.viewportHeight);
    fixture.assertClean();
  });

  test("PWA 更多菜单可以直接打开更新日志", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/overview", { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "更多导航" }).click();
    const changelogItem = page.getByRole("menuitem").filter({ hasText: "更新日志" });
    await expect(changelogItem).toHaveCount(1);
    await changelogItem.click();
    await expect(page.getByText("最近版本的主要变化，完整记录见仓库 CHANGELOG.md。")).toBeVisible();
    fixture.assertClean();
  });

  test("日志中心在移动端可以找到 Agent 运行记录", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/logs", { waitUntil: "networkidle" });

    await page.getByRole("tab", { name: "Agent 运行" }).click();
    await expect(page.getByRole("heading", { name: "Agent 运行" })).toBeVisible();
    await expect(page.getByText("当前条件下没有 Agent 运行记录")).toBeVisible();
    fixture.assertClean();
  });

  test("系统控制台按日志等级显示警示竖条并转换时区", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.route("**/api/logs/system-console", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          source: "docker_compose",
          services: ["web"],
          tail: 100,
          lines: [
            "web-1 | 2026-07-23T21:42:58.931730331Z INFO:app:启动完成",
            "web-1 | 2026-07-23T21:42:59Z WARNING:app:需要关注",
            "web-1 | 2026-07-23T21:43:00Z ERROR:app:运行失败",
            "web-1 | 2026-07-23T21:43:01Z DEBUG:app:诊断信息",
          ],
        }),
      });
    });
    await page.goto("/logs?view=console", { waitUntil: "networkidle" });
    await expect(page.locator('[data-console-level="info"]')).toHaveCount(1);
    await expect(page.locator('[data-console-level="warn"]')).toHaveCount(1);
    await expect(page.locator('[data-console-level="error"]')).toHaveCount(1);
    await expect(page.locator('[data-console-level="debug"]')).toHaveCount(1);
    await expect(page.getByText(/2026-07-24.*05:42:58\.931/)).toBeVisible();
    fixture.assertClean();
  });

  test("Provider 创建步骤保持单行置顶并随滚动更新", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/ai?tab=providers&newProvider=1", { waitUntil: "networkidle" });
    const steps = page.locator('ol[aria-label="创建步骤"]');
    await expect(steps).toBeVisible();
    const columns = await steps.evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(" ").length);
    expect(columns).toBe(3);

    const saveTitle = page.getByRole("heading", { name: "保存信息" });
    await saveTitle.evaluate((element) => element.scrollIntoView({ block: "start" }));
    const activeSaveStep = steps.locator("li.bg-primary");
    await expect(activeSaveStep).toContainText("保存");
    const stepsTop = await steps.evaluate((element) => Math.round(element.getBoundingClientRect().top));
    expect(stepsTop).toBeGreaterThanOrEqual(0);
    fixture.assertClean();
  });

  test("Provider 列表排序和测活控件使用紧凑全局样式", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await installProviderFixture(page);
    await page.goto("/ai?tab=providers", { waitUntil: "networkidle" });
    const sort = page.getByLabel("排序");
    await expect(sort).toBeVisible();
    const createButton = page.getByRole("button", { name: "新建" });
    const controls = await page.locator("#provider-sort").evaluate((element) => {
      const sortRect = element.getBoundingClientRect();
      const button = Array.from(document.querySelectorAll("button")).find((item) => item.textContent?.includes("新建"));
      const createRect = button?.getBoundingClientRect();
      return { sortRight: sortRect.right, createLeft: createRect?.left ?? 0 };
    });
    expect(controls.createLeft).toBeGreaterThanOrEqual(controls.sortRight);
    await expect(createButton).toBeVisible();
    await sort.selectOption("models");
    const providerCards = page.locator("[data-provider-card]");
    await expect(providerCards).toHaveCount(2);
    const cardText = await providerCards.allTextContents();
    expect(cardText[0]).toContain("Grok");

    await page.getByRole("button", { name: "编辑排序" }).click();
    await expect(page.getByLabel("Provider 自定义排序")).toBeVisible();
    await expect(page.locator("[data-provider-sort-id]")).toHaveCount(2);
    await expect(page.getByText("按住左侧手柄拖动卡片，完成后点击“保存排序”。")).toBeVisible();

    await page.goto("/ai/liveness?provider=2", { waitUntil: "networkidle" });
    await expect(page.getByRole("tab", { name: "Provider 多模型对话" })).toBeVisible();
    await expect(page.getByRole("button", { name: "打开测试范围" })).toContainText("范围");
    await expect(page.getByRole("button", { name: "打开请求设置" })).toContainText("设置");
    fixture.assertClean();
  });

  test("全量巡检在移动端使用范围按钮并可展开 Provider 选择模型", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await installProviderFixture(page);
    await page.goto("/ai/liveness?provider=2", { waitUntil: "networkidle" });

    await page.getByRole("tab", { name: "全部 Provider 巡检" }).click();
    await page.getByRole("button", { name: "范围", exact: true }).click();
    const scope = page.getByLabel("全局巡检 Provider 范围");
    await expect(scope).toBeVisible();
    await scope.getByRole("button", { name: /Grok/ }).click();
    await expect(scope.getByText("grok-4.20-fast", { exact: true })).toBeVisible();

    const overlay = page.getByRole("button", { name: "关闭 Provider 范围" }).first();
    await expect(overlay).toHaveCSS("background-color", "rgba(0, 0, 0, 0.2)");
    await scope.getByRole("button", { name: "关闭 Provider 范围" }).click();
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await expect(page.getByLabel("全局巡检请求设置")).toBeVisible();
    fixture.assertClean();
  });

  test("PWA 底栏使用独立圆形助手入口并替代悬浮标签", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/overview", { waitUntil: "networkidle" });

    const assistantButton = page.locator("[data-assistant-mobile-button]");
    await expect(assistantButton).toBeVisible();
    const shape = await assistantButton.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return { width: Math.round(rect.width), height: Math.round(rect.height) };
    });
    expect(shape.width).toBe(shape.height);
    await expect(assistantButton).toContainText("助手");
    await expect(page.locator("[data-assistant-tip]")).toBeHidden();
    await assistantButton.click();
    await expect(page.locator("[data-assistant-surface]")).toBeVisible();
    fixture.assertClean();
  });

  test("插件卡片在窄屏默认折叠且 AI 说明卡隐藏", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.route("**/api/system/settings", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ timezone: "Asia/Shanghai", ai_enabled: true, login_security: {} }) });
    });
    await page.route("**/api/feature-matrix", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          accounts: [{ id: 1, name: "视觉测试账号", features: { random_benefit: "active" }, feature_enabled: { random_benefit: true } }],
          features: [{
            key: "random_benefit",
            display_name: "随机福利",
            is_builtin: false,
            source_type: "local",
            source_label: "local",
            version: "1.0.0",
            usage: "这段插件说明在移动端默认不展示，展开卡片后才显示。",
            config_schema: {},
            category: "utility",
            capabilities: { telegram_direct_passthrough: true },
            experimental: false,
          }],
        }),
      });
    });
    await page.goto("/plugins", { waitUntil: "networkidle" });
    await expect(page.getByText("AI 插件入口")).toBeHidden();
    await expect(page.getByText("这段插件说明在移动端默认不展示，展开卡片后才显示。")).toBeHidden();
    await expect(page.getByRole("button", { name: "配置" })).toBeHidden();
    await page.getByRole("button", { name: "随机福利" }).click();
    await expect(page.getByText("这段插件说明在移动端默认不展示，展开卡片后才显示。")).toBeVisible();
    await expect(page.getByRole("button", { name: "配置" })).toBeVisible();
    fixture.assertClean();
  });

  test("中间宽度可从导航抽屉进入系统助手", async ({ page }) => {
    const fixture = await installApiFixture(page);
    await page.setViewportSize({ width: 758, height: 1100 });
    await page.goto("/ai", { waitUntil: "networkidle" });
    await expect(page.locator("[data-assistant-mobile-button]")).toBeHidden();
    await page.getByRole("button", { name: "打开导航菜单" }).click();
    const assistantEntry = page.getByRole("dialog", { name: "导航菜单" }).locator("[data-assistant-sidebar-button]");
    await expect(assistantEntry).toBeVisible();
    await assistantEntry.click();
    await expect(page.locator("[data-assistant-surface]")).toBeVisible();
    fixture.assertClean();
  });

  test("桌面端使用侧边栏固定助手入口", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "仅桌面视口");
    const fixture = await installApiFixture(page);
    await page.goto("/overview", { waitUntil: "networkidle" });
    await expect(page.locator("[data-assistant-sidebar-button]")).toBeVisible();
    await expect(page.locator("[data-assistant-mobile-button]")).toBeHidden();
    await expect(page.locator("[data-assistant-tip]")).toHaveCount(0);
    fixture.assertClean();
  });

  test("近期调用成功与失败指标可以筛选记录", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await installProviderFixture(page);
    await page.route("**/api/llm/usage/recent**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            { id: 1, account_id: 1, provider_id: 1, provider_name: "猫羽", model: "deepseek-chat", input_tokens: 10, output_tokens: 20, latency_ms: 100, success: true, created_at: "2026-07-24T00:00:00Z" },
            { id: 2, account_id: 1, provider_id: 2, provider_name: "Grok", model: "grok-4.20-fast", input_tokens: 12, output_tokens: 0, latency_ms: 200, success: false, error_type: "upstream_error", created_at: "2026-07-24T00:01:00Z" },
          ],
          summary: { request_count: 2, success_count: 1, failed_count: 1, fallback_count: 0, total_tokens: 42, avg_latency_ms: 150 },
        }),
      });
    });
    await page.goto("/ai?tab=usage", { waitUntil: "networkidle" });
    const failedMetric = page.locator('button[aria-pressed="false"]').filter({ hasText: "失败" });
    await expect(failedMetric).toHaveCount(1);
    await failedMetric.click();
    await expect(page.getByText("当前仅显示失败记录，再点一次指标可取消筛选。")).toBeVisible();
    await expect(page.locator("[data-assistant-surface]")).toBeHidden();
    const visibleRecords = page.locator("[data-usage-record]");
    await expect(visibleRecords).toHaveCount(1);
    await expect(visibleRecords).toContainText("Grok");
    await expect(visibleRecords).not.toContainText("猫羽");
    fixture.assertClean();
  });

  test("更新详情默认折叠并展示当前与目标版本", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);

    await page.goto("/plugins", { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "检查更新" }).click();
    const dialog = page.getByRole("dialog", { name: "检查更新" });
    await expect(dialog).toBeVisible();
    const checkingHeight = await dialog.evaluate((element) => Math.round(element.getBoundingClientRect().height));
    const details = page.locator("details").filter({ hasText: "更新详情" });
    await expect(details).toBeVisible();
    const resolvedHeight = await dialog.evaluate((element) => Math.round(element.getBoundingClientRect().height));
    expect(Math.abs(resolvedHeight - checkingHeight)).toBeLessThanOrEqual(1);
    await expect(details).not.toHaveAttribute("open", "");
    await expect(details.locator("summary")).toContainText(`v${APP_VERSION} → v0.72.0-beta.2`);
    await expect(details.getByText("当前提交: aaaa1111aaaa")).toBeHidden();
    await details.locator("summary").click();
    await expect(details).toHaveAttribute("open", "");
    await expect(details.getByText("当前提交: aaaa1111aaaa")).toBeVisible();
    fixture.assertClean();
  });
});
