// 应用主框架：左侧 Sidebar（桌面）/ MobileSidebar（移动）+ 顶部 TopBar + 内容 outlet
// 高度用 100dvh：iOS Safari 浏览器模式下避免 100vh 把内容塞到地址栏后面；
//                PWA 全屏模式下行为与 100vh 一致。
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { MoreHorizontal } from "lucide-react";

import {
  MobileSidebar,
  Sidebar,
  mobileMoreNavForCapabilities,
  mobilePrimaryNavForCapabilities,
} from "./Sidebar";
import { TopBar } from "./TopBar";
import { GlobalAlertBar } from "./GlobalAlertBar";
import { AssistantDockProvider, useAssistantDock } from "@/components/assistant/AssistantDock";
import { fetchMe } from "@/lib/auth";
import { getPlatformCapabilities, getSystemSettings } from "@/api/system";
import { capabilityEnabledMap } from "@/lib/navigation";
import { Skeleton } from "@/components/ui/misc";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { APP_VERSION_LABEL } from "@/lib/version";

const AssistantIndex = lazy(() => import("@/pages/Assistant/Index").then((module) => ({ default: module.AssistantIndex })));

type MobileScrollEdge = "top" | "bottom" | null;

export function AppShell() {
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileScrollEdge, setMobileScrollEdge] = useState<MobileScrollEdge>(null);
  const mainRef = useRef<HTMLElement>(null);
  const hasScrolledMainRef = useRef(false);
  const mobileScrollEdgeRef = useRef<MobileScrollEdge>(null);
  const mobileScrollEdgeTimerRef = useRef<number | null>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const [mobileActivePath, setMobileActivePath] = useState(location.pathname);
  const pageTransitionKey = location.pathname === "/plugins" || location.pathname.startsWith("/plugins/")
    ? "/plugins"
    : location.pathname;

  useEffect(() => {
    setMobileActivePath(location.pathname);
    setMobileScrollEdge(null);
    mobileScrollEdgeRef.current = null;
    hasScrolledMainRef.current = false;
    if (mobileScrollEdgeTimerRef.current != null) {
      window.clearTimeout(mobileScrollEdgeTimerRef.current);
      mobileScrollEdgeTimerRef.current = null;
    }
  }, [location.pathname]);

  // 主体框架内顺手取一次当前用户用于顶栏展示
  const { data, isLoading } = useQuery({
    queryKey: ["auth", "me"],
    queryFn: fetchMe,
  });
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
    staleTime: 30_000,
  });
  const capsQ = useQuery({
    queryKey: ["system", "capabilities"],
    queryFn: getPlatformCapabilities,
    staleTime: 15_000,
  });

  useEffect(() => {
    const main = mainRef.current;
    if (isLoading || !main) return;

    const revealMobileScrollEdge = (edge: Exclude<MobileScrollEdge, null>) => {
      mobileScrollEdgeRef.current = edge;
      setMobileScrollEdge(edge);
      if (mobileScrollEdgeTimerRef.current != null) {
        window.clearTimeout(mobileScrollEdgeTimerRef.current);
      }
      mobileScrollEdgeTimerRef.current = window.setTimeout(() => {
        mobileScrollEdgeRef.current = null;
        setMobileScrollEdge(null);
        mobileScrollEdgeTimerRef.current = null;
      }, 900);
    };

    const updateMobileScrollEdge = () => {
      if (!window.matchMedia("(max-width: 639px)").matches) {
        setMobileScrollEdge(null);
        return;
      }

      const maxScrollTop = main.scrollHeight - main.clientHeight;
      if (maxScrollTop <= 2) {
        setMobileScrollEdge(null);
        return;
      }

      if (main.scrollTop > 8) {
        hasScrolledMainRef.current = true;
      }
      if (!hasScrolledMainRef.current) {
        setMobileScrollEdge(null);
        return;
      }

      if (main.scrollTop <= 2) {
        if (mobileScrollEdgeRef.current !== "top") revealMobileScrollEdge("top");
      } else if (main.scrollTop >= maxScrollTop - 2) {
        if (mobileScrollEdgeRef.current !== "bottom") revealMobileScrollEdge("bottom");
      } else {
        mobileScrollEdgeRef.current = null;
        setMobileScrollEdge(null);
      }
    };

    main.addEventListener("scroll", updateMobileScrollEdge, { passive: true });
    window.addEventListener("resize", updateMobileScrollEdge);
    updateMobileScrollEdge();
    return () => {
      main.removeEventListener("scroll", updateMobileScrollEdge);
      window.removeEventListener("resize", updateMobileScrollEdge);
      if (mobileScrollEdgeTimerRef.current != null) {
        window.clearTimeout(mobileScrollEdgeTimerRef.current);
        mobileScrollEdgeTimerRef.current = null;
      }
    };
  }, [isLoading]);

  const enabled = capabilityEnabledMap(capsQ.data, settingsQ.data?.ai_enabled ?? true);
  const mobileNavItems = mobilePrimaryNavForCapabilities(enabled);
  const mobileMoreNavItems = mobileMoreNavForCapabilities(enabled);
  const mobileMoreActive = mobileMoreNavItems.some((item) =>
    isMobileNavActive(item.to, item.end, mobileActivePath),
  );

  if (isLoading) {
    return (
      <div role="status" aria-label="工作台加载中" className="flex h-[100dvh] flex-col bg-background">
        <div className="flex h-14 items-center gap-3 border-b px-4 sm:px-6">
          <Skeleton className="h-8 w-8 rounded-lg" />
          <Skeleton className="h-4 w-28" />
          <Skeleton className="ml-auto h-8 w-20 rounded-md" />
        </div>
        <div className="flex min-h-0 flex-1 gap-4 p-4 sm:p-6">
          <div className="hidden w-56 space-y-3 md:block"><Skeleton className="h-10 w-full rounded-md" />{[0, 1, 2, 3, 4].map((item) => <Skeleton key={item} className="h-9 w-full rounded-md" />)}</div>
          <div className="min-w-0 flex-1 space-y-4"><Skeleton className="h-36 w-full rounded-lg" /><div className="grid gap-4 md:grid-cols-3">{[0, 1, 2].map((item) => <Skeleton key={item} className="h-28 rounded-lg" />)}</div></div>
        </div>
      </div>
    );
  }

  return (
    <div className="app-frame flex h-[100dvh] w-full overflow-hidden bg-background">
      <Sidebar collapsed={sidebarCollapsed} />
      <MobileSidebar open={mobileNavOpen} onOpenChange={setMobileNavOpen} />
      <div className="app-workspace flex min-w-0 flex-1 flex-col overflow-hidden">
        <TopBar
          username={data?.username ?? "未知用户"}
          onMenuClick={() => setMobileNavOpen(true)}
          onSidebarToggle={() => setSidebarCollapsed((value) => !value)}
          sidebarCollapsed={sidebarCollapsed}
        />
        {/* kill switch 开启时显示全局红色横幅；关闭时不渲染 */}
        <GlobalAlertBar />
        <AssistantDockProvider>
          <div className="relative flex min-h-0 flex-1 flex-col">
            <main
              ref={mainRef}
              data-app-main
              className="
                app-main
                relative flex-1 overflow-auto
                px-4 py-4 md:px-8 md:py-7 xl:px-10
                pb-[calc(5.25rem+env(safe-area-inset-bottom))]
                sm:pb-[max(1rem,env(safe-area-inset-bottom))]
                pl-[max(1rem,env(safe-area-inset-left))]
                pr-[max(1rem,env(safe-area-inset-right))]
                md:pl-8 md:pr-8 xl:pl-10 xl:pr-10
              "
            >
              <MobileScrollEdgeLabel edge="top" visible={mobileScrollEdge === "top"} />
              <div
                key={pageTransitionKey}
                className="min-h-full w-full animate-page-enter"
              >
                <Outlet />
              </div>
              <MobileScrollEdgeLabel edge="bottom" visible={mobileScrollEdge === "bottom"} />
            </main>
            <AssistantSurface />
          </div>
          <nav
            className="
              pointer-events-none fixed inset-x-0 z-40 sm:hidden
              bottom-[env(safe-area-inset-bottom)]
              px-[max(0.75rem,env(safe-area-inset-left))]
            "
          >
            <div
              className="liquid-bottom-nav pointer-events-auto mx-auto grid h-[3.75rem] w-full max-w-sm gap-1 px-2 py-2"
              style={{ gridTemplateColumns: `repeat(${mobileNavItems.length + 1}, minmax(0, 1fr))` }}
            >
              {mobileNavItems.map((item) => {
              const active = isMobileNavActive(item.to, item.end, mobileActivePath);
              const activate = () => {
                flushSync(() => setMobileActivePath(item.to));
              };
              return (
                <button
                  key={item.to}
                  type="button"
                  onPointerDown={activate}
                  onTouchStart={activate}
                  onMouseDown={activate}
                  onClick={() => {
                    activate();
                    navigate(item.to);
                  }}
                  aria-current={active ? "page" : undefined}
                  data-active={active ? "true" : undefined}
                  className={cn(
                    "liquid-nav-item flex min-w-0 flex-col items-center justify-center gap-0.5 rounded-full text-[10px] font-semibold text-muted-foreground transition-none",
                    active && "liquid-nav-item-active",
                  )}
                  style={{
                    WebkitTapHighlightColor: "transparent",
                    backgroundColor: active ? "hsl(var(--foreground))" : undefined,
                    color: active ? "hsl(var(--background))" : undefined,
                  }}
                >
                  <item.icon className="h-4 w-4 shrink-0" />
                  <span className="max-w-full truncate">{item.label}</span>
                </button>
              );
              })}
              <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  aria-label="更多导航"
                  data-active={mobileMoreActive ? "true" : undefined}
                  className={cn(
                    "liquid-nav-item flex min-w-0 flex-col items-center justify-center gap-0.5 rounded-full text-[10px] font-semibold text-muted-foreground transition-none",
                    mobileMoreActive && "liquid-nav-item-active",
                  )}
                  style={{
                    WebkitTapHighlightColor: "transparent",
                    backgroundColor: mobileMoreActive ? "hsl(var(--foreground))" : undefined,
                    color: mobileMoreActive ? "hsl(var(--background))" : undefined,
                  }}
                >
                  <MoreHorizontal className="h-4 w-4 shrink-0" />
                  <span className="max-w-full truncate">更多</span>
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                side="top"
                align="end"
                sideOffset={12}
                collisionPadding={12}
                className="mb-2 w-56 p-1.5"
              >
                {mobileMoreNavItems.map((item) => {
                  const active = isMobileNavActive(item.to, item.end, mobileActivePath);
                  return (
                    <DropdownMenuItem
                      key={item.to}
                      onClick={() => {
                        flushSync(() => setMobileActivePath(item.to));
                        navigate(item.to);
                      }}
                      className={cn("min-h-11 gap-3 rounded-lg px-3 text-sm", active && "bg-accent text-accent-foreground")}
                    >
                      <item.icon className="h-4 w-4 shrink-0" />
                      <span className="truncate">{item.label}</span>
                    </DropdownMenuItem>
                  );
                })}
              </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </nav>
        </AssistantDockProvider>
      </div>
    </div>
  );
}

