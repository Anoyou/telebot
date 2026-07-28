import { type ComponentType, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  ChevronDown,
  History,
  MessageSquareText,
  Package2,
  Package,
  PackagePlus,
  Pencil,
  Settings2,
  Sparkles,
  Zap,
} from "lucide-react";

import { listAccountFeatures } from "@/api/accounts";
import { getFeatureMatrix } from "@/api/features";
import { listPluginLLMUsageSummary } from "@/api/llmUsage";
import { listInstalledPackages } from "@/api/plugins";
import { getSystemSettings } from "@/api/system";
import type { AccountFeatureItem, FeatureInfo } from "@/api/types";
import type { PluginInstallOut } from "@/api/plugins";
import type { PluginLLMUsageSummaryItem } from "@/api/llmUsage";
import { PageShell } from "@/components/layout/PageScaffold";
import { Spinner } from "@/components/ui/misc";
import { Button } from "@/components/ui/button";
import { MetaBadge } from "@/components/ui/meta-badge";
import {
  SectionHeader,
  SignalPill,
  ToneRailCard,
} from "@/components/ui/status";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { pluginUsageGuideWarning, splitPluginWarnings } from "@/lib/plugin-config-contract";
import { isPlatformFeature } from "@/lib/plugin-modes";
import { cn } from "@/lib/utils";
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
import { PluginWorkspaceHeader } from "./WorkspaceHeader";

type ModuleCategory = "direct_passthrough" | "interactive" | "automation" | "utility";
type ModuleCategoryFilter = "all" | ModuleCategory;
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
const OFFICIAL_RECOMMENDED_INSTALL_BANNER_KEY = "telebot.plugins_home.official_recommended_install_closed.v0_35";
const OFFICIAL_RECOMMENDED_KEYS = ["auto_reply", "autorepeat"] as const;

function moduleRuntimeLabel(status: string, enabled: boolean) {
  if (!enabled) return "已停用";
  if (status === "active") return "运行中";
  if (status === "failed") return "异常";
  return "等待 worker 生效";
}

