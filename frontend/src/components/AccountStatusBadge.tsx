// 账号状态 → 中文标签 + Badge 颜色
//
// 复合状态：当全局 kill switch 开启时，所有账号的"运行中"应该被覆盖为"总闸暂停"，
// 否则用户看到 banner 红条但单条账号还是绿色，会困惑。badge 通过 react-query
// 直接查 ["system","kill-switch"] cache，跟 GlobalAlertBar / KillSwitch 共享数据。
import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import type { AccountStatus } from "@/api/types";
import { statusTone, type VisualTone } from "@/components/ui/status";

const MAP: Record<
  AccountStatus,
  { text: string }
> = {
  active: { text: "运行中" },
  paused: { text: "已暂停" },
  floodwait: { text: "FloodWait" },
  dead: { text: "异常" },
  login_required: { text: "待重登" },
};

const toneVariant: Record<VisualTone, "success" | "warn" | "destructive" | "secondary"> = {
  primary: "secondary", success: "success", warn: "warn", danger: "destructive",
  info: "secondary", neutral: "secondary",
};

interface KillSwitchState {
  enabled: boolean;
}

async function fetchKill(): Promise<KillSwitchState> {
  const { data } = await api.get<KillSwitchState>("/api/system/kill-switch");
  return data;
}

export function AccountStatusBadge({ status }: { status: AccountStatus }) {
  // 共享 cache key；GlobalAlertBar / KillSwitch 都用同一个 query，避免重复请求
  const { data: kill } = useQuery({
    queryKey: ["system", "kill-switch"],
    queryFn: fetchKill,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });

  // 总闸开 + 当前 active → 显示为"总闸暂停"，引导用户去顶部恢复
  if (kill?.enabled && status === "active") {
    return (
      <Badge variant="destructive" title="全局总闸已开启，所有账号已被暂停">
        总闸暂停
      </Badge>
    );
  }

  const cfg = MAP[status] ?? { text: status };
  return <Badge variant={toneVariant[statusTone("account", status)]}>{cfg.text}</Badge>;
}
