// 顶栏：移动端汉堡按钮 + 副标题（仅 sm+ 显示）+ 系统健康灯 + 更新检查 + 紧急停用 + 登出
// iOS PWA：背景色延伸到 safe-area-inset-top，并随主题同步系统状态栏底色，
// 内容区高度仍维持 56px。
import { useEffect, useState } from "react";
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
import { UpdateDialog } from "./UpdateDialog";

interface TopBarProps {
  username: string;
  onMenuClick: () => void;
  onSidebarToggle: () => void;
  sidebarCollapsed: boolean;
}

export function TopBar({
  username,
  onMenuClick,
  onSidebarToggle,
  sidebarCollapsed,
}: TopBarProps) {
  const [updateOpen, setUpdateOpen] = useState(false);
  const isStandalone = useStandaloneDisplayMode();

  return (
    <header
      className="
        app-topbar flex shrink-0 items-center justify-between
        h-[calc(5rem+env(safe-area-inset-top))]
        pt-[env(safe-area-inset-top)]
        pl-[max(1rem,env(safe-area-inset-left))]
        pr-[max(1rem,env(safe-area-inset-right))]
        md:px-8 xl:px-10
      "
    >
      <div className="flex min-w-0 items-center gap-2">
        <div className="flex min-w-0 items-center gap-2 md:hidden">
          <BrandLogo className="h-9 w-9 shrink-0 rounded-xl" />
          <div className="min-w-0">
            <div className="truncate text-base font-semibold leading-none">TelePilot</div>
            <div className="mt-0.5 truncate text-[11px] leading-none text-muted-foreground">
              管理控制台
            </div>
          </div>
          {!isStandalone ? (
            <Button
              variant="outline"
              size="sm"
              className={cn(topbarActionClass(false), "h-9 w-9 shrink-0 px-0")}
              onClick={onMenuClick}
              aria-label="打开导航菜单"
              title="打开导航菜单"
            >
              <Menu className="h-4 w-4" />
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
          <PanelLeft className="h-4 w-4" />
          <span className="sr-only">{sidebarCollapsed ? "展开侧栏" : "收起侧栏"}</span>
        </Button>
      </div>
      <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
        <HealthDot compact={isStandalone} />
        <Button
          variant="outline"
          size="sm"
          className={topbarActionClass(isStandalone)}
          onClick={() => setUpdateOpen(true)}
          aria-label="检查更新"
          title="检查更新"
        >
          <RefreshCw className="h-4 w-4" />
          {isStandalone ? null : <span className="hidden text-xs sm:inline">检查更新</span>}
        </Button>
        <UpdateDialog open={updateOpen} onOpenChange={setUpdateOpen} />
        <ThemeSwitcher compact={isStandalone} />
        <KillSwitch compact={isStandalone} />
        <span className="sr-only">当前用户：{username}</span>
      </div>
    </header>
  );
}

function topbarActionClass(compact: boolean) {
  return cn(
    "h-10 rounded-full border-0 bg-secondary text-xs shadow-none hover:bg-secondary-hover",
    compact ? "w-10 px-0" : "w-10 px-0 sm:w-auto sm:gap-2 sm:px-3",
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
          <Icon className="h-4 w-4" />
          {compact ? null : <span className="hidden text-xs sm:inline">{currentLabel}</span>}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-36">
        {options.map((item) => (
          <DropdownMenuItem
            key={item.value}
            onSelect={() => setTheme(item.value)}
            className="gap-2"
          >
            <item.icon className="h-4 w-4" />
            <span className="flex-1">{item.label}</span>
            <Check
              className={theme === item.value ? "h-4 w-4" : "h-4 w-4 opacity-0"}
            />
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
