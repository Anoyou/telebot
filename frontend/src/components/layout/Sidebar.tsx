// 左侧导航：
//  - <Sidebar> 桌面端（≥md）常驻显示
//  - <MobileSidebar> 移动端通过抽屉模式呈现（Radix Dialog 实现，左侧滑入）
// 两者共享 NavList，移动端点击导航后自动关闭抽屉。
import { lazy, Suspense, useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  Boxes,
  Bug,
  Cog,
  GitFork,
  Github,
  GripVertical,
  Home,
  History,
  Inbox,
  ListTodo,
  ScrollText,
  Sparkles,
  WalletCards,
  Webhook,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { BrandLogo } from "@/components/BrandLogo";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
// DropdownMenuContent kept for Suspense fallback shell
import { cn } from "@/lib/utils";
import { formatRuntimeVersionLabel } from "@/lib/runtime-version";
import { getBackendVersion, getPlatformCapabilities, getSystemSettings, patchSystemSettings } from "@/api/system";
import { listSystemAgentActions } from "@/api/systemAgent";
import { getErrMsg } from "@/lib/api";
import {
  capabilityEnabledMap,
  filterNavByCapabilities,
  type CapabilityEnabledMap,
} from "@/lib/navigation";
const ChangelogMenu = lazy(() => import("./ChangelogMenu"));

export interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  end?: boolean;
  /** 角标查询 key；由 NavList 拉取 pending 数 */
  badgeKey?: "pending-actions";
}

