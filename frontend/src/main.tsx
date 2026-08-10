// 入口文件：挂载 React + Router + Query Client + Toaster
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";

import App, { AppErrorBoundary } from "./App";
import "./index.css";
import { registerPWA } from "./pwa";
import { ThemeProvider, useTheme } from "@/lib/theme";

// 全局 query client：默认 30s 缓存、失焦不刷新（避免与 401 跳转冲突）
const qc = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

function ThemedToaster() {
  const { resolvedTheme } = useTheme();
  return <Toaster richColors closeButton position="top-right" theme={resolvedTheme} />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={qc}>
        <BrowserRouter>
          <AppErrorBoundary>
            <App />
          </AppErrorBoundary>
        </BrowserRouter>
        <ThemedToaster />
      </QueryClientProvider>
    </ThemeProvider>
  </React.StrictMode>,
);

// 注册 Service Worker；开发模式由 Vite 配置关闭，生产构建负责预缓存静态资源。
registerPWA();
