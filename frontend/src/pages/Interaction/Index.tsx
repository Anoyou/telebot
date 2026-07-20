import { useEffect, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bot,
  MessageSquare,
  RefreshCw,
  Route,
  ScrollText,
} from "lucide-react";

import { getInteractionBotConfig } from "@/api/accountBots";
import { listAccounts } from "@/api/accounts";
import type {
  AccountBotInteractionConfig,
  AccountBotInteractionRule,
  AccountSummary,
} from "@/api/types";
import { AccountStatusBadge } from "@/components/AccountStatusBadge";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/misc";
import { Select } from "@/components/ui/select";
import { SignalPill } from "@/components/ui/status";
import { BotTab } from "@/pages/Accounts/BotTab";

function accountOptionLabel(account: AccountSummary): string {
  const name = account.display_name?.trim();
  const username = account.tg_username?.trim();
  return name || (username ? `@${username}` : null) || `账号 #${account.id}`;
}

function countRuleChats(rules: AccountBotInteractionRule[]): number {
  return new Set(
    rules.flatMap((rule) =>
      (rule.chat_ids ?? []).filter((chatId): chatId is number => Number.isFinite(chatId)),
    ),
  ).size;
}

function runtimeLabel(config?: AccountBotInteractionConfig): string {
  if (!config) return "读取中";
  if (config.interaction_running) return "运行中";
  if (config.enabled) return "未运行";
  return "未启用";
}

function runtimeTone(config?: AccountBotInteractionConfig): "success" | "warn" | "neutral" {
  if (config?.interaction_running) return "success";
  if (config?.enabled) return "warn";
  return "neutral";
}

