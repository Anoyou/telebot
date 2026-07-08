import { type ReactNode, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  ArrowRight,
  BookOpen,
  Bot,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  FileText,
  History,
  LayoutDashboard,
  Package,
  Power,
  PlusCircle,
  Sparkles,
} from "lucide-react";

import { getAICommandEnablementSummary, listCommandTemplates, listLLMProviders } from "@/api/commands";
import { listAccounts } from "@/api/accounts";
import { listRecentLLMUsage } from "@/api/llmUsage";
import { getSystemSettings } from "@/api/system";
import type { AccountSummary, CommandTemplateOut, LLMProviderOut } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/misc";
import { MetaBadge } from "@/components/ui/meta-badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { MeterBar, SectionHeader, ToneRailCard } from "@/components/ui/status";
import { CommandBadge } from "@/components/CommandBadge";
import { Glossary } from "@/components/ai/Glossary";
import { HowItWorks } from "@/components/ai/HowItWorks";
import { RecommendedSetup } from "@/components/ai/RecommendedSetup";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { LLMProviders } from "@/pages/AI/LLMProviders";
import { RecentUsageContent } from "@/pages/AI/_components/RecentUsage";

type AITab = "overview" | "providers" | "usage";

const AI_TABS = new Set<AITab>(["overview", "providers", "usage"]);

function normalizeTab(tab: string | null): AITab {
  return tab && AI_TABS.has(tab as AITab) ? (tab as AITab) : "overview";
}

function providerLabel(p?: LLMProviderOut, modelOverride?: unknown) {
  if (!p) return "未选择";
  const model = typeof modelOverride === "string" && modelOverride.trim() ? modelOverride : p.default_model;
  return `${p.name} · ${model}`;
}

function commandModeLabel(template: CommandTemplateOut) {
  const mode = typeof template.config?.mode === "string" ? template.config.mode : "chat";
  const auto = template.config?.routing_mode === "auto";
  const search = template.config?.web_search === true;
  const parts = [mode, auto ? "auto" : "固定"];
  if (search || mode === "search") parts.push("联网");
  if (mode === "image" && template.config?.image_backend === "codex_image") parts.push("codex_image");
  return parts.join(" · ");
}

