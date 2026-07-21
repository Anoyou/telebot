// 左侧导航：
//  - <Sidebar> 桌面端（≥md）常驻显示
//  - <MobileSidebar> 移动端通过抽屉模式呈现（Radix Dialog 实现，左侧滑入）
// 两者共享 NavList，移动端点击导航后自动关闭抽屉。
import { lazy, Suspense, useState } from "react";
import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  Boxes,
  Bot,
  Bug,
  Cog,
  Github,
  Home,
  ScrollText,
  Sparkles,
  WalletCards,
  Webhook,
  X,
} from "lucide-react";
import { BrandLogo } from "@/components/BrandLogo";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
// DropdownMenuContent kept for Suspense fallback shell
import { cn } from "@/lib/utils";
import { APP_VERSION_LABEL } from "@/lib/version";
import { getPlatformCapabilities, getSystemSettings } from "@/api/system";
import {
  capabilityEnabledMap,
  filterNavByCapabilities,
  type CapabilityEnabledMap,
} from "@/lib/navigation";
const ChangelogMenu = lazy(() => import("./ChangelogMenu"));

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  end?: boolean;
}

// 顶层导航条目。默认落地页是插件中心；概览降权到 /overview。
const NAV: NavItem[] = [
  { to: "/plugins", label: "插件", icon: Boxes },
  { to: "/ai", label: "AI", icon: Sparkles },
  { to: "/interaction", label: "交互", icon: Bot },
  { to: "/overview", label: "概览", icon: Home },
  { to: "/ledger", label: "资金台账", icon: WalletCards },
  { to: "/webhooks", label: "入站 Webhook", icon: Webhook },
  { to: "/dispatch-debug", label: "命中调试", icon: Bug },
  { to: "/logs", label: "日志", icon: ScrollText },
  { to: "/settings", label: "系统", icon: Cog },
];

/** @deprecated 使用 navForCapabilities；保留兼容导出 */
function navForAIState(aiEnabled: boolean): NavItem[] {
  return filterNavByCapabilities(NAV, { ai: aiEnabled });
}

export function navForCapabilities(enabled: CapabilityEnabledMap): NavItem[] {
  return filterNavByCapabilities(NAV, enabled);
}

export function mobilePrimaryNavForCapabilities(enabled: CapabilityEnabledMap): NavItem[] {
  return navForCapabilities(enabled).filter((item) =>
    item.to === "/plugins" ||
    item.to === "/interaction" ||
    item.to === "/ai" ||
    item.to === "/overview",
  );
}

export function mobileMoreNavForCapabilities(enabled: CapabilityEnabledMap): NavItem[] {
  const primary = new Set(mobilePrimaryNavForCapabilities(enabled).map((item) => item.to));
  return navForCapabilities(enabled).filter((item) => !primary.has(item.to));
}

/** @deprecated 兼容旧调用 */
export function mobilePrimaryNavForAIState(aiEnabled: boolean): NavItem[] {
  return mobilePrimaryNavForCapabilities({ ai: aiEnabled });
}

/** @deprecated 兼容旧调用 */
export function mobileMoreNavForAIState(aiEnabled: boolean): NavItem[] {
  return mobileMoreNavForCapabilities({ ai: aiEnabled });
}

// 避免 unused 警告：仍可能被外部测试引用
void navForAIState;

