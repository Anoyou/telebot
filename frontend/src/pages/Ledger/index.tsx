import { lazy, Suspense, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowDownLeft,
  ArrowUpRight,
  CheckCircle2,
  Filter,
  Gamepad2,
  HandCoins,
  Percent,
  RefreshCw,
  Trash2,
  Users,
  WalletCards,
} from "lucide-react";
import { toast } from "sonner";

import { listAccounts } from "@/api/accounts";
import {
  getLedgerStats,
  getLedgerSummary,
  listLedgerCompensations,
  listLedgerEntries,
  markLedgerCompensationManualPaid,
  resetLedgerData,
  type LedgerCompensation,
  type LedgerDirection,
  type LedgerEntry,
  type LedgerQueryParams,
  type LedgerRecipientBucket,
  type LedgerStatsQueryParams,
  type LedgerSummaryBucket,
  type LedgerSummary,
  type OperationalStats,
} from "@/api/ledger";
import { useTheme } from "@/lib/theme";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Spinner } from "@/components/ui/misc";
import { ResponsiveList } from "@/components/data/ResponsiveList";
import { EmptyState } from "@/components/feedback/EmptyState";
import { ErrorState } from "@/components/feedback/ErrorState";
import { Select } from "@/components/ui/select";
import { SectionHeader, SignalPill, ToneRailCard } from "@/components/ui/status";
import type { VisualTone } from "@/components/ui/status";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";

type FilterState = {
  since: string;
  until: string;
  account_id: string;
  chat_id: string;
  plugin_key: string;
  direction: "" | LedgerDirection;
  amount: string;
  amount_min: string;
  amount_max: string;
  status: string;
};

type TrendPeriod = "day" | "week" | "month";

const LineTrend = lazy(async () => {
  const module = await import("@/components/LineTrend");
  return { default: module.LineTrend };
});

function cssVarHsl(name: string, alpha?: number): string {
  if (typeof document === "undefined") return "#888";
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!value) return "#888";
  return alpha != null ? `hsl(${value} / ${alpha})` : `hsl(${value})`;
}

const DEFAULT_FILTERS: FilterState = {
  since: "",
  until: "",
  account_id: "",
  chat_id: "",
  plugin_key: "",
  direction: "",
  amount: "",
  amount_min: "",
  amount_max: "",
  status: "",
};

