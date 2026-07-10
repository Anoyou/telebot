// 账号概要卡：在概览页 / 账号列表页共用
//  - 显示：头像、显示名、状态徽章、@用户名、TG 数字 ID、手机号（默认遮掩，点击切换显示）
//  - 出网通道：DIRECT / 代理 type://host:port + **真实出口国家 / IP**（取自代理 last-probe
//    缓存）。点"刷新"按钮触发一次 ``POST /api/proxies/{id}/test`` 重测；30 min 内的
//    测试结果常驻（避免每次刷新页面都打 ipinfo）。
//  - 移动端单列、每条信息一行，避免横向挤压
//  - footer 可由调用方覆盖，用于列表页放置启停 / 删除等操作
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AtSign, Globe2, Hash, Loader2, Network, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { AccountAvatar } from "@/components/AccountAvatar";
import { AccountStatusBadge } from "@/components/AccountStatusBadge";
import { MaskedPhone } from "@/components/MaskedPhone";
import { testProxy } from "@/api/proxies";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { AccountSummary, ProxySummary } from "@/api/types";

interface AccountSummaryCardProps {
  account: AccountSummary;
  /**
   * 自定义页脚（操作区或额外信息）。不传则显示默认的"已启用 X 项功能 / 详情 →"。
   * 传入空 fragment / null 则不渲染页脚区。
   */
  footer?: React.ReactNode;
  /** 显示名是否作为详情链接，默认 true */
  linkToDetail?: boolean;
  className?: string;
}

// 国家代码 → 旗 emoji；与 ProxyManager / NetworkBadge 同实现
function flagOf(country?: string | null): string {
  if (!country || country.length !== 2) return "🌐";
  const cp = (s: string) => 0x1f1e6 + (s.toUpperCase().charCodeAt(0) - 65);
  try {
    return String.fromCodePoint(cp(country[0]), cp(country[1]));
  } catch {
    return "🌐";
  }
}

// epoch 秒 → "X 分钟前 / X 小时前 / 刚刚"
function timeAgoSec(epochSec?: number | null): string {
  if (!epochSec || epochSec <= 0) return "—";
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - epochSec));
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

export function AccountSummaryCard({
  account,
  footer,
  linkToDetail = true,
  className,
}: AccountSummaryCardProps) {
  const titleText = account.display_name || `#${account.id}`;

  return (
    <Card className={cn("transition-shadow hover:shadow-md", className)}>
      <CardHeader className="space-y-2.5 pb-3">
        {/* 标题行：头像 + 显示名 + 状态徽章 */}
        <div className="flex items-start gap-3">
          <AccountAvatar
            id={account.id}
            name={account.display_name}
            username={account.tg_username}
            size={40}
          />
          <div className="flex min-w-0 flex-1 items-start justify-between gap-2">
            {linkToDetail ? (
              <Link
                to={`/accounts/${account.id}`}
                className="min-w-0 flex-1 truncate text-base font-medium hover:underline"
              >
                {titleText}
              </Link>
            ) : (
              <span className="min-w-0 flex-1 truncate text-base font-medium">
                {titleText}
              </span>
            )}
            <div className="shrink-0">
              <AccountStatusBadge status={account.status} />
            </div>
          </div>
        </div>

        {/* 元信息：每行一条，移动端不会被挤压 */}
        <div className="space-y-1.5 text-xs text-muted-foreground">
          {account.tg_username ? (
            <InfoRow icon={AtSign} mono>
              {account.tg_username}
            </InfoRow>
          ) : null}
          {account.tg_user_id != null ? (
            <InfoRow icon={Hash} mono>
              {account.tg_user_id}
            </InfoRow>
          ) : null}
          <MaskedPhone phone={account.phone} />
          <ProxyRow proxy={account.proxy} />
        </div>
      </CardHeader>
      {footer === undefined ? (
        <CardContent className="flex items-center justify-between pt-0 text-xs text-muted-foreground">
          <span>已启用 {account.enabled_features} 项功能</span>
          {linkToDetail ? (
            <Link
              to={`/accounts/${account.id}`}
              className="text-primary hover:underline"
            >
              详情 →
            </Link>
          ) : null}
        </CardContent>
      ) : footer === null ? null : (
        <CardContent className="pt-0">{footer}</CardContent>
      )}
    </Card>
  );
}