export function AIIndex() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const [accountPickerOpen, setAccountPickerOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(searchParams.get("help") === "1");
  const [quickStartOpen, setQuickStartOpen] = useState(false);
  const activeTab = normalizeTab(searchParams.get("tab"));
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const aiEnabled = settingsQ.data?.ai_enabled ?? true;
  const providersQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
    enabled: !settingsQ.isLoading && aiEnabled,
    retry: false,
  });
  const templatesQ = useQuery({
    queryKey: ["cmd-tpl"],
    queryFn: listCommandTemplates,
  });
  const usageQ = useQuery({
    queryKey: ["llm-usage", "recent", "summary"],
    queryFn: () => listRecentLLMUsage(100),
    retry: false,
    enabled: !settingsQ.isLoading && aiEnabled && (providersQ.data?.length ?? 0) > 0,
  });
  const enablementQ = useQuery({
    queryKey: ["cmd-tpl", "ai-enablement-summary"],
    queryFn: getAICommandEnablementSummary,
    retry: false,
    enabled: !settingsQ.isLoading && aiEnabled,
  });
  const accountsQ = useQuery({
    queryKey: ["accounts", "ai-enable-picker"],
    queryFn: listAccounts,
    enabled: false,
    retry: false,
  });

  useEffect(() => {
    setHelpOpen(searchParams.get("help") === "1");
  }, [searchParams]);

  const setHelpMenuOpen = (open: boolean) => {
    setHelpOpen(open);
    const next = new URLSearchParams(searchParams);
    if (open) next.set("help", "1");
    else next.delete("help");
    setSearchParams(next, { replace: true });
  };

  const loading = settingsQ.isLoading || templatesQ.isLoading || (aiEnabled && providersQ.isLoading);
  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Spinner className="text-primary" />
      </div>
    );
  }

  const providers = providersQ.data || [];
  const templates = templatesQ.data || [];
  const cmdPrefix = settingsQ.data?.command_prefix || ",";
  if (!aiEnabled) {
    return (
      <PageShell>
        <AIHeader />
        <Card>
          <CardHeader>
            <SectionHeader
              icon={Power}
              title="AI 能力已关闭"
              description="模型提供商、AI 指令调用和插件 ctx.ai 已热拔出。已有模板仍保留，重新启用后可继续使用。"
            />
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            <Button asChild>
              <Link to="/settings?tab=platform">去系统设置启用</Link>
            </Button>
            <Button asChild variant="outline">
              <Link to="/plugins">返回插件中心</Link>
            </Button>
          </CardContent>
        </Card>
      </PageShell>
    );
  }
  const providerById = new Map(providers.map((p) => [p.id, p]));
  const aiTemplates = templates.filter((t) => t.type === "ai");
  const providerCount = providers.length;
  const readyCount = providers.filter((p) => p.has_api_key || p.provider === "ollama").length;
  const usageSummary = usageQ.data?.summary;
  const enablementSummary = enablementQ.data;
  const enabledAccountCount = enablementSummary?.enabled_accounts ?? 0;
  const totalAccountCount = enablementSummary?.total_accounts ?? 0;
  const accountChoices = accountsQ.data ?? [];

  const goAccountCommands = (accountId: number) => {
    navigate(`/accounts/${accountId}?tab=commands`);
  };

  const handleEnableCommand = async () => {
    const result = await accountsQ.refetch();
    const accounts = result.data ?? [];
    if (accounts.length === 0) {
      navigate("/accounts/new");
      return;
    }
    if (accounts.length === 1) {
      goAccountCommands(accounts[0].id);
      return;
    }
    setAccountPickerOpen(true);
  };

  if (activeTab === "providers") {
    return (
      <PageShell>
        <AIHeader />
        <Subnav
          activeTab={activeTab}
          helpOpen={helpOpen}
          onHelpOpenChange={setHelpMenuOpen}
          cmdPrefix={cmdPrefix}
        />
        <div className="rounded-md border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
          已配置 {providerCount} 个模型提供商，其中 {readyCount} 个可调用。联网搜索需要 api_format=responses 的 OpenAI provider。
        </div>
        <LLMProviders />
      </PageShell>
    );
  }

  if (activeTab === "usage") {
    return (
      <PageShell>
        <AIHeader />
        <Subnav
          activeTab={activeTab}
          helpOpen={helpOpen}
          onHelpOpenChange={setHelpMenuOpen}
          cmdPrefix={cmdPrefix}
        />
        <RecentUsageContent />
      </PageShell>
    );
  }

  return (
    <PageShell>
      <AIHeader />
      <Subnav
        activeTab={activeTab}
        helpOpen={helpOpen}
        onHelpOpenChange={setHelpMenuOpen}
        cmdPrefix={cmdPrefix}
      />
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <ToneRailCard
          icon={Package}
          title="Provider 就绪"
          value={`${readyCount}/${providerCount}`}
          description={providerCount > 0 ? "已可调用 / 总数" : "先添加一个模型提供商"}
          tone={readyCount > 0 ? "success" : "warn"}
          valueClassName="truncate text-xl font-bold tracking-tight sm:text-2xl"
        />
        <ToneRailCard
          icon={Bot}
          title="AI 指令数"
          value={aiTemplates.length}
          description={aiTemplates.length > 0 ? "type=ai 模板" : "创建第一条 AI 指令模板"}
          tone={aiTemplates.length > 0 ? "primary" : "warn"}
          valueClassName="truncate text-xl font-bold tracking-tight sm:text-2xl"
        />
        <button
          type="button"
          className="block h-full min-w-0 appearance-none rounded-lg border-0 bg-transparent p-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          onClick={() => navigate("/ai?tab=usage")}
        >
          <ToneRailCard
            icon={History}
            title="近期调用情况"
            value={usageSummary ? `${usageSummary.request_count} 次 / 失败 ${usageSummary.failed_count}` : "暂无"}
            description={usageSummary ? `Fallback ${usageSummary.fallback_count} 次 · 点开查看详情` : "触发调用后展示摘要"}
            tone={(usageSummary?.failed_count ?? 0) > 0 ? "warn" : "neutral"}
            className="h-full border-primary/50 bg-primary/5 shadow-sm transition-colors hover:border-primary hover:bg-primary/10"
            valueClassName="break-words text-xl font-bold tracking-tight sm:text-2xl"
          />
        </button>
        <Card className="border-t-4 border-t-emerald-500/90">
          <CardContent className="space-y-2 p-4">
            <div className="text-sm font-medium">调用成功率</div>
            <div className="text-xl font-semibold sm:text-2xl">
              {usageSummary && usageSummary.request_count > 0
                ? `${Math.round((usageSummary.success_count / usageSummary.request_count) * 100)}%`
                : "暂无"}
            </div>
            <MeterBar
              tone={(usageSummary?.failed_count ?? 0) > 0 ? "warn" : "success"}
              value={
                usageSummary && usageSummary.request_count > 0
                  ? (usageSummary.success_count / usageSummary.request_count) * 100
                  : null
              }
            />
            <div className="text-xs text-muted-foreground">
              {usageSummary
                ? `平均耗时 ${usageSummary.avg_latency_ms}ms`
                : usageQ.isError
                  ? "调用摘要暂不可用"
                  : "触发调用后展示摘要"}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <div className="px-4 pb-1 pt-3">
          <SectionHeader
            icon={Sparkles}
            title="快速上手"
            description="按顺序完成后，你的 Telegram 账号就能用 AI 指令回复消息。"
            actions={
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={() => setQuickStartOpen((v) => !v)}
                aria-expanded={quickStartOpen}
              >
                {quickStartOpen ? (
                  <ChevronDown className="h-4 w-4 text-muted-foreground" />
                ) : (
                  <ChevronRight className="h-4 w-4 text-muted-foreground" />
                )}
              </Button>
            }
          />
        </div>
        {quickStartOpen ? (
          <CardContent className="grid gap-3 pt-0 lg:grid-cols-3">
            <SetupStep
              no="1"
              title="添加模型提供商"
              desc="配置 OpenAI、Anthropic、Ollama 或兼容接口，确认至少一个模型可调用。"
              done={providerCount > 0}
              action="去配置"
              href="/ai?tab=providers&newProvider=1"
            />
            <SetupStep
              no="2"
              title="创建一条 AI 指令"
              desc={<>建议先建 <CommandBadge>{cmdPrefix}ai</CommandBadge>，绑定默认模型或开启 auto 路由。</>}
              done={aiTemplates.length > 0}
              action="去创建"
              href="/plugins/templates?new=ai&returnTo=/ai"
            />
            <SetupStep
              no="3"
              title="在账号上启用指令"
              desc={
                totalAccountCount > 0
                  ? `已有 ${enabledAccountCount}/${totalAccountCount} 个账号启用了至少一条 AI 指令。`
                  : "还没有账号；创建账号后到账号详情的指令 tab 勾选模板。"
              }
              done={enabledAccountCount > 0}
              action="去启用"
              onAction={handleEnableCommand}
              actionLoading={accountsQ.isFetching}
            />
          </CardContent>
        ) : null}
      </Card>

      <Dialog open={accountPickerOpen} onOpenChange={setAccountPickerOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>选择要启用 AI 指令的账号</DialogTitle>
            <DialogDescription>
              将跳转到账号详情的指令 Tab，你可以在那里勾选要启用的 AI 指令模板。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-2">
            {accountChoices.map((account) => (
              <Button
                key={account.id}
                type="button"
                variant="outline"
                className="h-auto justify-between gap-3 px-3 py-2 text-left"
                onClick={() => goAccountCommands(account.id)}
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium">{accountDisplayName(account)}</span>
                  <span className="block truncate text-xs text-muted-foreground">{account.phone}</span>
                </span>
                <ArrowRight className="h-4 w-4 shrink-0" />
              </Button>
            ))}
          </div>
        </DialogContent>
      </Dialog>

      <Card>
        <CardHeader>
          <SectionHeader
            icon={FileText}
            title="你的 AI 指令"
            description="展示 type=ai 的指令模板；编辑会带 returnTo=/ai 回到总览。"
          />
        </CardHeader>
        <CardContent>
          {aiTemplates.length === 0 ? (
            <div className="rounded-md border border-dashed py-8 text-center">
              <p className="text-sm text-muted-foreground">还没有 AI 指令模板。</p>
              <Button asChild className="mt-3" size="sm">
                <Link to="/plugins/templates?new=ai&returnTo=/ai">
                  <PlusCircle className="mr-1 h-4 w-4" />
                  创建 AI 指令
                </Link>
              </Button>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table className="min-w-[820px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>指令</TableHead>
                    <TableHead>模型</TableHead>
                    <TableHead>模式</TableHead>
                    <TableHead>说明</TableHead>
                    <TableHead className="w-24">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {aiTemplates.map((template) => {
                    const provider = providerById.get(Number(template.config?.provider_id));
                    const modelText =
                      template.config?.mode === "image" && template.config?.image_backend === "codex_image"
                        ? "codex_image 插件"
                        : providerLabel(provider, template.config?.model);
                    return (
                      <TableRow key={template.id}>
                        <TableCell className="whitespace-nowrap font-mono">{cmdPrefix}{template.name}</TableCell>
                        <TableCell>{modelText}</TableCell>
                        <TableCell>
                          <MetaBadge tone={template.config?.routing_mode === "auto" ? "success" : "neutral"}>
                            {commandModeLabel(template)}
                          </MetaBadge>
                        </TableCell>
                        <TableCell className="max-w-[22rem] truncate text-sm text-muted-foreground">
                          {template.description || "未填写说明"}
                        </TableCell>
                        <TableCell>
                          <Button asChild variant="outline" size="sm">
                            <Link to={`/plugins/templates?edit=${template.id}&returnTo=${encodeURIComponent("/ai")}`}>
                              编辑
                            </Link>
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </PageShell>
  );
}

function AIHeader() {
  return (
    <PageHeader
      title="AI 中心"
      description="把模型、指令模板、调用记录和帮助信息集中管理。"
      icon={Sparkles}
    />
  );
}

function Subnav({
  activeTab,
  helpOpen,
  onHelpOpenChange,
  cmdPrefix,
}: {
  activeTab: AITab;
  helpOpen: boolean;
  onHelpOpenChange: (open: boolean) => void;
  cmdPrefix: string;
}) {
  const navigate = useNavigate();
  return (
    <div className="flex flex-wrap items-center justify-center gap-2 sm:justify-start">
      <Tabs
        className="w-full sm:w-auto"
        value={activeTab}
        onValueChange={(value) => {
          navigate(value === "overview" ? "/ai" : `/ai?tab=${value}`);
        }}
      >
        <TabsList>
          <TabsTrigger value="overview" className="gap-1.5">
            <LayoutDashboard className="h-4 w-4" />
            总览
          </TabsTrigger>
          <TabsTrigger value="providers" className="gap-1.5">
            <Package className="h-4 w-4" />
            模型提供商
          </TabsTrigger>
          <TabsTrigger value="usage" className="gap-1.5">
            <History className="h-4 w-4" />
            近期调用
          </TabsTrigger>
        </TabsList>
      </Tabs>
      <Button asChild variant="outline" size="sm">
        <Link to="/plugins/templates">
          <FileText className="mr-1 h-4 w-4" />
          查看已配置的指令
        </Link>
      </Button>
      <AIHelpMenu open={helpOpen} onOpenChange={onHelpOpenChange} cmdPrefix={cmdPrefix} />
    </div>
  );
}

function AIHelpMenu({
  open,
  onOpenChange,
  cmdPrefix,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  cmdPrefix: string;
}) {
  return (
    <DropdownMenu modal={false} open={open} onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          <BookOpen className="mr-1 h-4 w-4" />
          AI 帮助
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        sideOffset={10}
        className="max-h-[min(76vh,44rem)] w-[min(52rem,calc(100vw-2rem))] p-0"
        style={{ overflowY: "auto" }}
      >
        <div className="border-b px-4 py-3">
          <div className="text-base font-semibold">AI 帮助</div>
          <div className="mt-1 text-sm text-muted-foreground">
            工作原理、配置示例和术语速查集中在这里，避免占用总览页纵向空间。
          </div>
        </div>
        <div className="space-y-4 p-4">
          <HowItWorks cmdPrefix={cmdPrefix} defaultOpen />
          <RecommendedSetup cmdPrefix={cmdPrefix} defaultOpen />
          <Glossary defaultOpen />
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function SetupStep({
  no,
  title,
  desc,
  done,
  action,
  href,
  onAction,
  actionLoading = false,
}: {
  no: string;
  title: string;
  desc: ReactNode;
  done: boolean;
  action: string;
  href?: string;
  onAction?: () => void | Promise<void>;
  actionLoading?: boolean;
}) {
  const actionContent = (
    <>
      {actionLoading ? "读取账号..." : action}
      <ArrowRight className="ml-1 h-4 w-4" />
    </>
  );
  return (
    <div className={done ? "rounded-xl border border-emerald-500/40 bg-emerald-500/5 p-3" : "rounded-xl border bg-background p-3"}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 font-medium">
          <span className="flex h-7 w-7 items-center justify-center rounded-full border text-xs">{no}</span>
          {title}
        </div>
        {done ? <CheckCircle2 className="h-4 w-4 text-emerald-600" /> : <Sparkles className="h-4 w-4 text-muted-foreground" />}
      </div>
      <p className="mt-2 min-h-10 text-xs leading-5 text-muted-foreground">{desc}</p>
      {href ? (
        <Button asChild variant={done ? "outline" : "default"} size="sm" className="mt-3">
          <Link to={href}>{actionContent}</Link>
        </Button>
      ) : (
        <Button
          type="button"
          variant={done ? "outline" : "default"}
          size="sm"
          className="mt-3"
          disabled={actionLoading}
          onClick={onAction}
        >
          {actionContent}
        </Button>
      )}
    </div>
  );
}

function accountDisplayName(account: AccountSummary) {
  const name = account.display_name?.trim();
  const username = account.tg_username?.trim();
  if (name && username) return `${name} (@${username})`;
  if (name) return name;
  if (username) return `@${username}`;
  return `账号 #${account.id}`;
}
