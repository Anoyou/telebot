// PWA Service Worker 注册与离线壳预热。
// vite-plugin-pwa 在构建时把 `virtual:pwa-register` 解析为真正的注册代码；
// 类型声明在 src/vite-env.d.ts 里通过 `vite-plugin-pwa/client` 引入。
const HTML_CACHE_NAME = "html";

async function warmOfflineShell(): Promise<void> {
  if (!("caches" in window)) return;
  const shellUrl = new URL("/", window.location.origin).toString();
  const response = await fetch(shellUrl, { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) return;
  const cache = await caches.open(HTML_CACHE_NAME);
  await cache.put(shellUrl, response);
}

export function registerPWA() {
  // 服务端渲染 / 测试环境跳过
  if (typeof window === "undefined") return;

  // 动态 import 避免在没有插件的构建里直接报错
  import("virtual:pwa-register")
    .then(({ registerSW }) => {
      registerSW({
        // autoUpdate 模式不会调用 onNeedRefresh，新 SW 激活后会自动刷新页面。
        onRegisteredSW() {
          void warmOfflineShell().catch(() => {
            // 在线预热失败不影响当前页；下次在线启动会再试。
          });
        },
        immediate: true,
      });
    })
    .catch(() => {
      // 开发环境关闭 PWA 时这里会失败，静默忽略
    });
}
