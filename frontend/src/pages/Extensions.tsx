// 插件安装与管理：插件包安装/更新/卸载 + 开发指南
//
// Tab 1：安装与更新 — 推荐插件 + 远程插件（安装/卸载/更新）
// Tab 2：开发指南 — 完整插件开发文档工作台
//
// 账号级启停与配置统一回 /plugins 首页，避免“安装页”和“插件中心”双入口重复。
// 远程插件原为独立 /remote-plugins 页面，现在统一收口到 /plugins/manage。
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  Brain,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Code2,
  Download,
  FileText,
  GitFork,
  Globe2,
  KeyRound,
  ListChecks,
  Network,
  Power,
  Plus,
  Puzzle,
  RefreshCw,
  Save,
  ShieldCheck,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github.css";
import aiGuideMd from "../../../docs/PLUGIN-AI.md?raw";
import apiReferenceMd from "../../../docs/PLUGIN-API-REFERENCE.md?raw";
import cheatsheetMd from "../../../docs/PLUGIN-CHEATSHEET.md?raw";
import devGuideMd from "../../../docs/PLUGIN-DEV-GUIDE.md?raw";
import httpGuideMd from "../../../docs/PLUGIN-HTTP.md?raw";
import overviewMd from "../../../docs/PLUGIN-OVERVIEW.md?raw";
import quickstartMd from "../../../docs/PLUGIN-QUICKSTART.md?raw";
import remoteGuideMd from "../../../docs/PLUGIN-REMOTE.md?raw";
import rulesMd from "../../../docs/PLUGIN-RULES.md?raw";
import safetyGuideMd from "../../../docs/PLUGIN-SAFETY.md?raw";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Spinner } from "@/components/ui/misc";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import { cn } from "@/lib/utils";
import { goBackOr } from "@/lib/navigation";
import { getErrMsg } from "@/lib/api";
import { splitPluginWarnings } from "@/lib/plugin-config-contract";
import { isPlatformFeature } from "@/lib/plugin-modes";
import { PluginWorkspaceNav } from "@/pages/Plugins/WorkspaceNav";

import { getFeatureMatrix } from "@/api/features";
import {
  listInstalledPackages,
  enableInstall,
  disableInstall,
  uninstallPlugin,
} from "@/api/plugins";
import {
  fetchRemotePlugins,
  enableRemotePlugin,
  disableRemotePlugin,
  updateRemotePlugin,
  checkRemotePluginUpdates,
  uninstallRemotePlugin,
} from "@/api/remotePlugin";
import { getSystemSettings, patchSystemSettings } from "@/api/system";
import {
  addPluginRepo,
  deletePluginRepo,
  fetchPluginRepos,
  fetchLocalPlugins,
  fetchOfficialPlugins,
  fetchRepoPlugins,
  refreshRepoPlugins,
  installLocalPlugin,
  installOfficialPlugin,
  installFromRepo,
  updateInstalledPluginsFromRepo,
  updatePluginRepoCredential,
} from "@/api/pluginRepo";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  compactUsageText,
  pluginContractRiskWarnings,
  pluginEventSubscriptionLabels,
  pluginOperationalCapabilityLabels,
} from "@/types/pluginContract";
import type { PluginRepo, PluginRepoPlugin } from "@/types/pluginRepo";
import type { RemotePlugin } from "@/types/remotePlugin";

// ── 常量 ──────────────────────────────────────────────────────────
type TabValue = "plugins" | "guide";
type DevDocId =
  | "all"
  | "quickstart"
  | "rules"
  | "dev-guide"
  | "overview"
  | "api-reference"
  | "http"
  | "safety"
  | "remote"
  | "cheatsheet"
  | "ai";

type DevDoc = {
  id: DevDocId;
  title: string;
  description: string;
  path: string;
  markdown: string;
  icon: LucideIcon;
};

const PLUGINS_QK = ["installed-packages"] as const;
const REMOTE_QK = ["remote-plugins"] as const;
const PLUGIN_REPOS_QK = ["plugin-repos"] as const;
const OFFICIAL_PLUGINS_QK = ["official-plugins"] as const;
const NEW_ACCOUNT_GUIDE_SEEN_KEY = "telebot.accounts.new_account_guide_seen.v4";
const FIRST_RECOMMENDED_PLUGIN_KEYS = new Set(["auto_reply", "autorepeat"]);
const DANGER_OUTLINE_BUTTON_CLASS = "border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive";
const DEV_DOCS: DevDoc[] = [
  {
    id: "quickstart",
    title: "5 分钟 Quickstart",
    description: "复制最小 hello_ping 插件，跑通 Event Bus + MessageOps。",
    path: "docs/PLUGIN-QUICKSTART.md",
    markdown: quickstartMd,
    icon: Sparkles,
  },
  {
    id: "rules",
    title: "插件开发铁律",
    description: "先确认必须、禁止、推荐的硬边界，避免后续返工。",
    path: "docs/PLUGIN-RULES.md",
    markdown: rulesMd,
    icon: ShieldCheck,
  },
  {
    id: "api-reference",
    title: "完整 API 参考",
    description: "查字段、facade、事件信封、MessageOps、Trace 和前端集成。",
    path: "docs/PLUGIN-API-REFERENCE.md",
    markdown: apiReferenceMd,
    icon: Code2,
  },
  {
    id: "dev-guide",
    title: "索引与路线",
    description: "插件市场路线、文档分篇和 0.x 安全策略入口。",
    path: "docs/PLUGIN-DEV-GUIDE.md",
    markdown: devGuideMd,
    icon: BookOpen,
  },
  {
    id: "overview",
    title: "插件概览",
    description: "快速开始、插件结构、个人可信插件标准模式与交互入口边界。",
    path: "docs/PLUGIN-OVERVIEW.md",
    markdown: overviewMd,
    icon: FileText,
  },
  {
    id: "http",
    title: "HTTP facade",
    description: "第三方插件访问外部 HTTP 的权限、配额和调用约束。",
    path: "docs/PLUGIN-HTTP.md",
    markdown: httpGuideMd,
    icon: Network,
  },
  {
    id: "safety",
    title: "安全边界",
    description: "权限声明、交互 Bot、工程规范和安全合规要求。",
    path: "docs/PLUGIN-SAFETY.md",
    markdown: safetyGuideMd,
    icon: ShieldCheck,
  },
  {
    id: "remote",
    title: "远程插件",
    description: "远程安装、manifest 读取、worker loader 与更新回滚。",
    path: "docs/PLUGIN-REMOTE.md",
    markdown: remoteGuideMd,
    icon: Globe2,
  },
  {
    id: "cheatsheet",
    title: "速查表",
    description: "最常用契约、文件结构、权限和验证命令的短清单。",
    path: "docs/PLUGIN-CHEATSHEET.md",
    markdown: cheatsheetMd,
    icon: ListChecks,
  },
  {
    id: "ai",
    title: "AI facade",
    description: "ctx.ai 文本能力、权限声明、降级路径和运行时约束。",
    path: "docs/PLUGIN-AI.md",
    markdown: aiGuideMd,
    icon: Brain,
  },
];

const DOC_LINK_TO_ID: Record<string, DevDocId> = DEV_DOCS.reduce<Record<string, DevDocId>>(
  (acc, doc) => {
    const pathParts = doc.path.split("/");
    const filename = pathParts[pathParts.length - 1];
    if (filename) {
      acc[filename] = doc.id;
      acc[`./${filename}`] = doc.id;
      acc[`docs/${filename}`] = doc.id;
      acc[`../docs/${filename}`] = doc.id;
    }
    return acc;
  },
  {},
);

