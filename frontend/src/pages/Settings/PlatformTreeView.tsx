import { useMemo, useState } from "react";
import { Pencil } from "lucide-react";

import type { PlatformModuleKey, PlatformTree } from "@/api/types";
import { Button } from "@/components/ui/button";
import { moduleLabel, runtimeStateLabel } from "@/lib/navigation";
import { cn } from "@/lib/utils";

type TreeLeaf = PlatformTree["leaves"][number];
type TreeLeafDetails = { displayName: string; canEdit: boolean };

type HoverTarget =
  | { kind: "branch"; key: PlatformModuleKey }
  | { kind: "leaf"; key: string }
  | null;

const BRANCH_ORDER: PlatformModuleKey[] = [
  "ai",
  "interaction_bot",
  "webhooks",
  "ledger",
  "dispatch_debug",
];

function sortLeaves(leaves: TreeLeaf[]): TreeLeaf[] {
  return [...leaves].sort(
    (a, b) => Number(b.enabled) - Number(a.enabled) || a.key.localeCompare(b.key),
  );
}

export function PlatformTreeView({
  tree,
  leafDetails,
  onEditLeaf,
}: {
  tree: PlatformTree;
  leafDetails?: ReadonlyMap<string, TreeLeafDetails>;
  onEditLeaf?: (key: string) => void;
}) {
  const [hover, setHover] = useState<HoverTarget>(null);

  const groups = useMemo(() => {
    const trunkLeaves: TreeLeaf[] = [];
    const interactionLeaves: TreeLeaf[] = [];
    for (const leaf of sortLeaves(tree.leaves)) {
      if (leaf.attachment === "交互") {
        interactionLeaves.push(leaf);
      } else {
        trunkLeaves.push(leaf);
      }
    }
    return { trunkLeaves, interactionLeaves };
  }, [tree.leaves]);

  const leavesByKey = useMemo(
    () => new Map(tree.leaves.map((leaf) => [leaf.key, leaf])),
    [tree.leaves],
  );

  const safeWatch = tree.trunk.current_profile === "safe_watch";
  const { alive, total } = tree.trunk.userbot;

  // 叶的生长位置由嫁接通道决定；requires 是取养分的管线。
  const hostBranchOf = (leaf: TreeLeaf): PlatformModuleKey | null =>
    leaf.attachment === "交互" ? "interaction_bot" : null;

  const leafFeedsFrom = (leaf: TreeLeaf, key: PlatformModuleKey): boolean =>
    leaf.requires.includes(key) || hostBranchOf(leaf) === key;

  const branchDimmed = (key: PlatformModuleKey): boolean => {
    if (!hover) return false;
    if (hover.kind === "branch") return hover.key !== key;
    const leaf = leavesByKey.get(hover.key);
    return leaf ? !leafFeedsFrom(leaf, key) : false;
  };

  const branchRinged = (key: PlatformModuleKey): boolean => {
    if (hover?.kind !== "leaf") return false;
    const leaf = leavesByKey.get(hover.key);
    return leaf ? leafFeedsFrom(leaf, key) : false;
  };

  const leafDimmed = (leaf: TreeLeaf): boolean => {
    if (!hover) return false;
    if (hover.kind === "leaf") return hover.key !== leaf.key;
    return !leafFeedsFrom(leaf, hover.key);
  };

  const clearHover = () => setHover(null);

  const renderLeafRow = (leaf: TreeLeaf) => {
    const details = leafDetails?.get(leaf.key);
    return (
    <div
      key={leaf.key}
      tabIndex={0}
      onMouseEnter={() => setHover({ kind: "leaf", key: leaf.key })}
      onMouseLeave={clearHover}
      onFocus={() => setHover({ kind: "leaf", key: leaf.key })}
      onBlur={clearHover}
      className={cn(
        "ptree-twig flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-sm py-1 pl-1 pr-2 text-xs transition-opacity duration-150",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        hover?.kind === "leaf" && hover.key === leaf.key && "bg-muted/30",
        leafDimmed(leaf) && "opacity-40",
      )}
    >
      <span
        className={cn(
          "h-1.5 w-1.5 shrink-0 rounded-full",
          leaf.enabled ? "bg-success" : "border border-muted-foreground/60 bg-transparent",
        )}
        aria-hidden
      />
      <span className="min-w-0 max-w-full">
        <span className={cn("break-words font-medium", !leaf.enabled && "text-muted-foreground")}>
          {details?.displayName || leaf.key}
        </span>
        {details?.displayName && details.displayName !== leaf.key ? (
          <span className="ml-1.5 break-all font-mono text-[10px] text-muted-foreground">
            {leaf.key}
          </span>
        ) : null}
      </span>
      <span className="rounded bg-muted px-1 py-px text-[10px] leading-4 text-muted-foreground">
        {leaf.attachment}
      </span>
      {!leaf.enabled ? <span className="text-muted-foreground">未启用</span> : null}
      {leaf.requires
        .filter((key) => key !== hostBranchOf(leaf))
        .map((key) => (
          <span
            key={key}
            className={cn(
              "rounded border px-1 text-[10px] leading-4 transition-colors duration-150",
              hover?.kind === "branch" && hover.key === key
                ? "border-success/70 text-success"
                : "border-border text-muted-foreground",
            )}
          >
            +{moduleLabel(key)}
          </span>
        ))}
      {leaf.warnings.map((warning) => (
        <span key={warning} className="basis-full pl-3.5 text-warning">
          {leaf.source_missing ? "源缺失：" : "警告："}
          {warning}
        </span>
      ))}
      {details?.canEdit && onEditLeaf ? (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="ml-auto h-6 shrink-0 px-2 text-[10px]"
          onClick={() => onEditLeaf(leaf.key)}
          aria-label={`编辑 ${details.displayName || leaf.key}`}
        >
          <Pencil className="mr-1 h-3 w-3" />
          编辑
        </Button>
      ) : null}
    </div>
    );
  };

  return (
    <div className="text-xs" onMouseLeave={clearHover}>
      {/* 树干：账号本体、总闸、预设 */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 pb-3">
        <span className="inline-flex items-center gap-1.5 font-medium">
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              total === 0
                ? "bg-destructive"
                : alive === total
                  ? "bg-success"
                  : "bg-warning",
            )}
            aria-hidden
          />
          Userbot {alive}/{total}
        </span>
        <span
          className={cn(
            tree.trunk.kill_switch ? "font-medium text-destructive" : "text-muted-foreground",
          )}
        >
          总闸 {tree.trunk.kill_switch ? "已拉闸" : "正常"}
        </span>
        <span className="text-muted-foreground">
          预设{" "}
          {tree.trunk.current_profile === "safe_watch"
            ? "值守"
            : tree.trunk.current_profile === "custom"
              ? "自定义"
              : "生产"}
        </span>
        {safeWatch ? (
          <span className="inline-flex items-center gap-1 rounded-full border border-warning/50 px-2 py-0.5 text-warning">
            ✂ 值守中 · 叶投递已暂停
          </span>
        ) : null}
      </div>
      <p className="pb-3 text-[11px] text-muted-foreground/70">
        叶挂在自己的嫁接通道上（直通、命令贴干；交互长在交互枝）；「+能力」是它额外取养分的枝，悬停可见牵连。
      </p>

      {/* 干与枝：值守时全树入冬（降饱和），树干状态行保持可读 */}
      <div
        className={cn(
          "ptree-spine space-y-4 pb-1 transition-[filter,opacity] duration-300",
          safeWatch && "opacity-90 saturate-[0.35]",
        )}
      >
        {groups.trunkLeaves.length > 0 ? (
          <div className="ptree-limb ptree-limb--flush">
            <div className="pl-1 text-[11px] text-muted-foreground">贴干叶 · 消息直接来自树干</div>
            <div className="ptree-twigs mt-1.5">{groups.trunkLeaves.map(renderLeafRow)}</div>
          </div>
        ) : null}

        {BRANCH_ORDER.map((key) => {
          const branch = tree.branches[key];
          if (!branch) return null;
          const leaves = key === "interaction_bot" ? groups.interactionLeaves : [];
          const aliveBranch = branch.desired;
          return (
            <div
              key={key}
              className={cn(
                "ptree-limb",
                aliveBranch ? "ptree-limb--alive" : "ptree-limb--dead",
              )}
            >
              <div
                tabIndex={0}
                onMouseEnter={() => setHover({ kind: "branch", key })}
                onMouseLeave={clearHover}
                onFocus={() => setHover({ kind: "branch", key })}
                onBlur={clearHover}
                className={cn(
                  "inline-flex flex-wrap items-center gap-2 rounded-md border bg-muted/20 px-3 py-1.5 transition-[opacity,box-shadow] duration-150",
                  "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                  aliveBranch ? "border-success/40" : "border-dashed border-border",
                  branchRinged(key) && "ring-1 ring-success/60",
                  branchDimmed(key) && "opacity-40",
                )}
              >
                <span
                  className={cn(
                    "h-1.5 w-1.5 rounded-full",
                    aliveBranch ? "bg-success" : "bg-muted-foreground/40",
                  )}
                  aria-hidden
                />
                <span className={cn("font-medium", !aliveBranch && "text-muted-foreground")}>
                  {moduleLabel(key)}
                </span>
                <span className="text-muted-foreground">
                  {branch.desired ? runtimeStateLabel(branch.state) : "已关闭"}
                </span>
                {branch.forced_off ? <span className="text-destructive">强制关闭</span> : null}
                <span className="text-muted-foreground/80">
                  {leaves.length > 0
                    ? `${leaves.length} 叶`
                    : branch.demanded_by.length > 0
                      ? `被 ${branch.demanded_by.length} 叶取养分`
                      : branch.can_turn_off
                        ? "无叶需要 · 可关"
                        : "无叶需要"}
                </span>
              </div>
              {leaves.length > 0 ? (
                <div className={cn("ptree-twigs mt-2", aliveBranch && "ptree-twigs--alive")}>
                  {leaves.map(renderLeafRow)}
                </div>
              ) : null}
            </div>
          );
        })}

        {tree.leaves.length === 0 ? (
          <div className="ptree-limb ptree-limb--dead">
            <div className="inline-flex rounded-md border border-dashed px-3 py-1.5 text-muted-foreground">
              当前没有已登记的插件叶。
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
