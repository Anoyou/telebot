import assert from "node:assert/strict";
import test from "node:test";

import {
  isSupportedProxyType,
  normalizeSupportedProxyType,
  proxySelectionNeedsLoadedList,
  proxySelectionIssue,
  shouldClearCredentialsForMigration,
  visibleProxyOptions,
} from "./proxySupport.ts";

const proxies = [
  { id: 1, type: "socks5" },
  { id: 2, type: "http" },
  { id: 3, type: "mtproxy" },
];

test("只允许当前支持的代理类型进入新绑定入口", () => {
  assert.equal(isSupportedProxyType("socks5"), true);
  assert.equal(isSupportedProxyType("SOCKS5"), true);
  assert.equal(normalizeSupportedProxyType("HTTPS"), "https");
  assert.equal(isSupportedProxyType("https"), true);
  assert.equal(isSupportedProxyType("mtproxy"), false);
});

test("当前历史绑定保留为可见警示项，其他历史代理被隐藏", () => {
  assert.deepEqual(visibleProxyOptions(proxies, "3").map((proxy) => proxy.id), [1, 2, 3]);
  assert.deepEqual(visibleProxyOptions(proxies, "").map((proxy) => proxy.id), [1, 2]);
});

test("区分缺失绑定与已停用类型", () => {
  assert.equal(proxySelectionIssue(proxies, "3"), "unsupported");
  assert.equal(proxySelectionIssue(proxies, "999"), "missing");
  assert.equal(proxySelectionIssue(proxies, "1"), null);
  assert.equal(proxySelectionIssue(proxies, ""), null);
});

test("非空旧绑定必须等代理列表成功加载后才能继续", () => {
  assert.equal(proxySelectionNeedsLoadedList("3", false), true);
  assert.equal(proxySelectionNeedsLoadedList("3", true), false);
  assert.equal(proxySelectionNeedsLoadedList("", false), false);
});

test("从历史类型迁移时默认清空跨协议凭据", () => {
  assert.equal(shouldClearCredentialsForMigration("mtproxy", "socks5"), true);
  assert.equal(shouldClearCredentialsForMigration("http", "https"), false);
});