function AssistantSurface() {
  const { collapsed, mounted } = useAssistantDock();
  const surfaceRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const main = document.querySelector<HTMLElement>("[data-app-main]");
    if (!main) return;
    if (collapsed) {
      main.removeAttribute("inert");
      main.removeAttribute("aria-hidden");
      return;
    }
    main.setAttribute("inert", "");
    main.setAttribute("aria-hidden", "true");
    surfaceRef.current?.focus({ preventScroll: true });
    return () => {
      main.removeAttribute("inert");
      main.removeAttribute("aria-hidden");
    };
  }, [collapsed]);

  if (!mounted) return null;

  return (
    <section
      ref={surfaceRef}
      tabIndex={-1}
      data-assistant-surface
      aria-label="系统助手"
      aria-hidden={collapsed}
      className={cn(
        "absolute inset-0 z-30 overflow-y-auto bg-background px-4 py-4 pb-[calc(5.25rem+env(safe-area-inset-bottom))] transition-[opacity,visibility] duration-150 md:px-8 md:py-7 xl:px-10",
        collapsed ? "invisible pointer-events-none opacity-0" : "visible opacity-100",
      )}
    >
      <Suspense fallback={<AssistantSurfaceSkeleton />}>
        <AssistantIndex />
      </Suspense>
    </section>
  );
}

