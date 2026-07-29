// Vite 配置：开发端口 5173；/api 代理到后端 8000；监听所有网卡以支持局域网访问；启用 PWA
import { readFile } from "node:fs/promises";
import { resolve, sep } from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import { fileURLToPath, URL } from "node:url";
import { visualizer } from "rollup-plugin-visualizer";

const repoRoot = fileURLToPath(new URL("../", import.meta.url));
const docsRoot = resolve(repoRoot, "docs");
const changelogPath = resolve(repoRoot, "CHANGELOG.md");
const runtimeDocFiles = new Set([
  "PLUGIN-AI.md",
  "PLUGIN-API-REFERENCE.md",
  "PLUGIN-CHEATSHEET.md",
  "PLUGIN-DEV-GUIDE.md",
  "PLUGIN-DEVTOOLS.md",
  "PLUGIN-HTTP.md",
  "PLUGIN-OVERVIEW.md",
  "PLUGIN-QUICKSTART.md",
  "PLUGIN-REMOTE.md",
  "PLUGIN-RULES.md",
  "PLUGIN-SAFETY.md",
  "PLUGIN-WEBHOOK-QUICKSTART.md",
  "PLATFORM-CAPABILITIES.md",
  "SECURITY-OPS.md",
]);

function runtimeMarkdownAssets(): Plugin {
  const middleware = () => async (
    req: import("node:http").IncomingMessage,
    res: import("node:http").ServerResponse,
    next: () => void,
  ) => {
    if (req.method !== "GET" && req.method !== "HEAD") return next();

    let pathname: string;
    try {
      pathname = decodeURIComponent(new URL(req.url ?? "/", "http://telepilot.local").pathname);
    } catch {
      return next();
    }

    let source: string | null = null;
    if (pathname === "/runtime-content/CHANGELOG.md") {
      source = changelogPath;
    } else if (pathname.startsWith("/runtime-content/docs/") && pathname.endsWith(".md")) {
      const relative = pathname.slice("/runtime-content/docs/".length);
      const candidate = resolve(docsRoot, relative);
      if (runtimeDocFiles.has(relative) && candidate.startsWith(`${docsRoot}${sep}`)) source = candidate;
    } else {
      return next();
    }

    if (!source) {
      res.statusCode = 404;
      return res.end("Not Found");
    }
    try {
      const content = await readFile(source);
      res.statusCode = 200;
      res.setHeader("Content-Type", "text/markdown; charset=utf-8");
      res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
      if (req.method === "HEAD") return res.end();
      return res.end(content);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        res.statusCode = 404;
        return res.end("Not Found");
      }
      return next();
    }
  };

  return {
    name: "telepilot-runtime-markdown-assets",
    configureServer(server) {
      server.middlewares.use(middleware());
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware());
    },
  };
}

