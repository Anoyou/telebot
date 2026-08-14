import { type ComponentType, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  History,
  MessageSquareText,
  Package2,
  Pencil,
  Rows3,
  Search,
  Settings2,
  Sparkles,
  Waypoints,
  Zap,
} from "lucide-react";

import { listAccountFeatures } from "@/api/accounts";
import { getFeatureMatrix } from "@/api/features";
import { listPluginLLMUsageSummary } from "@/api/llmUsage";
import {
  batchSetInstallState,
  listInstalledPackages,
  listPluginInstallHistory,
} from "@/api/plugins";
import { getPlatformTree } from "@/api/system";
import type { AccountFeatureItem, FeatureInfo } from "@/api/types";
import type { PluginInstallHistoryItem, PluginInstallOut } from "@/api/plugins";
import type { PluginLLMUsageSummaryItem } from "@/api/llmUsage";
import { PageShell } from "@/components/layout/PageScaffold";
import { Spinner } from "@/components/ui/misc";
import { Button } from "@/components/ui/button";
import { MetaBadge } from "@/components/ui/meta-badge";
import {
  SectionHeader,
  SignalPill,
} from "@/components/ui/status";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { pluginUsageGuideWarning, splitPluginWarnings } from "@/lib/plugin-config-contract";
import { isPlatformFeature } from "@/lib/plugin-modes";
import { cn, formatDateTime } from "@/lib/utils";
import { PlatformTreeView } from "@/pages/Settings/PlatformTreeView";
import {
  compactUsageText,
  pluginContractRiskWarnings,
  pluginEventSubscriptionLabels,
  pluginHasHighRiskContract,
  pluginOperationalCapabilityLabels,
  accountDirectPassthroughEnabled,
  formatDirectPassthroughRankLabel,
  formatDirectPassthroughRankTitle,
  rankAccountDirectPassthroughPlugins,
  pluginSupportsDirectPassthrough,
  pluginUsesAI,
} from "@/types/pluginContract";

import { featureConfigPath } from "./_shared/featureConfig";
import { AccountRuntimePanel } from "./AccountRuntimePanel";
import { PluginWorkspaceHeader } from "./WorkspaceHeader";

type ModuleCategory = "direct_passthrough" | "interactive" | "automation" | "utility";
type ModuleCategoryFilter = "all" | ModuleCategory;
type PluginStatusFilter = "all" | "enabled" | "disabled" | "failed";
type PluginView = "cards" | "tree";
const CATEGORY_META: Record<ModuleCategory, { title: string; hint: string; icon: ComponentType<{ className?: string }> }> = {
  direct_passthrough: {
    title: "裸直通",
    hint: "声明 telegram_direct_passthrough 的低延时插件；启用仍需账号配置二次开关。",
    icon: Zap,
  },
  interactive: {
    title: "互动娱乐",
    hint: "可交互的游戏、娱乐和群内互动插件。",
    icon: Sparkles,
  },
  automation: {
    title: "自动化",
    hint: "自动回复、转发、定时等账号自动化能力。",
    icon: Settings2,
  },
  utility: {
    title: "工具能力",
    hint: "AI、媒体生成和其他辅助工具插件。",
    icon: Package2,
  },
};
function moduleRuntimeLabel(status: string, enabled: boolean) {
  if (!enabled) return "已停用";
  if (status === "active") return "运行中";
  if (status === "failed") return "异常";
  return "等待 worker 生效";
}

function moduleSourceLabel(feature: FeatureInfo) {
  if (feature.source_label === "Official") return "历史安装记录";
  if (feature.source_label === "core") return "平台";
  return feature.source_type === "remote" ? "远程" : "本地";
}

function moduleTrustBadge(
  feature: FeatureInfo,
  install?: PluginInstallOut,
): { label: string; tone: "neutral" | "success" | "warn" | "danger" | "outline"; title: string } {
  const signatureOk = install?.signature_ok ?? feature.signature_ok;
  if (signatureOk === false) {
    return {
      label: "签名失败",
      tone: "danger",
      title: "安装包签名校验失败，后端会拒绝直接加载或启用。",
    };
  }
  if (feature.orphan || feature.source_label === "local-orphan") {
    return {
      label: "孤立目录",
      tone: "danger",
      title: "磁盘或 feature 表存在该插件，但后端没有找到可信安装记录。",
    };
  }
  if (feature.is_builtin) {
    return {
      label: "内置核心",
      tone: "success",
      title: "随 TelePilot 一起发布的核心能力。",
    };
  }
  if (feature.source_label === "Official" || install?.source === "official") {
    return {
      label: "历史安装记录",
      tone: "success",
      title: "升级前的安装记录；后续更新请关联使用者自行接入的插件仓库。",
    };
  }
  if (signatureOk === true) {
    return {
      label: "签名通过",
      tone: "success",
      title: "已安装包通过后端签名校验。",
    };
  }
  if (feature.source_label === "remote") {
    return {
      label: "远程 Git",
      tone: "outline",
      title: "来自远程 Git/社区仓库；当前未绑定 zip 签名状态。",
    };
  }
  if (feature.source_type === "remote") {
    return {
      label: "远程 Git",
      tone: "outline",
      title: "来自远程 Git/社区仓库；当前 feature-matrix 未暴露签名状态。",
    };
  }
  if (signatureOk === null) {
    return {
      label: "未验签",
      tone: "warn",
      title: "历史或本地安装包没有签名结果；后端兼容开关会决定是否允许加载。",
    };
  }
  return {
    label: install ? "本地安装" : "本地/孤立",
    tone: "neutral",
    title: install
      ? "本地安装插件；当前未拿到可验证签名结果。"
      : "feature-matrix 中存在该插件，但已安装包接口没有对应记录，来源需以后端补充字段确认。",
  };
}

function moduleVersionLabel(version?: string | null) {
  const value = (version || "").trim();
  if (!value) return "v-";
  return value.startsWith("v") || value.startsWith("V") ? value : `v${value}`;
}

