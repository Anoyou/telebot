// PWA Service Worker 注册 + 更新提示。
// vite-plugin-pwa 在构建时把 `virtual:pwa-register` 解析为真正的注册代码；
// 类型声明在 src/vite-env.d.ts 里通过 `vite-plugin-pwa/client` 引入。
let pwaRegistration: ServiceWorkerRegistration | undefined;
const HTML_CACHE_NAME = "html";

async function warmOfflineShell(): Promise<void> {
  if (!("caches" in window)) return;
  const shellUrl = new URL("/", window.location.origin).toString();
  const response = await fetch(shellUrl, { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) return;
  const cache = await caches.open(HTML_CACHE_NAME);
  await cache.put(shellUrl, response);
}

export async function checkFrontendUpdate(): Promise<"updating" | "up_to_date" | "unsupported" | "error"> {
  if (
    typeof window === "undefined" ||
    !("serviceWorker" in navigator) ||
    !navigator.serviceWorker.controller ||
    !pwaRegistration
  ) {
    return "unsupported";
  }

  try {
    if (pwaRegistration.waiting || pwaRegistration.installing) return "updating";
    let updateFound = false;
    const onUpdateFound = () => {
      updateFound = true;
    };
    pwaRegistration.addEventListener("updatefound", onUpdateFound);
    try {
      await pwaRegistration.update();
    } finally {
      pwaRegistration.removeEventListener("updatefound", onUpdateFound);
    }
    if (updateFound || pwaRegistration.installing || pwaRegistration.waiting) {
      return "updating";
    }
    return "up_to_date";
  } catch {
    return "error";
  }
}

export function registerPWA() {
  // 服务端渲染 / 测试环境跳过
  if (typeof window === "undefined") return;

  // 动态 import 避免在没有插件的构建里直接报错
  import("virtual:pwa-register")
    .then(({ registerSW }) => {
      registerSW({
        // autoUpdate 模式不会调用 onNeedRefresh，新 SW 激活后会自动刷新页面。
        onRegisteredSW(_swUrl, registration) {
          pwaRegistration = registration;
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