export default defineConfig({
  plugins: [
    runtimeMarkdownAssets(),
    react(),
    ...(process.env.ANALYZE === "1"
      ? [visualizer({ filename: "dist/build-report.html", template: "treemap", gzipSize: true, brotliSize: true, open: false })]
      : []),
    VitePWA({
      // 自动更新：新 SW 安装好后下次启动自动激活；前端再监听 needRefresh 提示用户刷新
      registerType: "autoUpdate",
      // 我们在 src/pwa.ts 里手动 import 'virtual:pwa-register' 注册并接管更新提示，
      // 所以关掉插件的自动注入，避免双重注册。
      injectRegister: null,
      // dev 模式默认 **不启 SW**：之前一旦启用，浏览器里安装的 SW 会缓存住 dist/
      // 让 vite dev 改的源码看不见（症状：刷新后页面还是旧版本，"以为代码没生效"）。
      // 想测 PWA 安装/离线功能：跑 `pnpm build && pnpm preview` 用 prod 构建调试。
      devOptions: {
        enabled: false,
        type: "module",
        navigateFallback: "index.html",
      },
      // public 下的 PNG 图标会被 workbox globPatterns 收进 precache；
      // 这里保持为空，避免 favicon / touch icon 在 sw.js 里重复出现。
      includeAssets: [],
      manifest: {
        name: "TelePilot",
        short_name: "TelePilot",
        description: "TelePilot 管理控制台",
        lang: "zh-CN",
        start_url: "/",
        scope: "/",
        display: "standalone",
        orientation: "portrait",
        background_color: "#F2F0EC",
        theme_color: "#F2F0EC",
        icons: [
          {
            src: "/pwa-192x192.png",
            sizes: "192x192",
            type: "image/png",
            purpose: "any",
          },
          {
            src: "/pwa-512x512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "any",
          },
          {
            src: "/pwa-maskable-512x512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
      workbox: {
        // index.html 不进 precache（globPatterns 去掉 html），也不用 precache 做
        // navigateFallback。导航改为 NetworkFirst，在线启动时先拿最新 HTML，避免旧 SW
        // 继续提供过期的首帧主题和状态栏配置。注意：这只能保证页面拿到最新配置，不能
        // 改写 iOS 已经固化在现有主屏 Web App 里的安装元数据。
        navigateFallback: null,
        globPatterns: ["**/*.{js,css,ico,png,svg,webp,woff,woff2}"],
        runtimeCaching: [
          {
            // 运行时文档在线优先取最新内容；断网时回退到最近一次成功读取。
            urlPattern: ({ url }) => url.pathname.startsWith("/runtime-content/"),
            handler: "NetworkFirst",
            options: {
              cacheName: "runtime-content",
              networkTimeoutSeconds: 3,
              expiration: { maxEntries: 24, maxAgeSeconds: 7 * 24 * 60 * 60 },
            },
          },
          {
            // HTML 导航：NetworkFirst，拿最新 index.html；断网回退最近一次缓存。
            urlPattern: ({ request }) => request.mode === "navigate",
            handler: "NetworkFirst",
            options: {
              cacheName: "html",
              networkTimeoutSeconds: 3,
              expiration: { maxEntries: 8 },
            },
          },
          {
            // 静态资源：StaleWhileRevalidate（带内容 hash，可长期缓存）
            urlPattern: ({ request }) =>
              ["style", "script", "worker", "image", "font"].includes(request.destination),
            handler: "StaleWhileRevalidate",
            options: { cacheName: "assets" },
          },
        ],
      },
    }),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: true, // 监听 0.0.0.0，允许同网段设备通过 http://<本机IP>:5173 访问
    port: 5173,
    strictPort: true,
    proxy: {
      // 后端绑定 0.0.0.0（IPv4）；固定 IPv4 回环，避免 localhost 优先解析为 ::1 时代理返回 500。
      "/api": "http://127.0.0.1:8000",
    },
  },
  preview: {
    host: true,
    port: 5173,
  },
  // 把几个偏大的依赖单独拆 chunk：浏览器可缓存命中率更高，
  // 不会每次首屏都把 echarts/highlight 整个 bundle 下下来。
  // - echarts：~600KB（已通过 echarts/core 子路径 tree-shaken，但仍偏大）
  // - highlight.js + rehype-highlight：~250KB（仅 Extensions 页用）
  // - react-markdown + remark-gfm：~200KB（同 Extensions 页）
  // - radix-ui 系列：复用率高，单独成块利于 long-term cache
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ["echarts/core", "echarts/charts", "echarts/components", "echarts/renderers"],
          "markdown-core": ["react-markdown", "remark-gfm"],
          "markdown-highlight": ["rehype-highlight", "highlight.js"],
          radix: [
            "@radix-ui/react-dialog",
            "@radix-ui/react-dropdown-menu",
            "@radix-ui/react-label",
            "@radix-ui/react-slot",
            "@radix-ui/react-switch",
            "@radix-ui/react-tabs",
          ],
        },
      },
    },
  },
});