function formatPluginVersion(version?: string | null) {
  const v = (version || "").trim();
  if (!v) return "-";
  return v.startsWith("v") ? v : `v${v}`;
}

function toastPluginLintWarnings(row: RemotePlugin) {
  const warnings = splitPluginWarnings(row.lint_warnings);
  if (!warnings.all.length) return;
  if (warnings.high.length > 0) {
    toast.error(`插件 ${row.name} 有 ${warnings.high.length} 条高级规范警告`, {
      description: warnings.high[0],
    });
    return;
  }
  toast.warning(`插件 ${row.name} 有 ${warnings.normal.length} 条开发规范警告`, {
    description: warnings.normal[0],
  });
}

function remoteVersionLabel(plugin: RemotePlugin) {
  if (plugin.update_available) return "不是最新版";
  if (plugin.last_update_check_error) return "检查失败";
  if (plugin.source_url?.startsWith("local://")) return "本地导入";
  if (plugin.last_update_check_at) return "已是最新版";
  return "未检查";
}

function remoteVersionTone(plugin: RemotePlugin): "neutral" | "success" | "warn" | "outline" {
  if (plugin.update_available) return "warn";
  if (plugin.last_update_check_error) return "warn";
  if (plugin.source_url?.startsWith("local://")) return "outline";
  if (plugin.last_update_check_at) return "success";
  return "neutral";
}

function normalizeSourceUrlForCompare(value?: string | null): string {
  return (value || "")
    .trim()
    .replace(/\/+$/, "")
    .replace(/\.git$/i, "")
    .toLowerCase();
}

function shortSourceUrl(value?: string | null): string {
  const raw = (value || "").trim();
  if (!raw) return "-";
  if (raw.startsWith("local://")) return "本地导入";
  if (raw.startsWith("official://")) return "历史推荐源";
  const urlText = raw.startsWith("git+ssh://") ? raw.replace(/^git\+/, "") : raw;
  try {
    const url = new URL(urlText);
    const path = url.pathname.replace(/^\/+/, "").replace(/\.git$/i, "");
    return path ? `${url.hostname}/${path}` : url.hostname;
  } catch {
    return raw.replace(/\.git$/i, "");
  }
}

function repoNameForSourceUrl(sourceUrl: string | null | undefined, repos: PluginRepo[]): string | null {
  const sourceKey = normalizeSourceUrlForCompare(sourceUrl);
  if (!sourceKey) return null;
  const matched = repos.find((repo) => normalizeSourceUrlForCompare(repo.url) === sourceKey);
  return matched ? (matched.name || shortSourceUrl(matched.url)) : null;
}

function installSourceLibraryLabel(
  source: string | null | undefined,
  sourceUrl: string | null | undefined,
  sourceLabel: string | null | undefined,
  repos: PluginRepo[],
): string {
  const sourceValue = (source || "").toLowerCase();
  if (sourceValue === "builtin") return "系统核心";
  if (sourceValue === "official" || sourceUrl?.startsWith("official://")) return "历史推荐源";
  if (sourceValue === "local" || sourceUrl?.startsWith("local://")) return "本地导入";
  const repoName = repoNameForSourceUrl(sourceUrl, repos);
  if (repoName) return repoName;
  if (sourceUrl) return shortSourceUrl(sourceUrl);
  if (sourceLabel && !["Git", "Plugin Repo", "Official", "Local", "ZIP"].includes(sourceLabel)) {
    return sourceLabel;
  }
  if (sourceValue === "repo") return "插件仓库";
  if (sourceValue === "git") return "Git";
  if (sourceValue === "zip") return "ZIP";
  return sourceLabel || source || "-";
}

function parseManageTab(value: string | null): TabValue {
  return value === "plugins" || value === "guide"
    ? value
    : "plugins";
}