export function InteractionIndex() {
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: () => listAccounts(),
  });

  const accounts = accountsQ.data ?? [];
  const rawAidParam = searchParams.get("aid") ?? "";
  const parsedAidParam = Number(rawAidParam);
  const aidParam = /^\d+$/.test(rawAidParam) && Number.isSafeInteger(parsedAidParam) && parsedAidParam > 0
    ? parsedAidParam
    : null;
  const selectedAccount = useMemo(() => {
    if (!accounts.length) return null;
    const byParam = accounts.find((account) => account.id === aidParam);
    return byParam ?? accounts[0];
  }, [accounts, aidParam]);
  const selectedAid = selectedAccount?.id ?? null;

  useEffect(() => {
    if (!selectedAid) return;
    if (selectedAid === aidParam) return;
    const next = new URLSearchParams(searchParams);
    next.set("aid", String(selectedAid));
    setSearchParams(next, { replace: true });
  }, [aidParam, searchParams, selectedAid, setSearchParams]);

  const interactionQ = useQuery({
    queryKey: ["account", selectedAid, "interaction-bot"],
    queryFn: () => getInteractionBotConfig(selectedAid as number),
    enabled: selectedAid !== null,
  });

  const config = interactionQ.data;
  const rules = config?.rules ?? [];
  const activeRules = rules.filter((rule) => rule.enabled).length;
  const chatCoverage = countRuleChats(rules);
  const hasInteractionToken = Boolean(config?.has_interaction_bot_token);
  const lastError = config?.interaction_last_error?.trim();

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    if (selectedAid !== null) {
      void queryClient.invalidateQueries({ queryKey: ["account", selectedAid, "interaction-bot"] });
    }
  };

  if (accountsQ.isLoading) {
    return (
      <PageShell>
        <PageHeader
          icon={Bot}
          title="交互中心"
          description="正在读取账号与交互 Bot 配置。"
        />
        <div role="status" aria-label="交互中心加载中" className="space-y-4 rounded-lg border bg-card p-4">
          <div className="flex items-center justify-between gap-3">
            <div className="space-y-2"><Skeleton className="h-5 w-36" /><Skeleton className="h-3 w-64" /></div>
            <Skeleton className="h-9 w-24 rounded-md" />
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            {[0, 1, 2].map((item) => <Skeleton key={item} className="h-20 rounded-lg" />)}
          </div>
          <div className="space-y-3 border-t pt-4">
            {[0, 1, 2].map((item) => <div key={item} className="flex items-center gap-3"><Skeleton className="h-9 w-9 rounded-full" /><Skeleton className="h-4 flex-1" /><Skeleton className="h-8 w-20 rounded-md" /></div>)}
          </div>
        </div>
      </PageShell>
    );
  }

  if (accountsQ.isError) {
    return (
      <PageShell>
        <PageHeader icon={Bot} title="交互中心" description="账号列表加载失败。" />
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2 text-base"><AlertTriangle className="h-4 w-4 text-destructive" />无法读取账号</CardTitle></CardHeader>
          <CardContent className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
            <span>{String(accountsQ.error instanceof Error ? accountsQ.error.message : accountsQ.error)}</span>
            <Button size="sm" variant="outline" onClick={() => void accountsQ.refetch()}>重试</Button>
          </CardContent>
        </Card>
      </PageShell>
    );
  }

  return (
    <PageShell>
      <PageHeader
        icon={Bot}
        title="交互中心"
        description="按账号管理交互 Bot、关键词规则、玩法入口和会话运行状态。"
        actions={
          <>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={refresh}
              disabled={accountsQ.isFetching || interactionQ.isFetching}
            >
              <RefreshCw className={accountsQ.isFetching || interactionQ.isFetching ? "mr-1 h-4 w-4 animate-spin" : "mr-1 h-4 w-4"} />
              刷新
            </Button>
            {selectedAid !== null ? (
              <Button asChild variant="outline" size="sm">
                <Link to={`/logs?account_id=${selectedAid}`}>
                  <ScrollText className="mr-1 h-4 w-4" />
                  日志排障
                </Link>
              </Button>
            ) : null}
          </>
        }
      />

      {accounts.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <AlertTriangle className="h-4 w-4 text-warning" />
              暂无账号
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
            <span>交互中心需要先绑定一个 Telegram 账号，再配置对应的交互 Bot 和互动规则。</span>
            <Button asChild size="sm">
              <Link to="/accounts/new">添加账号</Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <>
          <section className="nested-surface nested-surface-inset-4 grid gap-4 border bg-card shadow-sm lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
            <div className="space-y-2">
              <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
                <span className="text-sm text-muted-foreground">选择配置的账号：</span>
                <Select
                  value={selectedAid ? String(selectedAid) : ""}
                  onChange={(event) => {
                    const next = new URLSearchParams(searchParams);
                    next.set("aid", event.target.value);
                    setSearchParams(next);
                  }}
                  className="w-full sm:w-64"
                >
                  {accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {accountOptionLabel(account)}
                    </option>
                  ))}
                </Select>
              </div>
              {selectedAccount ? (
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <AccountStatusBadge status={selectedAccount.status} />
                  <span>ID {selectedAccount.id}</span>
                  {selectedAccount.tg_user_id ? <span>TG {selectedAccount.tg_user_id}</span> : null}
                </div>
              ) : null}
            </div>

            <div className="flex flex-wrap items-center gap-2 lg:justify-end">
              <SignalPill
                tone={config?.enabled ? "primary" : "neutral"}
                label="交互总闸"
                value={config?.enabled ? "已启用" : "未启用"}
              />
              <SignalPill
                tone={hasInteractionToken ? "success" : "warn"}
                label="Bot Token"
                value={hasInteractionToken ? "已配置" : "待配置"}
              />
              <SignalPill
                tone={runtimeTone(config)}
                label="监听状态"
                value={runtimeLabel(config)}
              />
              <SignalPill
                tone={activeRules > 0 ? "primary" : "neutral"}
                label="启用订阅"
                value={`${activeRules}/${rules.length} · ${chatCoverage} 群`}
              />
            </div>
          </section>

          {interactionQ.isError ? (
            <Card>
              <CardHeader><CardTitle className="flex items-center gap-2 text-base"><AlertTriangle className="h-4 w-4 text-destructive" />交互配置加载失败</CardTitle></CardHeader>
              <CardContent className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
                <span>{String(interactionQ.error instanceof Error ? interactionQ.error.message : interactionQ.error)}</span>
                <Button size="sm" variant="outline" onClick={() => void interactionQ.refetch()}>重试</Button>
              </CardContent>
            </Card>
          ) : <Card className="overflow-hidden">
            <CardHeader className="border-b bg-muted/30">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <CardTitle className="flex items-center gap-2 text-base">
                    <Route className="h-4 w-4 text-primary" />
                    互动规则与玩法入口
                  </CardTitle>
                  <p className="mt-1 text-sm text-muted-foreground">
                    设置别人发什么关键词、在哪些群生效、启动哪个玩法；保存后交互 Bot 会自动监听并交给对应插件处理。
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {config?.interaction_bot_username ? (
                    <Badge variant="outline" className="h-7">
                      <MessageSquare className="mr-1 h-3.5 w-3.5" />
                      @{config.interaction_bot_username}
                    </Badge>
                  ) : null}
                  <Badge variant={lastError ? "destructive" : "secondary"} className="h-7">
                    {lastError ? "最近运行有错误" : "规则监听正常"}
                  </Badge>
                </div>
              </div>
            </CardHeader>
            <CardContent className="p-3 sm:p-4">
              {selectedAid !== null ? <BotTab aid={selectedAid} mode="interaction" /> : null}
            </CardContent>
          </Card>}
        </>
      )}
    </PageShell>
  );
}
