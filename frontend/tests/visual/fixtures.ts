import type { Page, Route } from "@playwright/test";
import { APP_VERSION } from "../../src/lib/version";

const emptyResourceDashboard = {
  host: {
    cpu_percent: 0,
    memory_used_percent: 0,
    memory_total_mb: 1024,
    disk_used_percent: 0,
    disk_free_gb: 100,
    sampled_at: 1_784_635_200,
    uptime_seconds: 0,
  },
  main_process: { pid: 1, cpu_percent: 0, rss_mb: 0, uss_mb: 0 },
  project_total: { cpu_percent: 0, rss_mb: 0, uss_mb: 0 },
  app_uptime_seconds: 0,
  other_processes: [],
  containers: [],
  container_total: { cpu_percent: 0, rss_mb: 0, uss_mb: 0 },
  container_probe_error: null,
  workers: [],
  worker_alive: 0,
  worker_desired_running: 0,
  logs: { last_5m_total: 0, last_5m_error: 0, last_5m_warn: 0 },
};

const emptyLedgerSummary = {
  income: "0", payout: "0", net: "0", count: 0,
  by_day: [], by_chat: [], by_recipient: [],
};

const emptyLedgerStats = {
  total: {
    started_sessions: 0, participant_count: 0, payout_success_count: 0,
    payout_failure_count: 0, payout_attempt_count: 0, payout_success_rate: null,
    ledger_income: "0", ledger_payout: "0", ledger_net: "0", ledger_count: 0,
  },
  by_day: [], by_chat: [],
  source_matrix: [],
};

const accountFixture = {
  id: 1,
  phone: "+8613800000000",
  display_name: "视觉测试账号",
  tg_user_id: 10001,
  tg_username: "visual_test",
  status: "paused",
  tags: ["fixture"],
  enabled_features: 2,
  cold_start_until: null,
  created_at: "2026-07-21T00:00:00Z",
  proxy: null,
};

const ledgerEntryFixture = {
  id: 1, source: "transfer", source_id: 101, direction: "out", amount: "88.00", signed_amount: "-88.00",
  status: "posted", account_id: 1, chat_id: -10010001, chat_title: "视觉回归测试群",
  payer_user_id: 10001, payer_name: "测试付款人", receiver_user_id: 20002, receiver_name: "测试收款人",
  receiver_username: "recipient", plugin_key: "transfer", entry_key: "reward:101", channel: "telegram",
  session_key: "fixture-session", action_type: "reward", payout_key: "fixture-payout-101", error_code: null,
  created_at: "2026-07-21T08:00:00Z", params_summary: {},
};

const ledgerCompensationFixture = {
  id: 1, payout_key: "fixture-compensation-1", account_id: 1, trace_id: "fixture-trace",
  plugin_key: "transfer", entry_key: "reward:failed:1", origin: "worker", chat_id: -10010001,
  chat_title: "视觉回归测试群", receiver_user_id: 20002, receiver_name: "测试收款人", amount: "66.00",
  status: "pending", error_code_first: "UPSTREAM_TIMEOUT", error_code_last: "UPSTREAM_TIMEOUT",
  error_last: "上游响应超时", ambiguous: false, retry_count: 2, next_attempt_at: "2026-07-21T08:05:00Z",
  sent_message_id: null, sent_at: null, notified_at: null, created_at: "2026-07-21T08:00:00Z",
  updated_at: "2026-07-21T08:01:00Z",
};

function jsonResponse(pathname: string): unknown | undefined {
  if (pathname === "/api/auth/me") return { id: 1, username: "visual-test", has_totp: false };
  if (pathname === "/api/accounts") return [accountFixture];
  if (pathname === "/api/commands/llm-providers") return [];
  if (pathname === "/api/system/resource-dashboard") return emptyResourceDashboard;
  if (pathname === "/api/system/settings") return { timezone: "Asia/Shanghai", login_security: {} };
  if (pathname === "/api/feature-matrix") return { features: [] };
  if (pathname === "/api/ledger") return { items: [ledgerEntryFixture] };
  if (pathname === "/api/ledger/summary") return emptyLedgerSummary;
  if (pathname === "/api/ledger/stats") return emptyLedgerStats;
  if (pathname === "/api/ledger/compensations") return { items: [ledgerCompensationFixture] };
  if (pathname === "/api/logs/messages" || pathname === "/api/logs/runtime" || pathname === "/api/logs/audit" || pathname === "/api/logs/trace/events") return [];
  if (pathname === "/api/logs/trace/events/" || pathname.startsWith("/api/logs/trace/events/")) return { events: [] };
  if (pathname === "/api/commands/templates" || pathname === "/api/commands/builtin") return [];
  if (pathname === "/api/llm/usage/recent") return [];
  if (pathname === "/api/commands/ai/enablement-summary") return { total_accounts: 0, enabled_accounts: 0, ai_templates: 0 };
  if (pathname === "/api/proxies") return [];
  if (pathname === "/api/plugin-repos" || pathname === "/api/plugin-repos/local/plugins" || pathname === "/api/plugin-repos/official/plugins") return [];
  if (pathname === "/api/plugins/installed-overview" || pathname === "/api/plugins/installed-packages") return [];
  if (pathname === "/api/remote-plugins") return [];
  if (pathname === "/api/system/network") return { online: true };
  if (pathname === "/api/system/version") return { version: APP_VERSION };
  if (pathname === "/api/system/kill-switch") return { enabled: false };
  if (pathname === "/api/system/health-overview") return {
    db: { ok: true }, redis: { ok: true }, alembic: { ok: true, pending: [] },
    providers: { total: 0, with_api_key: 0 }, proxies: { total: 0 },
    workers: { total: 0, by_status: {}, runtime_failing: 0, runtime_desired_running: 0, runtime_desired_running_alive: 0 },
  };
  if (pathname === "/api/accounts/1") return { ...accountFixture, notes: null, template_id: null, proxy_id: null, device_profile_id: null };
  if (pathname === "/api/accounts/1/features") return [];
  if (pathname === "/api/accounts/1/commands") return [];
  if (pathname === "/api/accounts/1/rate-limit") return { template_id: null, rules: [] };
  if (pathname === "/api/accounts/1/bot") return { account_id: 1, enabled: false, status: "stopped", has_token: false, remote_plugin_policy: { enabled: false, install: false, update: false, uninstall: false, enable_disable: false } };
  if (pathname === "/api/accounts/1/bot/users" || pathname === "/api/accounts/1/ignored-peers") return [];
  if (pathname === "/api/accounts/1/interaction-bot") return { enabled: false, trigger_text: "", response_template: "" };
  if (pathname === "/api/device-profiles") return [];
  return undefined;
}

export async function installApiFixture(page: Page): Promise<{ assertClean: () => void }> {
  const unexpected: string[] = [];
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (!pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    if (request.method() !== "GET") {
      unexpected.push(`${request.method()} ${pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (pathname.endsWith("/avatar")) {
      await route.fulfill({ status: 200, contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64") });
      return;
    }
    const response = jsonResponse(pathname);
    if (response === undefined) {
      unexpected.push(`GET ${pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(response) });
  });
  return {
    assertClean: () => {
      if (unexpected.length) throw new Error(`视觉基线触发了未声明 API 请求: ${unexpected.join(", ")}`);
    },
  };
}