function moduleSourceLabel(feature: FeatureInfo) {
  if (feature.source_label === "Official") return "历史推荐源";
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
      label: "历史推荐源",
      tone: "success",
      title: "由旧推荐源安装记录保留；新安装会按普通插件库插件记录。",
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
  const [searchParams] = useSearchParams();
  const [selectedAid, setSelectedAid] = useState<number | null>(null);
  const [guideExpanded, setGuideExpanded] = useState(false);
  const [aiPanelExpanded, setAiPanelExpanded] = useState(false);
  const [selectedCategory, setSelectedCategory] = useState<ModuleCategoryFilter>("all");
  const guideActive = searchParams.get("guide") === "1";
  const [officialInstallBannerVisible, setOfficialInstallBannerVisible] = useState(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem(OFFICIAL_RECOMMENDED_INSTALL_BANNER_KEY) !== "1";
  });
  const matrixQ = useQuery({
    queryKey: ["matrix"],
    queryFn: getFeatureMatrix,
  });
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const installedQ = useQuery({
    queryKey: ["plugins", "installed-packages"],
    queryFn: listInstalledPackages,
  });
  const pluginUsageQ = useQuery({
    queryKey: ["llm", "plugin-usage-summary"],
    queryFn: () => listPluginLLMUsageSummary({ limit: 200 }),
  });

  const accounts = matrixQ.data?.accounts ?? [];
  const features = matrixQ.data?.features ?? [];
  const pluginFeatures = useMemo(
    () => features.filter((feature) => !isPlatformFeature(feature) && feature.key !== "forward"),
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
  const missingRecommendedOfficialPlugins = useMemo(
    () => OFFICIAL_RECOMMENDED_KEYS.filter((key) => !installByKey.has(key)),
    [installByKey],
  );
  const showOfficialInstallBanner =
    officialInstallBannerVisible
    && !installedQ.isLoading
    && !installedQ.isError
    && missingRecommendedOfficialPlugins.length > 0;
  const pluginUsageByKey = useMemo(() => {
    const map = new Map<string, PluginLLMUsageSummaryItem>();
    for (const item of pluginUsageQ.data?.items ?? []) {
      map.set(item.plugin_key, item);
    }
    return map;
  }, [pluginUsageQ.data]);

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
      {showOfficialInstallBanner ? (
        <Card className="border-primary/30 bg-primary/5">
          <CardHeader className="pb-2">
            <CardTitle className="text-base">首次部署推荐安装</CardTitle>
            <CardDescription>
              首次部署只推荐安装自动回复和自动复读。需要关键词回复或群内复读时，可以按需安装；安装后仍可随时卸载。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-2">
            <MetaBadge tone="outline">
              待安装 {missingRecommendedOfficialPlugins.length}
            </MetaBadge>
            <Button size="sm" onClick={() => nav("/plugins/manage?tab=plugins")}>
              <PackagePlus className="mr-1 h-4 w-4" />
              去安装推荐插件
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                localStorage.setItem(OFFICIAL_RECOMMENDED_INSTALL_BANNER_KEY, "1");
                setOfficialInstallBannerVisible(false);
              }}
            >
              暂不需要
            </Button>
          </CardContent>
        </Card>
      ) : null}

      <PluginWorkspaceHeader activeTab="home" guideActive={guideActive} />

      <Card className="hidden md:block">
        <CardContent className="space-y-4 !pt-5">
          {(settingsQ.data?.ai_enabled ?? false) ? (
            <div className="rounded-lg border px-4 py-3">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <SectionHeader
                  icon={Sparkles}
                  title="AI 插件入口"
                  description="AI 属于插件配置：先配置模型凭据，再创建指令模板，最后按账号启用；调用记录与排障集中在同一个工作台。"
                />
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="shrink-0"
                  onClick={() => setAiPanelExpanded((value) => !value)}
                  aria-expanded={aiPanelExpanded}
                >
                  {aiPanelExpanded ? "收起" : "展开"}
                  <ChevronDown className={`ml-1 h-4 w-4 transition-transform ${aiPanelExpanded ? "rotate-180" : ""}`} />
                </Button>
              </div>
              {aiPanelExpanded ? (
                <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
                  <ToneRailCard
                    icon={Sparkles}
                    title="AI 工作台"
                    value={<Button size="sm" variant="outline" className="border-primary/35 bg-primary/5 text-primary hover:bg-primary/10" onClick={() => nav("/ai")}>打开</Button>}
                    valueClassName="flex flex-wrap gap-2"
                    description="总览模型、指令模板和启用状态"
                    tone="primary"
                  />
                  <ToneRailCard
                    icon={Package}
                    title="模型提供商"
                    value={<Button size="sm" variant="outline" className="border-primary/35 bg-primary/5 text-primary hover:bg-primary/10" onClick={() => nav("/ai?tab=providers")}>配置</Button>}
                    valueClassName="flex flex-wrap gap-2"
                    description="配置 OpenAI、Anthropic、Ollama 等"
                    tone="neutral"
                  />
                  <ToneRailCard
                    icon={History}
                    title="近期调用"
                    value={<Button size="sm" variant="outline" className="border-primary/35 bg-primary/5 text-primary hover:bg-primary/10" onClick={() => nav("/ai?tab=usage")}>查看详情</Button>}
                    valueClassName="flex flex-wrap gap-2"
                    description="查看成功率、耗时和错误原因"
                    tone="success"
                  />
                  <ToneRailCard
                    icon={BookOpen}
                    title="帮助与示例"
                    value={<Button size="sm" variant="outline" className="border-primary/35 bg-primary/5 text-primary hover:bg-primary/10" onClick={() => nav("/ai?help=1")}>前往</Button>}
                    valueClassName="flex flex-wrap gap-2"
                    description="浮层查看原理、示例和术语"
                    tone="warn"
                  />
                </div>
              ) : null}
            </div>
          ) : null}
          {guideActive ? (
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
          ) : null}
        </CardContent>
      </Card>

      {codexImageFeature && codexImageState === "failed" ? (
        <Card className="border-warning/40 bg-warning/10">
          <CardHeader className="pb-2">
            <CardTitle className="text-base text-warning">codex_image 加载提示</CardTitle>
            <CardDescription className="text-warning">
              当前账号启用了 codex_image，但 worker 未能加载这个插件库插件。系统已自动降级为失败态并保持 worker 持续运行。
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
              features={visibleCategoryFeatures}
              selectedAccountId={selectedAccount?.id}
              selectedFeatures={selectedAccount?.features ?? {}}
              selectedFeatureEnabled={selectedAccount?.feature_enabled ?? {}}
              accountFeatureByKey={accountFeatureByKey}
              installByKey={installByKey}
              pluginUsageByKey={pluginUsageByKey}
            />
          </div>
        </CardContent>
      </Card>
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
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-[repeat(auto-fill,minmax(20rem,1fr))]">
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
                    "relative min-h-[6.5rem] overflow-hidden rounded-md border p-2.5 shadow-sm transition duration-200 ease-out hover:-translate-y-0.5 hover:shadow-md motion-reduce:transform-none sm:min-h-0",
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
                  <MetaBadge
                    mono
                    tone="outline"
                    className="absolute right-2.5 top-2.5 h-6 max-w-20 justify-center px-1.5 text-[10px]"
                    title={moduleVersionLabel(f.version)}
                    data-plugin-version
                  >
                    {moduleVersionLabel(f.version)}
                  </MetaBadge>
                  <div className="min-w-0">
                    <div className="grid grid-rows-[2.25rem_1.75rem] gap-0.5 sm:flex sm:grid-rows-none sm:flex-row sm:items-start sm:justify-between sm:gap-2">
                      <div className="min-w-0 pr-16 sm:pr-0">
                        <div className="flex min-w-0 items-center gap-1.5">
                          <button
                            type="button"
                            className="flex min-w-0 items-center gap-1.5 text-left sm:pointer-events-none"
                            aria-expanded={mobileExpanded}
                            onClick={() => setMobileExpandedKeys((current) => {
                              const next = new Set(current);
                              if (next.has(f.key)) next.delete(f.key);
                              else next.add(f.key);
                              return next;
                            })}
                          >
                            <span className="line-clamp-2 break-words text-sm font-medium leading-5" title={f.display_name}>
                              {f.display_name}
                            </span>
                            <ChevronDown className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform sm:hidden", mobileExpanded && "rotate-180")} />
                          </button>
                        </div>
                        <div className="hidden break-all font-mono text-xs leading-5 text-muted-foreground sm:block">{f.key}</div>
                      </div>
                      <div className="horizontal-scroll-touch flex h-8 min-w-0 flex-nowrap items-center gap-1.5 overflow-x-auto pr-12 sm:h-auto sm:flex-wrap sm:justify-end sm:overflow-visible sm:pr-14">
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
                    </div>
                    </div>
                  </div>
                  <div className="absolute bottom-2.5 right-2.5 flex items-center gap-1 sm:hidden">
                    <MetaBadge
                      tone={!enabled ? "neutral" : status === "failed" ? "danger" : "success"}
                      className="h-7 max-w-[4.5rem] shrink-0 justify-center px-1.5 text-[10px]"
                      title={`开关：${enabled ? "已启用" : "未启用"}；运行状态：${runtimeLabel}${lastError ? `；最近错误：${lastError}` : ""}`}
                    >
                      {enabled ? "已启用" : "未启用"}
                    </MetaBadge>
                    {canConfigure ? (
                      <Button
                        type="button"
                        size="icon"
                        variant="outline"
                        className="h-8 w-8 shrink-0"
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