// 顶层导航条目。默认落地页是插件中心；概览降权到 /overview。
export const NAV: NavItem[] = [
  { to: "/plugins", label: "插件", icon: Boxes },
  { to: "/ai", label: "AI", icon: Sparkles },
  { to: "/assistant/inbox", label: "待确认", icon: Inbox, badgeKey: "pending-actions" },
  { to: "/interaction", label: "交互", icon: GitFork },
  { to: "/operations", label: "指令与任务", icon: ListTodo },
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

export function orderNavItems(items: NavItem[], preferredOrder?: string[]): NavItem[] {
  if (!preferredOrder?.length) return items;
  const positions = new Map(preferredOrder.map((path, index) => [path, index]));
  return [...items].sort((left, right) => {
    const leftIndex = positions.get(left.to);
    const rightIndex = positions.get(right.to);
    if (leftIndex == null && rightIndex == null) return 0;
    if (leftIndex == null) return 1;
    if (rightIndex == null) return -1;
    return leftIndex - rightIndex;
  });
}

export function mobilePrimaryNavForCapabilities(enabled: CapabilityEnabledMap, preferredOrder?: string[]): NavItem[] {
  return orderNavItems(navForCapabilities(enabled).filter((item) =>
    item.to === "/plugins" ||
    item.to === "/interaction" ||
    item.to === "/ai" ||
    item.to === "/overview",
  ), preferredOrder);
}

export function mobileMoreNavForCapabilities(enabled: CapabilityEnabledMap, preferredOrder?: string[]): NavItem[] {
  const primary = new Set(mobilePrimaryNavForCapabilities(enabled, preferredOrder).map((item) => item.to));
  return orderNavItems(navForCapabilities(enabled).filter((item) => !primary.has(item.to)), preferredOrder);
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
  reorderable = false,
}: {
  collapsed?: boolean;
  onNavigate?: () => void;
  reorderable?: boolean;
}) {
  const queryClient = useQueryClient();
  const [draggedPath, setDraggedPath] = useState<string | null>(null);
  const [draftOrder, setDraftOrder] = useState<string[] | null>(null);
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
  const pendingActionsQ = useQuery({
    queryKey: ["system-agent", "actions", "pending-badge"],
    queryFn: () => listSystemAgentActions({ status: "pending", limit: 50 }),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const pendingCount = pendingActionsQ.data?.length ?? 0;
  const enabled = capabilityEnabledMap(capsQ.data, settingsQ.data?.ai_enabled ?? true);
  const savedOrder = settingsQ.data?.ui_preferences?.sidebar_order;
  const preferredOrder = draftOrder ?? savedOrder;
  const fullNavItems = orderNavItems(NAV, preferredOrder);
  const navItems = orderNavItems(
    navForCapabilities(enabled),
    preferredOrder,
  );
  const saveOrder = useMutation({
    scope: { id: "sidebar-order" },
    mutationFn: (next: string[]) => patchSystemSettings({
      ui_preferences: { sidebar_order: next },
    }),
    onSuccess: (_settings, savedOrder) => {
      setDraftOrder((current) => current?.join("|") === savedOrder.join("|") ? null : current);
      void queryClient.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (error, failedOrder) => {
      setDraftOrder((current) => current?.join("|") === failedOrder.join("|") ? null : current);
      toast.error(`侧边栏顺序保存失败：${getErrMsg(error)}`);
    },
  });

  useEffect(() => {
    setDraftOrder(null);
  }, [savedOrder?.join("|")]);

  const placeBefore = (path: string, targetPath: string) => {
    if (path === targetPath) return;
    const current = fullNavItems.map((item) => item.to);
    const next = current.filter((item) => item !== path);
    const targetIndex = next.indexOf(targetPath);
    next.splice(targetIndex < 0 ? next.length : targetIndex, 0, path);
    setDraftOrder(next);
    saveOrder.mutate(next);
  };

  return (
    <nav className="flex-1 space-y-1.5 overflow-y-auto px-4 py-3 text-sm">
      {navItems.map((item) => (
        <div
          key={item.to}
          className={cn("relative", draggedPath === item.to && "opacity-45")}
          draggable={reorderable && !collapsed}
          data-sidebar-sort-path={item.to}
          onDragStart={(event) => {
            setDraggedPath(item.to);
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", item.to);
          }}
          onDragEnd={() => setDraggedPath(null)}
          onDragOver={(event) => {
            if (reorderable && draggedPath) event.preventDefault();
          }}
          onDrop={(event) => {
            event.preventDefault();
            const source = draggedPath || event.dataTransfer.getData("text/plain");
            if (source) placeBefore(source, item.to);
            setDraggedPath(null);
          }}
        >
          {reorderable && !collapsed ? (
            <GripVertical className="pointer-events-none absolute left-1.5 top-1/2 z-10 h-4 w-4 -translate-y-1/2 text-muted-foreground/55" />
          ) : null}
          <NavLink
            to={item.to}
            end={item.end}
            onClick={onNavigate}
            aria-label={
              collapsed
                ? item.badgeKey === "pending-actions" && pendingCount > 0
                  ? `${item.label}（${pendingCount}）`
                  : item.label
                : undefined
            }
            title={collapsed ? item.label : undefined}
            className={({ isActive }) =>
              cn(
                "liquid-sidebar-link relative flex h-11 items-center gap-3 rounded-lg px-3 text-muted-foreground transition-all hover:text-accent-foreground",
                reorderable && !collapsed && "pl-7",
                collapsed && "justify-center px-0",
                isActive && "liquid-sidebar-link-active text-accent-foreground",
              )
            }
          >
            <item.icon className="h-5 w-5 shrink-0" />
            <span className={cn("truncate", collapsed && "sr-only")}>{item.label}</span>
            {item.badgeKey === "pending-actions" && pendingCount > 0 ? (
              <span
                className={cn(
                  "inline-flex min-w-[1.15rem] items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-medium leading-4 text-destructive-foreground",
                  collapsed ? "absolute right-1 top-1" : "ml-auto",
                )}
              >
                {pendingCount > 99 ? "99+" : pendingCount}
              </span>
            ) : null}
          </NavLink>
        </div>
      ))}
    </nav>
  );
}

function SidebarBody({
  collapsed = false,
  mobile = false,
  onNavigate,
}: {
  collapsed?: boolean;
  mobile?: boolean;
  onNavigate?: () => void;
}) {
  const [changelogOpen, setChangelogOpen] = useState(false);
  const versionQ = useQuery({
    queryKey: ["system", "version"],
    queryFn: getBackendVersion,
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  });
  const runtimeVersionLabel = formatRuntimeVersionLabel(
    versionQ.data,
    versionQ.isError ? "版本读取失败" : "正在读取…",
  );

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
      <NavList collapsed={collapsed} onNavigate={onNavigate} reorderable={!mobile} />
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
                "liquid-sidebar-link flex h-11 w-full items-center gap-3 rounded-lg px-3 text-left text-muted-foreground transition hover:text-accent-foreground",
                collapsed && "justify-center px-0",
              )}
              aria-label="更新日志"
              title="更新日志"
            >
              <History className="h-5 w-5 shrink-0" />
              <span className={cn("min-w-0 flex-1 truncate text-sm", collapsed && "sr-only")}>
                更新日志
              </span>
              <span
                className={cn("max-w-28 truncate text-xs font-medium", collapsed && "sr-only")}
                title={runtimeVersionLabel}
              >
                {runtimeVersionLabel}
              </span>
            </button>
          </DropdownMenuTrigger>
          {changelogOpen ? (
            <DropdownMenuContent
              side={mobile ? "top" : "right"}
              align={mobile ? "start" : "end"}
              sideOffset={10}
              collisionPadding={16}
              className="max-h-[min(72vh,34rem)] w-[min(28rem,calc(100vw-2rem))] p-0"
              style={{ overflowY: "auto" }}
            >
              <Suspense
                fallback={
                  <div className="p-4 text-sm text-muted-foreground">正在加载更新日志…</div>
                }
              >
                <ChangelogMenu />
              </Suspense>
            </DropdownMenuContent>
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
          <SidebarBody mobile onNavigate={() => onOpenChange(false)} />
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
