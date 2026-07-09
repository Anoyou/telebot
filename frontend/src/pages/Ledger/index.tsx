import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowDownLeft,
  ArrowUpRight,
  CheckCircle2,
  Filter,
  HandCoins,
  RefreshCw,
  WalletCards,
} from "lucide-react";
import { toast } from "sonner";

import { listAccounts } from "@/api/accounts";
import {
  getLedgerSummary,
  listLedgerCompensations,
  listLedgerEntries,
  markLedgerCompensationManualPaid,
  type LedgerCompensation,
  type LedgerDirection,
  type LedgerEntry,
  type LedgerQueryParams,
  type LedgerSummary,
} from "@/api/ledger";
import { LineTrend } from "@/components/LineTrend";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Spinner } from "@/components/ui/misc";
import { Select } from "@/components/ui/select";
import { SectionHeader, SignalPill, ToneRailCard } from "@/components/ui/status";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
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

  const accounts = accountsQ.data || [];
  const accountLabel = (accountId: number | null | undefined) => {
    if (accountId == null) return "-";
    const account = accounts.find((item) => item.id === accountId);
    return account?.display_name || account?.phone || `账号 #${accountId}`;
  };

  const summary = summaryQ.data;
  const entries = entriesQ.data?.items || [];
  const compensations = compensationsQ.data?.items || [];
  const isLoading = entriesQ.isLoading || summaryQ.isLoading || compensationsQ.isLoading;
  const error = entriesQ.error || summaryQ.error || compensationsQ.error || accountsQ.error;

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["ledger"] });
    void queryClient.invalidateQueries({ queryKey: ["accounts"] });
  };

  const handleManualPaid = (item: LedgerCompensation) => {
    const note = window.prompt(`核销挂账 ${item.payout_key}，备注可留空。`);
    if (note === null) return;
    manualPaidMut.mutate({ id: item.id, note });
  };

  return (
    <PageShell>
      <PageHeader
        icon={WalletCards}
        title="资金台账"
        description="按入账、出账和补付挂账核对每个账号与群的资金流。"
        actions={(
          <Button type="button" variant="outline" onClick={refreshAll}>
            <RefreshCw className="mr-2 h-4 w-4" />
            刷新
          </Button>
        )}
        signals={summary ? (
          <>
            <SignalPill tone="success" label="入账" value={formatAmount(summary.income)} />
            <SignalPill tone="warn" label="出账" value={formatAmount(summary.payout)} />
            <SignalPill tone={isNegative(summary.net) ? "danger" : "primary"} label="净额" value={formatAmount(summary.net)} />
            <SignalPill tone={compensations.length > 0 ? "warn" : "neutral"} label="挂账" value={compensations.length} />
          </>
        ) : null}
      />

      {summary ? <SummaryTiles summary={summary} /> : null}

      <FilterPanel
        filters={filters}
        accounts={accounts}
        onChange={setFilters}
        onReset={() => setFilters(DEFAULT_FILTERS)}
      />

      {error ? (
        <Card>
          <CardHeader>
            <SectionHeader
              icon={AlertTriangle}
              title="读取失败"
              description={getErrMsg(error)}
            />
          </CardHeader>
        </Card>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.7fr)_minmax(320px,0.8fr)]">
        <TrendPanel summary={summary} loading={isLoading} />
        <ChatSummaryPanel summary={summary} />
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

function SummaryTiles({ summary }: { summary: LedgerSummary }) {
  return (
    <div className="grid gap-3 md:grid-cols-3">
      <ToneRailCard
        icon={ArrowDownLeft}
        title="入账"
        value={formatAmount(summary.income)}
        tone="success"
        valueClassName="truncate text-2xl font-bold tabular-nums tracking-tight"
      />
      <ToneRailCard
        icon={ArrowUpRight}
        title="出账"
        value={formatAmount(summary.payout)}
        tone="warn"
        valueClassName="truncate text-2xl font-bold tabular-nums tracking-tight"
      />
      <ToneRailCard
        icon={WalletCards}
        title="净盈亏"
        value={formatAmount(summary.net)}
        tone={isNegative(summary.net) ? "danger" : "primary"}
        valueClassName="truncate text-2xl font-bold tabular-nums tracking-tight"
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
              <option value="">全部状态</option>
              <option value="OK">OK</option>
              <option value="FAILED">FAILED</option>
              <option value="DRY_RUN">DRY_RUN</option>
              <option value="PENDING">PENDING</option>
              <option value="COMPENSATED">COMPENSATED</option>
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
  const xAxis = summary?.by_day.map((item) => item.key) || [];
  const series = [
    { name: "入账", data: summary?.by_day.map((item) => chartNumber(item.income)) || [], color: "#10b981" },
    { name: "出账", data: summary?.by_day.map((item) => chartNumber(item.payout)) || [], color: "#f59e0b" },
    { name: "净额", data: summary?.by_day.map((item) => chartNumber(item.net)) || [], color: "#2563eb" },
  ];
  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={WalletCards}
          title="日趋势"
          meta={summary ? <MetaBadge tone="outline">{summary.count} 条流水</MetaBadge> : null}
        />
      </CardHeader>
      <CardContent>
        {loading ? (
          <LoadingState />
        ) : xAxis.length === 0 ? (
          <EmptyState text="暂无流水" />
        ) : (
          <LineTrend xAxis={xAxis} series={series} height={260} />
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
          <EmptyState text="暂无群汇总" />
        ) : (
          <div className="space-y-2">
            {rows.map((item) => (
              <div
                key={item.key}
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-lg border border-border/70 bg-muted/30 px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="truncate font-mono text-sm">{item.label}</div>
                  <div className="text-xs text-muted-foreground">{item.count} 条</div>
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
          <EmptyState text="暂无流水" />
        ) : (
          <Table className="min-w-[1080px]">
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>方向</TableHead>
                <TableHead>金额</TableHead>
                <TableHead>账号</TableHead>
                <TableHead>群</TableHead>
                <TableHead>插件</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>通道</TableHead>
                <TableHead>错误</TableHead>
                <TableHead>流水键</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.map((entry) => (
                <TableRow key={`${entry.source}:${entry.source_id}`}>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                    {formatDateTime(entry.created_at)}
                  </TableCell>
                  <TableCell>
                    <DirectionBadge direction={entry.direction} />
                  </TableCell>
                  <TableCell className={cn("text-right font-mono font-semibold tabular-nums", amountToneClass(entry.signed_amount))}>
                    {formatSignedAmount(entry)}
                  </TableCell>
                  <TableCell>
                    <div className="max-w-44 truncate">{accountLabel(entry.account_id)}</div>
                    <div className="font-mono text-[11px] text-muted-foreground">#{entry.account_id}</div>
                  </TableCell>
                  <TableCell className="font-mono text-xs">{entry.chat_id ?? "-"}</TableCell>
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
              ))}
            </TableBody>
          </Table>
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
          title="挂账"
          meta={<MetaBadge tone={items.length > 0 ? "warn" : "outline"}>{items.length} 条</MetaBadge>}
        />
      </CardHeader>
      <CardContent>
        {loading ? (
          <LoadingState />
        ) : items.length === 0 ? (
          <EmptyState text="暂无挂账" />
        ) : (
          <Table className="min-w-[1100px]">
            <TableHeader>
              <TableRow>
                <TableHead>创建时间</TableHead>
                <TableHead>账号</TableHead>
                <TableHead>群</TableHead>
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
                    <TableCell className="font-mono text-xs">{item.chat_id}</TableCell>
                    <TableCell className="text-right font-mono font-semibold tabular-nums text-amber-700 dark:text-amber-300">
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
                        disabled={pending}
                        onClick={() => onManualPaid(item)}
                      >
                        {rowPending ? <Spinner className="mr-2" /> : <CheckCircle2 className="mr-2 h-4 w-4" />}
                        核销
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
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

function DirectionBadge({ direction }: { direction: LedgerDirection }) {
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

function EmptyState({ text }: { text: string }) {
  return (
    <div className="rounded-lg border border-dashed py-10 text-center text-sm text-muted-foreground">
      {text}
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

function formatAmount(value: string) {
  return value || "0";
}

function formatSignedAmount(entry: LedgerEntry) {
  if (entry.direction === "in") return `+${entry.amount}`;
  return `-${entry.amount}`;
}

function isNegative(value: string) {
  return String(value || "").trim().startsWith("-");
}

function amountToneClass(value: string) {
  if (isNegative(value)) return "text-rose-600 dark:text-rose-300";
  if (String(value || "") !== "0") return "text-emerald-700 dark:text-emerald-300";
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
