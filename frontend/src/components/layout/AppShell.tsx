// 应用主框架：左侧 Sidebar（桌面）/ MobileSidebar（移动）+ 顶部 TopBar + 内容 outlet
// 高度用 100dvh：iOS Safari 浏览器模式下避免 100vh 把内容塞到地址栏后面；
//                PWA 全屏模式下行为与 100vh 一致。
import { useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { MoreHorizontal } from "lucide-react";

import {
  MobileSidebar,
  Sidebar,
  mobileMoreNavForAIState,
  mobilePrimaryNavForAIState,
} from "./Sidebar";
import { TopBar } from "./TopBar";
import { GlobalAlertBar } from "./GlobalAlertBar";
import { fetchMe } from "@/lib/auth";
import { getSystemSettings } from "@/api/system";
import { Spinner } from "@/components/ui/misc";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export function AppShell() {
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const [mobileActivePath, setMobileActivePath] = useState(location.pathname);

  useEffect(() => {
    setMobileActivePath(location.pathname);
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
  const aiEnabled = settingsQ.data?.ai_enabled ?? true;
  const mobileNavItems = mobilePrimaryNavForAIState(aiEnabled);
  const mobileMoreNavItems = mobileMoreNavForAIState(aiEnabled);
  const mobileMoreActive = mobileMoreNavItems.some((item) =>
    isMobileNavActive(item.to, item.end, mobileActivePath),
  );

  if (isLoading) {
    return (
      <div className="flex h-[100dvh] items-center justify-center">
        <Spinner className="h-6 w-6 text-primary" />
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
        <main
          className="
            app-main
            flex-1 overflow-auto
            px-4 py-5 md:px-8 md:py-7 xl:px-10
            pb-[calc(5.25rem+env(safe-area-inset-bottom))]
            sm:pb-[max(1rem,env(safe-area-inset-bottom))]
            pl-[max(1rem,env(safe-area-inset-left))]
            pr-[max(1rem,env(safe-area-inset-right))]
            md:pl-8 md:pr-8 xl:pl-10 xl:pr-10
          "
        >
          <div
            key={location.pathname}
            className="min-h-full w-full animate-page-enter"
          >
            <Outlet />
          </div>
        </main>
        <nav
          className="
            pointer-events-none fixed inset-x-0 z-40 sm:hidden
            bottom-[calc(0.75rem+env(safe-area-inset-bottom))]
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
      </div>
    </div>
  );
}

function isMobileNavActive(to: string, end: boolean | undefined, pathname: string) {
  if (end) return pathname === to;
  return pathname === to || pathname.startsWith(`${to}/`);
}
