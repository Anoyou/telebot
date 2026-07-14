import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  KeyRound,
  Loader2,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  TerminalSquare,
  Webhook,
} from "lucide-react";
import { toast } from "sonner";

import { listAccounts } from "@/api/accounts";
import {
  getAccountWebhookConfig,
  resetAccountWebhookToken,
  type AccountWebhookConfig,
  type WebhookHook,
} from "@/api/webhooks";
import type { AccountSummary } from "@/api/types";
import { AccountStatusBadge } from "@/components/AccountStatusBadge";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/misc";
import { Select } from "@/components/ui/select";
import { SignalPill } from "@/components/ui/status";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";

function accountLabel(account: AccountSummary): string {
  const name = account.display_name?.trim();
  const username = account.tg_username?.trim();
  return name || (username ? `@${username}` : null) || `账号 #${account.id}`;
}

function bytesLabel(value: number): string {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MiB`;
  if (value >= 1024) return `${Math.round(value / 1024)} KiB`;
  return `${value} B`;
}

function limitLabel(config?: AccountWebhookConfig | null): string {
  if (!config) return "-";
  const limit = config.rate_limit;
  const parts = [
    limit.per_second ? `${limit.per_second}/秒` : null,
    limit.per_minute ? `${limit.per_minute}/分` : null,
    limit.per_hour ? `${limit.per_hour}/时` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "未限制";
}

function deliveryUrl(accountId: number, hookKey: string): string {
  const origin = typeof window === "undefined" ? "" : window.location.origin.replace(/\/$/, "");
  return `${origin}/api/webhooks/${accountId}/${hookKey}`;
}

function curlExample(config: AccountWebhookConfig, hookKey: string): string {
  const url = deliveryUrl(config.account_id, hookKey);
  return [
    `curl -X POST '${url}' \\`,
    `  -H '${config.token_header}: ${config.token}' \\`,
    "  -H 'Content-Type: application/json' \\",
    "  -d '{\"event\":\"demo\",\"value\":1}'",
  ].join("\n");
}

async function copyText(value: string, label: string) {
  await navigator.clipboard.writeText(value);
  toast.success(`${label}已复制`);
}

function HookRow({
  hook,
  selected,
  onSelect,
  onCopy,
}: {
  hook: WebhookHook;
  selected: boolean;
  onSelect: () => void;
  onCopy: () => void;
}) {
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col gap-3 rounded-lg border bg-background/80 p-3 transition sm:flex-row sm:items-center sm:justify-between",
        selected && "border-primary/50 bg-primary/5",
      )}
    >
      <button
        type="button"
        onClick={onSelect}
        className="flex min-w-0 flex-1 items-center gap-3 text-left"
      >
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border bg-muted text-muted-foreground">
          <Webhook className="h-4 w-4" />
        </span>
        <span className="min-w-0">
          <span className="block truncate text-sm font-semibold text-foreground">{hook.label}</span>
          <code className="block truncate text-xs text-muted-foreground">{hook.key}</code>
        </span>
      </button>
      <div className="flex shrink-0 items-center gap-2">
        <Badge variant={hook.enabled ? "success" : "secondary"}>
          {hook.enabled ? "启用" : "停用"}
        </Badge>
        <Button type="button" variant="outline" size="sm" onClick={onCopy} className="active:scale-95">
          <Copy className="mr-1 h-4 w-4" />
          复制地址
        </Button>
      </div>
    </div>
  );
}

export function WebhooksPage() {
  const queryClient = useQueryClient();
  const accountsQ = useQuery({ queryKey: ["accounts"], queryFn: listAccounts });
  const accounts = accountsQ.data ?? [];
  const [selectedAid, setSelectedAid] = useState<number | null>(null);
  const [selectedHookKey, setSelectedHookKey] = useState("");

  useEffect(() => {
    if (selectedAid !== null) return;
    if (accounts.length > 0) setSelectedAid(accounts[0].id);
  }, [accounts, selectedAid]);

  const configQ = useQuery({
    queryKey: ["webhooks", selectedAid],
    queryFn: () => getAccountWebhookConfig(selectedAid as number),
    enabled: selectedAid !== null,
  });
  const config = configQ.data ?? null;

  useEffect(() => {
    const hooks = config?.hooks ?? [];
    if (!hooks.length) {
      setSelectedHookKey("");
      return;
    }
    if (!hooks.some((hook) => hook.key === selectedHookKey)) {
      setSelectedHookKey(hooks[0].key);
    }
  }, [config?.hooks, selectedHookKey]);

  const selectedAccount = useMemo(
    () => accounts.find((account) => account.id === selectedAid) ?? null,
    [accounts, selectedAid],
  );
  const selectedHook = config?.hooks.find((hook) => hook.key === selectedHookKey) ?? config?.hooks[0] ?? null;
  const example = config && selectedHook ? curlExample(config, selectedHook.key) : "";

  const resetMut = useMutation({
    mutationFn: () => resetAccountWebhookToken(selectedAid as number),
    onSuccess: (next) => {
      queryClient.setQueryData(["webhooks", next.account_id], next);
      toast.success("Webhook token 已重置");
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    if (selectedAid !== null) {
      void queryClient.invalidateQueries({ queryKey: ["webhooks", selectedAid] });
    }
  };

  if (accountsQ.isLoading) {
    return (
      <PageShell>
        <PageHeader icon={Webhook} title="入站 Webhook" description="正在读取账号列表。" />
        <div className="flex h-36 items-center justify-center rounded-lg border bg-card">
          <Spinner className="text-primary" />
        </div>
      </PageShell>
    );
  }


  if (accountsQ.isError) {
    return (
      <PageShell>
        <PageHeader icon={Webhook} title="入站 Webhook" description="账号列表加载失败。" />
        <Card><CardContent className="flex flex-wrap items-center justify-between gap-3 pt-6 text-sm text-muted-foreground">
          <span>{getErrMsg(accountsQ.error)}</span>
          <Button size="sm" variant="outline" onClick={() => void accountsQ.refetch()}>重试</Button>
        </CardContent></Card>
      </PageShell>
    );
  }

  return (
    <PageShell>
      <PageHeader
        icon={Webhook}
        title="入站 Webhook"
        description="为账号生成 HTTP 入口，把外部系统事件投递到声明 webhook 订阅的插件。"
        actions={
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={refresh}
            disabled={accountsQ.isFetching || configQ.isFetching}
            className="active:scale-95"
          >
            <RefreshCw className={cn("mr-1 h-4 w-4", (accountsQ.isFetching || configQ.isFetching) && "animate-spin")} />
            刷新
          </Button>
        }
        signals={
          <>
            <SignalPill tone={accounts.length > 0 ? "success" : "warn"} label="账号" value={accounts.length} />
            <SignalPill tone={config?.token ? "success" : "neutral"} label="Token" value={config?.token ? "已生成" : "未选择"} />
            <SignalPill tone="neutral" label="限流" value={limitLabel(config)} />
          </>
        }
      />

      <Card>
        <CardHeader className="border-b bg-muted/30">
          <CardTitle className="flex items-center gap-2 text-base">
            <TerminalSquare className="h-4 w-4 text-primary" />
            怎么用
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 pt-5 md:grid-cols-3">
          <UsageStep
            index="1"
            title="插件声明入口"
            text="插件需要在 event_subscriptions 里声明 webhook，并指定 hook_key。默认入口是 default。"
          />
          <UsageStep
            index="2"
            title="复制地址和 Token"
            text="左侧选择账号，右侧复制 Hook 地址；请求头带上本页生成的 X-TelePilot-Webhook-Token。"
          />
          <UsageStep
            index="3"
            title="外部系统 POST"
            text="发送 JSON 后，TelePilot 会把正文包装成 webhook 事件，再投递给匹配的插件入口。"
          />
        </CardContent>
      </Card>

      {accounts.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <AlertTriangle className="h-4 w-4 text-warning" />
              暂无账号
            </CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            入站 Webhook 需要先绑定至少一个 Telegram 账号。
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[420px_minmax(0,1fr)]">
          <Card className="h-fit xl:sticky xl:top-4">
            <CardHeader className="border-b bg-muted/30">
              <CardTitle className="flex items-center gap-2 text-base">
                <KeyRound className="h-4 w-4 text-primary" />
                账号与令牌
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-5 pt-5">
              <div className="space-y-2">
                <Label htmlFor="webhook-account">账号</Label>
                <Select
                  id="webhook-account"
                  value={selectedAid?.toString() ?? ""}
                  onChange={(event) => {
                    setSelectedAid(Number(event.target.value));
                    setSelectedHookKey("");
                  }}
                >
                  {accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {accountLabel(account)}
                    </option>
                  ))}
                </Select>
                {selectedAccount ? (
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <AccountStatusBadge status={selectedAccount.status} />
                    <span>ID {selectedAccount.id}</span>
                    {selectedAccount.tg_user_id ? <span>TG {selectedAccount.tg_user_id}</span> : null}
                  </div>
                ) : null}
              </div>

              {configQ.isLoading ? (
                <div className="flex h-24 items-center justify-center rounded-lg border bg-muted/30">
                  <Spinner className="text-primary" />
                </div>
              ) : configQ.isError ? (
                <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  {getErrMsg(configQ.error)}
                </div>
              ) : config ? (
                <>
                  <div className="space-y-2">
                    <div className="flex items-center justify-between gap-3">
                      <Label>Webhook Token</Label>
                      <Badge variant="outline">{config.token_header}</Badge>
                    </div>
                    <div className="min-w-0 rounded-lg border bg-muted/40 p-3">
                      <code className="block break-all text-xs leading-5 text-foreground">{config.token}</code>
                    </div>
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => copyText(config.token, "Token")}
                        className="w-full active:scale-95 sm:w-auto"
                      >
                        <Copy className="mr-1 h-4 w-4" />
                        复制 Token
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={resetMut.isPending}
                        onClick={() => {
                          if (window.confirm("确定重置该账号 Webhook token？旧 token 会立即失效。")) {
                            resetMut.mutate();
                          }
                        }}
                        className="w-full active:scale-95 sm:w-auto"
                      >
                        {resetMut.isPending ? (
                          <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                        ) : (
                          <RotateCcw className="mr-1 h-4 w-4" />
                        )}
                        重置
                      </Button>
                    </div>
                  </div>

                  <div className="grid gap-3 text-sm sm:grid-cols-2">
                    <div className="rounded-lg border bg-background/70 p-3">
                      <div className="text-xs text-muted-foreground">Body 上限</div>
                      <div className="mt-1 font-semibold">{bytesLabel(config.max_body_bytes)}</div>
                    </div>
                    <div className="rounded-lg border bg-background/70 p-3">
                      <div className="text-xs text-muted-foreground">存储位置</div>
                      <div className="mt-1 truncate font-semibold" title={config.token_storage}>
                        SystemSetting
                      </div>
                    </div>
                  </div>
                </>
              ) : null}
            </CardContent>
          </Card>

          <div className="min-w-0 space-y-4">
            <Card className="min-w-0">
              <CardHeader className="border-b bg-muted/30">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <CardTitle className="flex items-center gap-2 text-base">
                    <ShieldCheck className="h-4 w-4 text-primary" />
                    Hook keys
                  </CardTitle>
                  {config ? <Badge variant="secondary">{config.hooks.length} 个入口</Badge> : null}
                </div>
              </CardHeader>
              <CardContent className="space-y-3 pt-5">
                {config?.hooks.length ? (
                  config.hooks.map((hook) => (
                    <HookRow
                      key={hook.key}
                      hook={hook}
                      selected={hook.key === selectedHook?.key}
                      onSelect={() => setSelectedHookKey(hook.key)}
                      onCopy={() => copyText(deliveryUrl(config.account_id, hook.key), "Webhook 地址")}
                    />
                  ))
                ) : (
                  <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
                    暂无 hook key。
                  </div>
                )}
              </CardContent>
            </Card>

            <Card className="min-w-0 overflow-hidden">
              <CardHeader className="border-b bg-muted/30">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <CardTitle className="flex items-center gap-2 text-base">
                    <TerminalSquare className="h-4 w-4 text-primary" />
                    发送示例
                  </CardTitle>
                  {example ? (
                    <Button type="button" variant="outline" size="sm" onClick={() => copyText(example, "curl 示例")} className="active:scale-95">
                      <Copy className="mr-1 h-4 w-4" />
                      复制
                    </Button>
                  ) : null}
                </div>
              </CardHeader>
              <CardContent className="pt-5">
                {example ? (
                  <pre className="max-h-80 overflow-auto rounded-lg border bg-muted/50 p-4 text-xs leading-6">
                    <code>{example}</code>
                  </pre>
                ) : (
                  <div className="flex h-24 items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
                    请选择账号和 hook key。
                  </div>
                )}
                {config && selectedHook ? (
                  <div className="mt-3 flex min-w-0 items-center gap-2 rounded-lg border bg-background/70 px-3 py-2 text-xs text-muted-foreground">
                    <CheckCircle2 className="h-4 w-4 shrink-0 text-success" />
                    <span className="min-w-0 break-all">{deliveryUrl(config.account_id, selectedHook.key)}</span>
                  </div>
                ) : null}
              </CardContent>
            </Card>
          </div>
        </div>
      )}
    </PageShell>
  );
}

function UsageStep({ index, title, text }: { index: string; title: string; text: string }) {
  return (
    <div className="rounded-lg border border-border/70 bg-muted/25 p-3">
      <div className="flex items-center gap-2">
        <span className="grid h-6 w-6 place-items-center rounded-full bg-primary/10 text-xs font-semibold text-primary">
          {index}
        </span>
        <div className="text-sm font-semibold">{title}</div>
      </div>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{text}</p>
    </div>
  );
}
