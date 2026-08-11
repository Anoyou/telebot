// 插件安装与管理：插件包安装/更新/卸载 + 开发指南
//
// Tab 1：安装与更新 — 本地导入 + 远程插件（安装/卸载/更新）
// Tab 2：开发指南 — 完整插件开发文档工作台
//
// 账号级启停与配置统一回 /plugins 首页，避免“安装页”和“插件中心”双入口重复。
// 远程插件原为独立 /remote-plugins 页面，现在统一收口到 /plugins/manage。
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Brain,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Code2,
  Download,
  FileText,
  GitFork,
  Globe2,
  Info,
  KeyRound,
  ListChecks,
  Network,
  Plus,
  Power,
  Puzzle,
  RefreshCw,
  Save,
  ShieldCheck,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import "highlight.js/styles/github.css";

import { Button } from "@/components/ui/button";
import { PageShell } from "@/components/layout/PageScaffold";
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
import { EmptyState } from "@/components/feedback/EmptyState";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import { cn, formatDateTime } from "@/lib/utils";
import { getErrMsg } from "@/lib/api";
import { splitPluginWarnings } from "@/lib/plugin-config-contract";
import { isPlatformFeature } from "@/lib/plugin-modes";
import { queryKeys } from "@/lib/queryKeys";
import { PluginWorkspaceHeader } from "@/pages/Plugins/WorkspaceHeader";

