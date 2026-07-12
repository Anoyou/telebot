// 账号详情 → 允许会话 tab：左侧最近活跃会话（一键加入），右侧已允许列表（手填+移除）
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import {
  addIgnoredPeer,
  listIgnoredPeers,
  listRecentPeers,
  removeIgnoredPeer,
} from "@/api/ignored_peers";
import { getAccount } from "@/api/accounts";
import type { IgnoredPeer, PeerKind, RecentPeerItem } from "@/api/types";
import { getErrMsg } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";

// ── peer_kind 中文标签 ────────────────────────────────────────────
const KIND_LABEL: Record<string, string> = {
  private: "私聊",
  group: "普通群",
  supergroup: "超级群",
  channel: "频道",
};

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] || kind;
}

// ── 简易相对时间："刚刚 / N 分钟前 / N 小时前 / N 天前" ──────────
function timeAgo(epochSec: number): string {
  if (!epochSec || epochSec <= 0) return "—";
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - epochSec));
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

export function IgnoredTab({ aid }: { aid: number }) {
  const qc = useQueryClient();

  // ── 数据：账号状态 + 最近活跃 + 已允许 ──
  // 拉一次账号状态用于"为什么最近活跃为空"的精准引导：
  //   - paused / login_required → "账号未运行，先到概览启动"
  //   - active 但列表空        → "worker 在线但近期没有 incoming 消息"
  const accQ = useQuery({
    queryKey: ["account", aid],
    queryFn: () => getAccount(aid),
    enabled: !!aid,
  });
  const recentQ = useQuery({
    queryKey: ["recent-peers", aid],
    queryFn: () => listRecentPeers(aid),
    refetchInterval: 5_000, // 5s 轮询；worker 写入是内存级，足够快
    refetchIntervalInBackground: false,
  });
  const ignoredQ = useQuery({
    queryKey: queryKeys.ignoredPeers(aid),
    queryFn: () => listIgnoredPeers(aid),
  });

  // 后端已经把 "worker 在跑没消息" 和 "worker 离线" 拆成两态
  const recentItems = recentQ.data?.items ?? [];
  const workerAlive = recentQ.data?.worker_alive ?? false;

  // 把已允许 peer_id 抽成 Set，便于"最近活跃"列表过滤
  const ignoredSet = useMemo(
    () => new Set((ignoredQ.data ?? []).map((x) => x.peer_id)),
    [ignoredQ.data],
  );

  // 已允许的 peer 不再出现在"最近活跃"——加入允许后立刻消失，避免重复添加。
  const visibleRecentItems = useMemo(
    () => recentItems.filter((p) => !ignoredSet.has(p.peer_id)),
    [recentItems, ignoredSet],
  );
  // 被过滤掉的数量——空状态文案要告诉用户"不是没有最近活跃，是都已加入允许会话"
  const hiddenIgnoredCount = recentItems.length - visibleRecentItems.length;

  // ── mutation ──
  const addMut = useMutation({
    mutationFn: async (vars: {
      peer_id: number;
      peer_kind?: string;
      peer_label?: string | null;
    }) => addIgnoredPeer(aid, vars),
    onSuccess: () => {
      toast.success("已加入允许会话");
      qc.invalidateQueries({ queryKey: queryKeys.ignoredPeers(aid) });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const delMut = useMutation({
    mutationFn: async (id: number) => removeIgnoredPeer(aid, id),
    onSuccess: () => {
      toast.success("已移除");
      qc.invalidateQueries({ queryKey: queryKeys.ignoredPeers(aid) });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  // ── 手填 peer_id 加入允许会话 ──
  const [manualId, setManualId] = useState("");
  const [manualKind, setManualKind] = useState<PeerKind | "">("");
  const handleAddManual = () => {
    const trimmed = manualId.trim();
    if (!trimmed) {
      toast.error("请输入 peer_id");
      return;
    }
    // peer_id 可正可负；用 Number 解析允许负号；过滤 NaN
    const num = Number(trimmed);
    if (!Number.isFinite(num) || !Number.isInteger(num)) {
      toast.error("peer_id 必须是整数（可正可负）");
      return;
    }
    addMut.mutate(
      { peer_id: num, peer_kind: manualKind || "private" },
      {
        onSuccess: () => {
          setManualId("");
          setManualKind("");
        },
      },
    );
  };

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <RecentCard
        loading={recentQ.isLoading}
        items={visibleRecentItems}
        accountStatus={accQ.data?.status}
        workerAlive={workerAlive}
        hiddenIgnoredCount={hiddenIgnoredCount}
        onAdd={(p) =>
          addMut.mutate({
            peer_id: p.peer_id,
            peer_kind: p.peer_kind,
            peer_label: p.peer_label,
          })
        }
        adding={addMut.isPending}
      />
      <IgnoredCard
        loading={ignoredQ.isLoading}
        items={ignoredQ.data ?? []}
        onRemove={(id) => delMut.mutate(id)}
        removing={delMut.isPending}
        manualId={manualId}
        setManualId={setManualId}
        manualKind={manualKind}
        setManualKind={setManualKind}
        onAddManual={handleAddManual}
        adding={addMut.isPending}
      />
    </div>
  );
}

// ── 左卡片：最近活跃 ──
function RecentCard({
  loading,
  items,
  accountStatus,
  workerAlive,
  hiddenIgnoredCount,
  onAdd,
  adding,
}: {
  loading: boolean;
  items: RecentPeerItem[];
  accountStatus?: string;
  workerAlive: boolean;
  /** 已允许而被过滤掉的条数；用于空状态文案精准引导 */
  hiddenIgnoredCount: number;
  onAdd: (p: RecentPeerItem) => void;
  adding: boolean;
}) {
  // 三态空提示：
  //  - workerAlive=false                → "worker 没在跑，去暂停 → 启动一次"
  //  - workerAlive=true 且 全部已允许       → "都在右侧已允许列表里，看那边"
  //  - workerAlive=true 且 真没消息         → "worker 在跑，让别人发条消息试试"
  const emptyHint = !workerAlive ? (
    <>
      worker 没在跑或没响应（账号状态：
      <span className="font-medium">{accountStatus ?? "未知"}</span>）。
      <br />
      <span className="text-xs">
        请到「概览」tab → 暂停账号 → 启动账号；worker 上线后 5 秒内自动出现。
      </span>
    </>
  ) : hiddenIgnoredCount > 0 ? (
    <>
      最近 <span className="font-medium">{hiddenIgnoredCount}</span> 条活跃会话全部已在允许会话。
      <br />
      <span className="text-xs">右侧"已允许会话"可以查看 / 移除。</span>
    </>
  ) : (
    <>
      worker 已在跑，但内存里还没有最近活跃会话。
      <br />
      <span className="text-xs">
        让小号 / 群组里发条消息给这个账号试试；或在右侧手动输入 ID 加入允许会话。
      </span>
    </>
  );

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          title="最近活跃会话"
          description="worker 内存中最近 50 个 incoming 会话；已加入允许会话的不再出现在这里"
          meta={
            !loading ? (
              workerAlive ? (
                <SignalPill tone="success" label="状态" value="worker 在线" />
              ) : (
                <SignalPill tone="danger" label="状态" value="worker 离线" />
              )
            ) : null
          }
        />
      </CardHeader>
      <CardContent>
        {loading ? (
          <div className="flex h-20 items-center justify-center">
            <Spinner className="text-primary" />
          </div>
        ) : items.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            暂无最近活跃会话
            <br />
            <span className="block pt-2">{emptyHint}</span>
          </p>
        ) : (
          <ul className="divide-y">
            {items.map((p) => (
              <li
                key={p.peer_id}
                className="flex items-center justify-between gap-3 py-2.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm">
                    {p.peer_label || `(未命名) ${p.peer_id}`}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {kindLabel(p.peer_kind)} · ID {p.peer_id} ·{" "}
                    {timeAgo(p.ts)}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="shrink-0"
                  disabled={adding}
                  onClick={() => onAdd(p)}
                >
                  允许此会话
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ── 右卡片：已允许 ──
function IgnoredCard({
  loading,
  items,
  onRemove,
  removing,
  manualId,
  setManualId,
  manualKind,
  setManualKind,
  onAddManual,
  adding,
}: {
  loading: boolean;
  items: IgnoredPeer[];
  onRemove: (id: number) => void;
  removing: boolean;
  manualId: string;
  setManualId: (v: string) => void;
  manualKind: PeerKind | "";
  setManualKind: (v: PeerKind | "") => void;
  onAddManual: () => void;
  adding: boolean;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">已允许会话</CardTitle>
        <CardDescription>
          白名单模式：列表为空时允许全部；列表非空时只允许这些会话触发 incoming 流程。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* 手填 peer_id */}
        <div className="grid gap-2 sm:grid-cols-[1fr_auto_auto]">
          <Input
            placeholder="手动输入 peer_id（可正可负）"
            value={manualId}
            onChange={(e) =>
              setManualId(e.target.value.replace(/[^\d-]/g, ""))
            }
            onKeyDown={(e) => {
              if (e.key === "Enter") onAddManual();
            }}
          />
          <select
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            value={manualKind}
            onChange={(e) =>
              setManualKind(e.target.value as PeerKind | "")
            }
          >
            <option value="">类型（可选）</option>
            <option value="private">私聊</option>
            <option value="group">普通群</option>
            <option value="supergroup">超级群</option>
            <option value="channel">频道</option>
          </select>
          <Button onClick={onAddManual} disabled={adding}>
            加入
          </Button>
        </div>

        {/* 列表 */}
        {loading ? (
          <div className="flex h-20 items-center justify-center">
            <Spinner className="text-primary" />
          </div>
        ) : items.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            允许会话为空：当前默认允许全部会话
          </p>
        ) : (
          <ul className="divide-y">
            {items.map((x) => (
              <li
                key={x.id}
                className="flex items-center justify-between gap-3 py-2.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm">
                    {x.peer_label || "(未命名)"}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {kindLabel(x.peer_kind)} · ID {x.peer_id}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  className="shrink-0 text-destructive hover:text-destructive"
                  disabled={removing}
                  onClick={() => onRemove(x.id)}
                >
                  移除
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
