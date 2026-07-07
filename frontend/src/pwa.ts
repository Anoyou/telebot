// PWA Service Worker 注册 + 更新提示。
// vite-plugin-pwa 在构建时把 `virtual:pwa-register` 解析为真正的注册代码；
// 类型声明在 src/vite-env.d.ts 里通过 `vite-plugin-pwa/client` 引入。
import { toast } from "sonner";

export function registerPWA() {
  // 服务端渲染 / 测试环境跳过
  if (typeof window === "undefined") return;

  // 动态 import 避免在没有插件的构建里直接报错
  import("virtual:pwa-register")
    .then(({ registerSW }) => {
      const updateSW = registerSW({
        // 检测到新版本：sonner 弹一个带"刷新"按钮的提示
        onNeedRefresh() {
          toast("发现新版本", {
            description: "点击刷新加载最新内容",
            duration: Infinity,
            action: {
              label: "刷新",
              onClick: () => updateSW(true),
            },
          });
        },
        // 首次缓存完成（可离线使用）
        onOfflineReady() {
          toast.success("前端资源已缓存", {
            description: "断网时仍可打开控制台页面，不代表 Bot 离线运行。",
          });
        },
        immediate: true,
      });
    })
    .catch(() => {
      // 开发环境关闭 PWA 时这里会失败，静默忽略
    });
}
