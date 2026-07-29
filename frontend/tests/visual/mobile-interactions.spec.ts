import { expect, test } from "@playwright/test";

import { APP_VERSION } from "../../src/lib/version";
import { installApiFixture, installProviderFixture, providerFixtures } from "./fixtures";

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
    await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBeGreaterThan(8);
    await page.waitForTimeout(50);
    await main.evaluate((element) => { element.scrollTop = 0; });
    await expect(topEdge).toHaveAttribute("data-visible", "true");
    await expect(topEdge).toHaveAttribute("data-visible", "false", { timeout: 2_000 });
    fixture.assertClean();
  });

  test("PWA 页面与卡片使用紧凑间距", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅 PWA 视口");
    const fixture = await installApiFixture(page);
    await page.goto("/overview", { waitUntil: "networkidle" });
    const spacing = await page.locator("[data-app-main]").evaluate((main) => {
      const pageShell = main.querySelector<HTMLElement>("[data-page-shell]");
      const pageHeader = main.querySelector<HTMLElement>("[data-page-header]");
      const resourceCard = main.querySelector<HTMLElement>("[data-resource-usage-card]");
      const resourceHeader = resourceCard?.firstElementChild as HTMLElement | null;
      const mobileNav = document.querySelector<HTMLElement>("[data-mobile-navigation-dock]");
      const mobileNavBefore = mobileNav ? getComputedStyle(mobileNav, "::before") : null;
      const shellChildren = pageShell ? Array.from(pageShell.children) as HTMLElement[] : [];
      return {
        viewportWidth: document.documentElement.clientWidth,
        documentWidth: document.documentElement.scrollWidth,
        mainPaddingLeft: getComputedStyle(main).paddingLeft,
        mainPaddingTop: getComputedStyle(main).paddingTop,
        mainPaddingBottom: Number.parseFloat(getComputedStyle(main).paddingBottom),
        mobileNavHeight: mobileNav?.getBoundingClientRect().height ?? 0,
        mobileNavTransform: mobileNav ? getComputedStyle(mobileNav).transform : "",
        mobileNavBackdropFilter: mobileNavBefore?.backdropFilter ?? "",
        mobileNavBackgroundImage: mobileNavBefore?.backgroundImage ?? "",
        usesAppleWebKitMaterialTuning: CSS.supports("-webkit-touch-callout", "none"),
        pageHeaderPaddingLeft: pageHeader ? getComputedStyle(pageHeader).paddingLeft : "",
        sectionGap: shellChildren[1] ? getComputedStyle(shellChildren[1]).marginTop : "",
        cardHeaderPaddingLeft: resourceHeader ? getComputedStyle(resourceHeader).paddingLeft : "",
      };
    });
    expect(spacing).toMatchObject({
      mainPaddingLeft: "12px",
      mainPaddingTop: "12px",
      pageHeaderPaddingLeft: "12px",
      sectionGap: "16px",
      cardHeaderPaddingLeft: "16px",
    });
    expect(spacing.documentWidth).toBeLessThanOrEqual(spacing.viewportWidth);
    expect(spacing.mainPaddingBottom).toBeGreaterThan(spacing.mobileNavHeight);
    expect(spacing.mobileNavTransform).toBe("none");
    expect(spacing.usesAppleWebKitMaterialTuning).toBe(false);
    expect(spacing.mobileNavBackdropFilter).toContain("blur(");
    expect(spacing.mobileNavBackgroundImage).toContain("linear-gradient");
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
    const activeSaveStep = steps.locator("li:has(> span.bg-primary)");
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

  test("Provider 模型行只保留真实单模型测活入口", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === "tablet", "手机与桌面视口已覆盖");
    const fixture = await installApiFixture(page);
    await installProviderFixture(page);
    let requestedPayload: unknown = null;
    await page.route("**/api/commands/llm-providers/1/chat-test-models", async (route) => {
      requestedPayload = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          provider_id: 1,
          provider_name: "猫羽",
          results: [{
            ok: true,
            requested_model: "deepseek-chat",
            model: "deepseek-chat",
            latency_ms: 842,
            response: "模型测活正常。",
            preview: "模型测活正常。",
            input_tokens: 18,
            output_tokens: 7,
            empty_response: false,
          }],
        }),
      });
    });
    await page.goto("/ai?tab=providers", { waitUntil: "networkidle" });

    const providerSurface = testInfo.project.name === "mobile"
      ? page.locator("[data-provider-card]").filter({ hasText: "猫羽" })
      : page.locator("tr").filter({ hasText: "猫羽" });
    await expect(providerSurface).toHaveCount(1);
    await providerSurface.getByRole("button", { name: "编辑" }).click();

    await expect(page.getByRole("heading", { name: "编辑模型提供商" })).toBeVisible();
    await page.getByRole("button", { name: "未启用模型（2） · 点击展开" }).click();
    await expect(page.getByRole("button", { name: "测活", exact: true })).toHaveCount(4);
    await expect(page.getByRole("button", { name: "测试", exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "对话", exact: true })).toHaveCount(0);
    const modelRow = page.locator('[data-provider-model-id="deepseek-chat"]');
    await expect(modelRow).toHaveCount(1);
    await modelRow.getByRole("button", { name: "测活", exact: true }).click();
    await expect(modelRow).toContainText("842 ms");
    await expect(modelRow.getByTitle("模型测活正常。")).toBeVisible();
    const rowWidth = await modelRow.evaluate((element) => ({
      client: element.clientWidth,
      scroll: element.scrollWidth,
    }));
    expect(rowWidth.scroll).toBeLessThanOrEqual(rowWidth.client);
    await expect(page).toHaveURL(/\/ai\?tab=providers$/);
    expect(requestedPayload).toMatchObject({
      models: ["deepseek-chat"],
      max_tokens: 128,
      timeout_seconds: 90,
    });
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
    await expect(assistantButton).not.toContainText("助手");
    const compactAssistant = assistantButton.locator('[data-assistant-pet-compact="true"]');
    await expect(compactAssistant).toBeVisible();
    await expect(compactAssistant).toHaveAttribute("data-assistant-pet-intent", "idle");
    await expect(compactAssistant).toHaveAttribute("data-assistant-pet-compact-mode", "upper");
    const assistantBox = await assistantButton.boundingBox();
    const compactBox = await compactAssistant.boundingBox();
    expect(Math.abs(
      ((assistantBox?.x || 0) + (assistantBox?.width || 0) / 2)
      - ((compactBox?.x || 0) + (compactBox?.width || 0) / 2)
    )).toBeLessThanOrEqual(0.5);
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
    const pluginCard = page.locator('[data-plugin-key="random_benefit"]');
    await expect(pluginCard.locator("[data-plugin-version]")).toHaveText("v1.0.0");
    await expect(pluginCard.locator("[data-plugin-version]")).toBeVisible();
    await expect(pluginCard.getByRole("button", { name: "配置 随机福利" })).toBeVisible();
    await expect(page.getByRole("button", { name: "配置", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "随机福利", exact: true }).click();
    await expect(page.getByText("这段插件说明在移动端默认不展示，展开卡片后才显示。")).toBeVisible();
    fixture.assertClean();
  });

  test("插件中心使用分类栏并默认平铺全部已安装插件", async ({ page }, testInfo) => {
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const fixture = await installApiFixture(page);
    await page.route("**/api/feature-matrix", async (route) => {
      const features = [
        { key: "game_demo", display_name: "互动示例", is_builtin: false, source_type: "remote", version: "1.0.0", usage: "互动插件", category: "interactive", experimental: false },
        { key: "auto_demo", display_name: "自动化示例", is_builtin: false, source_type: "remote", version: "1.0.0", usage: "自动化插件", category: "automation", experimental: false },
        { key: "tool_demo", display_name: "工具示例", is_builtin: false, source_type: "remote", version: "1.0.0", usage: "工具插件", category: "utility", capabilities: { telegram_direct_passthrough: true }, experimental: false },
      ];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          accounts: [{ id: 1, name: "视觉测试账号", features: { game_demo: "active", auto_demo: "disabled", tool_demo: "active" }, feature_enabled: { game_demo: true, auto_demo: false, tool_demo: true } }],
          features,
        }),
      });
    });
    await page.goto("/plugins", { waitUntil: "networkidle" });
    await expect(page.getByText("0.13 安全变更提醒", { exact: true })).toHaveCount(0);
    await expect(page.locator('[data-plugin-category-filter="all"]')).toHaveAttribute("aria-current", "page");
    const allCategory = page.locator('[data-plugin-category-filter="all"]');
    if (testInfo.project.name === "desktop") {
      const categoryNavBox = await page.locator("[data-plugin-category-nav]").boundingBox();
      expect(categoryNavBox?.width || 999).toBeLessThanOrEqual(140);
    } else {
      const allBox = await allCategory.boundingBox();
      expect(allBox?.width || 999).toBeLessThan(150);
    }
    await expect(page.locator("[data-plugin-card]")).toHaveCount(3);
    const gameCard = page.locator('[data-plugin-key="game_demo"]');
    const gameCardStyle = await gameCard.evaluate((element) => {
      const style = getComputedStyle(element);
      return { backgroundColor: style.backgroundColor, paddingTop: Number.parseFloat(style.paddingTop) };
    });
    expect(gameCardStyle.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
    expect(gameCardStyle.paddingTop).toBeLessThanOrEqual(10);
    await expect(gameCard.locator('[data-plugin-state-rail="success"]')).toBeVisible();
    const disabledRail = page.locator('[data-plugin-key="auto_demo"] [data-plugin-state-rail="warn"]');
    await expect(disabledRail).toBeVisible();
    await expect(disabledRail).toHaveCSS("background-color", "rgb(250, 204, 21)");
    await expect(page.locator('[data-plugin-key="tool_demo"] [data-plugin-state-rail="danger"]')).toBeVisible();
    await gameCard.hover();
    await expect(gameCard).not.toHaveCSS("transform", "none");
    await page.locator('[data-plugin-category-filter="interactive"]').click();
    await expect(page.locator("[data-plugin-card]")).toHaveCount(1);
    await expect(page.locator('[data-plugin-key="game_demo"]')).toBeVisible();
    await expect(page.locator('[data-plugin-key="auto_demo"]')).toHaveCount(0);
    fixture.assertClean();
  });

  test("插件管理默认折叠配置并在详情中快捷启停账号和查看日志", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅移动视口");
    const fixture = await installApiFixture(page);
    const plugin = {
      key: "demo_plugin",
      display_name: "示例插件",
      source: "repo",
      source_url: "https://github.com/example/plugins/tree/main/demo_plugin",
      source_label: "telebot-plugins",
      version: "1.2.3",
      global_enabled: true,
      signature_ok: true,
      trust_tier: "community",
      lint_warnings: [],
      update: { update_available: false, latest_version: null, last_update_check_at: "2026-07-24T00:00:00Z", last_update_check_error: null },
      accounts: [{ account_id: 1, account_name: "视觉测试账号", enabled: false, state: "disabled", load_status: null, last_error: null, last_load_error: null, last_trace: { trace_id: "evt_demo", account_id: 1, status: "ok", event_type: "message", source_channel: "userbot", started_at: "2026-07-24T00:00:00Z" } }],
      recent_load_error: null,
      recent_trace: { trace_id: "evt_demo", account_id: 1, status: "ok", event_type: "message", source_channel: "userbot", started_at: "2026-07-24T00:00:00Z" },
    };
    await page.route("**/api/plugins/installed-overview", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([plugin]) });
    });
    await page.route("**/api/plugin-repos", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{ id: 1, name: "telebot-plugins", url: "https://github.com/example/plugins", description: "", auth_type: "none", has_credentials: false, added_at: null, updated_at: null }]) });
    });
    await page.route("**/api/plugins/install/demo_plugin/changelog", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ key: "demo_plugin", available: false, content: "", truncated: false, message: "该插件未提供 CHANGELOG.md。" }) });
    });
    let toggleRequested = false;
    await page.route("**/api/accounts/1/features/demo_plugin", async (route) => {
      toggleRequested = true;
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });

    await page.goto("/plugins/manage?tab=plugins", { waitUntil: "networkidle" });
    const installToolsGroup = page.locator("[data-install-tools-group]");
    await expect(installToolsGroup).toBeVisible();
    const installToolsTrigger = installToolsGroup.getByRole("button", { name: "展开可配置" });
    await expect(installToolsTrigger).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("button", { name: "展开配置" })).toHaveCount(0);
    await installToolsTrigger.click();
    await expect(page.getByRole("button", { name: "展开配置" })).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("button", { name: "展开添加仓库" })).toHaveAttribute("aria-expanded", "false");
    const savedRepo = page.getByText("telebot-plugins", { exact: true });
    await expect(savedRepo).toHaveCount(1);
    const installedTrigger = page.getByRole("button", { name: "展开已安装插件" });
    await expect(installedTrigger).toHaveAttribute("aria-expanded", "false");
    await installedTrigger.click();
    await page.getByRole("button", { name: "详情" }).click();
    const dialog = page.getByRole("dialog", { name: "示例插件" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText("来源库", { exact: true })).toBeVisible();
    await expect(dialog.getByText("当前版本", { exact: true })).toBeVisible();
    await expect(dialog.getByText("更新状态", { exact: true })).toBeVisible();
    const accountSwitch = dialog.getByRole("switch", { name: "视觉测试账号启用当前插件" });
    await accountSwitch.click();
    await expect.poll(() => toggleRequested).toBe(true);
    await dialog.getByRole("button", { name: "更新日志" }).click();
    await expect(dialog.getByText("该插件未提供 CHANGELOG.md。", { exact: true })).toBeVisible();

    const footerButtons = ["查看最近 trace", "去插件中心", "关闭"];
    const detailFooter = dialog.locator("[data-plugin-detail-footer]");
    const tops = await Promise.all(footerButtons.map(async (name) => {
      const box = await detailFooter.getByRole("button", { name, exact: true }).boundingBox();
      return Math.round(box?.y || 0);
    }));
    expect(new Set(tops).size).toBe(1);
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
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const fixture = await installApiFixture(page);
    await page.goto("/overview", { waitUntil: "networkidle" });
    const assistantPet = page.locator("[data-assistant-desktop-pet]");
    await expect(assistantPet).toBeVisible();
    await expect(assistantPet).toHaveAttribute("data-docked", "right");
    const sprite = assistantPet.locator('[data-assistant-pet-intent="idle"]');
    await expect(sprite.locator("canvas")).toBeVisible();
    await expect(sprite.locator("canvas")).toHaveAttribute("height", "208");
    const dockedSpriteBox = await sprite.boundingBox();
    expect(Math.round(dockedSpriteBox?.width || 0)).toBe(102);
    expect(Math.round(dockedSpriteBox?.height || 0)).toBe(114);
    const dockedTransform = await sprite.locator("canvas").evaluate((element) => getComputedStyle(element).transform);
    expect(dockedTransform).toBe("none");
    const petBox = await assistantPet.boundingBox();
    expect(petBox).not.toBeNull();
    expect(Math.round(petBox?.width || 0)).toBe(102);
    expect(Math.round(petBox?.height || 0)).toBe(114);
    expect(Math.round((petBox?.x || 0) + (petBox?.width || 0))).toBe(page.viewportSize()?.width);
    await page.mouse.move((petBox?.x || 0) + 10, (petBox?.y || 0) + 24);
    await page.mouse.down();
    await page.mouse.move(1040, 360, { steps: 8 });
    await expect(assistantPet.locator('[data-assistant-pet-intent="running-left"]')).toBeVisible();
    await page.mouse.up();
    await expect(assistantPet).toHaveAttribute("data-docked", "false");
    await expect(assistantPet).toHaveAttribute("data-docked", "right", { timeout: 3_000 });
    await assistantPet.click();
    const sessionAnchor = page.locator("[data-assistant-session-anchor]");
    await expect(sessionAnchor).toBeVisible();
    await expect(assistantPet).toHaveAttribute("aria-expanded", "true");
    await expect(assistantPet).toHaveAttribute("aria-controls", "telepilot-assistant-surface");
    await expect(assistantPet).toHaveAttribute("data-docked", "false");
    const wavingCanvas = assistantPet.locator('[data-assistant-pet-intent="waving"] canvas');
    await expect(wavingCanvas).toBeVisible();
    const frameSignatures: Array<{ full: number; fixedLowerBody: number; alphaPixels: number }> = [];
    for (let index = 0; index < 6; index += 1) {
      await page.waitForTimeout(150);
      frameSignatures.push(await wavingCanvas.evaluate((element) => {
        const canvas = element as HTMLCanvasElement;
        const context = canvas.getContext("2d");
        if (!context) return { full: 0, fixedLowerBody: 0, alphaPixels: 0 };
        const hash = (data: Uint8ClampedArray) => {
          let value = 2166136261;
          for (let offset = 0; offset < data.length; offset += 1) {
            value ^= data[offset];
            value = Math.imul(value, 16777619);
          }
          return value >>> 0;
        };
        const full = context.getImageData(0, 0, canvas.width, canvas.height).data;
        const fixedLowerBody = context.getImageData(0, 150, canvas.width, canvas.height - 150).data;
        let alphaPixels = 0;
        for (let offset = 3; offset < full.length; offset += 4) {
          if (full[offset] > 0) alphaPixels += 1;
        }
        return { full: hash(full), fixedLowerBody: hash(fixedLowerBody), alphaPixels };
      }));
    }
    expect(frameSignatures.every((sample) => sample.alphaPixels > 500)).toBe(true);
    expect(new Set(frameSignatures.map((sample) => sample.full)).size).toBeGreaterThan(1);
    expect(new Set(frameSignatures.map((sample) => sample.fixedLowerBody)).size).toBe(1);
    await page.waitForTimeout(2_100);
    await expect(assistantPet).toHaveAttribute("data-docked", "false");
    const activePetBox = await assistantPet.boundingBox();
    const sessionAnchorBox = await sessionAnchor.boundingBox();
    expect(Math.abs((activePetBox?.x || 0) - ((sessionAnchorBox?.x || 0) + (sessionAnchorBox?.width || 0) - 51))).toBeLessThanOrEqual(3);
    await expect(page.locator("[data-assistant-mobile-button]")).toBeHidden();
    await expect(page.locator("[data-assistant-tip]")).toHaveCount(0);
    fixture.assertClean();
  });

  test("PWA 助手会实时发现其它设备新建的会话", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "仅 PWA 视口");
    const fixture = await installApiFixture(page);
    const sessionRows = [{
      id: "session-local",
      web_user_id: 1,
      bot_tg_user_id: null,
      account_id: null,
      channel: "web",
      title: "当前会话",
      status: "active",
      created_at: "2026-07-24T00:00:00Z",
      updated_at: "2026-07-24T00:00:00Z",
    }];
    await page.route("**/api/system-agent/sessions?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(sessionRows) });
    });
    await page.route("**/api/system-agent/sessions/session-local/messages?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    });
    await page.route("**/api/system-agent/actions?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    });

    await page.goto("/ai", { waitUntil: "networkidle" });
    await page.locator("[data-assistant-mobile-button]").click();
    await page.locator("[data-assistant-composer]").getByRole("button", { name: "打开会话列表" }).click();
    sessionRows.push({
      ...sessionRows[0],
      id: "session-remote",
      title: "远端新会话",
      created_at: "2026-07-24T00:01:00Z",
      updated_at: "2026-07-24T00:01:00Z",
    });
    await expect(page.getByRole("button", { name: "远端新会话" })).toBeVisible({ timeout: 5_000 });
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
      messages.push({
        id: 31,
        session_id: session.id,
        role: "user",
        content: { text: "请继续排查日志" },
        run_status: "failed",
        error_message: "已理解你的需求，准备调用系统能力，请批准后继续。",
        retry_count: 0,
        usage: {
          tool_approval: {
            domains: ["logs"],
            tools: [{ name: "logs.recent", description: "获取最近运行日志", read_only: true, risk: "normal" }],
          },
        },
        created_at: "2026-07-24T00:00:01Z",
      } as (typeof messages)[number]);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(messages) });
    });
    await page.route("**/api/system-agent/actions?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    });
    await page.route("**/api/commands/llm-providers", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([{ ...providerFixtures[0], models: [{ id: "grok-4.20-fast", enabled: true, supports_tools: true }] }]),
      });
    });
    await page.route("**/api/system-agent/config", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          enabled: true,
          provider_id: 2,
          model: "grok-4.20-fast",
          fallback_provider_ids: [],
          require_tool_approval: false,
          max_steps: 8,
          max_tool_calls: 24,
          session_token_limit: 16_384,
        }),
      });
    });
    await page.route("**/api/system-agent/capabilities", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          enabled: true,
          provider_id: 2,
          model: "grok-4.20-fast",
          provider_name: "Grok",
          resolved_model: "grok-4.20-fast",
          ai_enabled: true,
          timezone: "Asia/Shanghai",
          tools: [],
          stage: 1,
          write_tools_available: false,
          model_matrix: [{
            provider_id: 2,
            provider_name: "Grok",
            model: "grok-4.20-fast",
            declared_supports_tools: true,
            probed_supports_tools: true,
            probed_status: "supported",
          }],
        }),
      });
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
    const conversation = surface.locator(".overflow-y-auto").filter({ hasText: "第 30 条用于撑开消息列表的测试内容。" });
    if (testInfo.project.name === "mobile") {
      await expect(surface.getByRole("heading", { name: "系统助手" })).toBeHidden();
      await expect(mobileSummary).toBeVisible();
      await expect(mobileSummary).toHaveAttribute("aria-expanded", "false");
      await expect(mobileSettings).toBeHidden();
      await expect(page.locator("[data-mobile-navigation-dock]")).toBeHidden();
      const compactPet = trigger.locator('[data-assistant-pet-intent="idle"]');
      await expect(compactPet).toBeVisible();
      await expect(compactPet.locator("canvas")).toHaveAttribute("height", "150");
      await expect(compactPet).toHaveAttribute("data-assistant-pet-compact-mode", "upper");
      const compactPetBox = await compactPet.boundingBox();
      expect(Math.round(compactPetBox?.width || 0)).toBe(65);
      expect(Math.round(compactPetBox?.height || 0)).toBe(50);
      const compactFrameSignatures: number[] = [];
      for (let index = 0; index < 26; index += 1) {
        await page.waitForTimeout(90);
        compactFrameSignatures.push(await compactPet.locator("canvas").evaluate((element) => {
          const canvas = element as HTMLCanvasElement;
          const context = canvas.getContext("2d");
          if (!context) return 0;
          const hash = (data: Uint8ClampedArray) => {
            let value = 2166136261;
            for (let offset = 0; offset < data.length; offset += 1) {
              value ^= data[offset];
              value = Math.imul(value, 16777619);
            }
            return value >>> 0;
          };
          return hash(context.getImageData(0, 0, canvas.width, canvas.height).data);
        }));
      }
      expect(new Set(compactFrameSignatures).size).toBe(2);
      await expect(trigger).not.toContainText("助手");
      await expect(trigger).toHaveAttribute("aria-expanded", "true");
      await expect(trigger).toHaveAttribute("aria-controls", "telepilot-assistant-surface");
      await expect(composer).toBeVisible();
      const triggerBox = await trigger.boundingBox();
      const initialComposerBox = await composer.boundingBox();
      const triggerGap = (initialComposerBox?.y || 0) - ((triggerBox?.y || 0) + (triggerBox?.height || 0));
      expect(triggerGap).toBeGreaterThanOrEqual(4);
      expect(triggerGap).toBeLessThanOrEqual(24);
      await mobileSummary.click();
      await expect(mobileSettings).toBeVisible();
      await mobileSettings.getByRole("button", { name: "配置" }).click();
      const configPanel = surface.locator("[data-assistant-config-panel]");
      await expect(configPanel).toBeVisible();
      await expect(mobileSettings).toBeHidden();
      await page.waitForTimeout(250);
      const configPanelBox = await configPanel.boundingBox();
      const viewportWidth = await page.evaluate(() => window.innerWidth);
      expect(Math.round((configPanelBox?.x || 0) + (configPanelBox?.width || 0))).toBe(viewportWidth);
      await surface.getByRole("button", { name: "收起配置" }).click();
      await expect(configPanel).toBeHidden();
    } else {
      await expect(surface.getByRole("heading", { name: "系统助手" })).toBeVisible();
      await expect(mobileSummary).toBeHidden();
      await expect(mobileSettings).toBeVisible();
    }
    if (testInfo.project.name === "mobile") {
      const sessionButton = composer.getByRole("button", { name: "打开会话列表" });
      const modelPicker = composer.getByRole("button", { name: "本轮模型" });
      await expect(sessionButton).toBeVisible();
      await expect(modelPicker).toBeVisible();
      const sessionButtonBox = await sessionButton.boundingBox();
      const modelPickerBox = await modelPicker.boundingBox();
      expect(modelPickerBox?.width || 0).toBeLessThanOrEqual(200);
      expect(sessionButtonBox?.x || 0).toBeLessThan(modelPickerBox?.x || 0);
      await sessionButton.click();
      const closeSessionDrawer = page.getByRole("button", { name: "关闭会话列表" });
      await expect(closeSessionDrawer).toBeVisible();
      const closeDrawerBox = await closeSessionDrawer.boundingBox();
      await closeSessionDrawer.click({ position: { x: (closeDrawerBox?.width || 430) - 12, y: 60 } });
      const approvalButton = conversation.getByRole("button", { name: "批准并继续" });
      await conversation.evaluate((element) => { element.scrollTop = element.scrollHeight; });
      await expect(approvalButton).toBeVisible();
      const approvalBox = await approvalButton.boundingBox();
      const assistantOrbBox = await trigger.boundingBox();
      expect(approvalBox?.height || 0).toBeGreaterThanOrEqual(36);
      const overlapsOrb = !(
        (approvalBox?.x || 0) + (approvalBox?.width || 0) <= (assistantOrbBox?.x || 0)
        || (assistantOrbBox?.x || 0) + (assistantOrbBox?.width || 0) <= (approvalBox?.x || 0)
        || (approvalBox?.y || 0) + (approvalBox?.height || 0) <= (assistantOrbBox?.y || 0)
        || (assistantOrbBox?.y || 0) + (assistantOrbBox?.height || 0) <= (approvalBox?.y || 0)
      );
      expect(overlapsOrb).toBe(false);
    }
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

  test("Provider Action 仅在确实缺少密钥时显示输入", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === "tablet", "桌面和手机视口已覆盖");
    const fixture = await installApiFixture(page);
    const session = {
      id: "provider-action-session",
      web_user_id: 1,
      bot_tg_user_id: null,
      account_id: null,
      channel: "web",
      title: "Provider 测活",
      status: "active",
      created_at: "2026-07-28T00:00:00Z",
      updated_at: "2026-07-28T00:00:00Z",
    };
    const baseAction = {
      session_id: session.id,
      account_id: null,
      channel: "web",
      tool_name: "providers.verify",
      arguments: { id: 4, provider_id: 4 },
      secret_fields: [],
      has_secret: false,
      risk: "normal",
      status: "pending",
      result: null,
      runtime_sync_status: "not_required",
      runtime_sync_error: null,
      expires_at: "2026-07-28T01:00:00Z",
      created_at: "2026-07-28T00:00:00Z",
      updated_at: "2026-07-28T00:00:00Z",
      executed_at: null,
    };
    const actions = [
      {
        ...baseAction,
        id: "provider-verified-create",
        tool_name: "providers.probe_and_add",
        arguments: {
          name: "api.example",
          provider: "openai",
          base_url: "https://api.example/v1",
          default_model: "chat-model",
          api_format: "chat_completions",
          has_api_key: true,
        },
        secret_fields: ["api_key"],
        has_secret: true,
        summary: "测活成功，是否添加 Provider「api.example」？",
        preview: {
          mode: "verified_create",
          provider: {
            name: "api.example",
            base_url: "https://api.example/v1",
            default_model: "chat-model",
          },
          liveness: { ok: true, model: "chat-model", latency_ms: 321 },
          note: "测活已通过，尚未保存。确认后才会添加 Provider。",
        },
        error_code: null,
        error_message: null,
      },
      {
        ...baseAction,
        id: "provider-upstream-error",
        summary: "验证 Provider #4 猫羽",
        preview: { mode: "existing", provider: { id: 4, has_api_key: true } },
        error_code: "PROVIDER_VERIFY_FAILED",
        error_message: "上游 503，已保存配置未修改，无需重新输入 API Key。",
      },
      {
        ...baseAction,
        id: "provider-auth-error",
        secret_fields: ["request_headers"],
        has_secret: true,
        summary: "验证 Provider #5 鉴权失败",
        preview: { mode: "existing", provider: { id: 5, has_api_key: true } },
        error_code: "API_KEY_REJECTED",
        error_message: "上游返回 401，请检查 API Key。",
      },
    ];
    await page.route("**/api/system-agent/sessions?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([session]) });
    });
    await page.route("**/api/system-agent/sessions/provider-action-session/messages?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    });
    await page.route("**/api/system-agent/actions?**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(actions) });
    });

    await page.goto("/ai", { waitUntil: "networkidle" });
    const assistantTrigger = testInfo.project.name === "mobile"
      ? page.locator("[data-assistant-mobile-button]")
      : page.locator("[data-assistant-desktop-pet]");
    await assistantTrigger.click();
    const surface = page.locator("[data-assistant-surface]");
    const secretInputs = surface.getByPlaceholder("api_key");
    await expect(secretInputs).toHaveCount(1);
    await expect(
      secretInputs.locator("xpath=ancestor::div[contains(@class, 'rounded-xl')][1]"),
    ).toContainText("验证 Provider #5 鉴权失败");
    const verifiedCard = surface.getByText(
      "测活成功，是否添加 Provider「api.example」？",
      { exact: true },
    ).locator("xpath=ancestor::div[contains(@class, 'rounded-xl')][1]");
    await expect(verifiedCard).toHaveCount(1);
    await expect(verifiedCard).toContainText("测活已通过，尚未保存");
    await expect(verifiedCard).toContainText("https://api.example/v1");
    await expect(verifiedCard).toContainText("chat-model");
    await expect(verifiedCard).toContainText("321 ms");
    await expect(verifiedCard.getByPlaceholder("api_key")).toHaveCount(0);
    await expect(verifiedCard.getByRole("button", { name: "确认执行" })).toBeVisible();
    const geometry = await verifiedCard.evaluate((element) => ({
      cardRight: element.getBoundingClientRect().right,
      viewportWidth: document.documentElement.clientWidth,
      documentWidth: document.documentElement.scrollWidth,
    }));
    expect(geometry.cardRight).toBeLessThanOrEqual(geometry.viewportWidth);
    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth);
    fixture.assertClean();
  });

  test("指令与任务使用独立一级页面且旧插件路径已移除", async ({ page }) => {
    const fixture = await installApiFixture(page);
    await page.goto("/operations/templates", { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { name: "指令与任务" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "自定义指令" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "定时任务" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "自动指令白名单" })).toBeVisible();
    await expect(page.getByRole("button", { name: /内置指令（只读）/ })).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByText("作用：定义能做什么", { exact: true })).toBeVisible();
    await expect(page.getByText("插件中心", { exact: true })).toHaveCount(0);
    await expect(page.locator('[data-sidebar-sort-path="/operations"]')).toContainText("指令与任务");

    const workspaceShell = page.locator("[data-page-transition-shell]");
    await expect(workspaceShell).toHaveAttribute("data-page-transition-key", "/operations");
    await workspaceShell.evaluate((element) => element.setAttribute("data-runtime-marker", "stable"));

    await page.getByRole("tab", { name: "定时任务" }).click();
    await expect(page).toHaveURL(/\/operations\/scheduler$/);
    await expect(workspaceShell).toHaveAttribute("data-runtime-marker", "stable");
    await expect(page.getByText("作用：定义何时执行", { exact: true })).toBeVisible();
    await expect(page.getByRole("combobox")).toBeVisible();

    await page.getByRole("tab", { name: "自动指令白名单" }).click();
    await expect(page).toHaveURL(/\/operations\/auto-command-whitelist$/);
    await expect(workspaceShell).toHaveAttribute("data-runtime-marker", "stable");
    await expect(page.getByText("作用：定义哪些指令允许被自动执行", { exact: true })).toBeVisible();
    await expect(page.getByLabel("页面加载中")).toHaveCount(0);

    await page.goto("/plugins/templates", { waitUntil: "networkidle" });
    await expect(page).toHaveURL(/\/plugins\/templates$/);
    await expect(page.getByRole("heading", { name: "页面已移除" })).toBeVisible();
    await page.goto("/ai", { waitUntil: "networkidle" });
    await expect(page.getByRole("button", { name: "配置系统助手" })).toHaveCount(0);
    fixture.assertClean();
  });

  test("自定义指令可在当前页按账号切换启用状态", async ({ page }) => {
    const fixture = await installApiFixture(page);
    const template = {
      id: 21,
      name: "hello",
      type: "reply_text",
      config: { text: "你好" },
      description: "测试指令",
      aliases: [],
      created_at: "2026-07-24T00:00:00Z",
    };
    let toggleMethod = "";
    await page.route("**/api/commands/templates", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([template]) });
    });
    await page.route("**/api/accounts/1/commands", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{ template, enabled: false }]) });
        return;
      }
      toggleMethod = route.request().method();
      await route.fulfill({ status: 204, body: "" });
    });
    await page.route("**/api/accounts/1/commands/21", async (route) => {
      toggleMethod = route.request().method();
      await route.fulfill({ status: 204, body: "" });
    });

    await page.goto("/operations/templates", { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "启用", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "选择要启用的账号" });
    await expect(dialog).toBeVisible();
    const accountSwitch = dialog.getByRole("switch", { name: "视觉测试账号启用hello" });
    await expect(accountSwitch).toBeEnabled();
    await accountSwitch.click();
    await expect.poll(() => toggleMethod).toBe("POST");
    await expect(page).toHaveURL(/\/operations\/templates$/);
    await expect(dialog).toBeVisible();
    fixture.assertClean();
  });

  test("打开系统助手时隐藏交互页悬浮保存按钮", async ({ page }, testInfo) => {
    const fixture = await installApiFixture(page);
    await page.goto("/interaction?aid=1", { waitUntil: "networkidle" });
    const saveButton = page.getByRole("button", { name: "保存规则" });
    await expect(saveButton).toBeVisible();
    const assistantTrigger = testInfo.project.name === "mobile"
      ? page.locator("[data-assistant-mobile-button]")
      : page.locator("[data-assistant-desktop-pet]");
    await assistantTrigger.click();
    await expect(page.locator("[data-assistant-surface]")).toBeVisible();
    await expect(saveButton).toBeHidden();
    fixture.assertClean();
  });

  test("交互规则总开关在 iPad 宽度内完整收缩", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "tablet", "仅 iPad 视口");
    const fixture = await installApiFixture(page);
    await page.goto("/interaction?aid=1", { waitUntil: "networkidle" });
    await expect(page.locator("[data-app-main] .lucide-git-fork")).toBeVisible();
    const actions = page.locator("[data-interaction-rule-actions]");
    const masterToggle = page.locator("[data-interaction-master-toggle]");
    const addRule = actions.getByRole("button", { name: "新增规则" });
    await expect(actions).toBeVisible();
    const geometry = await actions.evaluate((element) => {
      const toggle = element.querySelector<HTMLElement>("[data-interaction-master-toggle]");
      const button = element.querySelector<HTMLElement>("button");
      const actionsRect = element.getBoundingClientRect();
      const toggleRect = toggle?.getBoundingClientRect();
      const buttonRect = button?.getBoundingClientRect();
      return {
        viewportWidth: document.documentElement.clientWidth,
        documentWidth: document.documentElement.scrollWidth,
        actionsRight: actionsRect.right,
        toggleRight: toggleRect?.right ?? 0,
        buttonRight: buttonRect?.right ?? 0,
      };
    });
    await expect(masterToggle).toBeVisible();
    await expect(addRule).toBeVisible();
    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth);
    expect(geometry.toggleRight).toBeLessThanOrEqual(geometry.actionsRight + 1);
    expect(geometry.buttonRight).toBeLessThanOrEqual(geometry.actionsRight + 1);
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
    await expect(page.locator('[data-sidebar-sort-path="/interaction"] .lucide-git-fork')).toBeVisible();
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
      const headerContent = header?.firstElementChild;
      if (!headerContent) return -1;
      return Math.round(element.getBoundingClientRect().top - headerContent.getBoundingClientRect().bottom);
    });
    expect(gap).toBeGreaterThanOrEqual(16);
    fixture.assertClean();
  });

  test("资源详情归集完整项目容器并解释后台子进程", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === "tablet", "覆盖桌面和移动端视口");
    const fixture = await installApiFixture(page);
    await page.route("**/api/system/resource-dashboard", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          host: {
            cpu_percent: 12,
            memory_used_percent: 40,
            memory_total_mb: 1024,
            disk_used_percent: 25,
            disk_free_gb: 20,
            sampled_at: 1_784_635_200,
            uptime_seconds: 3_600,
          },
          main_process: { pid: 10, cpu_percent: 2, rss_mb: 128, uss_mb: 96 },
          project_total: { cpu_percent: 5.8, rss_mb: 320, uss_mb: 320 },
          app_uptime_seconds: 600,
          other_processes: [{
            pid: 12,
            name: "python3",
            role: "多进程资源跟踪器",
            cpu_percent: 0,
            rss_mb: 4,
            uss_mb: 2,
          }],
          workers: [{
            account_id: 1,
            pid: 11,
            alive: true,
            desired: "running",
            fail_count: 0,
            cpu_percent: 1,
            rss_mb: 64,
            uss_mb: 48,
          }],
          containers: [
            { id: "web", name: "telepilot-web-1", service: "web", cpu_percent: 4, memory_mb: 200, memory_limit_mb: 512, memory_percent: 39, pids: 4 },
            { id: "postgres", name: "telepilot-postgres-1", service: "postgres", cpu_percent: 1, memory_mb: 80, memory_limit_mb: 160, memory_percent: 50, pids: 7 },
            { id: "redis", name: "telepilot-redis-1", service: "redis", cpu_percent: 0.5, memory_mb: 20, memory_limit_mb: 48, memory_percent: 42, pids: 1 },
            { id: "updater", name: "telepilot-updater-1", service: "updater", cpu_percent: 0.1, memory_mb: 12, memory_limit_mb: 128, memory_percent: 9.4, pids: 1 },
            { id: "frontend", name: "telepilot-frontend-1", service: "frontend", cpu_percent: 0.2, memory_mb: 8, memory_limit_mb: 32, memory_percent: 25, pids: 2 },
          ],
          container_total: { cpu_percent: 5.8, rss_mb: 320, uss_mb: null },
          container_probe_error: null,
          container_source: "updater",
          project_total_basis: "compose_containers",
          worker_alive: 1,
          worker_desired_running: 1,
          logs: { last_5m_total: 0, last_5m_error: 0, last_5m_warn: 0 },
        }),
      });
    });

    await page.goto("/overview", { waitUntil: "networkidle" });
    const card = page.locator("[data-resource-usage-card]");
    await expect(card.getByText("全项目容器", { exact: true })).toBeVisible();
    await expect(card.getByText(/完整项目容器内存/)).toBeVisible();
    await card.getByRole("button", { name: "详情" }).click();
    for (const label of [
      "Web 后端容器",
      "PostgreSQL 容器",
      "Redis 容器",
      "Updater 更新器容器",
      "前端容器",
      "多进程资源跟踪器",
    ]) {
      await expect(page.getByText(label, { exact: true })).toBeVisible();
    }
    await expect(page.getByText(/^telepilot-web-1 · 4 PID · CPU/)).toBeVisible();
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
    const failedMetric = page.locator('[data-usage-status-filter="failed"]');
    await expect(failedMetric).toHaveCount(1);
    await failedMetric.click();
    await expect(failedMetric).toHaveAttribute("aria-pressed", "true");
    const metricBox = await failedMetric.boundingBox();
    const pillBox = await failedMetric.locator(":scope > div").boundingBox();
    expect(metricBox).not.toBeNull();
    expect(pillBox).not.toBeNull();
    expect(Math.abs((metricBox?.width ?? 0) - (pillBox?.width ?? 0))).toBeLessThan(1);
    for (const viewport of [
      { width: 375, height: 812 },
      { width: 430, height: 932 },
    ]) {
      await page.setViewportSize(viewport);
      const navigationDock = page.locator("[data-mobile-navigation-dock]");
      await expect(navigationDock).toBeVisible();
      const responsiveMetricBox = await failedMetric.boundingBox();
      const responsivePillBox = await failedMetric.locator(":scope > div").boundingBox();
      const dockBox = await navigationDock.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        return { left: rect.left, right: rect.right };
      });
      expect(Math.abs(
        (responsiveMetricBox?.width ?? 0) - (responsivePillBox?.width ?? 0),
      )).toBeLessThan(1);
      expect(dockBox.left).toBeGreaterThanOrEqual(0);
      expect(dockBox.right).toBeLessThanOrEqual(viewport.width);
    }
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
    const details = page.locator("details").filter({ hasText: "部署详情" });
    await expect(details).toBeVisible();
    const resolvedHeight = await dialog.evaluate((element) => Math.round(element.getBoundingClientRect().height));
    expect(resolvedHeight).toBeGreaterThan(0);
    expect(resolvedHeight).toBeLessThanOrEqual((page.viewportSize()?.height ?? 844) - 16);
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