export function LedgerPage() {
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
  const queryParams = useMemo(() => buildQueryParams(filters), [filters]);
  const statsParams = useMemo(() => buildStatsQueryParams(filters), [filters]);

  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  const entriesQ = useQuery({
    queryKey: ["ledger", "entries", queryParams],
    queryFn: () => listLedgerEntries({ ...queryParams, limit: 100 }),
  });
  const summaryQ = useQuery({
    queryKey: ["ledger", "summary", queryParams],
    queryFn: () => getLedgerSummary(queryParams),
  });
  const statsQ = useQuery({
    queryKey: ["ledger", "stats", statsParams],
    queryFn: () => getLedgerStats(statsParams),
  });
  const compensationsQ = useQuery({
    queryKey: ["ledger", "compensations", queryParams.account_id, queryParams.chat_id, queryParams.plugin_key],
    queryFn: () => listLedgerCompensations({
      account_id: queryParams.account_id,
      chat_id: queryParams.chat_id,
      plugin_key: queryParams.plugin_key,
      limit: 100,
    }),
  });

  const manualPaidMut = useMutation({
    mutationFn: ({ id, note }: { id: number; note?: string }) => markLedgerCompensationManualPaid(id, note),
    onSuccess: () => {
      toast.success("挂账已核销");
      void queryClient.invalidateQueries({ queryKey: ["ledger"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const resetMut = useMutation({
    mutationFn: resetLedgerData,
    onSuccess: (result) => {
      toast.success(`已重置 ${result.deleted_action_events} 条动作和 ${result.deleted_compensations} 条待补付记录`);
      void queryClient.invalidateQueries({ queryKey: ["ledger"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const accounts = accountsQ.data || [];
  const accountLabel = (accountId: number | null | undefined) => {
    if (accountId == null) return "-";
    const account = accounts.find((item) => item.id === accountId);
    return account?.display_name || account?.phone || `账号 #${accountId}`;
  };

  const summary = summaryQ.data;
  const stats = statsQ.data;
  const entries = entriesQ.data?.items || [];
  const compensations = compensationsQ.data?.items || [];
  const isLoading = entriesQ.isLoading || summaryQ.isLoading || compensationsQ.isLoading;
  const error = entriesQ.error || summaryQ.error || statsQ.error || compensationsQ.error || accountsQ.error;

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["ledger"] });
    void queryClient.invalidateQueries({ queryKey: ["accounts"] });
  };

  const handleManualPaid = (item: LedgerCompensation) => {
    const note = window.prompt(`核销待补付记录 ${item.payout_key}，备注可留空。`);
    if (note === null) return;
    manualPaidMut.mutate({ id: item.id, note });
  };

  const handleReset = () => {
    if (!window.confirm("确认清空资金台账与运营统计？流水、趋势、开局统计和待补付记录都会被永久删除，此操作不可恢复。")) return;
    resetMut.mutate();
  };

  return (
    <PageShell>
      <PageHeader
        icon={WalletCards}
        title="资金台账"
        description="按入账、出账和待补付事项核对每个账号、群与收款方的资金流。待补付只在付款失败且自动补付尚未完成时出现，正常情况下应为 0。"
        actions={(
          <>
            <Button type="button" variant="outline" onClick={refreshAll}>
              <RefreshCw className="mr-2 h-4 w-4" />
              刷新
            </Button>
            <Button type="button" variant="destructive" onClick={handleReset} loading={resetMut.isPending}>
              {!resetMut.isPending ? <Trash2 className="mr-2 h-4 w-4" /> : null}
              重置数据
            </Button>
          </>
        )}
        signals={summary ? (
          <>
            <SignalPill tone="success" label="入账" value={formatAmount(summary.income)} />
            <SignalPill tone="warn" label="出账" value={formatAmount(summary.payout)} />
            <SignalPill tone={isNegative(summary.net) ? "danger" : "primary"} label="净额" value={formatAmount(summary.net)} />
            <SignalPill tone={compensations.length > 0 ? "warn" : "neutral"} label="待补付" value={compensations.length} />
          </>
        ) : null}
      />

      <OperationalOverview stats={stats} loading={statsQ.isLoading} />

      {summary ? <SummaryTiles summary={summary} /> : null}

      <FilterPanel
        filters={filters}
        accounts={accounts}
        onChange={setFilters}
        onReset={() => setFilters(DEFAULT_FILTERS)}
      />

      {error ? <ErrorState error={getErrMsg(error)} onRetry={refreshAll} /> : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.7fr)_minmax(320px,0.8fr)]">
        <TrendPanel summary={summary} loading={isLoading} />
        <div className="space-y-6">
          <ChatSummaryPanel summary={summary} />
          <RecipientSummaryPanel summary={summary} />
        </div>
      </div>

      <LedgerTable
        entries={entries}
        loading={isLoading}
        accountLabel={accountLabel}
      />

      <CompensationTable
        items={compensations}
        loading={isLoading}
        accountLabel={accountLabel}
        pendingId={manualPaidMut.variables?.id}
        pending={manualPaidMut.isPending}
        onManualPaid={handleManualPaid}
      />
    </PageShell>
  );
}

function OperationalOverview({ stats, loading }: { stats?: OperationalStats; loading: boolean }) {
  if (loading) {
    return (
      <Card>
        <CardHeader>
          <SectionHeader icon={Gamepad2} title="运营概览" />
        </CardHeader>
        <CardContent>
          <LoadingState />
        </CardContent>
      </Card>
    );
  }
  if (!stats) return null;
  const total = stats.total;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <ToneRailCard
          icon={Gamepad2}
          title="开局数"
          value={formatInteger(total.started_sessions)}
          description="按已记录的有效开局事件统计"
          tone="primary"
          valueClassName="truncate font-mono text-2xl font-bold tabular-nums"
        />
        <ToneRailCard
          icon={Percent}
          title="派奖成功率"
          value={formatPercent(total.payout_success_rate)}
          description={`${formatInteger(total.payout_success_count)} 成功 / ${formatInteger(total.payout_attempt_count)} 次尝试`}
          tone={payoutRateTone(total.payout_success_rate, total.payout_attempt_count)}
          valueClassName="truncate font-mono text-2xl font-bold tabular-nums"
        />
        <ToneRailCard
          icon={WalletCards}
          title="运营净盈亏"
          value={formatAmount(total.ledger_net)}
          description={`${formatInteger(total.ledger_count)} 条资金流水，同台账汇总`}
          tone={isNegative(total.ledger_net) ? "danger" : "success"}
          valueClassName="truncate font-mono text-2xl font-bold tabular-nums"
        />
        <ToneRailCard
          icon={Users}
          title="参与人数"
          value={formatInteger(total.participant_count ?? 0)}
          description="按开局、付款与派奖对象的 User ID 去重统计"
          tone={total.participant_count ? "primary" : "neutral"}
          valueClassName="truncate font-mono text-2xl font-bold tabular-nums"
        />
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.8fr)]">
        <OperationalTrend stats={stats} />
        <SourceMatrixPanel stats={stats} />
      </div>
    </div>
  );
}

function OperationalTrend({ stats }: { stats: OperationalStats }) {
  useTheme(); // 订阅主题，切换时重取图表色值
  const xAxis = stats.by_day.map((item) => item.key);
  const totals = stats.by_day.reduce(
    (result, item) => ({
      started: result.started + item.started_sessions,
      succeeded: result.succeeded + item.payout_success_count,
      failed: result.failed + item.payout_failure_count,
    }),
    { started: 0, succeeded: 0, failed: 0 },
  );
  const series = [
    { name: "开局", data: stats.by_day.map((item) => item.started_sessions), color: cssVarHsl("--primary") },
    { name: "派奖成功", data: stats.by_day.map((item) => item.payout_success_count), color: cssVarHsl("--success") },
    { name: "派奖失败", data: stats.by_day.map((item) => item.payout_failure_count), color: cssVarHsl("--destructive") },
  ];
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={Gamepad2}
          title="运营日趋势"
          meta={<MetaBadge tone="outline">{stats.by_day.length} 天</MetaBadge>}
        />
      </CardHeader>
      <CardContent>
        {xAxis.length === 0 ? (
          <EmptyState title="暂无运营动作" />
        ) : (
          <div className="space-y-4">
            <Suspense fallback={<LoadingState />}>
              <LineTrend xAxis={xAxis} series={series} height={260} />
            </Suspense>
            <div className="grid grid-cols-3 gap-3 border-t border-border/70 pt-3 max-sm:gap-1.5">
              <TrendValue label="开局" value={totals.started} tone="primary" />
              <TrendValue label="派奖成功" value={totals.succeeded} tone="success" />
              <TrendValue label="派奖失败" value={totals.failed} tone="danger" />
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function TrendValue({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "primary" | "success" | "danger";
}) {
  const toneClass = {
    primary: "text-primary",
    success: "text-success",
    danger: "text-destructive",
  }[tone];
  return (
    <div className="min-w-0 text-center">
      <div className="truncate text-xs text-muted-foreground max-sm:text-[11px]">{label}</div>
      <div className={cn("mt-1 font-mono text-lg font-bold tabular-nums max-sm:text-sm", toneClass)}>
        {formatInteger(value)}
      </div>
    </div>
  );
}

function SourceMatrixPanel({ stats }: { stats: OperationalStats }) {
  return (
    <Card>
      <CardHeader>
        <SectionHeader icon={AlertTriangle} title="数据源口径" />
      </CardHeader>
      <CardContent>
        <div className="space-y-2">
          {stats.source_matrix.map((item) => (
            <div
              key={item.key}
              className="rounded-lg border border-border/70 bg-muted/30 px-3 py-2"
            >
              <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                <div className="min-w-0 truncate text-sm font-medium">{item.label}</div>
                <MetaBadge tone={item.status === "available" ? "success" : "warn"}>
                  {item.status === "available" ? "可算" : "需埋点"}
                </MetaBadge>
              </div>
              <div className="mt-1 break-words font-mono text-[11px] leading-4 text-muted-foreground">
                {item.source}
              </div>
              <div className="mt-1 text-xs leading-5 text-muted-foreground">{item.note}</div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function SummaryTiles({ summary }: { summary: LedgerSummary }) {
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-3 md:gap-3">
      <ToneRailCard
        icon={ArrowDownLeft}
        title="入账"
        value={formatAmount(summary.income)}
        tone="success"
        valueClassName="truncate text-xl font-bold tabular-nums tracking-tight sm:text-2xl"
      />
      <ToneRailCard
        icon={ArrowUpRight}
        title="出账"
        value={formatAmount(summary.payout)}
        tone="warn"
        valueClassName="truncate text-xl font-bold tabular-nums tracking-tight sm:text-2xl"
      />
      <ToneRailCard
        icon={WalletCards}
        title="净盈亏"
        value={formatAmount(summary.net)}
        tone={isNegative(summary.net) ? "danger" : "primary"}
        valueClassName="truncate text-xl font-bold tabular-nums tracking-tight sm:text-2xl"
      />
    </div>
  );
}

function FilterPanel({
  filters,
  accounts,
  onChange,
  onReset,
}: {
  filters: FilterState;
  accounts: Awaited<ReturnType<typeof listAccounts>>;
  onChange: (next: FilterState) => void;
  onReset: () => void;
}) {
  const patch = (key: keyof FilterState, value: string) => onChange({ ...filters, [key]: value });
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={Filter}
          title="筛选"
          actions={(
            <Button type="button" variant="outline" size="sm" onClick={onReset}>
              重置
            </Button>
          )}
        />
      </CardHeader>
      <CardContent>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <Field label="开始时间">
            <Input
              type="datetime-local"
              value={filters.since}
              onChange={(event) => patch("since", event.target.value)}
            />
          </Field>
          <Field label="结束时间">
            <Input
              type="datetime-local"
              value={filters.until}
              onChange={(event) => patch("until", event.target.value)}
            />
          </Field>
          <Field label="账号">
            <Select value={filters.account_id} onChange={(event) => patch("account_id", event.target.value)}>
              <option value="">全部账号</option>
              {accounts.map((account) => (
                <option key={account.id} value={String(account.id)}>
                  {account.display_name || account.phone || `账号 #${account.id}`}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="群 ID">
            <Input
              inputMode="numeric"
              placeholder="-100123456"
              value={filters.chat_id}
              onChange={(event) => patch("chat_id", event.target.value)}
            />
          </Field>
          <Field label="插件">
            <Input
              placeholder="plugin_key"
              value={filters.plugin_key}
              onChange={(event) => patch("plugin_key", event.target.value)}
            />
          </Field>
          <Field label="方向">
            <Select value={filters.direction} onChange={(event) => patch("direction", event.target.value)}>
              <option value="">全部方向</option>
              <option value="in">入账</option>
              <option value="out">出账</option>
            </Select>
          </Field>
          <Field label="状态">
            <Select value={filters.status} onChange={(event) => patch("status", event.target.value)}>
              <option value="">已计账（OK / COMPENSATED）</option>
              <option value="OK">成功（OK）</option>
              <option value="FAILED">失败尝试（FAILED）</option>
              <option value="DRY_RUN">演练（DRY_RUN）</option>
              <option value="PENDING">处理中（PENDING）</option>
              <option value="COMPENSATED">已补付（COMPENSATED）</option>
            </Select>
          </Field>
          <Field label="金额">
            <Input
              inputMode="decimal"
              placeholder="精确金额"
              value={filters.amount}
              onChange={(event) => patch("amount", event.target.value)}
            />
          </Field>
          <Field label="最小金额">
            <Input
              inputMode="decimal"
              value={filters.amount_min}
              onChange={(event) => patch("amount_min", event.target.value)}
            />
          </Field>
          <Field label="最大金额">
            <Input
              inputMode="decimal"
              value={filters.amount_max}
              onChange={(event) => patch("amount_max", event.target.value)}
            />
          </Field>
        </div>
      </CardContent>
    </Card>
  );
}

function TrendPanel({ summary, loading }: { summary?: LedgerSummary; loading: boolean }) {
  useTheme(); // 订阅主题，切换时重取图表色值
  const [period, setPeriod] = useState<TrendPeriod>("day");
  const buckets = useMemo(() => aggregateTrendBuckets(summary?.by_day || [], period), [period, summary?.by_day]);
  const xAxis = buckets.map((item) => item.label);
  const series = [
    { name: "入账", data: buckets.map((item) => chartNumber(item.income)), color: cssVarHsl("--success") },
    { name: "出账", data: buckets.map((item) => chartNumber(item.payout)), color: cssVarHsl("--warning") },
    { name: "净额", data: buckets.map((item) => chartNumber(item.net)), color: cssVarHsl("--primary") },
  ];
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={WalletCards}
          title="资金趋势"
          meta={summary ? <MetaBadge tone="outline">{summary.count} 条流水</MetaBadge> : null}
          actions={(
            <Tabs value={period} onValueChange={(value) => setPeriod(value as TrendPeriod)}>
              <TabsList>
                <TabsTrigger value="day">日</TabsTrigger>
                <TabsTrigger value="week">周</TabsTrigger>
                <TabsTrigger value="month">月</TabsTrigger>
              </TabsList>
            </Tabs>
          )}
        />
      </CardHeader>
      <CardContent>
        {loading ? (
          <LoadingState />
        ) : xAxis.length === 0 ? (
          <EmptyState title="暂无流水" />
        ) : (
          <Suspense fallback={<LoadingState />}>
            <LineTrend xAxis={xAxis} series={series} height={260} />
          </Suspense>
        )}
      </CardContent>
    </Card>
  );
}

function ChatSummaryPanel({ summary }: { summary?: LedgerSummary }) {
  const rows = summary?.by_chat.slice(0, 8) || [];
  return (
    <Card>
      <CardHeader>
        <SectionHeader icon={HandCoins} title="群净额" />
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <EmptyState title="暂无群汇总" />
        ) : (
          <div className="space-y-2">
            {rows.map((item) => (
              <div
                key={item.key}
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-lg border border-border/70 bg-muted/30 px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{item.label}</div>
                  <div className="font-mono text-[11px] text-muted-foreground">{item.key} · {item.count} 条</div>
                </div>
                <div className={cn("text-right font-mono text-sm font-semibold tabular-nums", amountToneClass(item.net))}>
                  {formatAmount(item.net)}
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function RecipientSummaryPanel({ summary }: { summary?: LedgerSummary }) {
  const rows = summary?.by_recipient.slice(0, 8) || [];
  return (
    <Card>
      <CardHeader>
        <SectionHeader icon={Users} title="收款方汇总" description="按收款方 User ID 汇总入账和派奖金额。" />
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <EmptyState title="暂无收款方身份记录" />
        ) : (
          <div className="space-y-2">
            {rows.map((item) => (
              <RecipientSummaryRow key={item.key} item={item} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function RecipientSummaryRow({ item }: { item: LedgerRecipientBucket }) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-lg border border-border/70 bg-muted/30 px-3 py-2">
      <div className="min-w-0">
        <div className="truncate text-sm font-medium">{item.label}</div>
        <div className="truncate font-mono text-[11px] text-muted-foreground">
          {item.user_id != null ? `UID ${item.user_id}` : "UID 未识别"} · {item.count} 笔
        </div>
      </div>
      <div className="text-right">
        <div className="font-mono text-sm font-semibold tabular-nums">{formatAmount(item.received)}</div>
        <div className="text-[11px] text-muted-foreground">入 {formatAmount(item.income)} · 出 {formatAmount(item.payout)}</div>
      </div>
    </div>
  );
}

function LedgerTable({
  entries,
  loading,
  accountLabel,
}: {
  entries: LedgerEntry[];
  loading: boolean;
  accountLabel: (id: number) => string;
}) {
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={WalletCards}
          title="流水"
          meta={<MetaBadge tone="outline">{entries.length} 条</MetaBadge>}
        />
      </CardHeader>
      <CardContent>
        {loading ? (
          <LoadingState />
        ) : entries.length === 0 ? (
          <EmptyState title="暂无流水" />
        ) : (
          <>
          <div className="hidden md:block overflow-x-auto">
          <Table className="min-w-[1260px]">
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>方向</TableHead>
                <TableHead>金额</TableHead>
                <TableHead>账号</TableHead>
                <TableHead>群</TableHead>
                <TableHead>收款方</TableHead>
                <TableHead>插件</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>通道</TableHead>
                <TableHead>错误</TableHead>
                <TableHead>流水键</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.map((entry) => {
                const posted = isPostedLedgerStatus(entry.status);
                return (
                  <TableRow key={`${entry.source}:${entry.source_id}`} className={posted ? undefined : "bg-muted/20"}>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {formatDateTime(entry.created_at)}
                    </TableCell>
                    <TableCell>
                      <DirectionBadge direction={entry.direction} posted={posted} />
                    </TableCell>
                    <TableCell className={cn(
                      "text-right font-mono font-semibold tabular-nums",
                      posted ? amountToneClass(entry.signed_amount) : "text-muted-foreground",
                    )}>
                      {posted ? formatSignedAmount(entry) : `未计账 · ${formatAmount(entry.amount)}`}
                    </TableCell>
                    <TableCell>
                      <div className="max-w-44 truncate">{accountLabel(entry.account_id)}</div>
                      <div className="font-mono text-[11px] text-muted-foreground">#{entry.account_id}</div>
                    </TableCell>
                    <TableCell>
                      <div className="max-w-44 truncate text-sm">{entry.chat_title || "-"}</div>
                      <div className="font-mono text-[11px] text-muted-foreground">{entry.chat_id ?? "-"}</div>
                    </TableCell>
                    <TableCell>
                      <div className="max-w-44 truncate text-sm">{recipientDisplayName(entry)}</div>
                      <div className="font-mono text-[11px] text-muted-foreground">
                        {entry.receiver_user_id != null ? `UID ${entry.receiver_user_id}` : "UID -"}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="max-w-40 truncate font-mono text-xs">{entry.plugin_key || "-"}</div>
                      <div className="max-w-40 truncate font-mono text-[11px] text-muted-foreground">{entry.entry_key || "-"}</div>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={entry.status} />
                    </TableCell>
                    <TableCell className="font-mono text-xs">{entry.channel || "-"}</TableCell>
                    <TableCell className="max-w-40 truncate font-mono text-xs">{entry.error_code || "-"}</TableCell>
                    <TableCell className="max-w-40 truncate font-mono text-xs">{entry.payout_key || `#${entry.source_id}`}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          </div>
          <div className="md:hidden">
            <ResponsiveList
              data={entries}
              rowKey={(entry) => `${entry.source}:${entry.source_id}`}
              columns={[
                { key: "time", header: "时间", priority: 0, render: (entry) => <div className="text-xs text-muted-foreground">{formatDateTime(entry.created_at)}</div> },
                { key: "amount", header: "金额", priority: 0, render: (entry) => {
                  const posted = isPostedLedgerStatus(entry.status);
                  return <div className={cn("font-mono text-sm font-semibold tabular-nums", posted ? amountToneClass(entry.signed_amount) : "text-muted-foreground")}>{posted ? formatSignedAmount(entry) : `未计账 · ${formatAmount(entry.amount)}`}</div>;
                } },
                { key: "direction", header: "方向", priority: 1, render: (entry) => <DirectionBadge direction={entry.direction} posted={isPostedLedgerStatus(entry.status)} /> },
                { key: "account", header: "账号", priority: 1, render: (entry) => <><div className="truncate text-sm">{accountLabel(entry.account_id)}</div><div className="font-mono text-[11px] text-muted-foreground">#{entry.account_id}</div></> },
                { key: "chat", header: "群", priority: 1, render: (entry) => <><div className="truncate text-sm">{entry.chat_title || "-"}</div><div className="font-mono text-[11px] text-muted-foreground">{entry.chat_id ?? "-"}</div></> },
                { key: "status", header: "状态", priority: 1, render: (entry) => <StatusBadge status={entry.status} /> },
                { key: "recipient", header: "收款方", priority: 2, render: (entry) => <><div className="text-sm">{recipientDisplayName(entry)}</div><div className="font-mono text-[11px] text-muted-foreground">{entry.receiver_user_id != null ? `UID ${entry.receiver_user_id}` : "UID -"}</div></> },
                { key: "plugin", header: "插件", priority: 2, render: (entry) => <><div className="font-mono text-xs">{entry.plugin_key || "-"}</div><div className="break-all font-mono text-[11px] text-muted-foreground">{entry.entry_key || "-"}</div></> },
                { key: "channel", header: "通道", priority: 2, render: (entry) => <div className="font-mono text-xs">{entry.channel || "-"}</div> },
                { key: "error", header: "错误", priority: 2, render: (entry) => <div className="font-mono text-xs">{entry.error_code || "-"}</div> },
                { key: "key", header: "流水键", priority: 2, render: (entry) => <div className="break-all font-mono text-xs">{entry.payout_key || `#${entry.source_id}`}</div> },
              ]}
              empty={<EmptyState title="暂无流水" />}
            />
          </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function CompensationTable({
  items,
  loading,
  accountLabel,
  pendingId,
  pending,
  onManualPaid,
}: {
  items: LedgerCompensation[];
  loading: boolean;
  accountLabel: (id: number) => string;
  pendingId?: number;
  pending: boolean;
  onManualPaid: (item: LedgerCompensation) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={HandCoins}
          title="待补付"
          description="付款失败后进入自动补付队列的未结事项。队列成功补付或人工核销后会从这里消失。"
          meta={<MetaBadge tone={items.length > 0 ? "warn" : "outline"}>{items.length} 条</MetaBadge>}
        />
      </CardHeader>
      <CardContent>
        {loading ? (
          <LoadingState />
        ) : items.length === 0 ? (
          <EmptyState title="暂无待补付事项" />
        ) : (
          <>
          <div className="hidden md:block overflow-x-auto">
          <Table className="min-w-[1240px]">
            <TableHeader>
              <TableRow>
                <TableHead>创建时间</TableHead>
                <TableHead>账号</TableHead>
                <TableHead>群</TableHead>
                <TableHead>收款方</TableHead>
                <TableHead>金额</TableHead>
                <TableHead>插件</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>重试</TableHead>
                <TableHead>错误</TableHead>
                <TableHead>键</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((item) => {
                const rowPending = pending && pendingId === item.id;
                return (
                  <TableRow key={item.id}>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {formatDateTime(item.created_at)}
                    </TableCell>
                    <TableCell>
                      <div className="max-w-44 truncate">{accountLabel(item.account_id)}</div>
                      <div className="font-mono text-[11px] text-muted-foreground">#{item.account_id}</div>
                    </TableCell>
                    <TableCell>
                      <div className="max-w-44 truncate text-sm">{item.chat_title || "-"}</div>
                      <div className="font-mono text-[11px] text-muted-foreground">{item.chat_id}</div>
                    </TableCell>
                    <TableCell>
                      <div className="max-w-44 truncate text-sm">{item.receiver_name || item.receiver_user_id || "-"}</div>
                      <div className="font-mono text-[11px] text-muted-foreground">
                        {item.receiver_user_id != null ? `UID ${item.receiver_user_id}` : "UID -"}
                      </div>
                    </TableCell>
                    <TableCell className="text-right font-mono font-semibold tabular-nums text-warning">
                      {formatAmount(item.amount)}
                    </TableCell>
                    <TableCell>
                      <div className="max-w-40 truncate font-mono text-xs">{item.plugin_key || "-"}</div>
                      <div className="max-w-40 truncate font-mono text-[11px] text-muted-foreground">{item.entry_key || "-"}</div>
                    </TableCell>
                    <TableCell>
                      <CompensationStatusBadge status={item.status} ambiguous={item.ambiguous} />
                    </TableCell>
                    <TableCell className="font-mono text-xs">{item.retry_count}</TableCell>
                    <TableCell className="max-w-52 truncate font-mono text-xs">
                      {item.error_code_last || item.error_code_first || item.error_last || "-"}
                    </TableCell>
                    <TableCell className="max-w-44 truncate font-mono text-xs">{item.payout_key}</TableCell>
                    <TableCell className="text-right">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        loading={rowPending}
                        disabled={pending}
                        onClick={() => onManualPaid(item)}
                      >
                        {!rowPending ? <CheckCircle2 className="mr-2 h-4 w-4" /> : null}
                        核销
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          </div>
          <div className="md:hidden">
            <ResponsiveList
              data={items}
              rowKey={(item) => item.id}
              columns={[
                { key: "time", header: "创建时间", priority: 0, render: (item) => <div className="text-xs text-muted-foreground">{formatDateTime(item.created_at)}</div> },
                { key: "amount", header: "金额", priority: 0, render: (item) => <div className="font-mono text-sm font-semibold tabular-nums text-warning">{formatAmount(item.amount)}</div> },
                { key: "status", header: "状态", priority: 1, render: (item) => <CompensationStatusBadge status={item.status} ambiguous={item.ambiguous} /> },
                { key: "account", header: "账号", priority: 1, render: (item) => <><div className="truncate text-sm">{accountLabel(item.account_id)}</div><div className="font-mono text-[11px] text-muted-foreground">#{item.account_id}</div></> },
                { key: "chat", header: "群", priority: 1, render: (item) => <><div className="truncate text-sm">{item.chat_title || "-"}</div><div className="font-mono text-[11px] text-muted-foreground">{item.chat_id}</div></> },
                { key: "receiver", header: "收款方", priority: 2, render: (item) => <><div className="text-sm">{item.receiver_name || item.receiver_user_id || "-"}</div><div className="font-mono text-[11px] text-muted-foreground">{item.receiver_user_id != null ? `UID ${item.receiver_user_id}` : "UID -"}</div></> },
                { key: "plugin", header: "插件", priority: 2, render: (item) => <><div className="font-mono text-xs">{item.plugin_key || "-"}</div><div className="break-all font-mono text-[11px] text-muted-foreground">{item.entry_key || "-"}</div></> },
                { key: "retry", header: "重试", priority: 2, render: (item) => <div className="font-mono text-xs">{item.retry_count}</div> },
                { key: "error", header: "错误", priority: 2, render: (item) => <div className="font-mono text-xs">{item.error_code_last || item.error_code_first || item.error_last || "-"}</div> },
                { key: "key", header: "键", priority: 2, render: (item) => <div className="break-all font-mono text-xs">{item.payout_key}</div> },
                { key: "action", header: "操作", priority: 2, render: (item) => { const rowPending = pending && pendingId === item.id; return <Button type="button" size="sm" variant="outline" loading={rowPending} disabled={pending} onClick={() => onManualPaid(item)}>{!rowPending ? <CheckCircle2 className="mr-2 h-4 w-4" /> : null}核销</Button>; } },
              ]}
              empty={<EmptyState title="暂无待补付事项" />}
            />
          </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="min-w-0 space-y-1.5">
      <span className="block truncate text-xs font-medium text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function DirectionBadge({ direction, posted = true }: { direction: LedgerDirection; posted?: boolean }) {
  if (!posted) {
    return (
      <MetaBadge tone="outline">
        {direction === "in" ? <ArrowDownLeft className="h-3 w-3" /> : <ArrowUpRight className="h-3 w-3" />}
        {direction === "in" ? "入账尝试" : "出账尝试"}
      </MetaBadge>
    );
  }
  if (direction === "in") {
    return (
      <MetaBadge tone="success">
        <ArrowDownLeft className="h-3 w-3" />
        入账
      </MetaBadge>
    );
  }
  return (
    <MetaBadge tone="warn">
      <ArrowUpRight className="h-3 w-3" />
      出账
    </MetaBadge>
  );
}

function isPostedLedgerStatus(status: string): boolean {
  return status === "OK" || status === "COMPENSATED";
}

function StatusBadge({ status }: { status: string }) {
  const tone = status === "OK" ? "success" : status === "FAILED" ? "danger" : status === "DRY_RUN" ? "warn" : "outline";
  return <MetaBadge tone={tone}>{status || "-"}</MetaBadge>;
}

function CompensationStatusBadge({ status, ambiguous }: { status: string; ambiguous: boolean }) {
  const tone = status === "pending" ? "warn" : status === "abandoned" ? "danger" : "outline";
  return (
    <div className="flex flex-wrap gap-1">
      <MetaBadge tone={tone}>{compensationStatusLabel(status)}</MetaBadge>
      {ambiguous ? <MetaBadge tone="warn">待确认</MetaBadge> : null}
    </div>
  );
}

function LoadingState() {
  return (
    <div className="flex h-36 items-center justify-center text-muted-foreground">
      <Spinner className="mr-2" />
      加载中
    </div>
  );
}

function buildQueryParams(filters: FilterState): Omit<LedgerQueryParams, "limit"> {
  return {
    since: filters.since || undefined,
    until: filters.until || undefined,
    account_id: filters.account_id || undefined,
    chat_id: filters.chat_id || undefined,
    plugin_key: filters.plugin_key.trim() || undefined,
    direction: filters.direction || undefined,
    amount: filters.amount.trim() || undefined,
    amount_min: filters.amount_min.trim() || undefined,
    amount_max: filters.amount_max.trim() || undefined,
    status: filters.status || undefined,
  };
}

function buildStatsQueryParams(filters: FilterState): LedgerStatsQueryParams {
  return {
    since: filters.since || undefined,
    until: filters.until || undefined,
    account_id: filters.account_id || undefined,
    chat_id: filters.chat_id || undefined,
    plugin_key: filters.plugin_key.trim() || undefined,
  };
}

function aggregateTrendBuckets(items: LedgerSummaryBucket[], period: TrendPeriod): LedgerSummaryBucket[] {
  if (period === "day") return items;
  const buckets = new Map<string, { key: string; label: string; income: number; payout: number; count: number }>();
  for (const item of items) {
    const group = trendGroup(item.key, period);
    const current = buckets.get(group.key) || { ...group, income: 0, payout: 0, count: 0 };
    current.income += chartNumber(item.income);
    current.payout += chartNumber(item.payout);
    current.count += item.count;
    buckets.set(group.key, current);
  }
  return [...buckets.values()].sort((a, b) => a.key.localeCompare(b.key)).map((item) => ({
    key: item.key,
    label: item.label,
    income: String(item.income),
    payout: String(item.payout),
    net: String(item.income - item.payout),
    count: item.count,
  }));
}

function trendGroup(dayKey: string, period: Exclude<TrendPeriod, "day">) {
  const date = new Date(`${dayKey}T00:00:00Z`);
  if (period === "month") {
    const key = dayKey.slice(0, 7);
    return { key, label: key };
  }
  const weekday = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - weekday);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const week = Math.ceil((((date.getTime() - yearStart.getTime()) / 86400000) + 1) / 7);
  const key = `${date.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
  return { key, label: key };
}

function recipientDisplayName(entry: LedgerEntry) {
  if (entry.receiver_name) return entry.receiver_name;
  if (entry.receiver_username) return `@${entry.receiver_username.replace(/^@/, "")}`;
  if (entry.receiver_user_id != null) return String(entry.receiver_user_id);
  return "-";
}

function formatAmount(value: string) {
  return value || "0";
}

function formatInteger(value: number) {
  return new Intl.NumberFormat().format(value || 0);
}

function formatPercent(value: string | null) {
  if (value == null) return "-";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return `${value}%`;
  return `${parsed.toFixed(2)}%`;
}

function payoutRateTone(value: string | null, attempts: number): VisualTone {
  if (attempts <= 0 || value == null) return "neutral";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "neutral";
  if (parsed >= 90) return "success";
  if (parsed >= 70) return "warn";
  return "danger";
}

function formatSignedAmount(entry: LedgerEntry) {
  if (entry.direction === "in") return `+${entry.amount}`;
  return `-${entry.amount}`;
}

function isNegative(value: string) {
  return String(value || "").trim().startsWith("-");
}

function amountToneClass(value: string) {
  if (isNegative(value)) return "text-destructive";
  if (String(value || "") !== "0") return "text-success";
  return "text-muted-foreground";
}

function chartNumber(value: string) {
  const next = Number(value || "0");
  return Number.isFinite(next) ? next : 0;
}

function formatDateTime(value: string) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function compensationStatusLabel(status: string) {
  if (status === "pending") return "待补付";
  if (status === "abandoned") return "已放弃";
  if (status === "compensated") return "已核销";
  if (status === "sent") return "已补付";
  return status || "-";
}