// ── 子组件 ────────────────────────────────────────────────────────────

function InfoRow({
  icon: Icon,
  mono,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  mono?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <Icon className="h-3.5 w-3.5 shrink-0" />
      <span className={cn("truncate", mono && "font-mono")}>{children}</span>
    </div>
  );
}

function ProxyRow({ proxy }: { proxy?: ProxySummary | null }) {
  const qc = useQueryClient();
  const probeMut = useMutation({
    mutationFn: (id: number) => testProxy(id),
    onSuccess: async (res) => {
      // 后端 test_proxy 成功后已写 Redis 缓存（含 country / exit_ip）；
      // 这里**等待**两条相关查询都重拉完——否则 toast 显示但 UI 还在拿旧数据
      // 让用户误以为"刷新没反应"。
      await Promise.all([
        qc.refetchQueries({ queryKey: ["accounts"] }),
        qc.refetchQueries({ queryKey: ["account"] }),  // 详情页用的
        qc.refetchQueries({ queryKey: ["proxies"] }),  // 代理库页用的
      ]);
      if (res.ok) {
        toast.success(
          `测试通过：${flagOf(res.country)} ${res.country || "?"} · ${res.latency_ms ?? "?"}ms`,
        );
      } else {
        toast.error(`测试失败：${res.error || "未知错误"}`);
      }
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  if (!proxy) {
    // 直连——账号没绑代理，走主进程默认出口（DIRECT 或全局 TG_DEFAULT_PROXY）
    return (
      <div
        className="flex min-w-0 items-center gap-1.5"
        title="该账号未绑定代理；走主进程默认出口（DIRECT 或全局 TG_DEFAULT_PROXY）"
      >
        <Globe2 className="h-3.5 w-3.5 shrink-0" />
        <span>直连出口</span>
      </div>
    );
  }
  const inLabel = proxy.label || `${proxy.host}:${proxy.port}`;
  const hasProbe =
    proxy.exit_country != null || proxy.exit_ip != null || proxy.probe_ok != null;
  return (
    <div className="space-y-0.5">
      {/* 第一行：代理入口（type · host:port） */}
      <div
        className="flex min-w-0 items-center gap-1.5"
        title={`代理 #${proxy.id} · ${proxy.type} · 入口地址`}
      >
        <Network className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate font-mono">
          <span className="text-muted-foreground/80">{proxy.type}</span>
          <span className="mx-1">·</span>
          <span>{inLabel}</span>
        </span>
      </div>
      {/* 第二行：真实出口（exit_country / exit_ip）+ 刷新按钮 */}
      <div className="flex min-w-0 items-center gap-1.5">
        <span className="ml-5 inline-flex shrink-0 items-center gap-1">
          {hasProbe && proxy.probe_ok === false ? (
            <span className="text-destructive" title="上次探测失败">
              ⚠ 探测失败
            </span>
          ) : hasProbe ? (
            <>
              <span className="text-base leading-none">
                {flagOf(proxy.exit_country)}
              </span>
              <span className="font-medium text-foreground">
                {proxy.exit_country || "?"}
              </span>
              {proxy.exit_ip ? (
                <span className="font-mono text-muted-foreground/80">
                  · {proxy.exit_ip}
                </span>
              ) : null}
            </>
          ) : (
            <span className="text-muted-foreground italic">未探测</span>
          )}
        </span>
        {proxy.probed_at ? (
          <span
            className="text-muted-foreground/70"
            title={new Date(proxy.probed_at * 1000).toLocaleString()}
          >
            · {timeAgoSec(proxy.probed_at)}
          </span>
        ) : null}
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto h-5 shrink-0 px-1 text-[11px] text-muted-foreground hover:text-foreground"
          disabled={probeMut.isPending}
          onClick={() => probeMut.mutate(proxy.id)}
          title={hasProbe ? "重新探测真实出口" : "测一下真实出口（IP / 国家）"}
        >
          {probeMut.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
        </Button>
      </div>
    </div>
  );
}