function moduleUpdateMessage(feature: FeatureInfo) {
  const current = moduleVersionLabel(feature.version);
  const latest = moduleVersionLabel(feature.latest_version);
  if (feature.latest_version) {
    return `当前 ${current}，远程 ${latest}；请到“插件管理”更新。`;
  }
  return "远程插件有新版，请到“插件管理”更新。";
}

function formatCompactNumber(value: number) {
  if (!Number.isFinite(value)) return "0";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

export function PluginsHome() {
  const nav = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const [selectedAid, setSelectedAid] = useState<number | null>(null);
  const [guideExpanded, setGuideExpanded] = useState(false);
  const [selectedCategory, setSelectedCategory] = useState<ModuleCategoryFilter>("all");
  const [pluginSearch, setPluginSearch] = useState("");
  const [pluginStatus, setPluginStatus] = useState<PluginStatusFilter>("all");
  const [pluginView, setPluginView] = useState<PluginView>("cards");
  const [selectedPluginKeys, setSelectedPluginKeys] = useState<Set<string>>(() => new Set());
  const [historyPluginKey, setHistoryPluginKey] = useState<string | null>(null);
  const guideActive = searchParams.get("guide") === "1";
  const matrixQ = useQuery({
    queryKey: ["matrix"],
    queryFn: getFeatureMatrix,
  });
  const treeQ = useQuery({
    queryKey: ["platform", "tree"],
    queryFn: getPlatformTree,
    staleTime: 10_000,
  });
  const installedQ = useQuery({
    queryKey: ["plugins", "installed-packages"],
    queryFn: listInstalledPackages,
  });
  const pluginUsageQ = useQuery({
    queryKey: ["llm", "plugin-usage-summary"],
    queryFn: () => listPluginLLMUsageSummary({ limit: 200 }),
  });
  const pluginHistoryQ = useQuery({
    queryKey: ["plugins", "install-history", historyPluginKey],
    queryFn: () => listPluginInstallHistory(historyPluginKey || ""),
    enabled: Boolean(historyPluginKey),
  });

  const accounts = matrixQ.data?.accounts ?? [];
  const features = matrixQ.data?.features ?? [];
  const pluginFeatures = useMemo(
    () => features.filter((feature) => !isPlatformFeature(feature) && feature.key !== "forward"),
    [features],
  );
  const treePluginFeatures = useMemo(
    () => features.filter((feature) => !isPlatformFeature(feature)),
    [features],
  );
  useEffect(() => {
    if (accounts.length === 0) return;

    const accountParam = searchParams.get("account");
    const requestedAid = accountParam ? Number(accountParam) : NaN;
    const validRequestedAid =
      Number.isInteger(requestedAid) && accounts.some((a) => a.id === requestedAid);

    if (validRequestedAid) {
      setSelectedAid(requestedAid);
      return;
    }

    setSelectedAid((prev) => {
      if (prev !== null && accounts.some((a) => a.id === prev)) return prev;
      return accounts[0].id;
    });
  }, [accounts, searchParams]);

  const selectedAccount = accounts.find((a) => a.id === selectedAid) ?? null;
  const accountFeaturesQ = useQuery({
    queryKey: ["account", selectedAid, "features"],
    queryFn: () => listAccountFeatures(selectedAid!),
    enabled: selectedAid !== null,
  });
  const codexImageFeature = pluginFeatures.find((f) => f.key === "codex_image");
  const codexImageState = selectedAccount?.features?.codex_image ?? "disabled";
  const accountFeatureByKey = useMemo(() => {
    const map = new Map<string, AccountFeatureItem>();
    for (const item of Array.isArray(accountFeaturesQ.data) ? accountFeaturesQ.data : []) {
      map.set(item.feature_key, item);
    }
    return map;
  }, [accountFeaturesQ.data]);
  const installByKey = useMemo(() => {
    const map = new Map<string, PluginInstallOut>();
    for (const item of Array.isArray(installedQ.data) ? installedQ.data : []) {
      map.set(item.key, item);
    }
    return map;
  }, [installedQ.data]);
  const pluginUsageByKey = useMemo(() => {
    const map = new Map<string, PluginLLMUsageSummaryItem>();
    for (const item of pluginUsageQ.data?.items ?? []) {
      map.set(item.plugin_key, item);
    }
    return map;
  }, [pluginUsageQ.data]);
  const treeLeafDetails = useMemo(() => {
    const details = new Map<string, { displayName: string; canEdit: boolean }>();
    for (const feature of treePluginFeatures) {
      details.set(feature.key, {
        displayName: feature.display_name || feature.key,
        canEdit: Boolean(featureConfigPath(selectedAid ?? undefined, feature.key, feature, { source: "plugins" })),
      });
    }
    return details;
  }, [selectedAid, treePluginFeatures]);
  const openTreeLeafEditor = (key: string) => {
    const feature = treePluginFeatures.find((item) => item.key === key);
    if (!feature) return;
    const path = featureConfigPath(selectedAid ?? undefined, key, feature, { source: "plugins" });
    if (path) nav(path);
  };

  const grouped = useMemo(() => {
    const zones: Record<ModuleCategory, typeof features> = {
      direct_passthrough: [],
      interactive: [],
      automation: [],
      utility: [],
    };

    for (const feature of pluginFeatures) {
      if (pluginSupportsDirectPassthrough(feature.capabilities)) {
        zones.direct_passthrough.push(feature);
        continue;
      }
      const category = feature.category === "interactive" || feature.category === "automation"
        ? feature.category
        : "utility";
      zones[category].push(feature);
    }

    return zones;
  }, [pluginFeatures]);
  const visibleCategoryFeatures = selectedCategory === "all"
    ? pluginFeatures
    : grouped[selectedCategory];
  const filteredFeatures = useMemo(() => {
    const keyword = pluginSearch.trim().toLocaleLowerCase();
    return visibleCategoryFeatures.filter((feature) => {
      const install = installByKey.get(feature.key);
      const accountStatus = selectedAccount?.features?.[feature.key] ?? "disabled";
      if (pluginStatus === "enabled" && install?.enabled !== true) return false;
      if (pluginStatus === "disabled" && install?.enabled !== false) return false;
      if (pluginStatus === "failed" && accountStatus !== "failed") return false;
      if (!keyword) return true;
      return [
        feature.key,
        feature.display_name,
        feature.usage,
        feature.category,
        feature.source_label,
      ].some((value) => String(value || "").toLocaleLowerCase().includes(keyword));
    });
  }, [
    installByKey,
    pluginSearch,
    pluginStatus,
    selectedAccount?.features,
    visibleCategoryFeatures,
  ]);
  const selectableKeys = useMemo(
    () => filteredFeatures.filter((feature) => installByKey.has(feature.key)).map((feature) => feature.key),
    [filteredFeatures, installByKey],
  );
  useEffect(() => {
    setSelectedPluginKeys((current) => {
      const next = new Set(Array.from(current).filter((key) => selectableKeys.includes(key)));
      return next.size === current.size ? current : next;
    });
  }, [selectableKeys]);
  const batchInstallMutation = useMutation({
    mutationFn: ({ keys, enabled }: { keys: string[]; enabled: boolean }) =>
      batchSetInstallState(keys, enabled),
    onSuccess: async (result) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["plugins", "installed-packages"] }),
        queryClient.invalidateQueries({ queryKey: ["matrix"] }),
        queryClient.invalidateQueries({ queryKey: ["account", selectedAid, "features"] }),
      ]);
      setSelectedPluginKeys(new Set());
      if (result.failed) {
        const failures = result.items
          .filter((item) => !item.ok)
          .map((item) => `${item.key}：${item.message || item.code || "未知错误"}`);
        toast.error(
          `${result.enabled ? "启用" : "停用"}完成：成功 ${result.succeeded} 个，失败 ${result.failed} 个（${failures.join("；")}）`,
        );
      } else {
        toast.success(`已${result.enabled ? "启用" : "停用"} ${result.succeeded} 个插件`);
      }
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "批量操作失败"),
  });
  const runBatchInstallAction = (enabled: boolean) => {
    const keys = Array.from(selectedPluginKeys);
    if (!keys.length) return toast.info("请先选择插件");
    const action = enabled ? "全局启用" : "全局停用";
    if (!window.confirm(`${action}所选 ${keys.length} 个插件？账号级配置不会被覆盖。`)) return;
    batchInstallMutation.mutate({ keys, enabled });
  };
  const visibleCategoryMeta = selectedCategory === "all"
    ? { title: "全部已安装插件", hint: "默认展示当前已安装的全部插件，可从分类栏进一步筛选。", icon: Package2 }
    : CATEGORY_META[selectedCategory];

  if (matrixQ.isLoading) {
    return (
      <PageShell>
        <PluginWorkspaceHeader activeTab="home" guideActive={guideActive} />
        <div className="flex h-[40vh] items-center justify-center">
          <Spinner className="text-primary" />
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell>
      <PluginWorkspaceHeader activeTab="home" guideActive={guideActive} />

      {guideActive ? (
        <Card className="hidden md:block">
          <CardContent className="space-y-4 !pt-5">
            <GuideContextCard
              expanded={guideExpanded}
              onToggle={() => setGuideExpanded((v) => !v)}
              onInstall={() => nav("/plugins/manage?tab=plugins&guide=1")}
              onDone={() => {
                if (typeof window !== "undefined") {
                  localStorage.setItem("telebot.accounts.new_account_guide_seen.v4", "1");
                }
                const next = new URLSearchParams(searchParams);
                next.delete("guide");
                nav(`/plugins${next.toString() ? `?${next.toString()}` : ""}`, { replace: true });
                setGuideExpanded(false);
              }}
            />
          </CardContent>
        </Card>
      ) : null}

      {codexImageFeature && codexImageState === "failed" ? (
        <Card className="border-warning/40 bg-warning/10">
          <CardHeader className="pb-2">
            <CardTitle className="text-base text-warning">codex_image 加载提示</CardTitle>
            <CardDescription className="text-warning">
              当前账号启用了 codex_image，但 worker 未能加载这个仓库插件。系统已自动降级为失败态并保持 worker 持续运行。
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0 text-sm text-warning">
            如需恢复，请确认已在“插件管理”中安装 Codex 图片生成，并检查该账号的 Codex 配置或运行日志。
          </CardContent>
        </Card>
      ) : null}

      <Card
        className={`transition ${
          guideActive ? "siri-glow-soft" : ""
        }`}
      >
        <CardHeader>
          <SectionHeader
            icon={Package2}
            title="账号插件启用详情与配置"
            description="先选择账号，再像软件商店一样按分类浏览当前已安装插件。"
          />
        </CardHeader>
        <CardContent className="space-y-4">
          <AccountRuntimePanel />
          {accountFeaturesQ.isError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              当前账号插件状态加载失败，暂时无法显示最近错误详情。
            </div>
          ) : null}
          {pluginUsageQ.isLoading ? (
            <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
              AI 用量加载中
            </div>
          ) : null}
          {pluginUsageQ.isError ? (
            <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
              AI 用量暂不可用
            </div>
          ) : null}
          {accounts.length > 0 ? (
            <div className="flex flex-col items-stretch gap-2 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
                <span className="text-sm text-muted-foreground">选择配置的账号：</span>
                <Select
                  value={selectedAid?.toString() ?? ""}
                  onChange={(e) => setSelectedAid(Number(e.target.value))}
                  className="w-full sm:w-64"
                >
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>{a.name}</option>
                  ))}
                </Select>
              </div>
              <Button
                type="button"
                variant="outline"
                className="justify-center"
                onClick={() =>
                  nav(
                    selectedAid
                      ? `/plugins/message-template-lab?aid=${selectedAid}`
                      : "/plugins/message-template-lab",
                  )
                }
              >
                <MessageSquareText className="mr-1 h-4 w-4" />
                消息模板测试
              </Button>
            </div>
          ) : null}
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-muted/20 p-2.5">
            <div>
              <div className="text-sm font-medium">插件视图</div>
              <div className="text-xs text-muted-foreground">按使用目的浏览卡片，或按平台能力查看树状依赖。</div>
            </div>
            <div className="grid w-full gap-1 rounded-md border bg-background p-1 sm:inline-flex sm:w-auto" role="group" aria-label="切换插件视图">
              <Button
                type="button"
                size="sm"
                variant={pluginView === "cards" ? "secondary" : "ghost"}
                className="min-w-0 w-full justify-center px-2.5 sm:w-auto sm:flex-none"
                aria-pressed={pluginView === "cards"}
                onClick={() => setPluginView("cards")}
              >
                <Rows3 className="mr-1 h-4 w-4" />
                卡片视图（功能分类）
              </Button>
              <Button
                type="button"
                size="sm"
                variant={pluginView === "tree" ? "secondary" : "ghost"}
                className="min-w-0 w-full justify-center px-2.5 sm:w-auto sm:flex-none"
                aria-pressed={pluginView === "tree"}
                onClick={() => setPluginView("tree")}
              >
                <Waypoints className="mr-1 h-4 w-4" />
                树状视图（能力分类）
              </Button>
            </div>
          </div>
          {pluginView === "cards" ? (
            <>
          <div className="grid gap-2 rounded-lg border bg-muted/20 p-3 sm:grid-cols-[minmax(0,1fr)_160px_auto] sm:items-end">
            <label className="space-y-1.5 text-sm">
              <span className="text-muted-foreground">全局搜索</span>
              <span className="relative block">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={pluginSearch}
                  onChange={(event) => setPluginSearch(event.target.value)}
                  placeholder="搜索名称、key、用途或分类"
                  className="pl-9"
                />
              </span>
            </label>
            <label className="space-y-1.5 text-sm">
              <span className="text-muted-foreground">状态</span>
              <Select value={pluginStatus} onChange={(event) => setPluginStatus(event.target.value as PluginStatusFilter)}>
                <option value="all">全部状态</option>
                <option value="enabled">全局已启用</option>
                <option value="disabled">全局已停用</option>
                <option value="failed">当前账号异常</option>
              </Select>
            </label>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={!selectableKeys.length || batchInstallMutation.isPending}
                onClick={() => setSelectedPluginKeys((current) => (
                  current.size === selectableKeys.length ? new Set() : new Set(selectableKeys)
                ))}
              >
                {selectedPluginKeys.size === selectableKeys.length && selectableKeys.length ? "取消全选" : "选择当前结果"}
              </Button>
              <Button
                type="button"
                size="sm"
                disabled={!selectedPluginKeys.size || batchInstallMutation.isPending}
                onClick={() => runBatchInstallAction(true)}
              >
                全局启用（{selectedPluginKeys.size}）
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={!selectedPluginKeys.size || batchInstallMutation.isPending}
                onClick={() => runBatchInstallAction(false)}
              >
                全局停用
              </Button>
            </div>
          </div>
          <div
            data-plugin-category-layout
            className="grid min-w-0 gap-4 lg:grid-cols-[8.5rem_minmax(0,1fr)] lg:items-start"
          >
            <nav
              aria-label="插件分类"
              data-plugin-category-nav
              className="horizontal-scroll-touch flex gap-2 overflow-x-auto pb-1 lg:sticky lg:top-3 lg:flex-col lg:overflow-visible lg:pb-0"
            >
              {(["all", ...Object.keys(CATEGORY_META)] as ModuleCategoryFilter[]).map((category) => {
                const meta = category === "all"
                  ? { title: "全部", hint: "所有已安装插件", icon: Package2 }
                  : CATEGORY_META[category];
                const Icon = meta.icon;
                const count = category === "all" ? pluginFeatures.length : grouped[category].length;
                const active = selectedCategory === category;
                return (
                  <button
                    key={category}
                    type="button"
                    data-plugin-category-filter={category}
                    className={cn(
                      "flex min-w-0 shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-2 text-left transition-colors lg:w-full",
                      active
                        ? "border-primary/35 bg-primary/10 text-foreground"
                        : "border-border/70 bg-background hover:bg-muted/40",
                    )}
                    aria-current={active ? "page" : undefined}
                    onClick={() => setSelectedCategory(category)}
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    <span className="min-w-0 flex-1 truncate text-sm font-medium">{meta.title}</span>
                    <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground">{count}</span>
                  </button>
                );
              })}
            </nav>
            <FeatureZone
              icon={visibleCategoryMeta.icon}
              title={visibleCategoryMeta.title}
              hint={visibleCategoryMeta.hint}
              features={filteredFeatures}
              selectedAccountId={selectedAccount?.id}
              selectedFeatures={selectedAccount?.features ?? {}}
              selectedFeatureEnabled={selectedAccount?.feature_enabled ?? {}}
              accountFeatureByKey={accountFeatureByKey}
              installByKey={installByKey}
              pluginUsageByKey={pluginUsageByKey}
              selectedPluginKeys={selectedPluginKeys}
              onTogglePluginSelection={(key) => setSelectedPluginKeys((current) => {
                const next = new Set(current);
                if (next.has(key)) next.delete(key);
                else next.add(key);
                return next;
              })}
              onOpenHistory={setHistoryPluginKey}
            />
          </div>
            </>
          ) : (
            <Card>
              <CardHeader className="pb-3">
                <SectionHeader
                  icon={Waypoints}
                  title="树状视图（能力分类）"
                  description="叶按嫁接通道生长，能力枝显示插件依赖；悬停任一枝叶可查看关联。"
                />
              </CardHeader>
              <CardContent>
                {treeQ.isLoading ? (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner className="h-4 w-4" />正在读取平台树</div>
                ) : treeQ.isError ? (
                  <p className="text-sm text-destructive">树状视图读取失败，请稍后重试。</p>
                ) : treeQ.data ? (
                  <PlatformTreeView tree={treeQ.data} leafDetails={treeLeafDetails} onEditLeaf={openTreeLeafEditor} />
                ) : null}
              </CardContent>
            </Card>
          )}
        </CardContent>
      </Card>
      <PluginInstallHistoryDialog
        pluginKey={historyPluginKey}
        history={pluginHistoryQ.data ?? []}
        loading={pluginHistoryQ.isLoading}
        error={pluginHistoryQ.error}
        onOpenChange={(open) => {
          if (!open) setHistoryPluginKey(null);
        }}
      />
    </PageShell>
  );
}