function NavList({
  collapsed = false,
  onNavigate,
}: {
  collapsed?: boolean;
  onNavigate?: () => void;
}) {
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
  const enabled = capabilityEnabledMap(capsQ.data, settingsQ.data?.ai_enabled ?? true);
  const navItems = navForCapabilities(enabled);

  return (
    <nav className="flex-1 space-y-1.5 overflow-y-auto px-4 py-3 text-sm">
      {navItems.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          onClick={onNavigate}
          aria-label={collapsed ? item.label : undefined}
          title={collapsed ? item.label : undefined}
          className={({ isActive }) =>
            cn(
              "liquid-sidebar-link flex h-11 items-center gap-3 rounded-lg px-3 text-muted-foreground transition-all hover:text-accent-foreground",
              collapsed && "justify-center px-0",
              isActive && "liquid-sidebar-link-active text-accent-foreground",
            )
          }
        >
          <item.icon className="h-5 w-5 shrink-0" />
          <span className={cn("truncate", collapsed && "sr-only")}>{item.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}

function SidebarBody({
  collapsed = false,
  onNavigate,
}: {
  collapsed?: boolean;
  onNavigate?: () => void;
}) {
  const [changelogOpen, setChangelogOpen] = useState(false);

  return (
    <>
      <div
        className={cn(
          "liquid-sidebar-header flex h-24 shrink-0 items-center px-5",
          collapsed && "justify-center px-3",
        )}
      >
        <div className="flex min-w-0 items-center gap-3">
          <div className="grid h-10 w-10 shrink-0 place-items-center">
            <BrandLogo className="h-10 w-10 shadow-sm" />
          </div>
          <div className={cn("min-w-0", collapsed && "sr-only")}>
            <div className="truncate text-[1.55rem] font-bold leading-none tracking-tight">
              TelePilot
            </div>
            <div className="mt-1 text-xs font-medium text-muted-foreground">
              Telegram 控制台
            </div>
          </div>
        </div>
      </div>
      <NavList collapsed={collapsed} onNavigate={onNavigate} />
      <div
        className={cn(
          "liquid-sidebar-footer shrink-0 space-y-2 px-4 py-5 text-sm text-muted-foreground",
          collapsed && "px-3",
        )}
      >
        <a
          href="https://github.com/Anoyou/Telebot"
          target="_blank"
          rel="noreferrer"
          className={cn(
            "liquid-sidebar-link flex h-11 items-center gap-3 rounded-lg px-3 transition-all hover:text-accent-foreground",
            collapsed && "justify-center px-0",
          )}
          aria-label="TelePilot GitHub"
          title="TelePilot GitHub"
        >
          <Github className="h-5 w-5 shrink-0" />
          <span className={cn("truncate", collapsed && "sr-only")}>TelePilot</span>
        </a>
        <DropdownMenu modal={false} open={changelogOpen} onOpenChange={setChangelogOpen}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className={cn(
                "truncate rounded-lg px-3 py-2 text-left text-xs font-medium text-muted-foreground transition hover:bg-accent hover:text-foreground",
                collapsed && "px-0 text-center",
              )}
            >
              {collapsed ? APP_VERSION_LABEL.replace(/^v/i, "") : APP_VERSION_LABEL}
            </button>
          </DropdownMenuTrigger>
          {changelogOpen ? (
            <Suspense
              fallback={
                <DropdownMenuContent
                  side="right"
                  align="end"
                  sideOffset={10}
                  className="w-[min(28rem,calc(100vw-2rem))] p-4 text-sm text-muted-foreground"
                >
                  正在加载更新日志…
                </DropdownMenuContent>
              }
            >
              <ChangelogMenu />
            </Suspense>
          ) : null}
        </DropdownMenu>
      </div>
    </>
  );
}

// 桌面常驻侧栏：< md 隐藏，由 MobileSidebar 接管
export function Sidebar({ collapsed = false }: { collapsed?: boolean }) {
  return (
    <aside
      className={cn(
        "liquid-glass liquid-sidebar hidden shrink-0 flex-col md:flex",
        collapsed ? "w-[5.5rem]" : "w-[18rem]",
      )}
    >
      <SidebarBody collapsed={collapsed} />
    </aside>
  );
}

interface MobileSidebarProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

// 移动端抽屉：从左滑入。点击导航链接自动关闭；点击遮罩 / Esc / 关闭按钮也会关闭。
// 动画用纯 CSS transition（不依赖 tailwindcss-animate 插件）。
export function MobileSidebar({ open, onOpenChange }: MobileSidebarProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-black/60 transition-opacity duration-200 md:hidden",
            "data-[state=closed]:pointer-events-none data-[state=closed]:opacity-0 data-[state=open]:opacity-100",
          )}
        />
        <DialogPrimitive.Content
          className={cn(
            "liquid-glass liquid-sidebar liquid-sidebar-drawer inset-y-0 left-0 z-[60] flex w-64 max-w-[80vw] flex-col md:hidden",
            // 安全区适配：iPhone 横屏时左侧刘海，全屏 PWA 顶/底状态栏区
            "pl-[env(safe-area-inset-left)] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]",
            "data-[state=closed]:pointer-events-none",
          )}
          // 屏幕阅读器需要 Title；视觉上隐藏
          aria-describedby={undefined}
        >
          <DialogPrimitive.Title className="sr-only">导航菜单</DialogPrimitive.Title>
          <DialogPrimitive.Close
            className="absolute right-3 top-[calc(env(safe-area-inset-top)+0.75rem)] rounded-lg p-2 text-muted-foreground hover:bg-accent hover:text-foreground"
            aria-label="关闭菜单"
          >
            <X className="h-4 w-4" />
          </DialogPrimitive.Close>
          <SidebarBody onNavigate={() => onOpenChange(false)} />
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
