// 顶栏：移动端汉堡按钮 + 副标题（仅 sm+ 显示）+ 系统健康灯 + 更新检查 + 紧急停用 + 登出
// iOS PWA：背景色延伸到 safe-area-inset-top，并随主题同步系统状态栏底色。
// 内容区高度约 3.25rem（相对旧版 5rem 收约 1/3）。
import { Component, lazy, Suspense, useEffect, useState, type ErrorInfo, type ReactNode } from "react";
import {
  Check,
  Menu,
  Monitor,
  Moon,
  PanelLeft,
  RefreshCw,
  Sun,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/feedback/ErrorState";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useTheme, type Theme } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { BrandLogo } from "@/components/BrandLogo";
import { HealthDot } from "@/components/HealthDot";
import { KillSwitch } from "./KillSwitch";
import { Skeleton } from "@/components/ui/misc";

const UpdateDialog = lazy(() => import("./UpdateDialog").then((module) => ({ default: module.UpdateDialog })));

interface TopBarProps {
  username: string;
  onMenuClick: () => void;
  onSidebarToggle: () => void;
  sidebarCollapsed: boolean;
  /** 点顶栏空白/品牌区时滚回主内容顶部（替代 iOS 状态栏回顶） */
  onScrollToTop?: () => void;
}

function isTopbarInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return Boolean(
    target.closest("button, a, input, select, textarea, [role='button'], [data-no-scroll-top]"),
  );
}

export function TopBar({
  username,
  onMenuClick,
  onSidebarToggle,
  sidebarCollapsed,
  onScrollToTop,
}: TopBarProps) {
  const [updateOpen, setUpdateOpen] = useState(false);
  const isStandalone = useStandaloneDisplayMode();

  return (
    <header
      className="
        app-topbar flex shrink-0 items-center justify-between
        h-[calc(3.25rem+env(safe-area-inset-top))]
        pt-[env(safe-area-inset-top)]
        pl-[max(0.75rem,env(safe-area-inset-left))]
        pr-[max(0.75rem,env(safe-area-inset-right))]
        md:px-6 xl:px-8
      "
      onClick={(event) => {
        // 点顶栏非按钮区域（含安全区/标题旁空白）回顶；按钮自己处理点击
        if (!onScrollToTop || isTopbarInteractiveTarget(event.target)) return;
        onScrollToTop();
      }}
    >
      <div className="flex min-w-0 items-center gap-1.5">
        <div className="flex min-w-0 items-center gap-1.5 md:hidden">
          <button
            type="button"
            className="flex min-w-0 max-w-full items-center gap-1.5 rounded-md text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={(event) => {
              event.stopPropagation();
              onScrollToTop?.();
            }}
            aria-label="回到页面顶部"
            title="回到顶部"
          >
            <BrandLogo className="h-6 w-6 shrink-0 rounded-lg" />
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold leading-none">TelePilot</div>
              <div className="mt-0.5 truncate text-[10px] leading-none text-muted-foreground">
                管理控制台
              </div>
            </div>
          </button>
          {!isStandalone ? (
            <Button
              variant="outline"
              size="sm"
              className={cn(topbarActionClass(false), "h-7 w-7 shrink-0 px-0")}
              onClick={onMenuClick}
              aria-label="打开导航菜单"
              title="打开导航菜单"
            >
              <Menu className="h-3.5 w-3.5" />
            </Button>
          ) : null}
        </div>
        <Button
          variant="outline"
          size="sm"
          className={cn(topbarActionClass(isStandalone), "hidden md:inline-flex")}
          onClick={onSidebarToggle}
          aria-label={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
          aria-pressed={sidebarCollapsed}
          title={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
        >
          <PanelLeft className="h-3.5 w-3.5" />
          <span className="sr-only">{sidebarCollapsed ? "展开侧栏" : "收起侧栏"}</span>
        </Button>
      </div>
      <div className={cn("flex shrink-0 items-center", isStandalone ? "gap-2" : "gap-1 sm:gap-1.5")}>
        <HealthDot compact={isStandalone} />
        <Button
          variant="outline"
          size="sm"
          className={topbarActionClass(isStandalone)}
          onClick={() => setUpdateOpen(true)}
          aria-label="检查更新"
          title="检查更新"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          {isStandalone ? null : <span className="hidden text-[11px] sm:inline">检查更新</span>}
        </Button>
        {updateOpen ? (
          <UpdateDialogErrorBoundary onClose={() => setUpdateOpen(false)}>
            <Suspense fallback={<UpdateDialogFallback onClose={() => setUpdateOpen(false)} />}>
              <UpdateDialog open={updateOpen} onOpenChange={setUpdateOpen} />
            </Suspense>
          </UpdateDialogErrorBoundary>
        ) : null}
        <ThemeSwitcher compact={isStandalone} />
        <KillSwitch compact={isStandalone} />
        <span className="sr-only">当前用户：{username}</span>
      </div>
    </header>
  );
}

