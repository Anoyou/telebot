import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, GripVertical, RotateCcw, Save } from "lucide-react";
import { toast } from "sonner";

import { patchSystemSettings } from "@/api/system";
import type { SystemSettings } from "@/api/types";
import { NAV } from "@/components/layout/Sidebar";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";

const DEFAULT_SIDEBAR_ORDER = NAV.map((item) => item.to);
const DEFAULT_MOBILE_ORDER = ["/plugins", "/ai", "/interaction", "/overview"];

function normalizeOrder(saved: string[] | undefined, defaults: string[]) {
  const known = new Set(defaults);
  const preferred = (saved ?? []).filter((path) => known.has(path));
  return [...preferred, ...defaults.filter((path) => !preferred.includes(path))];
}

export function NavigationPreferences({ settings }: { settings?: SystemSettings }) {
  const queryClient = useQueryClient();
  const [sidebarOrder, setSidebarOrder] = useState(DEFAULT_SIDEBAR_ORDER);
  const [mobileOrder, setMobileOrder] = useState(DEFAULT_MOBILE_ORDER);

  useEffect(() => {
    setSidebarOrder(normalizeOrder(settings?.ui_preferences?.sidebar_order, DEFAULT_SIDEBAR_ORDER));
    setMobileOrder(normalizeOrder(settings?.ui_preferences?.mobile_nav_order, DEFAULT_MOBILE_ORDER));
  }, [settings?.ui_preferences?.mobile_nav_order, settings?.ui_preferences?.sidebar_order]);

  const saveMutation = useMutation({
    mutationFn: () => patchSystemSettings({
      ui_preferences: {
        sidebar_order: sidebarOrder,
        mobile_nav_order: mobileOrder,
      },
    }),
    onSuccess: () => {
      toast.success("导航顺序已保存，侧边栏和 PWA 底栏已同步更新");
      void queryClient.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (error) => toast.error(getErrMsg(error)),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">导航顺序</CardTitle>
        <CardDescription>分别调整桌面侧边栏和 PWA 底栏页面顺序，保存后会跟随账号跨设备使用。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 lg:grid-cols-2">
          <OrderEditor title="桌面侧边栏" order={sidebarOrder} onChange={setSidebarOrder} />
          <OrderEditor title="PWA 底栏" order={mobileOrder} onChange={setMobileOrder} />
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              setSidebarOrder(DEFAULT_SIDEBAR_ORDER);
              setMobileOrder(DEFAULT_MOBILE_ORDER);
            }}
          >
            <RotateCcw className="mr-1 h-4 w-4" />恢复默认
          </Button>
          <Button type="button" loading={saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            {!saveMutation.isPending ? <Save className="mr-1 h-4 w-4" /> : null}保存顺序
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function OrderEditor({
  title,
  order,
  onChange,
}: {
  title: string;
  order: string[];
  onChange: (next: string[]) => void;
}) {
  const [draggedPath, setDraggedPath] = useState<string | null>(null);
  const navByPath = new Map(NAV.map((item) => [item.to, item]));
  const move = (path: string, offset: number) => {
    const index = order.indexOf(path);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= order.length) return;
    const next = [...order];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };
  const placeBefore = (path: string, targetPath: string) => {
    if (path === targetPath) return;
    const next = order.filter((item) => item !== path);
    next.splice(next.indexOf(targetPath), 0, path);
    onChange(next);
  };

  return (
    <div className="rounded-lg border border-border/70 bg-muted/15 p-3">
      <div className="mb-2 text-sm font-medium">{title}</div>
      <div className="space-y-1.5">
        {order.map((path, index) => {
          const item = navByPath.get(path);
          if (!item) return null;
          return (
            <div
              key={path}
              draggable
              onDragStart={() => setDraggedPath(path)}
              onDragEnd={() => setDraggedPath(null)}
              onDragOver={(event) => event.preventDefault()}
              onDrop={() => {
                if (draggedPath) placeBefore(draggedPath, path);
                setDraggedPath(null);
              }}
              className={cn(
                "flex min-h-11 items-center gap-2 rounded-md border bg-background px-2",
                draggedPath === path && "opacity-50",
              )}
            >
              <GripVertical className="h-4 w-4 cursor-grab text-muted-foreground" />
              <item.icon className="h-4 w-4 text-primary" />
              <span className="min-w-0 flex-1 truncate text-sm">{item.label}</span>
              <Button type="button" variant="ghost" size="icon" className="h-9 w-9" disabled={index === 0} onClick={() => move(path, -1)} aria-label={`上移${item.label}`}>
                <ArrowUp className="h-4 w-4" />
              </Button>
              <Button type="button" variant="ghost" size="icon" className="h-9 w-9" disabled={index === order.length - 1} onClick={() => move(path, 1)} aria-label={`下移${item.label}`}>
                <ArrowDown className="h-4 w-4" />
              </Button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