function FeatureCapabilityBadge({
  show,
  tone = "neutral",
  title,
  onClick,
  children,
}: {
  show: boolean;
  tone?: "neutral" | "success" | "warn" | "danger" | "info" | "outline";
  title?: string;
  onClick?: () => void;
  children: React.ReactNode;
}) {
  const interactive = Boolean(show && onClick);
  if (!show) return null;

  return (
    <MetaBadge
      tone={tone}
      className="h-7 shrink-0 justify-center px-2 text-[10px]"
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      title={title}
      onClick={interactive ? onClick : undefined}
      onKeyDown={
        interactive
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onClick?.();
              }
            }
          : undefined
      }
    >
      {children}
    </MetaBadge>
  );
}

function ModuleLintWarnings({ warnings }: { warnings?: string[] }) {
  const [expanded, setExpanded] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const warningGroups = splitPluginWarnings(warnings);
  const cleanWarnings = warningGroups.all;
  const hasHighWarnings = warningGroups.high.length > 0;

  if (cleanWarnings.length === 0) return null;

  const visibleWarnings = showAll ? cleanWarnings : cleanWarnings.slice(0, 3);
  const panelClassName = hasHighWarnings
    ? "mt-2 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-xs text-destructive"
    : "mt-2 rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-xs text-warning";
  const linkClassName = hasHighWarnings
    ? "text-destructive underline underline-offset-2 hover:text-destructive/80"
    : "text-warning underline underline-offset-2 hover:text-warning/80";

  return (
    <div className={panelClassName}>
      <button
        type="button"
        className="flex w-full items-center justify-between gap-2 text-left"
        onClick={() => {
          setExpanded((value) => !value);
          if (expanded) setShowAll(false);
        }}
        aria-expanded={expanded}
      >
        <span className="flex min-w-0 items-center gap-2">
          <MetaBadge tone={hasHighWarnings ? "danger" : "warn"} className="shrink-0">
            {hasHighWarnings ? "高级规范警告" : "插件 lint"}
          </MetaBadge>
          <span className="flex min-w-0 items-center gap-1 truncate">
            {hasHighWarnings ? <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> : null}
            <span className="truncate">
              {hasHighWarnings ? `${warningGroups.high.length} 条高级警告` : `${cleanWarnings.length} 条 lint 提醒`}
            </span>
          </span>
        </span>
        <span className="shrink-0">
          {expanded ? "收起" : "展开"}
        </span>
      </button>
      {expanded ? (
        <div className="mt-2 space-y-1">
          {visibleWarnings.map((warning, index) => (
            <div key={`${warning}-${index}`} className="break-words leading-5">
              {warning}
            </div>
          ))}
          {cleanWarnings.length > 3 && !showAll ? (
            <button
              type="button"
              className={linkClassName}
              onClick={() => setShowAll(true)}
            >
              查看全部 {cleanWarnings.length} 条
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function GuideContextCard({
  expanded,
  onToggle,
  onInstall,
  onDone,
}: {
  expanded: boolean;
  onToggle: () => void;
  onInstall: () => void;
  onDone: () => void;
}) {
  const percent = 100;

  if (!expanded) {
    return (
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={onToggle}
        className="liquid-glass justify-start text-primary hover:text-primary"
        aria-label="打开新手指引"
      >
        <Sparkles className="h-4 w-4" />
        新手指引：当前第 3 步，点击展开详情
      </Button>
    );
  }

  return (
    <div className="max-w-lg rounded-2xl border bg-card/95 p-4 shadow-lg shadow-primary/10 backdrop-blur">
      <div className="mb-2 flex items-center justify-between text-xs text-muted-foreground">
        <span>新手指引</span>
        <button type="button" onClick={onToggle} className="hover:text-foreground">
          收起
        </button>
      </div>
      <div className="mb-2 text-sm font-semibold">3. 启用指令模板或调用插件</div>
      <p className="text-xs leading-relaxed text-muted-foreground">
        这一页主要看三处：先用“指令模板”复用指令；再看下方插件卡片，按账号启用和配置；需要外部能力时点“插件管理”添加远程插件。
      </p>
      <div className="mt-3 grid gap-2 text-xs text-muted-foreground sm:grid-cols-3">
        <div className="rounded-lg border bg-muted/30 p-2">A. 指令模板</div>
        <div className="rounded-lg border bg-muted/30 p-2">B. 插件启用状态</div>
        <div className="rounded-lg border bg-muted/30 p-2">C. 插件管理</div>
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary transition-all"
          style={{ width: `${percent}%` }}
        />
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button size="sm" onClick={onInstall}>
          管理远程插件 <ArrowRight className="ml-1 h-4 w-4" />
        </Button>
        <Button size="sm" variant="outline" onClick={onDone}>
          我学会了！
        </Button>
      </div>
    </div>
  );
}

function FeatureZone({
  icon,
  title,
  hint,
  features,
  selectedAccountId,
  selectedFeatures,
  selectedFeatureEnabled,
  accountFeatureByKey,
  installByKey,
  pluginUsageByKey,
  selectedPluginKeys,
  onTogglePluginSelection,
  onOpenHistory,
}: {
  icon: ComponentType<{ className?: string }>;
  title: string;
  hint: string;
  features: FeatureInfo[];
  selectedAccountId?: number;
  selectedFeatures: Record<string, string>;
  selectedFeatureEnabled: Record<string, boolean>;
  accountFeatureByKey: Map<string, AccountFeatureItem>;
  installByKey: Map<string, PluginInstallOut>;
  pluginUsageByKey: Map<string, PluginLLMUsageSummaryItem>;
  selectedPluginKeys: Set<string>;
  onTogglePluginSelection: (key: string) => void;
  onOpenHistory: (key: string) => void;
}) {
  const nav = useNavigate();
  const [mobileExpandedKeys, setMobileExpandedKeys] = useState<Set<string>>(() => new Set());
  // 账号级直通名次：相对「本账号所有已开二次开关」的插件，而非裸数字 0/1000
  const directRankByKey = useMemo(
    () =>
      rankAccountDirectPassthroughPlugins(
        Array.from(accountFeatureByKey.entries()).map(([key, item]) => ({
          key,
          config: (item.config ?? {}) as Record<string, unknown>,
        })),
      ),
    [accountFeatureByKey],
  );
  const directRankTotal = directRankByKey.size;

  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionHeader
          icon={icon}
          title={title}
          description={hint}
          meta={(
            <div className="flex items-center gap-2">
              <SignalPill tone="neutral" label="插件" value={features.length} className="h-8" />
            </div>
          )}
        />
      </CardHeader>
      <CardContent>
        {features.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无内容</p>
        ) : (
          <div className="grid grid-cols-1 gap-2 min-[380px]:grid-cols-2 sm:grid-cols-[repeat(auto-fill,minmax(18rem,1fr))] xl:grid-cols-[repeat(auto-fill,minmax(20rem,1fr))]">
            {features.map((f) => {
              const directPassthrough = pluginSupportsDirectPassthrough(f.capabilities);
              const status = selectedFeatures[f.key] ?? "disabled";
              const enabled = selectedFeatureEnabled[f.key] ?? status !== "disabled";
              const runtimeLabel = moduleRuntimeLabel(status, enabled);
              const accountFeature = accountFeatureByKey.get(f.key);
              const accountConfig = (accountFeature?.config ?? {}) as Record<string, unknown>;
              const directPassthroughOn = directPassthrough && accountDirectPassthroughEnabled(accountConfig);
              const directRank = directPassthroughOn ? (directRankByKey.get(f.key) ?? null) : null;
              const pluginUsage = pluginUsageByKey.get(f.key);
              const lastError = accountFeature?.last_error?.trim();
              const usageWarning = pluginUsageGuideWarning(f);
              const contractWarnings = pluginContractRiskWarnings(f, {
                directPassthroughGated: directPassthrough,
              });
              const lintWarnings = [
                ...(usageWarning ? [usageWarning] : []),
                ...contractWarnings,
                ...(f.lint_warnings ?? []),
              ];
              const eventLabels = pluginEventSubscriptionLabels(f.event_subscriptions);
              const capabilityLabels = pluginOperationalCapabilityLabels({
                capabilities: f.capabilities,
                permissions: f.permissions,
                config_schema: f.config_schema,
                usage: f.usage,
              });
              const usesAI = pluginUsesAI({
                capabilities: f.capabilities,
                permissions: f.permissions,
                config_schema: f.config_schema,
                usage: f.usage,
              });
              const highRiskContract = pluginHasHighRiskContract(f, {
                directPassthroughGated: directPassthrough,
              });
              const trustBadge = moduleTrustBadge(f, installByKey.get(f.key));
              const path = featureConfigPath(selectedAccountId, f.key, f, {
                source: "plugins",
              });
              const canConfigure = Boolean(path);
              const mobileExpanded = mobileExpandedKeys.has(f.key);
              const stateRailTone = directPassthrough || status === "failed"
                ? "danger"
                : enabled
                  ? "success"
                  : "warn";
              return (
                <div
                  key={f.key}
                  data-plugin-card
                  data-plugin-key={f.key}
                  className={cn(
                    "relative min-h-[7.25rem] overflow-hidden rounded-md border p-2.5 pb-11 shadow-sm transition duration-200 ease-out hover:-translate-y-0.5 hover:shadow-md motion-reduce:transform-none sm:min-h-0 sm:pb-2.5",
                    status === "failed"
                      ? "border-destructive/40 bg-destructive/5"
                      : "border-border/70 bg-muted/20 hover:bg-muted/30",
                  )}
                >
                  <span
                    aria-hidden="true"
                    data-plugin-state-rail={stateRailTone}
                    className={cn(
                      "absolute inset-x-0 top-0 h-1",
                      stateRailTone === "danger"
                        ? "bg-destructive"
                        : stateRailTone === "success"
                          ? "bg-success"
                          : "bg-yellow-400",
                    )}
                  />
                  {installByKey.has(f.key) ? (
                    <label
                      className="absolute left-2 top-2.5 z-10 flex h-6 w-6 cursor-pointer items-center justify-center rounded border bg-background/90"
                      title="选择用于全局批量启停"
                    >
                      <input
                        type="checkbox"
                        className="h-4 w-4 accent-primary"
                        checked={selectedPluginKeys.has(f.key)}
                        onChange={() => onTogglePluginSelection(f.key)}
                        aria-label={`选择插件 ${f.display_name}`}
                      />
                    </label>
                  ) : null}
                  <MetaBadge
                    mono
                    tone="outline"
                    className="absolute right-2 top-2 h-5 max-w-[3.75rem] justify-center px-1 text-[10px] sm:right-2.5 sm:top-2.5 sm:h-6 sm:max-w-20 sm:px-1.5"
                    title={moduleVersionLabel(f.version)}
                    data-plugin-version
                  >
                    {moduleVersionLabel(f.version)}
                  </MetaBadge>
                  <div className="min-w-0">
                    <div className="flex min-w-0 flex-col gap-1.5 sm:flex-row sm:items-start sm:justify-between sm:gap-2">
                      <div className={cn("min-w-0 pr-14 sm:pr-0", installByKey.has(f.key) && "pl-8")}>
                        <div className="flex min-w-0 items-start gap-1">
                          <button
                            type="button"
                            className="flex min-w-0 flex-1 items-start gap-1 text-left sm:pointer-events-none"
                            aria-expanded={mobileExpanded}
                            onClick={() => setMobileExpandedKeys((current) => {
                              const next = new Set(current);
                              if (next.has(f.key)) next.delete(f.key);
                              else next.add(f.key);
                              return next;
                            })}
                          >
                            <span className="line-clamp-2 min-w-0 break-words text-[13px] font-medium leading-5 sm:text-sm" title={f.display_name}>
                              {f.display_name}
                            </span>
                            <ChevronDown className={cn("mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform sm:hidden", mobileExpanded && "rotate-180")} />
                          </button>
                        </div>
                        <div className="hidden break-all font-mono text-xs leading-5 text-muted-foreground sm:block">{f.key}</div>
                      </div>
                      <div className="horizontal-scroll-touch flex min-h-7 min-w-0 flex-nowrap items-center gap-1 overflow-x-auto pr-1 sm:h-auto sm:max-w-[55%] sm:flex-wrap sm:justify-end sm:overflow-visible sm:pr-14">
                        <FeatureCapabilityBadge show={Boolean(f.interaction_entries?.length)} tone="info">
                          可交互
                        </FeatureCapabilityBadge>
                        <FeatureCapabilityBadge
                          show={directPassthrough}
                          tone="warn"
                          title={
                            directPassthroughOn
                              ? "账号已二次开启裸直通；仅插件明确返回 consumed 才截断后续链路"
                              : "低延时能力，安装后还需在账号配置中二次开启才会生效"
                          }
                        >
                          {directPassthroughOn ? "裸直通 · 已开启" : "裸直通 · 二次开启"}
                        </FeatureCapabilityBadge>
                        <FeatureCapabilityBadge
                          show={directPassthrough}
                          tone={directPassthroughOn ? "info" : "outline"}
                          title={formatDirectPassthroughRankTitle(directRank, {
                            secondaryEnabled: Boolean(directPassthroughOn),
                          })}
                        >
                          {formatDirectPassthroughRankLabel(directRank, {
                            secondaryEnabled: Boolean(directPassthroughOn),
                            totalEnabled: directPassthroughOn ? directRankTotal : undefined,
                          })}
                        </FeatureCapabilityBadge>
                        <FeatureCapabilityBadge show={usesAI} tone="warn" title="插件会调用 TelePilot 的 AI 能力">
                          AI 调用
                        </FeatureCapabilityBadge>
                        <FeatureCapabilityBadge
                          show={Boolean(f.runtime_availability && f.runtime_availability !== "ready")}
                          tone={f.runtime_availability === "paused" ? "danger" : "warn"}
                          title={
                            f.runtime_availability === "partial" &&
                            (f.available_channels || []).includes("userbot")
                              ? "部分平台能力已关闭；userbot 入口仍可用"
                              : f.blocked_reason_code || "平台能力限制"
                          }
                        >
                          {f.runtime_availability === "partial"
                            ? "部分可用"
                            : f.runtime_availability === "paused"
                              ? "已暂停"
                              : f.runtime_availability === "transitioning"
                                ? "等待热加载"
                                : "能力受限"}
                        </FeatureCapabilityBadge>
                      </div>
                    </div>
                    {f.last_update_check_error ? (
                      <div className={cn("mt-1 text-xs text-destructive", mobileExpanded ? "block" : "hidden", "sm:block")}>
                        更新检查失败：{f.last_update_check_error}
                      </div>
                    ) : null}
                    <div className={cn(mobileExpanded ? "block" : "hidden", "sm:block")}>
                    {status === "failed" ? (
                      <div className="mt-1 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1 text-xs leading-5 text-destructive">
                        <div>加载异常{lastError ? `：${lastError}` : "：后端未返回错误详情"}</div>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="mt-1 h-7 border-destructive/40 bg-destructive/10 px-2 text-destructive hover:bg-destructive/15 hover:text-destructive"
                          onClick={() => {
                            const params = new URLSearchParams({ tab: "plugins", plugin_key: f.key, status: "failed" });
                            if (selectedAccountId) params.set("account_id", String(selectedAccountId));
                            nav(`/logs?${params.toString()}`);
                          }}
                        >
                          查看日志
                        </Button>
                      </div>
                    ) : null}
                    <div className="mt-1.5 line-clamp-2 text-xs leading-4 text-muted-foreground">
                      {compactUsageText(f.usage)}
                    </div>
                    <div className="mt-2 grid gap-1.5 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                        {pluginUsage ? (
                          <>
                            <span className="shrink-0 rounded-full border bg-muted/40 px-2 py-0.5 text-[11px] text-muted-foreground">
                              AI {formatCompactNumber(pluginUsage.total_tokens)} tokens
                            </span>
                            <span className="shrink-0 rounded-full border bg-muted/40 px-2 py-0.5 text-[11px] text-muted-foreground">
                              {pluginUsage.request_count} 次调用
                            </span>
                            {pluginUsage.failed_count > 0 ? (
                              <span className="shrink-0 rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-[11px] text-warning">
                                失败 {pluginUsage.failed_count}
                              </span>
                            ) : null}
                          </>
                        ) : null}
                        <FeatureCapabilityBadge
                          show={Boolean(f.update_available)}
                          tone="success"
                          title={f.update_available ? moduleUpdateMessage(f) : undefined}
                          onClick={() => toast.info(moduleUpdateMessage(f))}
                        >
                          有更新
                        </FeatureCapabilityBadge>
                        <FeatureCapabilityBadge
                          show={eventLabels.length > 0}
                          title={eventLabels.join(" / ")}
                        >
                          触发入口 {eventLabels.length}
                        </FeatureCapabilityBadge>
                        <FeatureCapabilityBadge
                          show={capabilityLabels.length > 0}
                          tone={highRiskContract ? "warn" : "outline"}
                          title={capabilityLabels.join(" / ")}
                        >
                          能力 {capabilityLabels.length}
                        </FeatureCapabilityBadge>
                        <FeatureCapabilityBadge
                          show={highRiskContract}
                          tone="danger"
                          title={contractWarnings.join("；")}
                        >
                          高风险
                        </FeatureCapabilityBadge>
                        <FeatureCapabilityBadge show={Boolean(f.experimental)}>
                          实验性
                        </FeatureCapabilityBadge>
                        <MetaBadge
                          tone={trustBadge.tone}
                          className="h-7 shrink-0 justify-center px-2 text-[10px]"
                          title={`${trustBadge.title} 来源：${moduleSourceLabel(f)}`}
                        >
                          {trustBadge.label}
                        </MetaBadge>
                        <MetaBadge
                          tone={!enabled ? "neutral" : status === "failed" ? "danger" : "success"}
                          className="h-7 shrink-0 justify-center px-2 text-[10px]"
                          title={`开关：${enabled ? "已启用" : "未启用"}；运行状态：${runtimeLabel}${lastError ? `；最近错误：${lastError}` : ""}`}
                        >
                          {enabled ? "已启用" : "未启用"}
                        </MetaBadge>
                      </div>
                      {canConfigure ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="hidden h-8 justify-self-end px-2.5 sm:inline-flex"
                          onClick={() => {
                            if (path) {
                              nav(path);
                            }
                          }}
                        >
                          配置
                        </Button>
                      ) : null}
                      {installByKey.has(f.key) ? (
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          className="hidden h-8 justify-self-end px-2.5 sm:inline-flex"
                          onClick={() => onOpenHistory(f.key)}
                        >
                          <History className="mr-1 h-3.5 w-3.5" />
                          安装历史
                        </Button>
                      ) : null}
                    </div>
                    </div>
                  </div>
                  <div className="absolute bottom-2 right-2 flex max-w-[calc(100%-1rem)] items-center gap-1 sm:hidden">
                    <MetaBadge
                      tone={!enabled ? "neutral" : status === "failed" ? "danger" : "success"}
                      className="h-6 max-w-[4rem] shrink-0 justify-center px-1.5 text-[10px]"
                      title={`开关：${enabled ? "已启用" : "未启用"}；运行状态：${runtimeLabel}${lastError ? `；最近错误：${lastError}` : ""}`}
                    >
                      {enabled ? "已启用" : "未启用"}
                    </MetaBadge>
                    {canConfigure ? (
                      <Button
                        type="button"
                        size="icon"
                        variant="outline"
                        className="h-7 w-7 shrink-0"
                        aria-label={`配置 ${f.display_name}`}
                        title="配置"
                        onClick={() => {
                          if (path) {
                            nav(path);
                          }
                        }}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                    ) : null}
                    {installByKey.has(f.key) ? (
                      <Button
                        type="button"
                        size="icon"
                        variant="outline"
                        className="h-7 w-7 shrink-0"
                        aria-label={`查看 ${f.display_name} 安装历史`}
                        title="安装历史"
                        onClick={() => onOpenHistory(f.key)}
                      >
                        <History className="h-3.5 w-3.5" />
                      </Button>
                    ) : null}
                  </div>
                  <div className={cn(mobileExpanded ? "block" : "hidden", "sm:block")}><ModuleLintWarnings warnings={lintWarnings} /></div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const INSTALL_HISTORY_LABELS: Record<string, string> = {
  installed: "已安装",
  updated: "已更新",
  enabled: "已启用",
  disabled: "已停用",
  uninstalled: "已卸载",
};

function pluginHistorySummary(item: PluginInstallHistoryItem): string {
  if (item.event_type === "updated" && item.previous_version && item.version) {
    return `${moduleVersionLabel(item.previous_version)} → ${moduleVersionLabel(item.version)}`;
  }
  if (item.version) return moduleVersionLabel(item.version);
  if (item.enabled != null) return item.enabled ? "全局启用" : "全局停用";
  return item.source_label || item.source || "状态已记录";
}

function PluginInstallHistoryDialog({
  pluginKey,
  history,
  loading,
  error,
  onOpenChange,
}: {
  pluginKey: string | null;
  history: PluginInstallHistoryItem[];
  loading: boolean;
  error?: unknown;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={Boolean(pluginKey)} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-xl rounded-xl">
        <DialogHeader>
          <DialogTitle className="flex min-w-0 items-center gap-2 text-base">
            <History className="h-4 w-4 shrink-0" />
            <span className="truncate">安装历史 · {pluginKey}</span>
          </DialogTitle>
          <DialogDescription>
            记录安装、更新和全局启停变化；卸载后历史仍会保留。
          </DialogDescription>
        </DialogHeader>
        {loading ? (
          <div className="flex min-h-40 items-center justify-center text-sm text-muted-foreground">
            <Spinner className="mr-2 h-4 w-4" /> 正在读取历史
          </div>
        ) : error ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-3 text-sm text-destructive">
            安装历史加载失败，请稍后重试。
          </div>
        ) : history.length === 0 ? (
          <div className="rounded-md border bg-muted/20 px-3 py-8 text-center text-sm text-muted-foreground">
            暂无安装历史；后续安装、更新或启停操作会从这里开始记录。
          </div>
        ) : (
          <div className="max-h-[60vh] space-y-0 overflow-y-auto pr-1">
            {history.map((item, index) => (
              <div key={item.id} className="relative grid grid-cols-[1.25rem_minmax(0,1fr)] gap-3 pb-4">
                {index < history.length - 1 ? (
                  <span className="absolute bottom-0 left-[0.35rem] top-3 w-px bg-border" />
                ) : null}
                <span className="relative mt-1 h-3 w-3 rounded-full border-2 border-background bg-primary shadow-sm" />
                <div className="min-w-0 rounded-md border bg-muted/20 px-3 py-2.5">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="text-sm font-medium">
                        {INSTALL_HISTORY_LABELS[item.event_type] || item.event_type}
                      </div>
                      <div className="mt-0.5 break-words font-mono text-xs text-muted-foreground">
                        {pluginHistorySummary(item)}
                      </div>
                    </div>
                    <time className="shrink-0 text-xs text-muted-foreground">
                      {formatDateTime(item.created_at)}
                    </time>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {item.source_label || item.source ? (
                      <MetaBadge tone="outline">
                        来源：{item.source_label || item.source}
                      </MetaBadge>
                    ) : null}
                    {item.signature_ok != null ? (
                      <MetaBadge tone={item.signature_ok ? "success" : "danger"}>
                        {item.signature_ok ? "签名通过" : "签名失败"}
                      </MetaBadge>
                    ) : null}
                  </div>
                  {item.detail ? (
                    <p className="mt-2 break-words text-xs leading-5 text-muted-foreground">
                      {item.detail}
                    </p>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