class UpdateDialogErrorBoundary extends Component<
  { children: ReactNode; onClose: () => void },
  { error: unknown }
> {
  state: { error: unknown } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("Update dialog chunk failed:", error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Dialog open onOpenChange={(open) => { if (!open) this.props.onClose(); }}>
        <DialogContent className="dialog-center w-[calc(100vw-1.5rem)] max-w-md">
          <DialogHeader className="pr-6">
            <DialogTitle>检查更新</DialogTitle>
            <DialogDescription>更新面板暂时无法载入，控制台其他功能不受影响。</DialogDescription>
          </DialogHeader>
          <ErrorState
            className="min-h-28 border-0 bg-transparent py-4"
            title="更新面板载入失败"
            error="可能是网络中断或部署后资源已更新，请刷新页面后重试。"
          />
        </DialogContent>
      </Dialog>
    );
  }
}

function UpdateDialogFallback({ onClose }: { onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="dialog-center !flex h-[min(34rem,calc(100dvh-1.5rem))] w-[calc(100vw-1.5rem)] max-w-md flex-col overflow-hidden">
        <DialogHeader className="shrink-0 pr-6">
          <DialogTitle>检查更新</DialogTitle>
          <DialogDescription>正在载入更新信息。</DialogDescription>
        </DialogHeader>
        <div role="status" aria-label="更新信息加载中" className="space-y-4">
          <Skeleton className="h-10 w-full" />
          <div className="grid grid-cols-[100px_minmax(0,1fr)] gap-3">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
          <Skeleton className="h-24 w-full" />
        </div>
      </DialogContent>
    </Dialog>
  );
}

function topbarActionClass(compact: boolean) {
  return cn(
    "rounded-full border-0 bg-secondary text-[11px] shadow-none hover:bg-secondary-hover active:scale-95 motion-reduce:transform-none",
    compact ? "h-9 w-9 px-0" : "h-7 w-7 px-0 sm:w-auto sm:gap-1.5 sm:px-2.5",
  );
}

function isStandaloneDisplayMode() {
  if (typeof window === "undefined") {
    return false;
  }
  const navigatorWithStandalone = window.navigator as Navigator & {
    standalone?: boolean;
  };
  return (
    window.matchMedia?.("(display-mode: standalone)").matches === true ||
    navigatorWithStandalone.standalone === true
  );
}

function useStandaloneDisplayMode() {
  const [standalone, setStandalone] = useState(isStandaloneDisplayMode);

  useEffect(() => {
    const media = window.matchMedia?.("(display-mode: standalone)");
    if (!media) {
      return;
    }

    const update = () => setStandalone(isStandaloneDisplayMode());
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);

  return standalone;
}

function ThemeSwitcher({ compact = false }: { compact?: boolean }) {
  const { theme, resolvedTheme, setTheme } = useTheme();
  const Icon = theme === "system" ? Monitor : resolvedTheme === "dark" ? Moon : Sun;

  const options: Array<{ value: Theme; label: string; icon: typeof Sun }> = [
    { value: "light", label: "浅色", icon: Sun },
    { value: "dark", label: "深色", icon: Moon },
    { value: "system", label: "跟随系统", icon: Monitor },
  ];
  const currentLabel = options.find((item) => item.value === theme)?.label ?? "主题";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={topbarActionClass(compact)}
          aria-label="切换主题"
          title="切换主题"
        >
          <Icon className="h-3.5 w-3.5" />
          {compact ? null : <span className="hidden text-[11px] sm:inline">{currentLabel}</span>}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-36">
        {options.map((item) => (
          <DropdownMenuItem
            key={item.value}
            onSelect={() => setTheme(item.value)}
            className="gap-2"
          >
            <item.icon className="h-3.5 w-3.5" />
            <span className="flex-1">{item.label}</span>
            <Check
              className={theme === item.value ? "h-3.5 w-3.5" : "h-3.5 w-3.5 opacity-0"}
            />
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
