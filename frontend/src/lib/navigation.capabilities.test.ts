import assert from "node:assert/strict";
import test from "node:test";

import type { PlatformCapabilities } from "../api/types.ts";
import {
  capabilityEnabledMap,
  filterNavByCapabilities,
  isRouteEnabledByCapabilities,
  pluginRuntimeAvailabilityLabel,
} from "./navigation.ts";

const sampleCaps = (enabled: Partial<Record<string, boolean>>): PlatformCapabilities => ({
  modules: [
    {
      key: "ai",
      label: "AI",
      desired_enabled: enabled.ai ?? true,
      generation: 0,
      runtime_state: "ready",
    },
    {
      key: "interaction_bot",
      label: "Interaction",
      desired_enabled: enabled.interaction_bot ?? true,
      generation: 0,
      runtime_state: "ready",
    },
    {
      key: "webhooks",
      label: "Webhooks",
      desired_enabled: enabled.webhooks ?? true,
      generation: 0,
      runtime_state: "ready",
    },
    {
      key: "ledger",
      label: "Ledger",
      desired_enabled: enabled.ledger ?? true,
      generation: 0,
      runtime_state: "ready",
    },
    {
      key: "dispatch_debug",
      label: "Debug",
      desired_enabled: enabled.dispatch_debug ?? true,
      generation: 0,
      runtime_state: "ready",
    },
  ],
  channels: [],
  worker_convergence: {
    total_accounts: 0,
    notified: 0,
    acked: 0,
    pending: 0,
    offline_or_timeout: 0,
  },
  cache_ready: true,
});

test("capability navigation hides disabled module routes", () => {
  const map = capabilityEnabledMap(sampleCaps({ ai: false, ledger: false }));
  const nav = filterNavByCapabilities(
    [
      { to: "/" },
      { to: "/ai" },
      { to: "/ai/liveness" },
      { to: "/ledger" },
      { to: "/plugins" },
      { to: "/settings" },
    ],
    map,
  );
  assert.deepEqual(
    nav.map((n) => n.to),
    ["/", "/plugins", "/settings"],
  );
});

test("capability map falls back to settings ai_enabled when caps missing", () => {
  const map = capabilityEnabledMap(null, false);
  assert.equal(map.ai, false);
  assert.equal(isRouteEnabledByCapabilities("/ai", map), false);
  assert.equal(isRouteEnabledByCapabilities("/plugins", map), true);
});

test("plugin runtime availability labels", () => {
  assert.equal(pluginRuntimeAvailabilityLabel("partial"), "部分可用");
  assert.equal(pluginRuntimeAvailabilityLabel("paused"), "已暂停");
  assert.equal(pluginRuntimeAvailabilityLabel("transitioning"), "等待热加载");
  assert.equal(pluginRuntimeAvailabilityLabel("ready"), "正常");
});