function AssistantSurfaceSkeleton() {
  return (
    <div role="status" aria-label="系统助手加载中" className="space-y-4">
      <div className="flex items-center gap-3 rounded-lg border border-border/70 bg-card p-4">
        <div className="skeleton-shimmer h-10 w-10 rounded-lg" />
        <div className="min-w-0 flex-1 space-y-2">
          <div className="skeleton-shimmer h-5 w-28 rounded-md" />
          <div className="skeleton-shimmer h-3 w-[min(28rem,80%)] rounded-md" />
        </div>
        <div className="skeleton-shimmer h-9 w-9 rounded-md" />
      </div>
      <div className="grid min-h-[60vh] gap-3 overflow-hidden rounded-xl border bg-card md:grid-cols-[15rem_minmax(0,1fr)]">
        <div className="hidden space-y-3 border-r p-3 md:block">
          <div className="skeleton-shimmer h-9 w-full rounded-md" />
          {[0, 1, 2, 3].map((item) => <div key={item} className="skeleton-shimmer h-10 w-full rounded-md" />)}
        </div>
        <div className="flex min-w-0 flex-col gap-4 p-4">
          <div className="flex items-end gap-3"><div className="skeleton-shimmer h-12 w-3/5 rounded-2xl" /></div>
          <div className="flex items-end justify-end gap-3"><div className="skeleton-shimmer h-10 w-2/5 rounded-2xl" /></div>
          <div className="mt-auto space-y-2 rounded-xl border p-2"><div className="skeleton-shimmer h-16 w-full rounded-lg" /><div className="flex justify-end"><div className="skeleton-shimmer h-8 w-8 rounded-md" /></div></div>
        </div>
      </div>
    </div>
  );
}

function MobileScrollEdgeLabel({
  edge,
  visible,
}: {
  edge: Exclude<MobileScrollEdge, null>;
  visible: boolean;
}) {
  return (
    <div
      aria-hidden="true"
      data-edge={edge}
      data-visible={visible ? "true" : "false"}
      className="mobile-scroll-edge sm:hidden"
    >
      <span className="mobile-scroll-edge-label">
        <span className="font-semibold text-foreground">TelePilot</span>
        <span className="mobile-scroll-edge-separator" />
        <span className="tabular-nums">{APP_VERSION_LABEL}</span>
      </span>
    </div>
  );
}

function isMobileNavActive(to: string, end: boolean | undefined, pathname: string) {
  if (end) return pathname === to;
  return pathname === to || pathname.startsWith(`${to}/`);
}