function stripFirstHeading(markdown: string) {
  return markdown.replace(/^#\s+.*(?:\r?\n)+/, "").trim();
}

function buildCompleteDevGuide() {
  return DEV_DOCS.map((doc, index) => {
    const level = index === 0 ? "#" : "##";
    return `${level} ${doc.title}\n\n> 源文件：\`${doc.path}\`\n\n${stripFirstHeading(doc.markdown)}`;
  }).join("\n\n---\n\n");
}

function normalizeDocHref(href?: string) {
  if (!href) return null;
  const [pathPart, anchorPart] = href.split("#");
  const normalizedPath = pathPart.replace(/^\.\//, "");
  const id = DOC_LINK_TO_ID[pathPart] ?? DOC_LINK_TO_ID[normalizedPath];
  return id ? { id, anchor: anchorPart ? `#${anchorPart}` : "" } : null;
}

function PluginContractBadges({
  pluginKey,
  events,
  capabilities,
}: {
  pluginKey: string;
  events: string[];
  capabilities: string[];
}) {
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      <MetaBadge tone="outline" title="插件声明自己会被哪些事件唤起">
        触发入口 {events.length}
      </MetaBadge>
      {events.map((label) => (
        <MetaBadge
          key={`${pluginKey}-event-${label}`}
          tone="outline"
          className="border-sky-200/80 bg-sky-500/10 text-sky-700 dark:border-sky-300/25 dark:text-sky-300"
        >
          {label}
        </MetaBadge>
      ))}
      <MetaBadge tone="outline" title="插件声明或推断出的运行能力">
        能力 {capabilities.length}
      </MetaBadge>
      {capabilities.map((label) => (
        <MetaBadge key={`${pluginKey}-cap-${label}`} tone="warn">
          {label}
        </MetaBadge>
      ))}
    </div>
  );
}

// ── 顶层组件 ──────────────────────────────────────────────────────
export function Extensions() {
  const nav = useNavigate();
  const [searchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const [tab, setTab] = useState<TabValue>(() => parseManageTab(tabParam));
  const [guideExpanded, setGuideExpanded] = useState(false);
  const guideActive = searchParams.get("guide") === "1";

  useEffect(() => {
    setTab(parseManageTab(tabParam));
  }, [tabParam]);

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <Button variant="ghost" size="sm" onClick={() => goBackOr(nav, "/plugins")}>
          <ArrowLeft className="mr-1 h-4 w-4" /> 返回上一页
        </Button>
        <Card>
          <CardHeader>
            <SectionHeader
              icon={Puzzle}
              title="插件安装与管理"
              description="这里负责安装、更新、卸载远程插件；装好后回插件中心按账号启用和配置。"
            />
          </CardHeader>
        </Card>
      </div>

      <PluginWorkspaceNav activeTab="manage" />

      {guideActive ? (
      <PluginInstallGuide
        expanded={guideExpanded}
        onToggle={() => setGuideExpanded((v) => !v)}
        onBack={() => nav("/plugins?guide=1")}
        onDone={() => {
          if (typeof window !== "undefined") {
            localStorage.setItem(NEW_ACCOUNT_GUIDE_SEEN_KEY, "1");
          }
          const next = new URLSearchParams(searchParams);
          next.delete("guide");
          nav(`/plugins/manage${next.toString() ? `?${next.toString()}` : ""}`, { replace: true });
          setGuideExpanded(false);
        }}
      />
      ) : null}

      <Tabs value={tab} onValueChange={(v) => setTab(v as TabValue)}>
        <TabsList>
          <TabsTrigger value="plugins" className="gap-1.5">
            <Puzzle className="h-4 w-4" /> 安装与更新
          </TabsTrigger>
          <TabsTrigger value="guide" className="gap-1.5">
            <BookOpen className="h-4 w-4" /> 开发指南
          </TabsTrigger>
        </TabsList>

        <TabsContent value="plugins">
          <PluginsManagementTab />
        </TabsContent>
        <TabsContent value="guide">
          <DevGuideTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function PluginInstallGuide({
  expanded,
  onToggle,
  onBack,
  onDone,
}: {
  expanded: boolean;
  onToggle: () => void;
  onBack: () => void;
  onDone: () => void;
}) {
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
        新手指引：安装后回插件中心启用
      </Button>
    );
  }

  return (
    <Card className="max-w-2xl border-primary/30 bg-card/95 shadow-lg shadow-primary/10">
      <CardHeader className="pb-2">
        <CardTitle className="text-base">3. 启用指令模板或调用插件</CardTitle>
        <CardDescription>
          这里只负责安装、更新和卸载远程插件。安装完成后，回插件中心选择账号，再启用和配置对应插件。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-2">
        <Button size="sm" onClick={onBack}>
          返回插件中心 <ChevronRight className="ml-1 h-4 w-4" />
        </Button>
        <Button size="sm" variant="outline" onClick={onDone}>
          我学会了！
        </Button>
        <Button size="sm" variant="ghost" onClick={onToggle}>
          收起
        </Button>
      </CardContent>
    </Card>
  );
}


// ═══════════════════════════════════════════════════════════════════
// Tab 2：插件管理 — 插件库 + 远程插件统一展示
// ═══════════════════════════════════════════════════════════════════
function PluginsManagementTab() {
  return (
    <div className="space-y-6">
      <RemoteUpdateSettingsCard />
      <OfficialPluginsCard />
      <LocalImportCard />
      <RemoteInstallCard />
      <InstalledPluginsSection />
    </div>
  );
}

function RemoteUpdateSettingsCard() {
  const qc = useQueryClient();
  const settingsQ = useQuery({ queryKey: ["system", "settings"], queryFn: getSystemSettings });
  const cfg = settingsQ.data?.remote_plugin_update_check ?? { enabled: true, interval_minutes: 360 };
  const [enabled, setEnabled] = useState(cfg.enabled);
  const [interval, setInterval] = useState(String(cfg.interval_minutes));

  useEffect(() => {
    setEnabled(cfg.enabled);
    setInterval(String(cfg.interval_minutes));
  }, [cfg.enabled, cfg.interval_minutes]);

  const saveMut = useMutation({
    mutationFn: () =>
      patchSystemSettings({
        remote_plugin_update_check: {
          enabled,
          interval_minutes: Number(interval) || 360,
        },
      }),
    onSuccess: () => {
      toast.success("远程插件自动检查设置已保存");
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const checkMut = useMutation({
    mutationFn: checkRemotePluginUpdates,
    onSuccess: (res) => {
      toast.success(`检查完成：${res.update_available} 个插件有更新`);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionHeader
          icon={RefreshCw}
          title="远程插件更新检查"
          description="后台只检查是否有新版本，不会自动安装；发现更新后会在插件中心和已安装插件里提示。"
        />
      </CardHeader>
      <CardContent className="flex flex-col gap-3 md:flex-row md:items-end">
        <div className="flex items-center gap-3 rounded-md border px-3 py-2">
          <Switch checked={enabled} onCheckedChange={setEnabled} />
          <div>
            <div className="text-sm font-medium">自动检查</div>
            <div className="text-xs text-muted-foreground">{enabled ? "已开启" : "已关闭"}</div>
          </div>
        </div>
        <div className="w-full space-y-1.5 md:w-56">
          <Label>检查间隔（分钟）</Label>
          <Input
            inputMode="numeric"
            value={interval}
            onChange={(e) => setInterval(e.target.value.replace(/[^0-9]/g, ""))}
            placeholder="360"
          />
          <div className="text-xs text-muted-foreground">最小 30，最大 10080</div>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => saveMut.mutate()} disabled={saveMut.isPending}>
            {saveMut.isPending ? <Spinner className="mr-2 h-4 w-4" /> : <Save className="mr-2 h-4 w-4" />}
            保存
          </Button>
          <Button variant="outline" onClick={() => checkMut.mutate()} disabled={checkMut.isPending}>
            {checkMut.isPending ? <Spinner className="mr-2 h-4 w-4" /> : <RefreshCw className="mr-2 h-4 w-4" />}
            立即检查
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function OfficialPluginsCard() {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const officialQ = useQuery({ queryKey: OFFICIAL_PLUGINS_QK, queryFn: fetchOfficialPlugins });
  const installOfficialMut = useMutation({
    mutationFn: (name: string) => installOfficialPlugin(name),
    onSuccess: (row) => {
      toast.success(`已安装/更新推荐插件 ${row.display_name || row.name} v${row.version}`);
      toastPluginLintWarnings(row);
      qc.invalidateQueries({ queryKey: OFFICIAL_PLUGINS_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const uninstallOfficialMut = useMutation({
    mutationFn: (key: string) => uninstallPlugin(key),
    onSuccess: (_row, key) => {
      toast.success(`已卸载 ${key}`);
      qc.invalidateQueries({ queryKey: OFFICIAL_PLUGINS_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const allItems = officialQ.data ?? [];
  const items = allItems.filter((plugin) => FIRST_RECOMMENDED_PLUGIN_KEYS.has(plugin.name));

  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionHeader
          icon={Sparkles}
          title="推荐插件"
          description="这些条目来自你的插件库快捷入口，只保留首次部署建议安装的自动回复和自动复读；更多插件请添加自己的 Git 插件仓库。"
          meta={<SignalPill tone="neutral" label="推荐项" value={items.length} className="h-8" />}
          actions={(
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-8 gap-1.5"
              onClick={() => setExpanded((value) => !value)}
              aria-expanded={expanded}
              aria-controls="official-recommended-plugins"
            >
              <ChevronDown className={cn("h-4 w-4 transition-transform", expanded && "rotate-180")} />
              {expanded ? "收起" : "展开"}
            </Button>
          )}
        />
      </CardHeader>
      {expanded ? (
      <CardContent id="official-recommended-plugins">
        {officialQ.isLoading ? (
          <div className="flex h-16 items-center justify-center">
            <Spinner className="text-primary" />
          </div>
        ) : officialQ.isError ? (
          <p className="py-3 text-sm text-destructive">推荐插件源加载失败：{getErrMsg(officialQ.error)}</p>
        ) : items.length === 0 ? (
          <p className="py-3 text-sm text-muted-foreground">当前插件库没有发现自动回复或自动复读。</p>
        ) : (
          <div className="grid gap-2 lg:grid-cols-2">
            {items.map((plugin) => {
              return (
                <div key={plugin.name} className="flex flex-col gap-3 rounded-md border px-3 py-3 sm:flex-row sm:items-center">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{plugin.display_name || plugin.name}</span>
                      <span className="font-mono text-xs text-muted-foreground">v{plugin.version}</span>
                      <MetaBadge tone="success">首次推荐</MetaBadge>
                      {plugin.installed ? <MetaBadge>已安装</MetaBadge> : null}
                      {plugin.update_available ? <MetaBadge tone="warn">有更新</MetaBadge> : null}
                    </div>
                    <div className="mt-1 font-mono text-xs text-muted-foreground">{plugin.name}</div>
                    {plugin.description ? (
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">{plugin.description}</p>
                    ) : null}
                  </div>
                  <div className="flex shrink-0 flex-wrap justify-end gap-2">
                    {plugin.installed ? (
                      <Button
                        size="sm"
                        variant="outline"
                        className={DANGER_OUTLINE_BUTTON_CLASS}
                        disabled={uninstallOfficialMut.isPending}
                        onClick={() => {
                          if (confirm(`确认卸载「${plugin.name}」？`)) uninstallOfficialMut.mutate(plugin.name);
                        }}
                      >
                        <Trash2 className="mr-1 h-3 w-3" />
                        卸载
                      </Button>
                    ) : null}
                    {!plugin.installed || plugin.update_available ? (
                      <Button
                        size="sm"
                        className="shrink-0"
                        variant={plugin.installed ? "outline" : "default"}
                        disabled={installOfficialMut.isPending}
                        onClick={() => installOfficialMut.mutate(plugin.name)}
                      >
                        {installOfficialMut.isPending ? (
                          <Spinner className="mr-2 h-4 w-4" />
                        ) : (
                          <Download className="mr-2 h-4 w-4" />
                        )}
                        {plugin.installed ? "更新" : "安装"}
                      </Button>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
      ) : null}
    </Card>
  );
}

function LocalImportCard() {
  const qc = useQueryClient();
  const localQ = useQuery({ queryKey: ["local-plugins"], queryFn: fetchLocalPlugins });
  const installLocalMut = useMutation({
    mutationFn: (name: string) => installLocalPlugin(name),
    onSuccess: (row) => {
      toast.success(`已导入本地插件 ${row.name} v${row.version}`);
      toastPluginLintWarnings(row);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
      qc.invalidateQueries({ queryKey: ["local-plugins"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionHeader
          icon={GitFork}
          title="本地导入"
          description={<>把按开发文档编写好的插件目录放到 <code>plugins/local_imports/</code>，然后在这里一键导入用于本地调试。</>}
        />
      </CardHeader>
      <CardContent>
        {localQ.isLoading ? (
          <div className="flex h-16 items-center justify-center">
            <Spinner className="text-primary" />
          </div>
        ) : (localQ.data ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">
            还没发现可导入插件。请先把插件目录放入 <code>plugins/local_imports/</code>（目录内需包含 <code>plugin.json</code>）。
          </p>
        ) : (
          <div className="space-y-2">
            {(localQ.data ?? []).map((p) => (
              <div key={p.name} className="flex items-center justify-between gap-3 rounded-md border px-3 py-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{p.display_name || p.name}</div>
                  <div className="truncate text-xs text-muted-foreground">{p.subdir || p.name} · v{p.version}</div>
                </div>
                <Button
                  size="sm"
                  disabled={installLocalMut.isPending || p.installed}
                  onClick={() => installLocalMut.mutate(p.name)}
                >
                  {installLocalMut.isPending ? (
                    <Spinner className="mr-2 h-4 w-4" />
                  ) : (
                    <Download className="mr-2 h-4 w-4" />
                  )}
                  {p.installed ? "已导入" : "导入"}
                </Button>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── 远程安装：仓库管理 + 浏览插件 ────────────────────────────────
function RemoteInstallCard() {
  const qc = useQueryClient();
  const [addUrl, setAddUrl] = useState("");
  const [addName, setAddName] = useState("");
  const [addToken, setAddToken] = useState("");
  const [repoTokens, setRepoTokens] = useState<Record<number, string>>({});
  const [expandedRepoId, setExpandedRepoId] = useState<number | null>(null);
  const [refreshingRepoId, setRefreshingRepoId] = useState<number | null>(null);
  const [updatingRepoId, setUpdatingRepoId] = useState<number | null>(null);
  const [pendingBulkUpdate, setPendingBulkUpdate] = useState<{
    repoId: number;
    repoName: string;
    plugins: PluginRepoPlugin[];
  } | null>(null);

  // 已保存仓库列表（后端）
  const reposQ = useQuery({ queryKey: PLUGIN_REPOS_QK, queryFn: fetchPluginRepos });
  const repos = reposQ.data ?? [];

  // 仓库内插件列表
  const pluginsQ = useQuery({
    queryKey: ["repo-plugins", expandedRepoId],
    queryFn: () => fetchRepoPlugins(expandedRepoId!),
    enabled: expandedRepoId !== null,
  });
  const bulkPreviewPlugins = pendingBulkUpdate?.plugins.filter((p) => p.installed && p.update_available) ?? [];

  const openBulkUpdatePreview = (repo: { id: number; name?: string | null; url: string }, plugins?: PluginRepoPlugin[]) => {
    const available = plugins?.filter((p) => p.installed && p.update_available) ?? [];
    if (!plugins) {
      setExpandedRepoId(repo.id);
      toast.info("请先展开或刷新该仓库，确认可升级插件和风险变化后再一键更新。");
      return;
    }
    if (available.length === 0) {
      toast.info("该仓库暂无可升级的已安装插件。");
      return;
    }
    setPendingBulkUpdate({
      repoId: repo.id,
      repoName: repo.name || repo.url,
      plugins: available,
    });
  };

  const refreshRepoMut = useMutation({
    mutationFn: async (repoId: number) => {
      setRefreshingRepoId(repoId);
      return { repoId, plugins: await refreshRepoPlugins(repoId) };
    },
    onSuccess: ({ repoId, plugins }) => {
      toast.success("插件仓库已刷新");
      setExpandedRepoId(repoId);
      qc.setQueryData(["repo-plugins", repoId], plugins);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
    onSettled: () => setRefreshingRepoId(null),
  });

  // 添加仓库
  const addRepoMut = useMutation({
    mutationFn: () => addPluginRepo({
      url: addUrl.trim(),
      name: addName.trim() || undefined,
      credential: addToken.trim()
        ? { auth_type: "github_token", token: addToken.trim() }
        : undefined,
    }),
    onSuccess: (row) => {
      toast.success(`已添加仓库 ${row.name || row.url}`);
      setAddUrl("");
      setAddName("");
      setAddToken("");
      qc.invalidateQueries({ queryKey: PLUGIN_REPOS_QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const updateRepoCredentialMut = useMutation({
    mutationFn: ({ id, token }: { id: number; token: string }) =>
      updatePluginRepoCredential(id, {
        auth_type: token.trim() ? "github_token" : "none",
        token: token.trim() || null,
      }),
    onSuccess: (row) => {
      toast.success(row.has_credentials ? "仓库凭证已保存" : "仓库凭证已清除");
      setRepoTokens((prev) => {
        const next = { ...prev };
        delete next[row.id];
        return next;
      });
      qc.invalidateQueries({ queryKey: PLUGIN_REPOS_QK });
      qc.invalidateQueries({ queryKey: ["repo-plugins", row.id] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  // 删除仓库
  const delRepoMut = useMutation({
    mutationFn: (id: number) => deletePluginRepo(id),
    onSuccess: () => {
      toast.success("已移除仓库");
      setExpandedRepoId(null);
      qc.invalidateQueries({ queryKey: PLUGIN_REPOS_QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  // 从仓库安装插件
  const installFromRepoMut = useMutation({
    mutationFn: ({ repoId, name }: { repoId: number; name: string }) =>
      installFromRepo(repoId, name),
    onSuccess: (row) => {
      toast.success(`已安装 ${row.name} v${row.version}`);
      toastPluginLintWarnings(row);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
      qc.invalidateQueries({ queryKey: ["repo-plugins", expandedRepoId] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const updateFromRepoMut = useMutation({
    mutationFn: (name: string) => updateRemotePlugin(name),
    onSuccess: (row) => {
      toast.success(`已更新 ${row.name} → v${row.version}`);
      toastPluginLintWarnings(row);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
      qc.invalidateQueries({ queryKey: ["repo-plugins", expandedRepoId] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const bulkUpdateRepoMut = useMutation({
    mutationFn: async (repoId: number) => {
      setUpdatingRepoId(repoId);
      return updateInstalledPluginsFromRepo(repoId);
    },
    onSuccess: (res) => {
      setExpandedRepoId(res.repo_id);
      if (res.updated > 0) {
        const failedSuffix = res.failed > 0 ? `，${res.failed} 个失败` : "";
        toast.success(`已从 ${res.repo_name} 更新 ${res.updated} 个插件${failedSuffix}`);
      } else if (res.failed > 0) {
        toast.error(`${res.repo_name} 更新失败：${res.failed} 个插件未完成`);
      } else {
        toast.success(`${res.repo_name} 没有需要更新的已安装插件`);
      }
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
      qc.invalidateQueries({ queryKey: ["repo-plugins", res.repo_id] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
    onSettled: () => setUpdatingRepoId(null),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionHeader
          icon={GitFork}
          title="插件仓库"
          description="添加 Git 仓库后浏览并安装插件"
        />
      </CardHeader>
      <CardContent className="space-y-4">
        {/* 添加仓库 */}
        <div className="grid gap-2 md:grid-cols-[160px_minmax(220px,1fr)_minmax(180px,280px)_auto]">
          <Input
            className="h-9 rounded-md bg-background"
            placeholder="仓库名（可选）"
            value={addName}
            onChange={(e) => setAddName(e.target.value)}
            disabled={addRepoMut.isPending}
          />
          <Input
            className="h-9 rounded-md bg-background"
            placeholder="https://github.com/user/repo.git 或 /tree/branch"
            value={addUrl}
            onChange={(e) => setAddUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && addUrl.trim()) addRepoMut.mutate();
            }}
            disabled={addRepoMut.isPending}
          />
          <Input
            className="h-9 rounded-md bg-background"
            type="password"
            autoComplete="off"
            placeholder="GitHub Token（私有库可选）"
            value={addToken}
            onChange={(e) => setAddToken(e.target.value)}
            disabled={addRepoMut.isPending}
          />
          <Button
            onClick={() => addRepoMut.mutate()}
            disabled={!addUrl.trim() || addRepoMut.isPending}
            className="shrink-0"
          >
            {addRepoMut.isPending ? (
              <><Spinner className="mr-2 h-4 w-4" /> 添加中…</>
            ) : (
              <>
                <Plus className="mr-2 h-4 w-4" />
                添加仓库
              </>
            )}
          </Button>
          <p className="text-xs text-muted-foreground md:col-span-4">
            私有 GitHub 仓库请填写 fine-grained token，至少授予对应仓库 Contents 读取权限。Token 会加密保存且不会回显。
          </p>
        </div>

        {/* 仓库列表 */}
        {repos.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted-foreground">暂无已保存的仓库</p>
        ) : (
          <div className="space-y-2">
            {repos.map((repo) => {
              const expandedPlugins = expandedRepoId === repo.id ? pluginsQ.data : undefined;
              const knownUpdateCount = expandedPlugins?.filter((p) => p.installed && p.update_available).length;
              const bulkUpdating = bulkUpdateRepoMut.isPending && updatingRepoId === repo.id;
              return (
                <div key={repo.id} className="rounded-md border">
                  <div
                    className="flex cursor-pointer items-center gap-2 px-3 py-2 hover:bg-accent/50"
                    onClick={() => setExpandedRepoId(expandedRepoId === repo.id ? null : repo.id)}
                  >
                    <ChevronRight
                      className={cn("h-4 w-4 shrink-0 transition-transform", expandedRepoId === repo.id && "rotate-90")}
                    />
                    <span className="flex-1 truncate text-sm font-medium">
                      {repo.name || repo.url}
                    </span>
                    {repo.name && (
                      <span className="truncate font-mono text-xs text-muted-foreground">
                        {repo.url}
                      </span>
                    )}
                    {repo.has_credentials ? (
                      <MetaBadge tone="success" className="shrink-0">
                        <KeyRound className="mr-1 h-3 w-3" />
                        私有凭证
                      </MetaBadge>
                    ) : null}
                    <MetaBadge tone="outline" className="shrink-0">
                      {expandedRepoId === repo.id && pluginsQ.isLoading ? "加载中…" : "仓库"}
                    </MetaBadge>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 shrink-0 gap-1 px-2 text-xs"
                      disabled={
                        bulkUpdateRepoMut.isPending
                        || (expandedPlugins !== undefined && knownUpdateCount === 0)
                      }
                      onClick={(e) => {
                        e.stopPropagation();
                        openBulkUpdatePreview(repo, expandedPlugins);
                      }}
                      aria-label={`更新插件仓库 ${repo.name || repo.url} 中可升级的已安装插件`}
                      title={
                        knownUpdateCount && knownUpdateCount > 0
                          ? `更新 ${knownUpdateCount} 个可升级插件`
                          : "刷新仓库并更新其中可升级的已安装插件"
                      }
                    >
                      {bulkUpdating ? (
                        <Spinner className="h-3.5 w-3.5" />
                      ) : (
                        <RefreshCw className="h-3.5 w-3.5" />
                      )}
                      <span className="hidden sm:inline">
                        {knownUpdateCount && knownUpdateCount > 0 ? `更新 ${knownUpdateCount}` : "更新可升级"}
                      </span>
                      <span className="sm:hidden">更新</span>
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 w-6 shrink-0 p-0 text-muted-foreground hover:text-foreground"
                      disabled={refreshRepoMut.isPending && refreshingRepoId === repo.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        refreshRepoMut.mutate(repo.id);
                      }}
                      aria-label={`刷新插件仓库 ${repo.name || repo.url}`}
                      title="刷新仓库插件列表"
                    >
                      {refreshRepoMut.isPending && refreshingRepoId === repo.id ? (
                        <Spinner className="h-3.5 w-3.5" />
                      ) : (
                        <RefreshCw className="h-3.5 w-3.5" />
                      )}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 w-6 shrink-0 p-0 text-muted-foreground hover:text-destructive"
                      onClick={(e) => {
                        e.stopPropagation();
                        delRepoMut.mutate(repo.id);
                      }}
                      title="移除仓库"
                    >
                      <X className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                  {/* 展开：仓库内插件列表 */}
                  {expandedRepoId === repo.id && (
                    <div className="border-t px-3 py-2">
                    <div className="mb-3 grid gap-2 rounded-md bg-muted/30 p-2 sm:grid-cols-[minmax(180px,1fr)_auto_auto] sm:items-center">
                      <Input
                        className="h-8 rounded-md bg-background"
                        type="password"
                        autoComplete="off"
                        placeholder={repo.has_credentials ? "输入新 GitHub Token 可替换凭证" : "GitHub Token（私有库可选）"}
                        value={repoTokens[repo.id] ?? ""}
                        onChange={(event) =>
                          setRepoTokens((prev) => ({ ...prev, [repo.id]: event.target.value }))
                        }
                        disabled={updateRepoCredentialMut.isPending}
                      />
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-8"
                        disabled={updateRepoCredentialMut.isPending || !(repoTokens[repo.id] ?? "").trim()}
                        onClick={() =>
                          updateRepoCredentialMut.mutate({
                            id: repo.id,
                            token: repoTokens[repo.id] ?? "",
                          })
                        }
                      >
                        <KeyRound className="mr-1 h-3.5 w-3.5" />
                        保存凭证
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-8 text-muted-foreground hover:text-destructive"
                        disabled={updateRepoCredentialMut.isPending || !repo.has_credentials}
                        onClick={() => updateRepoCredentialMut.mutate({ id: repo.id, token: "" })}
                      >
                        清除
                      </Button>
                    </div>
                    {pluginsQ.isLoading ? (
                      <div className="flex h-16 items-center justify-center">
                        <Spinner className="text-primary" />
                      </div>
                    ) : pluginsQ.isError ? (
                      <p className="py-2 text-center text-sm text-destructive">
                        加载失败：{getErrMsg(pluginsQ.error)}
                      </p>
                    ) : (pluginsQ.data ?? []).length === 0 ? (
                      <p className="py-2 text-center text-sm text-muted-foreground">仓库内未找到插件</p>
                    ) : (
                      <div className="space-y-1">
                        {(pluginsQ.data ?? []).map((p) => {
                          const canUpdate = !!p.installed && !!p.update_available;
                          const events = pluginEventSubscriptionLabels(p.event_subscriptions);
                          const capabilities = pluginOperationalCapabilityLabels({
                            capabilities: p.capabilities,
                            permissions: p.permissions,
                            usage: p.usage,
                            description: p.description,
                          });
                          const risks = pluginContractRiskWarnings({
                            capabilities: p.capabilities,
                            event_subscriptions: p.event_subscriptions,
                          });
                          return (
                          <div
                            key={p.name}
                            className="flex flex-col gap-2 rounded-md px-2 py-2 hover:bg-accent/30 sm:flex-row sm:items-start"
                          >
                            <div className="flex-1 min-w-0">
                              <div className="flex flex-wrap items-center gap-2">
                                <span className="min-w-0 text-sm font-medium">{p.display_name || p.name}</span>
                                <span className="font-mono text-xs text-muted-foreground">v{p.version}</span>
                                {canUpdate ? (
                                  <MetaBadge tone="success">可更新</MetaBadge>
                                ) : p.installed ? (
                                  <MetaBadge>已安装</MetaBadge>
                                ) : null}
                                {risks.length > 0 ? <MetaBadge tone="danger">高风险能力</MetaBadge> : null}
                              </div>
                              {p.description && (
                                <p className="truncate text-xs text-muted-foreground">{p.description}</p>
                              )}
                              <p className="mt-1 text-xs text-muted-foreground">{compactUsageText(p.usage)}</p>
                              <PluginContractBadges
                                pluginKey={p.name}
                                events={events}
                                capabilities={capabilities}
                              />
                              {risks.length > 0 ? (
                                <div className="mt-2 space-y-1 text-xs text-destructive">
                                  {risks.slice(0, 2).map((risk) => (
                                    <div key={`${p.name}-risk-${risk}`} className="flex gap-1.5">
                                      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                                      <span>{risk}</span>
                                    </div>
                                  ))}
                                </div>
                              ) : null}
                              {canUpdate && p.installed_version && (
                                <p className="mt-1 text-xs text-muted-foreground">
                                  当前 {formatPluginVersion(p.installed_version)}，仓库 {formatPluginVersion(p.version)}
                                </p>
                              )}
                            </div>
                            <Button
                              size="sm"
                              variant={canUpdate ? "default" : p.installed ? "outline" : "default"}
                              className="h-7 shrink-0"
                              disabled={
                                (p.installed && !canUpdate)
                                || installFromRepoMut.isPending
                                || updateFromRepoMut.isPending
                              }
                              onClick={() =>
                                canUpdate
                                  ? updateFromRepoMut.mutate(p.name)
                                  : installFromRepoMut.mutate({ repoId: repo.id, name: p.name })
                              }
                            >
                              {canUpdate ? (
                                <RefreshCw className="mr-1 h-3.5 w-3.5" />
                              ) : p.installed ? null : (
                                <Download className="mr-1 h-3.5 w-3.5" />
                              )}
                              {canUpdate ? "更新" : p.installed ? "已安装" : "安装"}
                            </Button>
                          </div>
                        );
                        })}
                      </div>
                    )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <Dialog open={pendingBulkUpdate !== null} onOpenChange={(open) => !open && setPendingBulkUpdate(null)}>
          <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
            <DialogHeader>
              <DialogTitle>确认批量更新插件</DialogTitle>
              <DialogDescription>
                将从 {pendingBulkUpdate?.repoName ?? "该仓库"} 更新 {bulkPreviewPlugins.length} 个已安装插件。更新前请确认版本变化和高风险能力变化。
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-3">
              {bulkPreviewPlugins.map((plugin) => {
                const events = pluginEventSubscriptionLabels(plugin.event_subscriptions);
                const capabilities = pluginOperationalCapabilityLabels({
                  capabilities: plugin.capabilities,
                  permissions: plugin.permissions,
                  usage: plugin.usage,
                  description: plugin.description,
                });
                const risks = pluginContractRiskWarnings({
                  capabilities: plugin.capabilities,
                  event_subscriptions: plugin.event_subscriptions,
                });
                return (
                  <div key={plugin.name} className="rounded-md border border-border/70 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="font-medium">{plugin.display_name || plugin.name}</div>
                      <MetaBadge tone="outline">{formatPluginVersion(plugin.installed_version)} → {formatPluginVersion(plugin.version)}</MetaBadge>
                      {risks.length > 0 ? <MetaBadge tone="danger">高风险能力</MetaBadge> : null}
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">{compactUsageText(plugin.usage)}</p>
                    <PluginContractBadges
                      pluginKey={plugin.name}
                      events={events}
                      capabilities={capabilities}
                    />
                    {risks.length > 0 ? (
                      <div className="mt-2 space-y-1 text-xs text-destructive">
                        {risks.map((risk) => (
                          <div key={risk} className="flex gap-1.5">
                            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                            <span>{risk}</span>
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setPendingBulkUpdate(null)}>
                取消
              </Button>
              <Button
                onClick={() => {
                  if (!pendingBulkUpdate) return;
                  bulkUpdateRepoMut.mutate(pendingBulkUpdate.repoId);
                  setPendingBulkUpdate(null);
                }}
                disabled={!pendingBulkUpdate || bulkUpdateRepoMut.isPending}
              >
                {bulkUpdateRepoMut.isPending ? <Spinner className="mr-2 h-4 w-4" /> : <RefreshCw className="mr-2 h-4 w-4" />}
                确认更新
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}

// ── 已安装插件列表（插件库 + 远程） ────────────────────────────────
function InstalledPluginsSection() {
  const nav = useNavigate();
  const qc = useQueryClient();

  const builtinQ = useQuery({
    queryKey: ["matrix"],
    queryFn: getFeatureMatrix,
    select: (data) =>
      data.features.filter((f) => f.is_builtin && f.key !== "forward" && !isPlatformFeature(f)),
  });

  const thirdPartyQ = useQuery({ queryKey: PLUGINS_QK, queryFn: listInstalledPackages });
  const remoteQ = useQuery({ queryKey: REMOTE_QK, queryFn: fetchRemotePlugins });
  const reposQ = useQuery({ queryKey: PLUGIN_REPOS_QK, queryFn: fetchPluginRepos });

  const enableTPMut = useMutation({
    mutationFn: (key: string) => enableInstall(key),
    onSuccess: () => {
      toast.success("已启用");
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: OFFICIAL_PLUGINS_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const disableTPMut = useMutation({
    mutationFn: (key: string) => disableInstall(key),
    onSuccess: () => {
      toast.success("已禁用");
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: OFFICIAL_PLUGINS_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const uninstallTPMut = useMutation({
    mutationFn: (key: string) => uninstallPlugin(key),
    onSuccess: (_r, key) => {
      toast.success(`已卸载 ${key}`);
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: OFFICIAL_PLUGINS_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const enableRMMut = useMutation({
    mutationFn: (name: string) => enableRemotePlugin(name),
    onSuccess: (res) => {
      const suffix = typeof res.applied === "number" ? `，已同步 ${res.applied} 个账号` : "";
      toast.success(`已启用远程插件${suffix}`);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const disableRMMut = useMutation({
    mutationFn: (name: string) => disableRemotePlugin(name),
    onSuccess: () => {
      toast.success("已禁用全局开关");
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const updateRMMut = useMutation({
    mutationFn: (name: string) => updateRemotePlugin(name),
    onSuccess: (row) => {
      toast.success(`已更新 ${row.name} → v${row.version}`);
      toastPluginLintWarnings(row);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const uninstallRMMut = useMutation({
    mutationFn: (name: string) => uninstallRemotePlugin(name),
    onSuccess: (_r, name) => {
      toast.success(`已卸载 ${name}`);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const isLoading = builtinQ.isLoading || thirdPartyQ.isLoading || remoteQ.isLoading;
  const [expandedWarnings, setExpandedWarnings] = useState<Set<string>>(() => new Set());
  const builtin = builtinQ.data ?? [];
  const thirdParty = thirdPartyQ.data ?? [];
  const remote = remoteQ.data ?? [];
  const repos = reposQ.data ?? [];
  const matrixQ = useQuery({ queryKey: ["matrix"], queryFn: getFeatureMatrix });
  const accounts = matrixQ.data?.accounts ?? [];
  const accountCount = accounts.length;
  const remoteEnabledCount = (name: string) =>
    accounts.filter((account) => account.feature_enabled?.[name] ?? account.features[name] !== "disabled").length;
  const toggleWarnings = (name: string) => {
    setExpandedWarnings((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={Puzzle}
          title="已安装插件"
          description="这里管理插件包本身；账号级启停和配置统一回插件中心处理。"
          meta={(
            <SignalPill
              tone="neutral"
              label="总计"
              value={builtin.length + thirdParty.length + remote.length}
              className="h-8"
            />
          )}
        />
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex h-24 items-center justify-center"><Spinner className="text-primary" /></div>
        ) : builtin.length === 0 && thirdParty.length === 0 && remote.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">暂无已安装插件</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>插件</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>来自库</TableHead>
                <TableHead>版本</TableHead>
                <TableHead>版本状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {/* 平台基础能力 */}
              {builtin.map((f) => (
                <TableRow key={f.key}>
                  <TableCell>
                    <div className="font-medium">{f.display_name}</div>
                    <div className="font-mono text-xs text-muted-foreground">{f.key}</div>
                  </TableCell>
                  <TableCell><MetaBadge>平台能力</MetaBadge></TableCell>
                  <TableCell>
                    <div className="max-w-[180px] truncate text-sm" title="系统核心">系统核心</div>
                  </TableCell>
                  <TableCell>{formatPluginVersion(f.version)}</TableCell>
                  <TableCell><MetaBadge tone="success">随系统更新</MetaBadge></TableCell>
                  <TableCell className="text-right">
                    <Button size="sm" variant="outline" onClick={() => nav("/plugins")}>
                      去插件中心
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {/* 第三方插件 */}
              {thirdParty.map((row) => (
                <TableRow key={row.key}>
                  <TableCell>
                    <div className="font-medium">{row.key}</div>
                  </TableCell>
                  <TableCell>
                    <MetaBadge tone={row.source === "official" ? "success" : "neutral"}>
                      {row.source === "official" ? "历史推荐源" : "第三方"}
                    </MetaBadge>
                  </TableCell>
                  <TableCell>
                    <div
                      className="max-w-[200px] truncate text-sm"
                      title={row.source_url || row.source_label || row.source}
                    >
                      {installSourceLibraryLabel(row.source, row.source_url, row.source_label, repos)}
                    </div>
                  </TableCell>
                  <TableCell>{formatPluginVersion(row.version)}</TableCell>
                  <TableCell>
                    <MetaBadge tone="outline">本地安装</MetaBadge>
                    <div className="mt-1 text-xs text-muted-foreground">
                      状态 {row.enabled ? "已启用" : "未启用"}
                    </div>
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-2">
                      {row.enabled ? (
                        <Button size="sm" variant="outline" onClick={() => disableTPMut.mutate(row.key)} disabled={disableTPMut.isPending}>禁用</Button>
                      ) : (
                        <Button size="sm" onClick={() => enableTPMut.mutate(row.key)} disabled={enableTPMut.isPending}>启用</Button>
                      )}
                      <Button
                        size="sm"
                        variant="outline"
                        className={DANGER_OUTLINE_BUTTON_CLASS}
                        onClick={() => { if (confirm(`确认卸载「${row.key}」？`)) uninstallTPMut.mutate(row.key); }}
                        disabled={uninstallTPMut.isPending}
                      >
                        <Trash2 className="mr-1 h-3 w-3" />
                        卸载
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              {/* 远程插件 */}
              {remote.map((p) => {
                const warningGroups = splitPluginWarnings(p.lint_warnings);
                const hasWarnings = warningGroups.all.length > 0;
                const hasHighWarnings = warningGroups.high.length > 0;
                return (
                <TableRow key={`rm-${p.name}`}>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="font-medium">{p.display_name || p.name}</div>
                      {p.update_available ? <MetaBadge tone="warn">有新版本</MetaBadge> : null}
                      {hasWarnings ? (
                        <button
                          type="button"
                          className="inline-flex"
                          onClick={() => toggleWarnings(p.name)}
                          aria-expanded={expandedWarnings.has(p.name)}
                        >
                          <MetaBadge tone={hasHighWarnings ? "danger" : "warn"}>
                            {hasHighWarnings ? (
                              <AlertTriangle className="h-3 w-3" />
                            ) : null}
                            {hasHighWarnings ? "高级规范警告" : "规范警告"}
                            <ChevronDown
                              className={cn(
                                "h-3 w-3 transition-transform",
                                expandedWarnings.has(p.name) && "rotate-180",
                              )}
                            />
                          </MetaBadge>
                        </button>
                      ) : null}
                    </div>
                    <div className="font-mono text-xs text-muted-foreground">{p.name}</div>
                    {p.update_available && p.latest_version ? (
                      <div className="mt-1 text-xs text-muted-foreground">
                        当前 {formatPluginVersion(p.version)}，远程 {formatPluginVersion(p.latest_version)}
                      </div>
                    ) : null}
                    {p.last_update_check_error ? (
                      <div className="mt-1 text-xs text-destructive">
                        更新检查失败：{p.last_update_check_error}
                      </div>
                    ) : null}
                    {hasWarnings && expandedWarnings.has(p.name) ? (
                      <div
                        className={cn(
                          "mt-2 space-y-1 rounded-md border px-3 py-2 text-xs",
                          hasHighWarnings
                            ? "border-destructive/30 bg-destructive/10 text-destructive"
                            : "border-amber-500/30 bg-amber-50/70 text-amber-900 dark:bg-amber-950/20 dark:text-amber-200",
                        )}
                      >
                        {warningGroups.all.map((warning, index) => (
                          <div key={`${p.name}-warning-${index}`} className="leading-5">
                            {warning}
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    {p.source_url?.startsWith("local://") ? (
                      <MetaBadge>本地导入</MetaBadge>
                    ) : (
                      <MetaBadge><GitFork className="h-3 w-3" />远程</MetaBadge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div
                      className="max-w-[200px] truncate text-sm"
                      title={p.source_url}
                    >
                      {installSourceLibraryLabel(
                        p.source_url?.startsWith("local://") ? "local" : "repo",
                        p.source_url,
                        null,
                        repos,
                      )}
                    </div>
                  </TableCell>
                  <TableCell>{formatPluginVersion(p.version)}</TableCell>
                  <TableCell>
                    <MetaBadge tone={remoteVersionTone(p)}>
                      {remoteVersionLabel(p)}
                    </MetaBadge>
                    {p.update_available && p.latest_version ? (
                      <div className="mt-1 text-xs text-muted-foreground">
                        远程 {formatPluginVersion(p.latest_version)}
                      </div>
                    ) : null}
                    {accountCount > 0 ? (
                      <div className="mt-1 text-xs text-muted-foreground">
                        账号启用 {remoteEnabledCount(p.name)}/{accountCount}
                      </div>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex flex-wrap justify-end gap-2">
                      <Button
                        size="sm"
                        variant={p.update_available ? "default" : "outline"}
                        onClick={() => updateRMMut.mutate(p.name)}
                        disabled={updateRMMut.isPending || p.source_url?.startsWith("local://")}
                        title={p.source_url?.startsWith("local://") ? "本地导入插件不支持远程更新" : "从远程更新"}
                      >
                        <RefreshCw className="mr-1 h-3 w-3" />
                        {p.update_available ? "更新到新版" : "更新"}
                      </Button>
                      {p.enabled ? (
                        <Button size="sm" variant="outline" onClick={() => disableRMMut.mutate(p.name)} disabled={disableRMMut.isPending}>
                          <X className="mr-1 h-3 w-3" />
                          禁用
                        </Button>
                      ) : (
                        <Button size="sm" onClick={() => enableRMMut.mutate(p.name)} disabled={enableRMMut.isPending}>
                          <Power className="mr-1 h-3 w-3" />
                          启用
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="outline"
                        className={DANGER_OUTLINE_BUTTON_CLASS}
                        onClick={() => { if (confirm(`确认卸载「${p.name}」？`)) uninstallRMMut.mutate(p.name); }}
                        disabled={uninstallRMMut.isPending}
                      >
                        <Trash2 className="mr-1 h-3 w-3" />
                        卸载
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => nav("/plugins")}>
                        去插件中心
                      </Button>
                    </div>
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

// ═══════════════════════════════════════════════════════════════════
// Tab 2：开发指南
// ═══════════════════════════════════════════════════════════════════
function DevGuideTab() {
  const completeDoc = useMemo<DevDoc>(
    () => ({
      id: "all",
      title: "完整文档",
      description: "把 Quickstart、铁律、索引、概览、API、HTTP、AI、安全、远程和速查合并为一份可滚动正文。",
      path: "docs/PLUGIN-*.md",
      markdown: buildCompleteDevGuide(),
      icon: Sparkles,
    }),
    [],
  );
  const docs = useMemo(() => [completeDoc, ...DEV_DOCS], [completeDoc]);
  const [activeDocId, setActiveDocId] = useState<DevDocId>("all");
  const contentRef = useRef<HTMLDivElement | null>(null);
  const activeDoc = docs.find((doc) => doc.id === activeDocId) ?? completeDoc;
  const ActiveIcon = activeDoc.icon;
  const markdownComponents = useMemo<Components>(
    () => ({
      a({ href, children, ...props }) {
        const target = normalizeDocHref(href);
        if (target) {
          return (
            <button
              type="button"
              className="font-medium text-primary underline decoration-primary/35 underline-offset-4 transition-colors hover:text-primary/80"
              onClick={() => setActiveDocId(target.id)}
              title={target.anchor ? `${DOC_LINK_TO_ID[href ?? ""] ?? target.id}${target.anchor}` : undefined}
            >
              {children}
            </button>
          );
        }
        const external = href?.startsWith("http://") || href?.startsWith("https://");
        return (
          <a
            {...props}
            href={href}
            target={external ? "_blank" : undefined}
            rel={external ? "noreferrer" : undefined}
          >
            {children}
          </a>
        );
      },
    }),
    [],
  );

  useEffect(() => {
    contentRef.current?.scrollTo({ top: 0 });
  }, [activeDocId]);

  return (
    <Card className="overflow-hidden">
      <CardHeader className="gap-4">
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div className="min-w-0">
            <CardTitle className="text-base">插件开发文档</CardTitle>
            <CardDescription className="mt-1">
              先按 Quickstart、铁律、完整 API 三层阅读；需要时再按主题查看每个分篇。
            </CardDescription>
          </div>
          <div className="flex flex-wrap gap-2 md:justify-end">
            <SignalPill tone="primary" label="文档" value={`${DEV_DOCS.length} 篇`} />
            <SignalPill tone="neutral" label="当前" value={activeDoc.title} />
          </div>
        </div>
        <div className="grid gap-2 md:grid-cols-3">
          {([
            {
              id: "quickstart",
              icon: Sparkles,
              title: "5 分钟 Quickstart",
              text: "复制最小插件，先跑通 ping/pong。",
            },
            {
              id: "rules",
              icon: ShieldCheck,
              title: "插件开发铁律",
              text: "确认不能违反的能力边界。",
            },
            {
              id: "api-reference",
              icon: Code2,
              title: "完整 API 参考",
              text: "查字段、facade、事件信封和 MessageOps。",
            },
          ] as const).map((item) => {
            const Icon = item.icon;
            const active = activeDoc.id === item.id;
            return (
              <button
                key={item.id}
                type="button"
                className={cn(
                  "min-w-0 rounded-lg border px-3 py-3 text-left transition",
                  active
                    ? "border-primary/30 bg-primary/10"
                    : "border-border/70 bg-background hover:border-primary/30 hover:bg-primary/5",
                )}
                onClick={() => setActiveDocId(item.id)}
              >
                <span className="flex min-w-0 items-center gap-2 text-sm font-medium">
                  <Icon className="h-4 w-4 shrink-0 text-primary" />
                  <span className="truncate">{item.title}</span>
                </span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                  {item.text}
                </span>
              </button>
            );
          })}
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <div className="grid min-h-[680px] border-t border-border/70 lg:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="border-b border-border/70 bg-muted/20 p-3 lg:border-b-0 lg:border-r">
            <nav className="flex gap-2 overflow-x-auto pb-1 lg:block lg:space-y-1 lg:overflow-visible lg:pb-0">
              {docs.map((doc) => {
                const Icon = doc.icon;
                const active = doc.id === activeDoc.id;
                return (
                  <button
                    key={doc.id}
                    type="button"
                    className={cn(
                      "group flex min-w-[11rem] items-start gap-3 rounded-lg border px-3 py-3 text-left text-sm transition lg:w-full",
                      active
                        ? "border-primary/30 bg-primary/10 text-foreground shadow-sm"
                        : "border-transparent bg-background/65 text-muted-foreground hover:border-border hover:bg-background hover:text-foreground",
                    )}
                    onClick={() => setActiveDocId(doc.id)}
                  >
                    <span
                      className={cn(
                        "mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-md border",
                        active
                          ? "border-primary/25 bg-primary/10 text-primary"
                          : "border-border/70 bg-muted/60 text-muted-foreground group-hover:text-foreground",
                      )}
                    >
                      <Icon className="h-4 w-4" />
                    </span>
                    <span className="min-w-0">
                      <span className="block truncate font-medium">{doc.title}</span>
                      <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                        {doc.description}
                      </span>
                    </span>
                  </button>
                );
              })}
            </nav>
          </aside>
          <section className="min-w-0 bg-background">
            <div className="border-b border-border/70 px-5 py-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-border/70 bg-muted/60 text-primary">
                  <ActiveIcon className="h-4 w-4" />
                </span>
                <h3 className="min-w-0 text-base font-semibold tracking-tight">{activeDoc.title}</h3>
                <MetaBadge tone="outline" mono className="max-w-full truncate">
                  {activeDoc.path}
                </MetaBadge>
              </div>
              <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
                {activeDoc.description}
              </p>
            </div>
            <div ref={contentRef} className="max-h-[72vh] min-h-[560px] overflow-auto px-5 py-5 md:px-7">
              <article className="prose prose-sm prose-pwa-safe max-w-none dark:prose-invert">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  rehypePlugins={[rehypeHighlight]}
                  components={markdownComponents}
                >
                  {activeDoc.markdown}
                </ReactMarkdown>
              </article>
            </div>
          </section>
        </div>
      </CardContent>
    </Card>
  );
}