import { getFeatureMatrix } from "@/api/features";
import {
  listInstalledOverview,
  getInstalledPluginChangelog,
  enableInstall,
  disableInstall,
  uninstallPlugin,
  uploadPluginZip,
  type InstalledPluginOverviewAccountItem,
  type InstalledPluginOverviewItem,
} from "@/api/plugins";
import { toggleAccountFeature } from "@/api/accounts";
import {
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
  fetchRepoPlugins,
  refreshRepoPlugins,
  installLocalPlugin,
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
  | "devtools"
  | "overview"
  | "api-reference"
  | "http"
  | "safety"
  | "remote"
  | "cheatsheet"
  | "ai"
  | "webhook-quickstart"
  | "platform-capabilities"
  | "security-ops";

type DevDoc = {
  id: DevDocId;
  title: string;
  description: string;
  path: string;
  icon: LucideIcon;
};
type PluginAccountRow = {
  id: number;
  name: string;
  features: Record<string, string>;
  feature_enabled?: Record<string, boolean>;
};

const PLUGINS_QK = ["installed-packages"] as const;
const INSTALLED_OVERVIEW_QK = ["installed-overview"] as const;
const REMOTE_QK = ["remote-plugins"] as const;
const PLUGIN_REPOS_QK = ["plugin-repos"] as const;
const NEW_ACCOUNT_GUIDE_SEEN_KEY = "telebot.accounts.new_account_guide_seen.v4";
const DANGER_OUTLINE_BUTTON_CLASS = "border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive";
const DEV_DOCS: DevDoc[] = [
  {
    id: "quickstart",
    title: "5 分钟 Quickstart",
    description: "复制最小 hello_ping 插件，跑通 Event Bus + MessageOps。",
    path: "docs/PLUGIN-QUICKSTART.md",
    icon: Sparkles,
  },
  {
    id: "rules",
    title: "插件开发铁律",
    description: "先确认必须、禁止、推荐的硬边界，避免后续返工。",
    path: "docs/PLUGIN-RULES.md",
    icon: ShieldCheck,
  },
  {
    id: "api-reference",
    title: "完整 API 参考",
    description: "查字段、facade、事件信封、MessageOps、Trace 和前端集成。",
    path: "docs/PLUGIN-API-REFERENCE.md",
    icon: Code2,
  },
  {
    id: "dev-guide",
    title: "索引与路线",
    description: "插件市场路线、文档分篇和 0.x 安全策略入口。",
    path: "docs/PLUGIN-DEV-GUIDE.md",
    icon: BookOpen,
  },
  {
    id: "devtools",
    title: "开发工具",
    description: "使用项目脚手架、校验器和本地调试工具。",
    path: "docs/PLUGIN-DEVTOOLS.md",
    icon: Code2,
  },
  {
    id: "overview",
    title: "插件概览",
    description: "快速开始、插件结构、个人可信插件标准模式与交互入口边界。",
    path: "docs/PLUGIN-OVERVIEW.md",
    icon: FileText,
  },
  {
    id: "http",
    title: "HTTP facade",
    description: "第三方插件访问外部 HTTP 的权限、配额和调用约束。",
    path: "docs/PLUGIN-HTTP.md",
    icon: Network,
  },
  {
    id: "safety",
    title: "安全边界",
    description: "权限声明、交互 Bot、工程规范和安全合规要求。",
    path: "docs/PLUGIN-SAFETY.md",
    icon: ShieldCheck,
  },
  {
    id: "remote",
    title: "远程插件",
    description: "远程安装、manifest 读取、worker loader 与更新回滚。",
    path: "docs/PLUGIN-REMOTE.md",
    icon: Globe2,
  },
  {
    id: "cheatsheet",
    title: "速查表",
    description: "最常用契约、文件结构、权限和验证命令的短清单。",
    path: "docs/PLUGIN-CHEATSHEET.md",
    icon: ListChecks,
  },
  {
    id: "ai",
    title: "AI facade",
    description: "ctx.ai 文本能力、权限声明、降级路径和运行时约束。",
    path: "docs/PLUGIN-AI.md",
    icon: Brain,
  },
  {
    id: "webhook-quickstart",
    title: "Webhook Quickstart",
    description: "快速接入账号级 Webhook 投递与鉴权。",
    path: "docs/PLUGIN-WEBHOOK-QUICKSTART.md",
    icon: Network,
  },
  {
    id: "platform-capabilities",
    title: "平台能力",
    description: "查看 Web、Bot、插件与运行环境的能力边界。",
    path: "docs/PLATFORM-CAPABILITIES.md",
    icon: Puzzle,
  },
  {
    id: "security-ops",
    title: "安全运维",
    description: "部署凭据、恢复与安全处置的操作基线。",
    path: "docs/SECURITY-OPS.md",
    icon: ShieldCheck,
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

function isRemoteManagedInstalledPlugin(row: InstalledPluginOverviewItem) {
  return row.source === "repo" || row.source === "git" || row.source === "local";
}

function isLocalImportedInstalledPlugin(row: InstalledPluginOverviewItem) {
  return row.source === "local" || row.source_url?.startsWith("local://");
}

function installedOverviewTypeLabel(row: InstalledPluginOverviewItem) {
  switch (row.source) {
    case "official":
      return "历史安装记录";
    case "repo":
      return "仓库插件";
    case "git":
      return "Git";
    case "local":
      return "本地导入";
    case "zip":
      return "ZIP";
    default:
      return "第三方";
  }
}

function installedOverviewTypeTone(row: InstalledPluginOverviewItem): "neutral" | "success" | "warn" | "outline" {
  switch (row.source) {
    case "official":
      return "success";
    case "local":
      return "outline";
    case "zip":
      return "neutral";
    case "repo":
    case "git":
      return "warn";
    default:
      return "neutral";
  }
}

function installedOverviewVersionLabel(row: InstalledPluginOverviewItem) {
  if (row.update.update_available) return "可更新";
  if (row.update.last_update_check_error) return "检查失败";
  if (isLocalImportedInstalledPlugin(row)) return "本地导入";
  if (isRemoteManagedInstalledPlugin(row) && row.update.last_update_check_at) return "已是最新版";
  if (isRemoteManagedInstalledPlugin(row)) return "未检查";
  return "本地安装";
}

function installedOverviewVersionTone(row: InstalledPluginOverviewItem): "neutral" | "success" | "warn" | "danger" | "outline" {
  if (row.update.update_available) return "warn";
  if (row.update.last_update_check_error) return "danger";
  if (isLocalImportedInstalledPlugin(row)) return "outline";
  if (isRemoteManagedInstalledPlugin(row) && row.update.last_update_check_at) return "success";
  if (isRemoteManagedInstalledPlugin(row)) return "neutral";
  return "outline";
}

function accountStateLabel(state?: string | null) {
  switch ((state || "").toLowerCase()) {
    case "active":
      return "运行中";
    case "enabled":
      return "已启用";
    case "disabled":
      return "未启用";
    case "error":
    case "failed":
      return "异常";
    default:
      return state || "未知";
  }
}

function accountStateTone(state?: string | null): "neutral" | "success" | "warn" | "danger" | "outline" {
  switch ((state || "").toLowerCase()) {
    case "active":
    case "enabled":
      return "success";
    case "disabled":
      return "outline";
    case "error":
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

function loadStatusLabel(status?: string | null) {
  switch ((status || "").toLowerCase()) {
    case "loaded":
      return "已加载";
    case "loading":
      return "加载中";
    case "failed":
      return "加载失败";
    default:
      return status || "未上报";
  }
}

function loadStatusTone(status?: string | null): "neutral" | "success" | "warn" | "danger" | "outline" {
  switch ((status || "").toLowerCase()) {
    case "loaded":
      return "success";
    case "loading":
      return "warn";
    case "failed":
      return "danger";
    case "":
      return "outline";
    default:
      return "neutral";
  }
}

function trustTierLabel(tier?: string | null) {
  switch ((tier || "").toLowerCase()) {
    case "official":
      return "历史可信记录";
    case "trusted":
      return "可信";
    case "community":
      return "社区";
    case "untrusted":
      return "未验证";
    default:
      return tier || "未标注";
  }
}

function trustTierTone(tier?: string | null): "neutral" | "success" | "warn" | "danger" | "outline" {
  switch ((tier || "").toLowerCase()) {
    case "official":
    case "trusted":
      return "success";
    case "community":
      return "warn";
    case "untrusted":
      return "danger";
    case "":
      return "outline";
    default:
      return "neutral";
  }
}

function signatureLabel(value?: boolean | null) {
  if (value === true) return "签名通过";
  if (value === false) return "签名异常";
  return "未提供签名";
}

function signatureTone(value?: boolean | null): "neutral" | "success" | "warn" | "danger" | "outline" {
  if (value === true) return "success";
  if (value === false) return "danger";
  return "outline";
}

function hasActiveOverviewAccountError(account: InstalledPluginOverviewAccountItem) {
  if (!account.enabled) return false;
  return (
    Boolean(account.last_error)
    || Boolean(account.last_load_error)
    || ["error", "failed"].includes((account.state || "").toLowerCase())
    || (account.load_status || "").toLowerCase() === "failed"
  );
}

function summarizeOverviewAccounts(accounts: InstalledPluginOverviewAccountItem[]) {
  const enabled = accounts.filter((account) => account.enabled).length;
  const errors = accounts.filter(hasActiveOverviewAccountError).length;
  return { enabled, errors, total: accounts.length };
}

function describeInstalledOverviewUpdate(row: InstalledPluginOverviewItem) {
  if (row.update.last_update_check_error) {
    return `最近检查失败：${row.update.last_update_check_error}`;
  }
  if (row.update.update_available) {
    const target = row.update.latest_version ? `，可升级到 ${formatPluginVersion(row.update.latest_version)}` : "";
    return `发现新版本${target}。更新前建议回插件仓库核对能力变化。`;
  }
  if (isLocalImportedInstalledPlugin(row)) {
    return "本地导入插件不支持远程更新，需要重新导入新目录。";
  }
  if (isRemoteManagedInstalledPlugin(row) && row.update.last_update_check_at) {
    return `最近检查：${formatDateTime(row.update.last_update_check_at)}`;
  }
  if (isRemoteManagedInstalledPlugin(row)) {
    return "还没有做过更新检查。";
  }
  return "当前来源只提供本地安装能力，不走远程更新。";
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
  if (raw.startsWith("official://")) return "历史安装记录";
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
  if (sourceValue === "official" || sourceUrl?.startsWith("official://")) return "历史安装记录";
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

function buildCompleteDevGuide(contents: Map<DevDocId, string>) {
  return DEV_DOCS.map((doc, index) => {
    const level = index === 0 ? "#" : "##";
    return `${level} ${doc.title}\n\n> 源文件：\`${doc.path}\`\n\n${stripFirstHeading(contents.get(doc.id) ?? "")}`;
  }).join("\n\n---\n\n");
}

async function fetchDevDoc(doc: DevDoc): Promise<string> {
  const response = await fetch(`/runtime-content/${doc.path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${doc.path} 加载失败（HTTP ${response.status}）`);
  }
  return response.text();
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
          className="border-info/25 bg-info/10 text-info"
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
    <PageShell>
      <PluginWorkspaceHeader activeTab="manage" />

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
    </PageShell>
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
      <InstallToolsGroup />
      <RemoteInstallCard />
      <InstalledPluginsSection />
    </div>
  );
}

function InstallToolsGroup() {
  const [mobileExpanded, setMobileExpanded] = useState(false);

  return (
    <>
      <Card className="sm:hidden" data-install-tools-group>
        <CardHeader className="pb-3">
          <SectionHeader
            icon={Puzzle}
            title="安装与检查"
            description="远程更新检查与本地导入默认折叠；展开后可配置。"
            meta={<SignalPill tone="neutral" label="工具" value={2} className="h-8" />}
            actions={(
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-8 gap-1.5"
                onClick={() => setMobileExpanded((value) => !value)}
                aria-expanded={mobileExpanded}
                aria-controls="install-tools-group-content"
              >
                <ChevronDown className={cn("h-4 w-4 transition-transform", mobileExpanded && "rotate-180")} />
                {mobileExpanded ? "收起" : "展开可配置"}
              </Button>
            )}
          />
        </CardHeader>
        {mobileExpanded ? (
          <CardContent id="install-tools-group-content" className="space-y-4">
            <RemoteUpdateSettingsCard />
            <LocalPluginImportCard />
          </CardContent>
        ) : null}
      </Card>
      <div className="hidden space-y-6 sm:block">
        <RemoteUpdateSettingsCard />
        <LocalPluginImportCard />
      </div>
    </>
  );
}

function LocalPluginImportCard() {
  const qc = useQueryClient();
  const localQ = useQuery({ queryKey: ["local-plugins"], queryFn: fetchLocalPlugins });
  const [open, setOpen] = useState(false);
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [signatureFile, setSignatureFile] = useState<File | null>(null);
  const localPlugins = localQ.data ?? [];

  const installLocalMut = useMutation({
    mutationFn: (name: string) => installLocalPlugin(name),
    onSuccess: (row) => {
      toast.success(`已导入本地插件 ${row.name} v${row.version}`);
      toastPluginLintWarnings(row);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
      qc.invalidateQueries({ queryKey: ["local-plugins"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const uploadMut = useMutation({
    mutationFn: () => {
      if (!zipFile) throw new Error("请选择插件 zip 文件");
      return uploadPluginZip(zipFile, signatureFile);
    },
    onSuccess: (row) => {
      toast.success(`已安装 ${row.key} v${row.version}`);
      setZipFile(null);
      setSignatureFile(null);
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionHeader
          icon={Upload}
          title="本地导入与 ZIP 上传"
          description="目录导入用于本地调试，ZIP 上传用于安装已打包签名的插件；两种方式安装后都会进入下方已安装列表。"
          actions={(
            <Button type="button" variant="outline" size="sm" onClick={() => setOpen((value) => !value)}>
              <ChevronDown className={cn("mr-1 h-4 w-4 transition-transform", open && "rotate-180")} />
              {open ? "收起" : "展开"}
            </Button>
          )}
        />
      </CardHeader>
      {open ? (
      <CardContent className="grid gap-4 xl:grid-cols-2">
        <section className="rounded-lg border border-border/70 bg-background/70 p-4">
          <div className="mb-3 flex items-center gap-2">
            <GitFork className="h-4 w-4 text-primary" />
            <div>
              <div className="text-sm font-semibold">本地目录导入</div>
              <div className="text-xs text-muted-foreground">
                把插件目录放到 <code>plugins/local_imports/</code> 后在这里导入。
              </div>
            </div>
          </div>
          {localQ.isLoading ? (
            <div className="flex h-16 items-center justify-center">
              <Spinner className="text-primary" />
            </div>
          ) : localPlugins.length === 0 ? (
            <p className="rounded-md border border-dashed px-3 py-4 text-sm text-muted-foreground">
              还没发现可导入插件。目录内需包含 <code>plugin.json</code>。
            </p>
          ) : (
            <div className="space-y-2">
              {localPlugins.map((p) => (
                <div key={p.name} className="flex items-center justify-between gap-3 rounded-md border px-3 py-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{p.display_name || p.name}</div>
                    <div className="truncate text-xs text-muted-foreground">{p.subdir || p.name} · v{p.version}</div>
                  </div>
                  <Button
                    size="sm"
                    loading={installLocalMut.isPending}
                    disabled={p.installed}
                    onClick={() => installLocalMut.mutate(p.name)}
                  >
                    {!installLocalMut.isPending ? (
                      <Download className="mr-2 h-4 w-4" />
                    ) : null}
                    {p.installed ? "已导入" : "导入"}
                  </Button>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="rounded-lg border border-border/70 bg-background/70 p-4">
          <div className="mb-3 flex items-center gap-2">
            <Upload className="h-4 w-4 text-primary" />
            <div>
              <div className="text-sm font-semibold">ZIP 上传</div>
              <div className="text-xs text-muted-foreground">适合从外部拿到的签名插件包。</div>
            </div>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label>插件包</Label>
              <Input
                type="file"
                accept=".zip,application/zip,application/x-zip-compressed"
                onChange={(event) => setZipFile(event.target.files?.[0] ?? null)}
                disabled={uploadMut.isPending}
              />
              <div className="min-h-5 truncate text-xs text-muted-foreground">
                {zipFile ? zipFile.name : "请选择包含 manifest.py / plugin.py 的 zip"}
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>签名文件</Label>
              <Input
                type="file"
                onChange={(event) => setSignatureFile(event.target.files?.[0] ?? null)}
                disabled={uploadMut.isPending}
              />
              <div className="min-h-5 truncate text-xs text-muted-foreground">
                {signatureFile ? signatureFile.name : "当前后端要求签名校验通过才会安装"}
              </div>
            </div>
          </div>
          <div className="mt-3 flex justify-end">
            <Button
              type="button"
              className="shrink-0"
              loading={uploadMut.isPending}
              disabled={!zipFile}
              onClick={() => uploadMut.mutate()}
            >
              {!uploadMut.isPending ? <Upload className="mr-2 h-4 w-4" /> : null}
              上传安装
            </Button>
          </div>
        </section>
      </CardContent>
      ) : null}
    </Card>
  );
}

function RemoteUpdateSettingsCard() {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState(false);
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
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <SectionHeader
          icon={RefreshCw}
          title="远程插件更新检查"
          description="后台只检查是否有新版本，不会自动安装；展开后可配置自动检查和检查间隔。"
          actions={(
            <Button type="button" size="sm" variant="outline" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
              <ChevronDown className={cn("mr-1 h-4 w-4 transition-transform", expanded && "rotate-180")} />
              {expanded ? "收起" : "展开配置"}
            </Button>
          )}
        />
      </CardHeader>
      {expanded ? <CardContent className="grid gap-3 md:grid-cols-2 md:items-end xl:grid-cols-[minmax(140px,180px)_minmax(180px,1fr)_auto]">
        <div className="flex min-w-0 items-center gap-3 rounded-md border px-3 py-2">
          <Switch aria-label="自动检查" checked={enabled} onCheckedChange={setEnabled} />
          <div className="min-w-0">
            <div className="whitespace-nowrap text-sm font-medium">自动检查</div>
            <div className="text-xs text-muted-foreground">{enabled ? "已开启" : "已关闭"}</div>
          </div>
        </div>
        <div className="min-w-0 space-y-1.5">
          <Label htmlFor="remote-plugin-check-interval">检查间隔（分钟）</Label>
          <Input
            id="remote-plugin-check-interval"
            inputMode="numeric"
            value={interval}
            onChange={(e) => setInterval(e.target.value.replace(/[^0-9]/g, ""))}
            placeholder="360"
          />
          <div className="text-xs text-muted-foreground">最小 30，最大 10080</div>
        </div>
        <div className="flex gap-2 md:col-span-2 md:justify-end xl:col-span-1">
          <Button onClick={() => saveMut.mutate()} loading={saveMut.isPending}>
            {!saveMut.isPending ? <Save className="mr-2 h-4 w-4" /> : null}
            保存
          </Button>
          <Button variant="outline" onClick={() => checkMut.mutate()} loading={checkMut.isPending}>
            {!checkMut.isPending ? <RefreshCw className="mr-2 h-4 w-4" /> : null}
            立即检查
          </Button>
        </div>
      </CardContent> : null}
    </Card>
  );
}

// ── 远程安装：仓库管理 + 浏览插件 ────────────────────────────────
function RemoteInstallCard() {
  const qc = useQueryClient();
  const [addFormExpanded, setAddFormExpanded] = useState(false);
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
  const [pendingSourceMigration, setPendingSourceMigration] = useState<{
    repoId: number;
    repoUrl: string;
    plugin: PluginRepoPlugin;
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
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
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
    mutationFn: ({ repoId, name, replaceExisting = false }: {
      repoId: number;
      name: string;
      replaceExisting?: boolean;
    }) => installFromRepo(repoId, name, { replace_existing: replaceExisting }),
    onSuccess: (row, variables) => {
      toast.success(
        variables.replaceExisting
          ? `已迁移 ${row.name} 的更新来源，并保留原有配置`
          : `已安装 ${row.name} v${row.version}`,
      );
      setPendingSourceMigration(null);
      toastPluginLintWarnings(row);
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
      qc.invalidateQueries({ queryKey: ["repo-plugins", expandedRepoId] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const updateFromRepoMut = useMutation({
    mutationFn: (name: string) => updateRemotePlugin(name),
    onSuccess: (row) => {
      toast.success(`已更新 ${row.name} → v${row.version}`);
      toastPluginLintWarnings(row);
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
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
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
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
          description="已添加的仓库始终显示；展开配置后可添加新的 Git 仓库。"
          actions={(
            <Button type="button" size="sm" variant="outline" onClick={() => setAddFormExpanded((value) => !value)} aria-expanded={addFormExpanded}>
              <ChevronDown className={cn("mr-1 h-4 w-4 transition-transform", addFormExpanded && "rotate-180")} />
              {addFormExpanded ? "收起配置" : "展开添加仓库"}
            </Button>
          )}
        />
      </CardHeader>
      <CardContent className="space-y-4">
        {/* 添加仓库 */}
        {addFormExpanded ? <div className="grid min-w-0 gap-2 rounded-lg border border-border/70 bg-muted/20 p-3 md:grid-cols-2 xl:grid-cols-[160px_minmax(0,1fr)_minmax(0,280px)_auto]">
          <Input
            className="h-9 min-w-0 rounded-md bg-background"
            placeholder="仓库名（可选）"
            value={addName}
            onChange={(e) => setAddName(e.target.value)}
            disabled={addRepoMut.isPending}
          />
          <Input
            className="h-9 min-w-0 rounded-md bg-background"
            placeholder="https://github.com/user/repo.git 或 /tree/branch"
            value={addUrl}
            onChange={(e) => setAddUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && addUrl.trim()) addRepoMut.mutate();
            }}
            disabled={addRepoMut.isPending}
          />
          <Input
            className="h-9 min-w-0 rounded-md bg-background"
            type="password"
            autoComplete="off"
            placeholder="GitHub Token（私有库可选）"
            value={addToken}
            onChange={(e) => setAddToken(e.target.value)}
            disabled={addRepoMut.isPending}
          />
          <Button
            onClick={() => addRepoMut.mutate()}
            loading={addRepoMut.isPending}
            loadingText="添加中…"
            disabled={!addUrl.trim()}
            className="shrink-0 md:justify-self-start xl:justify-self-auto"
          >
            <Plus className="mr-2 h-4 w-4" />
            添加仓库
          </Button>
          <p className="text-xs text-muted-foreground md:col-span-2 xl:col-span-4">
            私有 GitHub 仓库请填写 fine-grained token，至少授予对应仓库 Contents 读取权限。Token 会加密保存且不会回显。
          </p>
        </div> : null}

        {/* 仓库列表 */}
        {repos.length === 0 ? (
          <EmptyState title="暂无已保存的仓库" size="sm" />
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
                          const canMigrateSource = !!p.installed && !p.source_matches;
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
                                (p.installed && !canUpdate && !canMigrateSource)
                                || installFromRepoMut.isPending
                                || updateFromRepoMut.isPending
                              }
                              onClick={() => {
                                if (canUpdate) {
                                  updateFromRepoMut.mutate(p.name);
                                  return;
                                }
                                if (canMigrateSource) {
                                  setPendingSourceMigration({
                                    repoId: repo.id,
                                    repoUrl: repo.url,
                                    plugin: p,
                                  });
                                  return;
                                }
                                installFromRepoMut.mutate({ repoId: repo.id, name: p.name });
                              }}
                            >
                              {canUpdate ? (
                                <RefreshCw className="mr-1 h-3.5 w-3.5" />
                              ) : canMigrateSource ? (
                                <GitFork className="mr-1 h-3.5 w-3.5" />
                              ) : p.installed ? null : (
                                <Download className="mr-1 h-3.5 w-3.5" />
                              )}
                              {canUpdate ? "更新" : canMigrateSource ? "迁移来源" : p.installed ? "已安装" : "安装"}
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
        <Dialog
          open={pendingSourceMigration !== null}
          onOpenChange={(open) => !open && setPendingSourceMigration(null)}
        >
          <DialogContent className="dialog-center max-w-lg">
            <DialogHeader>
              <DialogTitle>迁移插件更新来源</DialogTitle>
              <DialogDescription>
                将用所选仓库中的同名插件覆盖当前安装包，并把后续更新来源切换到该仓库。
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-3 text-sm">
              <div className="rounded-md border border-border/70 bg-muted/20 p-3">
                <div className="font-medium">
                  {pendingSourceMigration?.plugin.display_name || pendingSourceMigration?.plugin.name}
                </div>
                <div className="mt-1 font-mono text-xs text-muted-foreground">
                  {formatPluginVersion(pendingSourceMigration?.plugin.installed_version)} → {formatPluginVersion(pendingSourceMigration?.plugin.version)}
                </div>
              </div>
              <div className="grid gap-2 text-xs text-muted-foreground">
                <div>
                  <span className="font-medium text-foreground">当前来源：</span>
                  <span className="break-all font-mono">{pendingSourceMigration?.plugin.installed_source_url || "未知来源"}</span>
                </div>
                <div>
                  <span className="font-medium text-foreground">新来源：</span>
                  <span className="break-all font-mono">{pendingSourceMigration?.repoUrl}</span>
                </div>
              </div>
              <div className="rounded-md border border-primary/25 bg-primary/5 p-3 text-xs leading-5 text-muted-foreground">
                账号启停、账号配置、插件全局配置和持久化数据都会保留。仅替换插件代码、元数据和更新来源。安装或校验失败时保留当前版本。
              </div>
            </div>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setPendingSourceMigration(null)}
                disabled={installFromRepoMut.isPending}
              >
                取消
              </Button>
              <Button
                loading={installFromRepoMut.isPending}
                loadingText="迁移中…"
                onClick={() => {
                  if (!pendingSourceMigration) return;
                  installFromRepoMut.mutate({
                    repoId: pendingSourceMigration.repoId,
                    name: pendingSourceMigration.plugin.name,
                    replaceExisting: true,
                  });
                }}
              >
                <GitFork className="mr-2 h-4 w-4" />
                确认迁移来源
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
        <Dialog open={pendingBulkUpdate !== null} onOpenChange={(open) => !open && setPendingBulkUpdate(null)}>
          <DialogContent className="dialog-center !flex max-h-[85dvh] max-w-2xl flex-col gap-0 overflow-hidden p-0">
            <DialogHeader className="shrink-0 border-b border-border/70 px-4 py-4 sm:px-6">
              <DialogTitle>确认批量更新插件</DialogTitle>
              <DialogDescription>
                将从 {pendingBulkUpdate?.repoName ?? "该仓库"} 更新 {bulkPreviewPlugins.length} 个已安装插件。更新前请确认版本变化和高风险能力变化。
              </DialogDescription>
            </DialogHeader>
            <div className="safe-scrollbar min-h-0 flex-1 touch-pan-y space-y-3 overflow-y-auto overscroll-contain px-4 py-4 sm:px-6">
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
            <DialogFooter className="!flex-row shrink-0 justify-end gap-2 border-t border-border/70 bg-card px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:space-x-0 sm:px-6">
              <Button
                variant="outline"
                className="min-w-0 flex-1 border-foreground/25 bg-background shadow-sm hover:border-foreground/40 sm:flex-none sm:px-6"
                onClick={() => setPendingBulkUpdate(null)}
              >
                取消
              </Button>
              <Button
                className="min-w-0 flex-1 sm:flex-none sm:px-6"
                onClick={() => {
                  if (!pendingBulkUpdate) return;
                  bulkUpdateRepoMut.mutate(pendingBulkUpdate.repoId);
                  setPendingBulkUpdate(null);
                }}
                loading={bulkUpdateRepoMut.isPending}
                disabled={!pendingBulkUpdate}
              >
                {!bulkUpdateRepoMut.isPending ? <RefreshCw className="mr-2 h-4 w-4" /> : null}
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
  const [expanded, setExpanded] = useState(false);
  const [expandedAccountPlugin, setExpandedAccountPlugin] = useState<string | null>(null);
  const [selectedDetailKey, setSelectedDetailKey] = useState<string | null>(null);
  const matrixQ = useQuery({ queryKey: queryKeys.featureMatrix, queryFn: getFeatureMatrix });
  const overviewQ = useQuery({ queryKey: INSTALLED_OVERVIEW_QK, queryFn: listInstalledOverview });
  const reposQ = useQuery({ queryKey: PLUGIN_REPOS_QK, queryFn: fetchPluginRepos });
  const builtin = useMemo(
    () =>
      (matrixQ.data?.features ?? []).filter(
        (feature) => feature.is_builtin && feature.key !== "forward" && !isPlatformFeature(feature),
      ),
    [matrixQ.data],
  );
  const accounts = matrixQ.data?.accounts ?? [];
  const installedOverview = overviewQ.data ?? [];
  const repos = reposQ.data ?? [];
  const selectedDetail = installedOverview.find((row) => row.key === selectedDetailKey) ?? null;

  useEffect(() => {
    if (!selectedDetailKey || overviewQ.isLoading) return;
    if (!selectedDetail) setSelectedDetailKey(null);
  }, [overviewQ.isLoading, selectedDetail, selectedDetailKey]);

  const enableInstalledMut = useMutation({
    mutationFn: (key: string) => enableInstall(key),
    onSuccess: () => {
      toast.success("已启用");
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const disableInstalledMut = useMutation({
    mutationFn: (key: string) => disableInstall(key),
    onSuccess: () => {
      toast.success("已禁用");
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const uninstallInstalledMut = useMutation({
    mutationFn: (key: string) => uninstallPlugin(key),
    onSuccess: (_r, key) => {
      toast.success(`已卸载 ${key}`);
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const enableRMMut = useMutation({
    mutationFn: (name: string) => enableRemotePlugin(name),
    onSuccess: (res) => {
      const suffix = typeof res.applied === "number" ? `，已同步 ${res.applied} 个账号` : "";
      toast.success(`已启用远程插件${suffix}`);
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const disableRMMut = useMutation({
    mutationFn: (name: string) => disableRemotePlugin(name),
    onSuccess: () => {
      toast.success("已禁用全局开关");
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const updateRMMut = useMutation({
    mutationFn: (name: string) => updateRemotePlugin(name),
    onSuccess: (row) => {
      toast.success(`已更新 ${row.name} → v${row.version}`);
      toastPluginLintWarnings(row);
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const uninstallRMMut = useMutation({
    mutationFn: (name: string) => uninstallRemotePlugin(name),
    onSuccess: (_r, name) => {
      toast.success(`已卸载 ${name}`);
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const accountToggleMut = useMutation({
    mutationFn: ({ accountId, key, enabled }: { accountId: number; key: string; enabled: boolean }) =>
      toggleAccountFeature(accountId, key, enabled),
    onSuccess: (_row, vars) => {
      toast.success(`${vars.enabled ? "已启用" : "已禁用"}账号插件`);
      qc.invalidateQueries({ queryKey: queryKeys.featureMatrix });
      qc.invalidateQueries({ queryKey: INSTALLED_OVERVIEW_QK });
      qc.invalidateQueries({ queryKey: PLUGINS_QK });
      qc.invalidateQueries({ queryKey: REMOTE_QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const isLoading = matrixQ.isLoading || overviewQ.isLoading;
  const errorMessages = [
    matrixQ.isError ? `平台能力加载失败：${getErrMsg(matrixQ.error)}` : null,
    overviewQ.isError ? `已安装插件概览加载失败：${getErrMsg(overviewQ.error)}` : null,
  ].filter(Boolean) as string[];

  const toggleAccountPanel = (key: string) => {
    setExpandedAccountPlugin((current) => (current === key ? null : key));
  };

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={Puzzle}
          title="已安装插件"
          description="这里显示全局已安装的插件。展开详情可按账号启停；列表中的“禁用”会全局禁用该插件。"
          meta={(
            <SignalPill
              tone="neutral"
              label="总计"
              value={builtin.length + installedOverview.length}
              className="h-8"
            />
          )}
          actions={(
            <Button type="button" size="sm" variant="outline" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
              <ChevronDown className={cn("mr-1 h-4 w-4 transition-transform", expanded && "rotate-180")} />
              {expanded ? "收起" : "展开已安装插件"}
            </Button>
          )}
        />
      </CardHeader>
      {expanded ? <CardContent className="space-y-3">
        {errorMessages.length > 0 ? (
          <div className="space-y-2">
            {errorMessages.map((message) => (
              <div
                key={message}
                className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                <div className="line-clamp-3 break-all">{message}</div>
              </div>
            ))}
          </div>
        ) : null}
        {isLoading ? (
          <div className="flex h-24 items-center justify-center"><Spinner className="text-primary" /></div>
        ) : builtin.length === 0 && installedOverview.length === 0 ? (
          <EmptyState title="暂无已安装插件" />
        ) : (
          <>
          <div className="hidden overflow-x-auto md:block">
          <Table className="min-w-[980px]">
            <TableHeader>
              <TableRow>
                <TableHead>插件</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>来自库</TableHead>
                <TableHead>版本</TableHead>
                <TableHead>版本状态</TableHead>
                <TableHead>账号启停</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {/* 平台基础能力 */}
              {builtin.map((f) => (
                <Fragment key={f.key}>
                  <TableRow>
                    <TableCell>
                      <div className="font-medium">{f.display_name}</div>
                      <div className="font-mono text-xs text-muted-foreground">{f.key}</div>
                    </TableCell>
                    <TableCell>
                      <MetaBadge>平台能力</MetaBadge>
                    </TableCell>
                    <TableCell>
                      <div className="max-w-[180px] truncate text-sm" title="系统核心">
                        系统核心
                      </div>
                    </TableCell>
                    <TableCell>{formatPluginVersion(f.version)}</TableCell>
                    <TableCell>
                      <MetaBadge tone="success">随系统更新</MetaBadge>
                    </TableCell>
                    <TableCell>
                      <PluginAccountSummary
                        pluginKey={f.key}
                        accounts={accounts}
                        expanded={expandedAccountPlugin === f.key}
                        onTogglePanel={toggleAccountPanel}
                      />
                    </TableCell>
                    <TableCell className="text-right">
                      <Button size="sm" variant="outline" onClick={() => nav("/plugins")}>
                        去插件中心
                      </Button>
                    </TableCell>
                  </TableRow>
                  {expandedAccountPlugin === f.key ? (
                    <PluginAccountToggleRow
                      pluginKey={f.key}
                      accounts={accounts}
                      pending={accountToggleMut.isPending}
                      onToggle={(accountId, enabled) =>
                        accountToggleMut.mutate({ accountId, key: f.key, enabled })}
                    />
                  ) : null}
                </Fragment>
              ))}
              {/* 已安装的第三方 / 远程插件 */}
              {installedOverview.map((row) => {
                const summary = summarizeOverviewAccounts(row.accounts);
                const hasWarnings = row.lint_warnings.length > 0;
                const canUpdate = isRemoteManagedInstalledPlugin(row);
                const canUseRemoteActions = isRemoteManagedInstalledPlugin(row);
                return (
                <Fragment key={row.key}>
                  <TableRow>
                    <TableCell>
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <div className="max-w-[20rem] break-words font-medium leading-5">
                            {row.display_name || row.key}
                          </div>
                          <MetaBadge tone={row.global_enabled ? "success" : "outline"}>
                            {row.global_enabled ? "全局已启用" : "全局未启用"}
                          </MetaBadge>
                          {row.update.update_available ? <MetaBadge tone="warn">有新版本</MetaBadge> : null}
                          {hasWarnings ? (
                            <MetaBadge tone={splitPluginWarnings(row.lint_warnings).high.length > 0 ? "danger" : "warn"}>
                              Lint {row.lint_warnings.length}
                            </MetaBadge>
                          ) : null}
                        </div>
                        <div className="mt-1 break-all font-mono text-xs text-muted-foreground">{row.key}</div>
                      </div>
                    </TableCell>
                    <TableCell>
                      <MetaBadge tone={installedOverviewTypeTone(row)}>
                        {installedOverviewTypeLabel(row)}
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
                      <MetaBadge tone={installedOverviewVersionTone(row)}>
                        {installedOverviewVersionLabel(row)}
                      </MetaBadge>
                      {row.update.update_available && row.update.latest_version ? (
                        <div className="mt-1 text-xs text-muted-foreground">
                          最新 {formatPluginVersion(row.update.latest_version)}
                        </div>
                      ) : null}
                      {row.update.last_update_check_error ? (
                        <div className="mt-1 line-clamp-2 break-all text-xs text-destructive">
                          {row.update.last_update_check_error}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      <InstalledOverviewAccountSummary
                        plugin={row}
                        expanded={expandedAccountPlugin === row.key}
                        onTogglePanel={toggleAccountPanel}
                      />
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex flex-wrap justify-end gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setSelectedDetailKey(row.key)}
                        >
                          详情
                        </Button>
                        {canUpdate ? (
                          <Button
                            size="sm"
                            variant={row.update.update_available ? "default" : "outline"}
                            onClick={() => updateRMMut.mutate(row.key)}
                            disabled={updateRMMut.isPending || isLocalImportedInstalledPlugin(row)}
                            title={isLocalImportedInstalledPlugin(row) ? "本地导入插件不支持远程更新" : "从远程更新"}
                          >
                            <RefreshCw className="mr-1 h-3 w-3" />
                            {row.update.update_available ? "更新到新版" : "更新"}
                          </Button>
                        ) : null}
                        {row.global_enabled ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => {
                              if (canUseRemoteActions) disableRMMut.mutate(row.key);
                              else disableInstalledMut.mutate(row.key);
                            }}
                            disabled={disableInstalledMut.isPending || disableRMMut.isPending}
                          >
                            禁用
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            onClick={() => {
                              if (canUseRemoteActions) enableRMMut.mutate(row.key);
                              else enableInstalledMut.mutate(row.key);
                            }}
                            disabled={enableInstalledMut.isPending || enableRMMut.isPending}
                          >
                            启用
                          </Button>
                        )}
                        <Button
                          size="sm"
                          variant="outline"
                          className={DANGER_OUTLINE_BUTTON_CLASS}
                          onClick={() => {
                            if (!confirm(`确认卸载「${row.key}」？`)) return;
                            if (canUseRemoteActions) uninstallRMMut.mutate(row.key);
                            else uninstallInstalledMut.mutate(row.key);
                          }}
                          disabled={uninstallInstalledMut.isPending || uninstallRMMut.isPending}
                        >
                          <Trash2 className="mr-1 h-3 w-3" />
                          卸载
                        </Button>
                        {summary.errors > 0 ? (
                          <div className="ml-1 flex items-center text-xs text-destructive">
                            {summary.errors} 个账号异常
                          </div>
                        ) : null}
                      </div>
                    </TableCell>
                  </TableRow>
                  {expandedAccountPlugin === row.key ? (
                    <InstalledOverviewAccountToggleRow
                      plugin={row}
                      pending={accountToggleMut.isPending}
                      onToggle={(accountId, enabled) =>
                        accountToggleMut.mutate({ accountId, key: row.key, enabled })}
                    />
                  ) : null}
                </Fragment>
                );
              })}
            </TableBody>
          </Table>
          </div>
          <div className="space-y-3 md:hidden">
            {builtin.map((f) => {
              const enabled = accounts.filter((account) => isAccountPluginEnabled(account, f.key)).length;
              const expanded = expandedAccountPlugin === f.key;
              return (
                <div key={`mobile-${f.key}`} className="rounded-xl border border-border/70 bg-background/70 p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="break-words text-sm font-semibold">{f.display_name}</div>
                      <div className="mt-1 break-all font-mono text-xs text-muted-foreground">{f.key}</div>
                    </div>
                    <MetaBadge>平台能力</MetaBadge>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    <MetaBadge tone="success">随系统更新</MetaBadge>
                    <MetaBadge tone={enabled > 0 ? "success" : "outline"}>
                      {accounts.length ? `${enabled}/${accounts.length} 账号` : "无账号"}
                    </MetaBadge>
                  </div>
                  {accounts.length ? (
                    <button
                      type="button"
                      className="mt-3 inline-flex items-center rounded-full border border-border/70 px-3 py-1.5 text-xs text-muted-foreground"
                      onClick={() => toggleAccountPanel(f.key)}
                    >
                      <ChevronDown className={cn("mr-1 h-3.5 w-3.5 transition-transform", expanded && "rotate-180")} />
                      账号开关
                    </button>
                  ) : null}
                  {expanded ? (
                    <div className="mt-3 grid gap-2">
                      {accounts.map((account) => {
                        const accountEnabled = isAccountPluginEnabled(account, f.key);
                        const state = account.features[f.key] ?? "missing";
                        return (
                          <div key={`mobile-${f.key}-${account.id}`} className="flex items-center justify-between gap-3 rounded-lg border border-border/70 bg-muted/30 px-3 py-2">
                            <div className="min-w-0">
                              <div className="truncate text-sm font-medium">{account.name || `账号 ${account.id}`}</div>
                              <div className="font-mono text-xs text-muted-foreground">#{account.id} · {state}</div>
                            </div>
                            <Switch
                              checked={accountEnabled}
                              disabled={accountToggleMut.isPending}
                              onCheckedChange={(checked) => accountToggleMut.mutate({ accountId: account.id, key: f.key, enabled: checked })}
                            />
                          </div>
                        );
                      })}
                    </div>
                  ) : null}
                  <Button size="sm" variant="outline" className="mt-3 w-full" onClick={() => nav("/plugins")}>
                    去插件中心
                  </Button>
                </div>
              );
            })}
            {installedOverview.map((row) => {
              const summary = summarizeOverviewAccounts(row.accounts);
              const hasWarnings = row.lint_warnings.length > 0;
              const canUpdate = isRemoteManagedInstalledPlugin(row);
              const canUseRemoteActions = isRemoteManagedInstalledPlugin(row);
              const expanded = expandedAccountPlugin === row.key;
              return (
                <div key={`mobile-${row.key}`} className="rounded-xl border border-border/70 bg-background/70 p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="break-words text-sm font-semibold">{row.display_name || row.key}</div>
                      <div className="mt-1 break-all font-mono text-xs text-muted-foreground">{row.key}</div>
                    </div>
                    <MetaBadge tone={row.global_enabled ? "success" : "outline"}>
                      {row.global_enabled ? "已启用" : "未启用"}
                    </MetaBadge>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    <MetaBadge tone={installedOverviewTypeTone(row)}>{installedOverviewTypeLabel(row)}</MetaBadge>
                    <MetaBadge tone={installedOverviewVersionTone(row)}>{installedOverviewVersionLabel(row)}</MetaBadge>
                    <MetaBadge tone={summary.enabled > 0 ? "success" : "outline"}>
                      {summary.total ? `${summary.enabled}/${summary.total} 账号` : "无账号"}
                    </MetaBadge>
                    {row.update.update_available ? <MetaBadge tone="warn">有新版本</MetaBadge> : null}
                    {summary.errors > 0 ? <MetaBadge tone="danger">{summary.errors} 异常</MetaBadge> : null}
                    {hasWarnings ? <MetaBadge tone={splitPluginWarnings(row.lint_warnings).high.length > 0 ? "danger" : "warn"}>Lint {row.lint_warnings.length}</MetaBadge> : null}
                  </div>
                  <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                    <PluginMobileInfo label="来源库" value={installSourceLibraryLabel(row.source, row.source_url, row.source_label, repos)} />
                    <PluginMobileInfo label="版本" value={formatPluginVersion(row.version)} />
                  </div>
                  {row.update.last_update_check_error ? (
                    <div className="mt-3 line-clamp-3 break-all rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                      {row.update.last_update_check_error}
                    </div>
                  ) : null}
                  {summary.total > 0 ? (
                    <button
                      type="button"
                      className="mt-3 inline-flex items-center rounded-full border border-border/70 px-3 py-1.5 text-xs text-muted-foreground"
                      onClick={() => toggleAccountPanel(row.key)}
                    >
                      <ChevronDown className={cn("mr-1 h-3.5 w-3.5 transition-transform", expanded && "rotate-180")} />
                      账号开关
                    </button>
                  ) : null}
                  {expanded ? (
                    <div className="mt-3 grid gap-2">
                      {row.accounts.map((account) => (
                        <div key={`mobile-${row.key}-${account.account_id}`} className="flex items-center justify-between gap-3 rounded-lg border border-border/70 bg-muted/30 px-3 py-2">
                          <div className="min-w-0">
                            <div className="truncate text-sm font-medium">{account.account_name || `账号 ${account.account_id}`}</div>
                            <div className="text-xs text-muted-foreground">
                              <span className="font-mono">#{account.account_id}</span>
                              <span> · {account.enabled ? accountStateLabel(account.state) : "已关闭"}</span>
                            </div>
                            {account.enabled && account.last_error ? (
                              <div className="mt-1 line-clamp-2 break-all text-xs text-destructive">{account.last_error}</div>
                            ) : null}
                          </div>
                          <Switch
                            checked={account.enabled}
                            disabled={accountToggleMut.isPending}
                            onCheckedChange={(checked) => accountToggleMut.mutate({ accountId: account.account_id, key: row.key, enabled: checked })}
                          />
                        </div>
                      ))}
                    </div>
                  ) : null}
                  <div className="mt-3 flex flex-nowrap gap-1.5">
                    <Button
                      size="sm"
                      variant="outline"
                      className="min-w-0 flex-1 px-2"
                      onClick={() => setSelectedDetailKey(row.key)}
                    >
                      <Info className="h-3.5 w-3.5 shrink-0" />
                      详情
                    </Button>
                    {canUpdate ? (
                      <Button
                        size="sm"
                        variant={row.update.update_available ? "default" : "outline"}
                        className="min-w-0 flex-1 px-2"
                        onClick={() => updateRMMut.mutate(row.key)}
                        disabled={updateRMMut.isPending || isLocalImportedInstalledPlugin(row)}
                        title={isLocalImportedInstalledPlugin(row) ? "本地导入插件不支持远程更新" : "从远程更新"}
                      >
                        <RefreshCw className="h-3.5 w-3.5 shrink-0" />
                        更新
                      </Button>
                    ) : null}
                    {row.global_enabled ? (
                      <Button
                        size="sm"
                        variant="outline"
                        className="min-w-0 flex-1 px-2"
                        onClick={() => {
                          if (canUseRemoteActions) disableRMMut.mutate(row.key);
                          else disableInstalledMut.mutate(row.key);
                        }}
                        disabled={disableInstalledMut.isPending || disableRMMut.isPending}
                      >
                        <Power className="h-3.5 w-3.5 shrink-0" />
                        禁用
                      </Button>
                    ) : (
                      <Button
                        size="sm"
                        className="min-w-0 flex-1 px-2"
                        onClick={() => {
                          if (canUseRemoteActions) enableRMMut.mutate(row.key);
                          else enableInstalledMut.mutate(row.key);
                        }}
                        disabled={enableInstalledMut.isPending || enableRMMut.isPending}
                      >
                        <Power className="h-3.5 w-3.5 shrink-0" />
                        启用
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="outline"
                      className={cn(DANGER_OUTLINE_BUTTON_CLASS, "min-w-0 flex-1 px-2")}
                      onClick={() => {
                        if (!confirm(`确认卸载「${row.key}」？`)) return;
                        if (canUseRemoteActions) uninstallRMMut.mutate(row.key);
                        else uninstallInstalledMut.mutate(row.key);
                      }}
                      disabled={uninstallInstalledMut.isPending || uninstallRMMut.isPending}
                    >
                      <Trash2 className="h-3.5 w-3.5 shrink-0" />
                      卸载
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
          </>
        )}
        <PluginOverviewDetailDialog
          plugin={selectedDetail}
          repos={repos}
          open={selectedDetail !== null}
          onOpenChange={(open) => {
            if (!open) setSelectedDetailKey(null);
          }}
          onOpenTrace={(traceId) => {
            setSelectedDetailKey(null);
            nav(`/logs?tab=events&trace_id=${encodeURIComponent(traceId)}`);
          }}
          onOpenPluginCenter={() => {
            setSelectedDetailKey(null);
            nav("/plugins");
          }}
          accountTogglePending={accountToggleMut.isPending}
          onToggleAccount={(accountId, enabled) => {
            if (!selectedDetail) return;
            accountToggleMut.mutate({ accountId, key: selectedDetail.key, enabled });
          }}
        />
      </CardContent> : null}
    </Card>
  );
}

function PluginMobileInfo({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="min-w-0 rounded-lg border border-border/70 bg-muted/30 px-3 py-2">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 truncate text-xs font-medium">{value}</div>
    </div>
  );
}

function InstalledOverviewAccountSummary({
  plugin,
  expanded,
  onTogglePanel,
}: {
  plugin: InstalledPluginOverviewItem;
  expanded: boolean;
  onTogglePanel: (key: string) => void;
}) {
  const summary = summarizeOverviewAccounts(plugin.accounts);
  return (
    <div className="flex flex-col items-start gap-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <MetaBadge tone={summary.enabled > 0 ? "success" : "outline"}>
          {summary.total ? `${summary.enabled}/${summary.total}` : "无账号"}
        </MetaBadge>
        {summary.errors > 0 ? <MetaBadge tone="danger">{summary.errors} 异常</MetaBadge> : null}
      </div>
      {summary.total > 0 ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-0 text-xs text-muted-foreground hover:text-foreground"
          onClick={() => onTogglePanel(plugin.key)}
        >
          <ChevronDown className={cn("mr-1 h-3.5 w-3.5 transition-transform", expanded && "rotate-180")} />
          账号开关
        </Button>
      ) : null}
    </div>
  );
}

function InstalledOverviewAccountToggleRow({
  plugin,
  pending,
  onToggle,
}: {
  plugin: InstalledPluginOverviewItem;
  pending: boolean;
  onToggle: (accountId: number, enabled: boolean) => void;
}) {
  return (
    <TableRow className="bg-muted/25">
      <TableCell colSpan={7}>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {plugin.accounts.map((account) => (
            <div
              key={`${plugin.key}-${account.account_id}`}
              className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2"
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{account.account_name || `账号 ${account.account_id}`}</div>
                <div className="text-xs text-muted-foreground">
                  <span className="font-mono">#{account.account_id}</span>
                  <span> · {account.enabled ? accountStateLabel(account.state) : "已关闭"}</span>
                  {account.enabled && account.last_error ? (
                    <span className="line-clamp-1 break-all text-destructive"> · {account.last_error}</span>
                  ) : null}
                </div>
              </div>
              <Switch
                checked={account.enabled}
                disabled={pending}
                onCheckedChange={(checked) => onToggle(account.account_id, checked)}
              />
            </div>
          ))}
        </div>
      </TableCell>
    </TableRow>
  );
}

function PluginOverviewDetailDialog({
  plugin,
  repos,
  open,
  onOpenChange,
  onOpenTrace,
  onOpenPluginCenter,
  accountTogglePending,
  onToggleAccount,
}: {
  plugin: InstalledPluginOverviewItem | null;
  repos: PluginRepo[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onOpenTrace: (traceId: string) => void;
  onOpenPluginCenter: () => void;
  accountTogglePending: boolean;
  onToggleAccount: (accountId: number, enabled: boolean) => void;
}) {
  const [changelogExpanded, setChangelogExpanded] = useState(false);
  const changelogQ = useQuery({
    queryKey: ["plugin-changelog", plugin?.key],
    queryFn: () => getInstalledPluginChangelog(plugin!.key),
    enabled: Boolean(plugin && changelogExpanded),
    retry: false,
  });

  useEffect(() => {
    setChangelogExpanded(false);
  }, [plugin?.key]);

  if (!plugin) return null;

  const summary = summarizeOverviewAccounts(plugin.accounts);
  const warningGroups = splitPluginWarnings(plugin.lint_warnings);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="dialog-center !flex w-[calc(100vw-1.5rem)] max-w-4xl flex-col gap-0 overflow-hidden border-border/80 p-0 shadow-2xl">
        <DialogHeader className="shrink-0 border-b border-border/70 px-5 py-4 sm:px-6">
          <div className="pr-8">
            <div className="flex flex-wrap items-center gap-2">
              <DialogTitle className="text-base sm:text-lg">{plugin.display_name || plugin.key}</DialogTitle>
              <MetaBadge tone={installedOverviewTypeTone(plugin)}>{installedOverviewTypeLabel(plugin)}</MetaBadge>
              <MetaBadge tone={plugin.global_enabled ? "success" : "outline"}>
                {plugin.global_enabled ? "全局已启用" : "全局未启用"}
              </MetaBadge>
            </div>
            <DialogDescription className="mt-2 break-all font-mono text-xs">
              {plugin.key}
            </DialogDescription>
          </div>
        </DialogHeader>

        <div className="safe-scrollbar min-h-0 flex-1 touch-pan-y space-y-5 overflow-y-auto overscroll-contain px-5 py-4 sm:px-6">
          <section className="grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)]">
            <div className="rounded-md border border-border/70 bg-background/80 p-4">
              <div className="mb-3 flex items-center justify-between gap-2">
                <div className="text-sm font-medium">来源与版本</div>
                <Button type="button" size="sm" variant="outline" className="h-8 px-2 text-xs" onClick={() => setChangelogExpanded((value) => !value)}>
                  <FileText className="mr-1 h-3.5 w-3.5" />
                  更新日志
                </Button>
              </div>
              <dl className="grid grid-cols-3 gap-2">
                <PluginOverviewField
                  label="来源库"
                  value={<span className="block truncate" title={installSourceLibraryLabel(plugin.source, plugin.source_url, plugin.source_label, repos)}>{installSourceLibraryLabel(plugin.source, plugin.source_url, plugin.source_label, repos)}</span>}
                />
                <PluginOverviewField label="当前版本" value={formatPluginVersion(plugin.version)} />
                <PluginOverviewField
                  label="更新状态"
                  value={<MetaBadge tone={installedOverviewVersionTone(plugin)}>{installedOverviewVersionLabel(plugin)}</MetaBadge>}
                />
              </dl>
              <div className="mt-3 min-w-0 rounded-md bg-muted/30 px-3 py-2">
                <div className="text-[11px] text-muted-foreground">来源地址</div>
                <div className="mt-1 truncate font-mono text-xs text-muted-foreground" title={plugin.source_url || plugin.source_label || "-"}>
                  {plugin.source_url || plugin.source_label || "-"}
                </div>
              </div>
              <div className="mt-2 text-xs text-muted-foreground">{describeInstalledOverviewUpdate(plugin)}</div>
              {changelogExpanded ? (
                <div className="mt-3 max-h-64 overflow-y-auto rounded-md border bg-muted/20 p-3">
                  {changelogQ.isLoading ? (
                    <div className="flex h-20 items-center justify-center"><Spinner className="text-primary" /></div>
                  ) : changelogQ.isError ? (
                    <div className="text-sm text-destructive">更新日志读取失败：{getErrMsg(changelogQ.error)}</div>
                  ) : changelogQ.data?.available ? (
                    <div className="prose prose-sm max-w-none break-words dark:prose-invert">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{changelogQ.data.content}</ReactMarkdown>
                      {changelogQ.data.message ? <p className="text-xs text-warning">{changelogQ.data.message}</p> : null}
                    </div>
                  ) : (
                    <div className="text-sm text-muted-foreground">{changelogQ.data?.message || "该插件没有提供更新日志。"}</div>
                  )}
                </div>
              ) : null}
            </div>

            <div className="rounded-md border border-border/70 bg-muted/20 p-4">
              <div className="mb-3 text-sm font-medium">运行摘要</div>
              <div className="flex flex-wrap gap-2">
                <SignalPill tone={plugin.global_enabled ? "success" : "neutral"} label="全局" value={plugin.global_enabled ? "启用" : "关闭"} />
                <SignalPill tone={summary.enabled > 0 ? "success" : "neutral"} label="账号启用" value={`${summary.enabled}/${summary.total}`} />
                <SignalPill tone={summary.errors > 0 ? "danger" : "neutral"} label="异常账号" value={summary.errors} />
              </div>
              {plugin.recent_load_error ? (
                <div className="mt-3 rounded-md border border-destructive/25 bg-destructive/10 px-3 py-2">
                  <div className="text-xs font-medium text-destructive">最近 load error</div>
                  <div className="mt-1 line-clamp-3 break-all text-xs text-destructive">
                    {plugin.recent_load_error.message}
                  </div>
                  <div className="mt-1 text-[11px] text-muted-foreground">
                    {plugin.recent_load_error.account_id ? `账号 #${plugin.recent_load_error.account_id} · ` : ""}
                    {plugin.recent_load_error.load_status || plugin.recent_load_error.source}
                    {plugin.recent_load_error.updated_at ? ` · ${formatDateTime(plugin.recent_load_error.updated_at)}` : ""}
                  </div>
                </div>
              ) : (
                <div className="mt-3 text-xs text-muted-foreground">最近没有上报 load error。</div>
              )}
            </div>
          </section>

          <section className="rounded-md border border-border/70 bg-background/80 p-4">
            <div className="flex flex-wrap items-center gap-2">
              <div className="text-sm font-medium">签名与信任</div>
              <MetaBadge tone={signatureTone(plugin.signature_ok)}>{signatureLabel(plugin.signature_ok)}</MetaBadge>
              <MetaBadge tone={trustTierTone(plugin.trust_tier)}>{trustTierLabel(plugin.trust_tier)}</MetaBadge>
            </div>
            {warningGroups.all.length > 0 ? (
              <div className="mt-3 rounded-md border border-warning/30 bg-warning/10 px-3 py-2">
                <div className="flex flex-wrap items-center gap-2 text-xs font-medium text-warning">
                  {warningGroups.high.length > 0 ? <AlertTriangle className="h-3.5 w-3.5" /> : null}
                  Lint warnings {warningGroups.all.length}
                </div>
                <div className="mt-2 space-y-1.5 text-xs text-warning">
                  {warningGroups.all.map((warning, index) => (
                    <div key={`${plugin.key}-detail-warning-${index}`} className="break-all leading-5">
                      {warning}
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="mt-3 text-xs text-muted-foreground">当前没有 lint warnings。</div>
            )}
          </section>

          <section className="rounded-md border border-border/70 bg-background/80 p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm font-medium">账号启停矩阵</div>
              <div className="text-xs text-muted-foreground">
                {summary.enabled}/{summary.total} 个账号启用
              </div>
            </div>
            {plugin.accounts.length === 0 ? (
              <div className="text-sm text-muted-foreground">当前还没有账号记录。</div>
            ) : (
              <div className="grid gap-2 lg:grid-cols-2">
                {plugin.accounts.map((account) => (
                  <InstalledOverviewAccountCard
                    key={`${plugin.key}-detail-${account.account_id}`}
                    account={account}
                    onOpenTrace={onOpenTrace}
                    pending={accountTogglePending}
                    onToggle={(enabled) => onToggleAccount(account.account_id, enabled)}
                  />
                ))}
              </div>
            )}
          </section>
        </div>

        <DialogFooter data-plugin-detail-footer className="!grid shrink-0 grid-cols-3 gap-2 border-t border-border/70 px-5 py-4 sm:px-6 [&>*]:min-w-0 [&>*]:px-2 [&>*]:text-xs sm:[&>*]:text-sm">
          <Button
            variant="outline"
            disabled={!plugin.recent_trace}
            onClick={() => plugin.recent_trace && onOpenTrace(plugin.recent_trace.trace_id)}
          >
            查看最近 trace
          </Button>
          <Button variant="outline" onClick={onOpenPluginCenter}>
            去插件中心
          </Button>
          <Button onClick={() => onOpenChange(false)}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function InstalledOverviewAccountCard({
  account,
  onOpenTrace,
  pending,
  onToggle,
}: {
  account: InstalledPluginOverviewAccountItem;
  onOpenTrace: (traceId: string) => void;
  pending: boolean;
  onToggle: (enabled: boolean) => void;
}) {
  const hasCurrentError = hasActiveOverviewAccountError(account);

  return (
    <div className="rounded-md border border-border/70 bg-muted/10 px-3 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{account.account_name || `账号 ${account.account_id}`}</div>
          <div className="mt-1 font-mono text-xs text-muted-foreground">#{account.account_id}</div>
        </div>
        <MetaBadge tone={account.enabled ? "success" : "outline"}>
          {account.enabled ? "已启用" : "未启用"}
        </MetaBadge>
        {account.enabled ? (
          <>
            <MetaBadge tone={accountStateTone(account.state)}>{accountStateLabel(account.state)}</MetaBadge>
            <MetaBadge tone={loadStatusTone(account.load_status)}>{loadStatusLabel(account.load_status)}</MetaBadge>
          </>
        ) : null}
      </div>

      {account.enabled && account.last_error ? (
        <div className="mt-3">
          <div className="text-[11px] font-medium text-destructive">账号错误</div>
          <div className="mt-1 line-clamp-3 break-all text-xs text-destructive">{account.last_error}</div>
        </div>
      ) : null}

      {account.enabled && account.last_load_error ? (
        <div className="mt-3">
          <div className="text-[11px] font-medium text-muted-foreground">最近 load error</div>
          <div className="mt-1 line-clamp-3 break-all text-xs text-muted-foreground">{account.last_load_error}</div>
        </div>
      ) : null}

      {!account.enabled && (account.last_error || account.last_load_error || hasCurrentError) ? (
        <div className="mt-3 rounded-md border border-border/70 bg-background px-3 py-2 text-xs text-muted-foreground">
          该账号已关闭此插件，历史错误不计入当前异常。
        </div>
      ) : null}

      <div className="mt-3 flex items-end justify-between gap-3">
        <div className="min-w-0 flex-1 text-xs text-muted-foreground">
          {account.last_trace ? (
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span>{account.last_trace.status ? `最近 trace · ${account.last_trace.status}` : "最近 trace"}</span>
              <button
                type="button"
                className="max-w-full truncate font-mono text-primary underline decoration-primary/30 underline-offset-4 hover:text-primary/80"
                onClick={() => onOpenTrace(account.last_trace!.trace_id)}
              >
                {account.last_trace.trace_id}
              </button>
              {account.last_trace.started_at ? <span>{formatDateTime(account.last_trace.started_at)}</span> : null}
            </div>
          ) : <span>最近 trace：-</span>}
        </div>
        <Switch
          checked={account.enabled}
          disabled={pending}
          aria-label={`${account.account_name || `账号 ${account.account_id}`}启用当前插件`}
          onCheckedChange={onToggle}
        />
      </div>
    </div>
  );
}

function PluginOverviewField({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="space-y-1">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm">{value}</dd>
    </div>
  );
}

function PluginAccountSummary({
  pluginKey,
  accounts,
  expanded,
  onTogglePanel,
}: {
  pluginKey: string;
  accounts: PluginAccountRow[];
  expanded: boolean;
  onTogglePanel: (key: string) => void;
}) {
  const enabled = accounts.filter((account) => isAccountPluginEnabled(account, pluginKey)).length;
  return (
    <div className="flex flex-col items-start gap-1.5">
      <MetaBadge tone={enabled > 0 ? "success" : "outline"}>
        {accounts.length ? `${enabled}/${accounts.length}` : "无账号"}
      </MetaBadge>
      {accounts.length ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-0 text-xs text-muted-foreground hover:text-foreground"
          onClick={() => onTogglePanel(pluginKey)}
        >
          <ChevronDown className={cn("mr-1 h-3.5 w-3.5 transition-transform", expanded && "rotate-180")} />
          账号开关
        </Button>
      ) : null}
    </div>
  );
}

function PluginAccountToggleRow({
  pluginKey,
  accounts,
  pending,
  onToggle,
}: {
  pluginKey: string;
  accounts: PluginAccountRow[];
  pending: boolean;
  onToggle: (accountId: number, enabled: boolean) => void;
}) {
  return (
    <TableRow className="bg-muted/25">
      <TableCell colSpan={7}>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {accounts.map((account) => {
            const enabled = isAccountPluginEnabled(account, pluginKey);
            const state = account.features[pluginKey] ?? "missing";
            return (
              <div key={`${pluginKey}-${account.id}`} className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{account.name || `账号 ${account.id}`}</div>
                  <div className="font-mono text-xs text-muted-foreground">#{account.id} · {state}</div>
                </div>
                <Switch
                  checked={enabled}
                  disabled={pending}
                  onCheckedChange={(checked) => onToggle(account.id, checked)}
                />
              </div>
            );
          })}
        </div>
      </TableCell>
    </TableRow>
  );
}

function isAccountPluginEnabled(account: PluginAccountRow, pluginKey: string): boolean {
  const explicit = account.feature_enabled?.[pluginKey];
  if (typeof explicit === "boolean") return explicit;
  if (!Object.prototype.hasOwnProperty.call(account.features, pluginKey)) return false;
  return account.features[pluginKey] !== "disabled";
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
      icon: Sparkles,
    }),
    [],
  );
  const docs = useMemo(() => [completeDoc, ...DEV_DOCS], [completeDoc]);
  const [activeDocId, setActiveDocId] = useState<DevDocId>("all");
  const [highlightPlugin, setHighlightPlugin] = useState<typeof import("rehype-highlight").default | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const activeDoc = docs.find((doc) => doc.id === activeDocId) ?? completeDoc;
  const ActiveIcon = activeDoc.icon;
  const documentQ = useQuery({
    queryKey: ["runtime-content", "plugin-doc", activeDocId],
    queryFn: async () => {
      if (activeDocId !== "all") {
        const doc = DEV_DOCS.find((item) => item.id === activeDocId);
        if (!doc) throw new Error("未知插件文档");
        return fetchDevDoc(doc);
      }
      const entries = await Promise.all(
        DEV_DOCS.map(async (doc) => [doc.id, await fetchDevDoc(doc)] as const),
      );
      return buildCompleteDevGuide(new Map(entries));
    },
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
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
        const repositoryHref = href && !external && !href.startsWith("#")
          ? new URL(
              href,
              `https://github.com/Anoyou/Telebot/blob/main/${activeDoc.path}`,
            ).toString()
          : href;
        return (
          <a
            {...props}
            href={repositoryHref}
            target={external || repositoryHref !== href ? "_blank" : undefined}
            rel={external || repositoryHref !== href ? "noreferrer" : undefined}
          >
            {children}
          </a>
        );
      },
    }),
    [activeDoc.path],
  );

  useEffect(() => {
    contentRef.current?.scrollTo({ top: 0 });
  }, [activeDocId]);

  useEffect(() => {
    let active = true;
    void import("rehype-highlight")
      .then((module) => {
        if (active) setHighlightPlugin(() => module.default);
      })
      .catch(() => {
        if (active) setHighlightPlugin(null);
      });
    return () => { active = false; };
  }, []);

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
            <nav className="horizontal-scroll-touch -mx-1 flex snap-x gap-2 px-1 pb-2 lg:mx-0 lg:block lg:space-y-1 lg:overflow-visible lg:px-0 lg:pb-0">
              {docs.map((doc) => {
                const Icon = doc.icon;
                const active = doc.id === activeDoc.id;
                return (
                  <button
                    key={doc.id}
                    type="button"
                    className={cn(
                      "group flex min-w-[11rem] shrink-0 snap-start items-start gap-3 rounded-lg border px-3 py-3 text-left text-sm transition lg:w-full",
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
              {documentQ.isPending ? (
                <div className="flex min-h-64 items-center justify-center gap-2 text-sm text-muted-foreground">
                  <Spinner />
                  正在读取最新文档…
                </div>
              ) : documentQ.isError ? (
                <div className="mx-auto flex min-h-64 max-w-lg flex-col items-center justify-center gap-3 text-center">
                  <AlertTriangle className="h-6 w-6 text-destructive" />
                  <p className="text-sm text-muted-foreground">
                    {documentQ.error instanceof Error ? documentQ.error.message : "文档加载失败"}
                  </p>
                  <Button size="sm" variant="outline" onClick={() => void documentQ.refetch()}>
                    <RefreshCw className="mr-1 h-3.5 w-3.5" />
                    重新读取
                  </Button>
                </div>
              ) : (
                <article className="prose prose-sm prose-pwa-safe max-w-none dark:prose-invert">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    rehypePlugins={highlightPlugin ? [highlightPlugin] : []}
                    components={markdownComponents}
                  >
                    {documentQ.data}
                  </ReactMarkdown>
                </article>
              )}
            </div>
          </section>
        </div>
      </CardContent>
    </Card>
  );
}
