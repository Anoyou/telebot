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
    await expect(page.locator("span").filter({ hasText: /^2026-07-24 05:42:58$/ })).toBeVisible();
    await expect(page.getByText("05:42:58.931", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /DEBUG 1/ }).click();
    await expect(page.locator('[data-console-level="debug"]')).toHaveCount(1);
    await expect(page.locator('[data-console-level="info"]')).toHaveCount(0);
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
      return {
        sortRight: sortRect.right,
        sortTop: sortRect.top,
        createLeft: createRect?.left ?? 0,
        createTop: createRect?.top ?? 0,
      };
    });
    expect(controls.createLeft).toBeGreaterThanOrEqual(controls.sortRight);
    expect(Math.round(controls.createTop)).toBe(Math.round(controls.sortTop));
    await expect(createButton).toBeVisible();
    await expect(page.getByRole("button", { name: "模型测活" })).toBeVisible();
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
    await expect(scope.getByRole("switch")).not.toHaveCount(0);
    await expect(scope.locator('input[type="checkbox"]')).toHaveCount(0);
    await scope.getByRole("button", { name: /Grok/ }).click();
    await expect(scope.getByText("grok-4.20-fast", { exact: true })).toBeVisible();

    const overlay = page.getByRole("button", { name: "关闭 Provider 范围" }).first();
    await expect(overlay).toHaveCSS("background-color", "rgba(0, 0, 0, 0.2)");
    await scope.getByRole("button", { name: "关闭 Provider 范围" }).click();
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await expect(page.getByLabel("全局巡检请求设置")).toBeVisible();
    await expect(page.getByRole("button", { name: "刷新所选模型范围" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "清空对话" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "开始全量测活" })).toBeEnabled();
    fixture.assertClean();
  });

  test("PWA 底栏使用独立圆形助手入口并替代悬浮标签", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    await page.emulateMedia({ reducedMotion: "no-preference" });
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
    const assistantSurface = page.locator("[data-assistant-surface]");
    await expect(assistantSurface).toBeVisible();
    const motion = await assistantSurface.evaluate((element) => {
      const style = getComputedStyle(element);
      return { duration: style.transitionDuration, property: style.transitionProperty };
    });
    expect(motion.duration).not.toBe("0s");
    expect(motion.property).toContain("transform");
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

  test("中间宽度可从贴边机器人进入系统助手", async ({ page }) => {
    const fixture = await installApiFixture(page);
    await page.setViewportSize({ width: 758, height: 1100 });
    await page.goto("/ai", { waitUntil: "networkidle" });
    await expect(page.locator("[data-assistant-mobile-button]")).toBeHidden();
    const assistantEntry = page.locator("[data-assistant-desktop-pet]");
    await expect(assistantEntry).toBeVisible();
    await assistantEntry.click();
    await expect(page.locator("[data-assistant-surface]")).toBeVisible();
    fixture.assertClean();
  });

  test("桌面端使用可拖动贴边机器人入口", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "仅桌面视口");
    const fixture = await installApiFixture(page);
    await page.goto("/overview", { waitUntil: "networkidle" });
    const assistantPet = page.locator("[data-assistant-desktop-pet]");
    await expect(assistantPet).toBeVisible();
    await expect(assistantPet).toHaveAttribute("data-docked", "right");
    await expect(assistantPet.locator(".assistant-pet-head")).toBeVisible();
    const petBox = await assistantPet.boundingBox();
    expect(petBox).not.toBeNull();
    await page.mouse.move((petBox?.x || 0) + 10, (petBox?.y || 0) + 24);
    await page.mouse.down();
    await page.mouse.move(1040, 360, { steps: 8 });
    await page.mouse.up();
    await expect(assistantPet).toHaveAttribute("data-docked", "false");
    await expect(assistantPet).toHaveAttribute("data-docked", "right", { timeout: 3_000 });
    await expect(assistantPet.locator(".assistant-pet-arm-left")).toHaveCSS("animation-name", "assistant-pet-arm-left");
    await expect(assistantPet.locator(".assistant-pet-foot-right")).toHaveCSS("animation-name", "assistant-pet-foot-right");
    await expect(page.locator("[data-assistant-mobile-button]")).toBeHidden();
    await expect(page.locator("[data-assistant-tip]")).toHaveCount(0);
    fixture.assertClean();
  });

  test("系统助手输入框固定在可视底部且仅消息区滚动", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === "tablet", "桌面与 PWA 各验证一次");
    const fixture = await installApiFixture(page);
    const session = {
      id: "visual-session",
      web_user_id: 1,
      bot_tg_user_id: null,
      account_id: null,
      channel: "web",
      title: "固定输入框验证",
      status: "active",
      created_at: "2026-07-24T00:00:00Z",
      updated_at: "2026-07-24T00:00:00Z",
    };
    await page.route("**/api/system-agent/sessions?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([session]) });
    });
    await page.route("**/api/system-agent/sessions/visual-session/messages?**", async (route) => {
      const messages = Array.from({ length: 30 }, (_, index) => ({
        id: index + 1,
        session_id: session.id,
        role: index % 2 ? "assistant" : "user",
        content: { text: `第 ${index + 1} 条用于撑开消息列表的测试内容。` },
        run_status: "succeeded",
        created_at: "2026-07-24T00:00:00Z",
      }));
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(messages) });
    });
    await page.route("**/api/system-agent/actions?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    });

    await page.goto("/ai", { waitUntil: "networkidle" });
    const trigger = testInfo.project.name === "mobile"
      ? page.locator("[data-assistant-mobile-button]")
      : page.locator("[data-assistant-desktop-pet]");
    await trigger.click();
    const surface = page.locator("[data-assistant-surface]");
    const composer = page.locator("[data-assistant-composer]");
    const mobileSummary = surface.locator("[data-assistant-mobile-summary]");
    const mobileSettings = surface.locator("[data-assistant-mobile-settings]");
    if (testInfo.project.name === "mobile") {
      await expect(surface.getByRole("heading", { name: "系统助手" })).toBeHidden();
      await expect(mobileSummary).toBeVisible();
      await expect(mobileSummary).toHaveAttribute("aria-expanded", "false");
      await expect(mobileSettings).toBeHidden();
      await expect(page.locator("[data-mobile-navigation-dock]")).toBeHidden();
      await expect(trigger.locator('[data-agent-pet-intent="awake"]')).toBeVisible();
      await expect(composer).toBeVisible();
      const triggerBox = await trigger.boundingBox();
      const initialComposerBox = await composer.boundingBox();
      const triggerGap = (initialComposerBox?.y || 0) - ((triggerBox?.y || 0) + (triggerBox?.height || 0));
      expect(triggerGap).toBeGreaterThanOrEqual(4);
      expect(triggerGap).toBeLessThanOrEqual(24);
      await mobileSummary.click();
      await expect(mobileSettings).toBeVisible();
      await mobileSettings.getByRole("button", { name: "配置" }).click();
      await expect(surface.locator("[data-assistant-config-panel]")).toBeVisible();
      await expect(mobileSettings).toBeHidden();
      await expect(surface.getByRole("button", { name: "收起配置" })).toBeVisible();
    } else {
      await expect(surface.getByRole("heading", { name: "系统助手" })).toBeVisible();
      await expect(mobileSummary).toBeHidden();
      await expect(mobileSettings).toBeVisible();
    }
    const conversation = surface.locator(".overflow-y-auto").filter({ hasText: "第 30 条用于撑开消息列表的测试内容。" });
    await expect(composer).toBeVisible();
    await expect(conversation).toBeVisible();
    const before = await composer.boundingBox();
    await conversation.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    const after = await composer.boundingBox();
    expect(Math.round(after?.y || 0)).toBe(Math.round(before?.y || 0));
    const surfaceBox = await surface.boundingBox();
    const chatBox = await surface.locator("[data-assistant-chat-window]").boundingBox();
    expect(chatBox?.height || 0).toBeGreaterThan(testInfo.project.name === "mobile" ? 310 : 340);
    expect((surfaceBox?.y || 0) + (surfaceBox?.height || 0) - ((after?.y || 0) + (after?.height || 0))).toBeGreaterThan(15);
    fixture.assertClean();
  });

  test("指令与任务使用独立一级页面且旧插件路径已移除", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "仅桌面视口");
    const fixture = await installApiFixture(page);
    await page.goto("/operations/templates", { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { name: "指令与任务" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "自定义指令" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "定时任务" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "自动指令白名单" })).toBeVisible();
    await expect(page.getByText("插件中心", { exact: true })).toHaveCount(0);
    await expect(page.locator('[data-sidebar-sort-path="/operations"]')).toContainText("指令与任务");

    await page.goto("/plugins/templates", { waitUntil: "networkidle" });
    await expect(page).toHaveURL(/\/plugins\/templates$/);
    await expect(page.getByRole("heading", { name: "页面已移除" })).toBeVisible();
    await page.goto("/ai", { waitUntil: "networkidle" });
    await expect(page.getByRole("button", { name: "配置系统助手" })).toHaveCount(0);
    fixture.assertClean();
  });

  test("主滚动区预留滚动条槽位避免子页面切换跳动", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    await page.goto("/operations/templates", { waitUntil: "networkidle" });
    await expect(page.locator("[data-app-main]")).toHaveCSS("scrollbar-gutter", "stable");
    fixture.assertClean();
  });

  test("桌面侧边栏可直接拖动并自动保存顺序", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "仅桌面视口");
    let savedOrder: string[] | null = null;
    const fixture = await installApiFixture(page);
    await page.route("**/api/system/settings", async (route) => {
      if (route.request().method() !== "PATCH") {
        await route.fallback();
        return;
      }
      const payload = route.request().postDataJSON() as { ui_preferences?: { sidebar_order?: string[] } };
      savedOrder = payload.ui_preferences?.sidebar_order ?? null;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ timezone: "Asia/Shanghai", login_security: {}, ui_preferences: { sidebar_order: savedOrder } }),
      });
    });
    await page.goto("/overview", { waitUntil: "networkidle" });
    const overview = page.locator('[data-sidebar-sort-path="/overview"]');
    const plugins = page.locator('[data-sidebar-sort-path="/plugins"]');
    await overview.dragTo(plugins);
    await expect.poll(() => savedOrder?.[0]).toBe("/overview");
    fixture.assertClean();
  });

  test("概览资源标题与首排卡片保持稳定间距", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "仅桌面视口");
    const fixture = await installApiFixture(page);
    await page.goto("/overview", { waitUntil: "networkidle" });
    const card = page.locator("[data-resource-usage-card]");
    const sampling = page.locator("[data-resource-sampling-panel]");
    await expect(card).toBeVisible();
    await expect(sampling).toBeVisible();
    const gap = await sampling.evaluate((element) => {
      const card = element.closest("[data-resource-usage-card]");
      const header = card?.querySelector(":scope > div:first-child");
      if (!header) return -1;
      return Math.round(element.getBoundingClientRect().top - header.getBoundingClientRect().bottom);
    });
    expect(gap).toBeGreaterThanOrEqual(16);
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

  test("部署详情与更新内容默认折叠并展示版本摘要", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);

    await page.goto("/plugins", { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "检查更新" }).click();
    const dialog = page.getByRole("dialog", { name: "检查更新" });
    await expect(dialog).toBeVisible();
    const checkingHeight = await dialog.evaluate((element) => Math.round(element.getBoundingClientRect().height));
    const details = page.locator("details").filter({ hasText: "部署详情" });
    await expect(details).toBeVisible();
    const resolvedHeight = await dialog.evaluate((element) => Math.round(element.getBoundingClientRect().height));
    expect(Math.abs(resolvedHeight - checkingHeight)).toBeLessThanOrEqual(1);
    await expect(details).not.toHaveAttribute("open", "");
    await expect(details.locator("summary")).toContainText(`v${APP_VERSION} → v0.72.0-beta.2`);
    await expect(details.getByText("当前提交: aaaa1111aaaa")).toBeHidden();
    await details.locator("summary").click();
    await expect(details).toHaveAttribute("open", "");
    await expect(details.getByText("当前提交: aaaa1111aaaa")).toBeVisible();
    const releaseNotes = page.locator("details").filter({ hasText: "查看更新内容" });
    await expect(releaseNotes).not.toHaveAttribute("open", "");
    await releaseNotes.locator("summary").click();
    await expect(releaseNotes.getByText("优化更新弹窗的信息层级")).toBeVisible();
    fixture.assertClean();
  });
});
