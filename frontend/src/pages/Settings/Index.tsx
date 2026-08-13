import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Bot,
  ChevronDown,
  Cog,
  Download,
  Save,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  UserPlus,
  Waypoints,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { CommandBadge } from "@/components/CommandBadge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { PageHeader, PageShell } from "@/components/layout/PageScaffold";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import {
  applyRuntimeProfile,
  dryRunRuntimeProfile,
  getPlatformCapabilities,
  getRuntimeProfile,
  getSystemSettings,
  patchPlatformCapability,
  patchSystemSettings,
  restoreRuntimeProfile,
} from "@/api/system";
import type { PlatformModuleKey, RuntimeProfileDryRunOut } from "@/api/types";
import { listAccounts } from "@/api/accounts";
import { getErrMsg, api } from "@/lib/api";
import { moduleLabel, runtimeStateLabel } from "@/lib/navigation";
import { NotifyBots } from "./NotifyBots";
import { DeviceProfileManager } from "./DeviceProfileManager";
import { ProxyManager } from "./ProxyManager";
import { RateTemplates } from "./RateTemplates";
import { SudoManagement } from "./SudoManagement";
import { UserAccount } from "./UserAccount";
import { ConfigBackup } from "./ConfigBackup";
import { NavigationPreferences } from "./NavigationPreferences";

interface KillSwitchState {
  enabled: boolean;
}

type RuntimeLogLevel = "debug" | "info" | "warn" | "error";

const RUNTIME_PROFILE_STATUS_LABEL = {
  idle: "空闲",
  applying: "正在进入",
  active: "值守中",
  restoring: "正在恢复",
  failed: "收敛失败",
} as const;

const GUIDE_STEPS: Array<{
  title: string;
  desc: ReactNode;
  actionLabel: string;
  actionTo: string;
}> = [
  {
    title: "1. 添加并启用账号",
    desc: "先新增 Telegram 账号并启用它，系统会为该账号启动独立 worker。",
    actionLabel: "去添加账号",
    actionTo: "/accounts/new",
  },
  {
    title: "2. 设置指令前缀",
    desc: "在系统设置里确定 Telegram 指令开头字符。",
    actionLabel: "去设置前缀",
    actionTo: "/settings?tab=platform",
  },
  {
    title: "3. 启用指令模板或调用插件",
    desc: "去插件中心启用模板或插件，然后就能在 Telegram 里直接调用。",
    actionLabel: "去插件中心",
    actionTo: "/plugins",
  },
];

function getGuideStepByPath(pathname: string, search: string): number {
  if (pathname === "/accounts" || pathname === "/accounts/new") return 0;
  if (pathname === "/settings" && new URLSearchParams(search).get("tab") === "platform") return 1;
  if (pathname === "/plugins" || pathname.startsWith("/plugins/")) return 2;
  return 0;
}

export function SettingsIndex() {
  const qc = useQueryClient();
  const location = useLocation();
  const nav = useNavigate();
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<"account" | "platform" | "proxy-identity" | "security" | "migration">("account");
  const [rateExpanded, setRateExpanded] = useState(false);
  const [guideExpanded, setGuideExpanded] = useState(false);
  const [quickAid, setQuickAid] = useState("");
  const [quickBindOpen, setQuickBindOpen] = useState(false);
  const [profileConfirmOpen, setProfileConfirmOpen] = useState(false);
  const [profilePreview, setProfilePreview] = useState<RuntimeProfileDryRunOut | null>(null);
  const guideActive = searchParams.get("guide") === "1";
  const currentStep = useMemo(
    () => getGuideStepByPath(location.pathname, location.search),
    [location.pathname, location.search],
  );

  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const killQ = useQuery<KillSwitchState>({
    queryKey: ["system", "kill-switch"],
    queryFn: async () => (await api.get("/api/system/kill-switch")).data,
  });
  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });

  const [prefix, setPrefix] = useState("");
  const [commandPrefixRequired, setCommandPrefixRequired] = useState(true);
  const [aiEnabled, setAiEnabled] = useState(true);
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [llmLimits, setLlmLimits] = useState({
    per_minute: "0",
    daily_requests: "0",
    daily_tokens: "0",
    premium_daily: "0",
  });
  const [payoutLimits, setPayoutLimits] = useState({
    single_max: "0",
    daily_max: "0",
  });
  const [logRetention, setLogRetention] = useState({
    trace_enabled: true,
    event_bus_delivery_enabled: true,
    inline_updates_enabled: true,
    runtime_log_retention_days: "30",
    runtime_log_max_message_chars: "2000",
    runtime_log_max_detail_chars: "8000",
    runtime_log_min_level: "info" as RuntimeLogLevel,
    trace_retention_days: "30",
    trace_payload_snapshot_retention_days: "7",
    native_raw_persist_enabled: false,
    native_raw_retention_days: "1",
  });
  useEffect(() => {
    if (settingsQ.data) {
      setPrefix(settingsQ.data.command_prefix ?? ",");
      setCommandPrefixRequired(settingsQ.data.command_prefix_required ?? true);
      setAiEnabled(settingsQ.data.ai_enabled ?? true);
      setTimezone(settingsQ.data.timezone ?? "Asia/Shanghai");
      setLlmLimits({
        per_minute: String(settingsQ.data.llm_limits?.per_minute ?? 0),
        daily_requests: String(settingsQ.data.llm_limits?.daily_requests ?? 0),
        daily_tokens: String(settingsQ.data.llm_limits?.daily_tokens ?? 0),
        premium_daily: String(settingsQ.data.llm_limits?.premium_daily ?? 0),
      });
      setPayoutLimits({
        single_max: String(settingsQ.data.payout_limits?.single_max ?? 0),
        daily_max: String(settingsQ.data.payout_limits?.daily_max ?? 0),
      });
      setLogRetention({
        trace_enabled: Boolean(settingsQ.data.log_retention?.trace_enabled ?? true),
        event_bus_delivery_enabled: Boolean(settingsQ.data.log_retention?.event_bus_delivery_enabled ?? true),
        inline_updates_enabled: Boolean(settingsQ.data.log_retention?.inline_updates_enabled ?? true),
        runtime_log_retention_days: String(settingsQ.data.log_retention?.runtime_log_retention_days ?? 30),
        runtime_log_max_message_chars: String(settingsQ.data.log_retention?.runtime_log_max_message_chars ?? 2000),
        runtime_log_max_detail_chars: String(settingsQ.data.log_retention?.runtime_log_max_detail_chars ?? 8000),
        runtime_log_min_level: (settingsQ.data.log_retention?.runtime_log_min_level ?? "info") as RuntimeLogLevel,
        trace_retention_days: String(settingsQ.data.log_retention?.trace_retention_days ?? 30),
        trace_payload_snapshot_retention_days: String(settingsQ.data.log_retention?.trace_payload_snapshot_retention_days ?? 7),
        native_raw_persist_enabled: Boolean(settingsQ.data.log_retention?.native_raw_persist_enabled ?? false),
        native_raw_retention_days: String(settingsQ.data.log_retention?.native_raw_retention_days ?? 1),
      });
    }
  }, [settingsQ.data]);

  useEffect(() => {
    const accounts = accountsQ.data ?? [];
    if (accounts.length === 0) {
      setQuickAid("");
      return;
    }
    if (!quickAid || !accounts.some((a) => String(a.id) === quickAid)) {
      setQuickAid(String(accounts[0].id));
    }
  }, [accountsQ.data, quickAid]);

  useEffect(() => {
    const tabParam = searchParams.get("tab");
    if (tabParam === "backup") {
      setTab("migration");
      return;
    }
    if (tabParam === "proxy" || tabParam === "device" || tabParam === "resource" || tabParam === "proxy-identity") {
      setTab("proxy-identity");
      return;
    }
    if (tabParam === "rate") {
      setTab("security");
      return;
    }
    if (
      tabParam === "account" ||
      tabParam === "platform" ||
      tabParam === "security" ||
      tabParam === "migration"
    ) {
      setTab(tabParam as "account" | "platform" | "security" | "migration");
    }
  }, [searchParams]);

  useEffect(() => {
    const accounts = accountsQ.data ?? [];
    if (quickAid || accounts.length === 0) return;
    setQuickAid(String(accounts[0].id));
  }, [accountsQ.data, quickAid]);

  const savePrefix = useMutation({
    mutationFn: () => patchSystemSettings({ command_prefix: prefix }),
    onSuccess: () => {
      toast.success("指令前缀已保存（worker 将热加载）");
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const saveCommandPrefixRequired = useMutation({
    mutationFn: (enabled: boolean) => patchSystemSettings({ command_prefix_required: enabled }),
    onSuccess: (data) => {
      setCommandPrefixRequired(data.command_prefix_required ?? true);
      toast.success(
        (data.command_prefix_required ?? true)
          ? "已要求账号本人指令必须带前缀，worker 将热加载"
          : "已允许账号本人裸命令触发，worker 将热加载",
      );
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const saveTimezone = useMutation({
    mutationFn: () => patchSystemSettings({ timezone }),
    onSuccess: () => {
      toast.success("时区已保存");
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const capsQ = useQuery({
    queryKey: ["system", "capabilities"],
    queryFn: getPlatformCapabilities,
    staleTime: 10_000,
  });

  const profileQ = useQuery({
    queryKey: ["system", "runtime-profile"],
    queryFn: getRuntimeProfile,
    staleTime: 5_000,
  });

  const invalidateRuntimeProfile = () => {
    qc.invalidateQueries({ queryKey: ["system", "runtime-profile"] });
    qc.invalidateQueries({ queryKey: ["system", "capabilities"] });
    qc.invalidateQueries({ queryKey: ["system", "settings"] });
  };

  const previewSafeWatch = useMutation({
    mutationFn: () => dryRunRuntimeProfile("safe_watch"),
    onSuccess: (data) => {
      setProfilePreview(data);
      setProfileConfirmOpen(true);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const applySafeWatch = useMutation({
    mutationFn: () => applyRuntimeProfile("safe_watch"),
    onSuccess: () => {
      setProfileConfirmOpen(false);
      toast.success("值守模式已激活：插件叶投递、定时任务和资金动作已冻结");
      invalidateRuntimeProfile();
    },
    onError: (err) => {
      toast.error(getErrMsg(err));
      invalidateRuntimeProfile();
    },
  });

  const restoreProfile = useMutation({
    mutationFn: restoreRuntimeProfile,
    onSuccess: () => {
      toast.success("已恢复值守前快照，插件投递与定时任务恢复");
      invalidateRuntimeProfile();
    },
    onError: (err) => {
      toast.error(getErrMsg(err));
      invalidateRuntimeProfile();
    },
  });

  const saveCapability = useMutation({
    mutationFn: ({ key, enabled }: { key: PlatformModuleKey | string; enabled: boolean }) =>
      patchPlatformCapability(key, enabled),
    onSuccess: (data) => {
      const label = data.module.label || moduleLabel(data.module.key);
      toast.success(
        data.message
          || (data.module.desired_enabled
            ? `${label} 已启用，正在热加载`
            : `${label} 已关闭，运行时将收敛为暂停`),
      );
      qc.invalidateQueries({ queryKey: ["system", "capabilities"] });
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
      if (data.module.key === "ai") {
        setAiEnabled(data.module.desired_enabled);
        qc.invalidateQueries({ queryKey: ["llm-providers"] });
        qc.invalidateQueries({ queryKey: ["llm-usage"] });
      }
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const saveRiskBudget = useMutation({
    mutationFn: () => patchSystemSettings({
      payout_limits: {
        single_max: Number(payoutLimits.single_max) || 0,
        daily_max: Number(payoutLimits.daily_max) || 0,
      },
      llm_limits: {
        per_minute: Number(llmLimits.per_minute) || 0,
        daily_requests: Number(llmLimits.daily_requests) || 0,
        daily_tokens: Number(llmLimits.daily_tokens) || 0,
        premium_daily: Number(llmLimits.premium_daily) || 0,
      },
    }),
    onSuccess: () => {
      toast.success("风控与预算已保存");
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const saveLogRetention = useMutation({
    mutationFn: () => patchSystemSettings({
      log_retention: {
        trace_enabled: Boolean(logRetention.trace_enabled),
        event_bus_delivery_enabled: Boolean(logRetention.event_bus_delivery_enabled),
        inline_updates_enabled: Boolean(logRetention.inline_updates_enabled),
        runtime_log_retention_days: Number(logRetention.runtime_log_retention_days) || 0,
        runtime_log_max_message_chars: Number(logRetention.runtime_log_max_message_chars) || 2000,
        runtime_log_max_detail_chars: Number(logRetention.runtime_log_max_detail_chars) || 0,
        runtime_log_min_level: logRetention.runtime_log_min_level,
        trace_retention_days: Number(logRetention.trace_retention_days) || 0,
        trace_payload_snapshot_retention_days: Number(logRetention.trace_payload_snapshot_retention_days) || 0,
        native_raw_persist_enabled: Boolean(logRetention.native_raw_persist_enabled),
        native_raw_retention_days: Number(logRetention.native_raw_retention_days) || 0,
      },
    }),
    onSuccess: () => {
      toast.success("运行日志设置已保存，新日志立即按该等级落库");
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const killMut = useMutation({
    mutationFn: async (next: boolean) => {
      await api.post("/api/system/kill-switch", { enabled: next });
    },
    onSuccess: () => {
      toast.success("已下发");
      qc.invalidateQueries({ queryKey: ["system", "kill-switch"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const loading = settingsQ.isLoading || killQ.isLoading;
  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Spinner className="text-primary" />
      </div>
    );
  }

  return (
    <PageShell className="pb-24">
      <PageHeader
        title="系统设置"
        description="按用户管理、前缀通知、网络身份、风控限额和备份恢复拆分，保留常用入口并收敛历史配置位。"
        icon={Cog}
      />

      <Card className="border-dashed">
        <CardHeader>
          <SectionHeader
            icon={Sparkles}
            title="猜你想要？"
            description="常用入口和当前设置风险放在一起，先处理最可能要做的事。"
          />
        </CardHeader>
        <CardContent className="grid grid-cols-3 gap-2 lg:flex lg:items-center">
          <Button asChild variant="outline" size="sm" className="min-w-0 px-2">
            <Link to="/ai?tab=providers">添加模型</Link>
          </Button>
          <Button asChild variant="outline" size="sm" className="min-w-0 px-2">
            <Link to="/operations/templates">添加指令</Link>
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="min-w-0 px-2"
            disabled={(accountsQ.data ?? []).length === 0}
            onClick={() => {
              const accounts = accountsQ.data ?? [];
              if (accounts.length === 1) {
                nav(`/accounts/${accounts[0].id}?tab=bot-management`);
                return;
              }
              setQuickBindOpen(true);
            }}
          >
            <Bot className="mr-1 h-4 w-4" /> 绑定管理 Bot
          </Button>
        </CardContent>
      </Card>

      <Dialog open={quickBindOpen} onOpenChange={setQuickBindOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>选择要绑定机器人的账号</DialogTitle>
            <DialogDescription>
              请选择一个账号，进入该账号的管理 Bot 配置页。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="quick-bind-account">账号</Label>
            <Select
              id="quick-bind-account"
              value={quickAid}
              onChange={(e) => setQuickAid(e.target.value)}
              className="w-full"
              disabled={(accountsQ.data ?? []).length === 0}
            >
              {(accountsQ.data ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.display_name || (a.tg_username ? `@${a.tg_username}` : a.phone)}
                </option>
              ))}
            </Select>
          </div>
          <DialogFooter className="!flex !flex-row gap-2 sm:space-x-0 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
            <Button variant="outline" onClick={() => setQuickBindOpen(false)}>
              取消
            </Button>
            <Button
              disabled={!quickAid}
              onClick={() => {
                setQuickBindOpen(false);
                nav(`/interaction?aid=${quickAid}`);
              }}
            >
              前往配置
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={profileConfirmOpen} onOpenChange={setProfileConfirmOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>确认进入值守模式</DialogTitle>
            <DialogDescription>
              值守会暂停插件叶投递与定时任务，并注册资金动作拒绝原因。平台观测、告警通知和内置管理命令仍可用。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <div className="rounded-md border bg-muted/20 p-3">
              <div className="font-medium">模块变更预览</div>
              {profilePreview?.diff.length ? (
                <div className="mt-2 space-y-1.5">
                  {profilePreview.diff.map((item) => (
                    <div key={item.key} className="flex items-center justify-between gap-3 text-xs">
                      <span>{moduleLabel(item.key)}</span>
                      <span className="text-muted-foreground">
                        {item.from_enabled ? "开启" : "关闭"} → {item.to_enabled ? "开启" : "关闭"}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="mt-2 text-xs text-muted-foreground">
                  模块开关无需调整，仍会执行 worker 暂停收敛与资金冻结。
                </p>
              )}
            </div>
            <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs leading-5">
              {profilePreview?.blind_spot}
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setProfileConfirmOpen(false)}
              disabled={applySafeWatch.isPending}
            >
              取消
            </Button>
            <Button
              onClick={() => applySafeWatch.mutate()}
              loading={applySafeWatch.isPending}
              loadingText="正在收敛"
            >
              确认进入值守
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Tabs value={tab} onValueChange={(v) => setTab(v as typeof tab)}>
        <TabsList className="flex h-auto flex-wrap justify-start gap-1">
          <TabsTrigger value="account" className="gap-1.5">
            <ShieldCheck className="h-4 w-4" /> 用户与管理
          </TabsTrigger>
          <TabsTrigger value="platform" className="gap-1.5">
            <SlidersHorizontal className="h-4 w-4" /> 能力与通知
          </TabsTrigger>
          <TabsTrigger value="proxy-identity" className="gap-1.5">
            <Waypoints className="h-4 w-4" /> 代理与标识
          </TabsTrigger>
          <TabsTrigger value="security" className="gap-1.5">
            <UserPlus className="h-4 w-4" /> 风控与限额
          </TabsTrigger>
          <TabsTrigger value="migration" className="gap-1.5">
            <Download className="h-4 w-4" /> 备份与恢复
          </TabsTrigger>
        </TabsList>

        <TabsContent value="account" className="space-y-6">
          <UserAccount />
          <SudoManagement />
        </TabsContent>

        <TabsContent value="platform" className="space-y-6">
          <NavigationPreferences settings={settingsQ.data} />

          <Card className={guideActive && currentStep === 1 ? "siri-glow-soft" : undefined}>
            <CardHeader>
              <CardTitle className="text-base">指令前缀</CardTitle>
              <CardDescription>
                TG 内指令开头字符（默认 <code>,</code>）。修改后 worker 自动热加载
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex max-w-xs items-end gap-2">
                <div className="flex-1 space-y-1.5">
                  <Label>前缀</Label>
                  <Input
                    value={prefix}
                    maxLength={3}
                    onChange={(e) => setPrefix(e.target.value)}
                  />
                </div>
                <Button
                  className={
                    guideActive && currentStep === 1
                      ? "siri-glow border border-primary/25 bg-background text-primary shadow-sm hover:bg-primary/10 hover:text-primary"
                      : undefined
                  }
                  onClick={() => prefix && savePrefix.mutate()}
                  loading={savePrefix.isPending}
                >
                  {!savePrefix.isPending ? <Save className="mr-2 h-4 w-4" /> : null}
                  保存
                </Button>
              </div>
              <div className="mt-4 flex max-w-xl items-start justify-between gap-4 rounded-md border bg-muted/20 px-3 py-3">
                <div className="space-y-1">
                  <div className="text-sm font-medium">账号本人必须带前缀</div>
                  <p className="text-xs leading-5 text-muted-foreground">
                    关闭后，只有当前 userbot 账号本人发出的消息可以直接用命令名触发；群成员仍不会因为裸命令或系统前缀触发 userbot 命令。
                  </p>
                </div>
                <Switch
                  checked={commandPrefixRequired}
                  disabled={saveCommandPrefixRequired.isPending}
                  onCheckedChange={(checked) => saveCommandPrefixRequired.mutate(checked)}
                />
              </div>
              {guideActive ? (
                <div className="mt-3">
                  <GuideInlineCard
                    expanded={guideExpanded}
                    currentStep={currentStep}
                    onToggle={() => setGuideExpanded((v) => !v)}
                    onPrimary={() => nav("/plugins?guide=1")}
                    onSkip={() => nav("/plugins?guide=1")}
                  />
                </div>
              ) : null}
              <div className="mt-4 max-w-[460px] rounded-xl border bg-background p-3 text-xs">
                <div className="mb-3 font-medium">触发预览</div>
                <div className="rounded-2xl border bg-gradient-to-b from-info/10 to-success/10 p-4">
                  <div className="space-y-2.5">
                    <div className="w-fit max-w-[78%] rounded-2xl rounded-bl-lg border bg-card px-3.5 py-2.5 text-foreground shadow-sm sm:max-w-[66%]">
                      <div className="font-mono text-sm">
                        这是一段被回复的原文。
                      </div>
                    </div>

                    <div className="ml-auto w-fit max-w-[68%] rounded-2xl rounded-br-lg bg-primary px-3.5 py-2.5 text-primary-foreground shadow-sm sm:max-w-[52%]">
                      <div className="mb-1.5 inline-block max-w-full rounded-lg border-l-2 border-white/70 bg-white/15 px-2 py-1 text-[11px] leading-relaxed text-white/90">
                        这是一段被回复的原文。
                      </div>
                      <div className="font-mono text-sm">
                        {commandPrefixRequired ? (prefix || ",") : ""}ai 请总结这段内容
                      </div>
                    </div>

                    <div className="ml-auto w-fit max-w-[78%] rounded-2xl rounded-br-lg bg-primary px-3.5 py-2.5 text-primary-foreground shadow-sm sm:max-w-[66%]">
                      <div className="font-semibold text-sm">
                        {commandPrefixRequired ? (prefix || ",") : ""}(๑•̌.•̑๑)ˀ̣ˀ̣ˀ̣ 好奇
                      </div>
                      <div className="mt-2 inline-block max-w-full rounded-lg border-l-2 border-white/60 bg-white/15 px-2 py-1 text-white/90">
                        这是一段被回复的原文。
                      </div>
                      <div className="mt-2 block w-fit max-w-full rounded-lg border-l-2 border-white/60 bg-white/15 px-2 py-1 text-white/90">
                        请总结这段内容
                      </div>
                      <div className="mt-2.5 font-semibold text-sm">ᕦ(ˇò_ó)ᕤ 回答</div>
                      <p className="mt-2 text-white/90 leading-relaxed">
                        这是 AI 回答示例，已按当前消息模板渲染。
                      </p>
                      <div className="mt-2 inline-block max-w-full rounded-lg border-l-2 border-white/60 bg-white/15 px-2 py-1 text-white/90">
                        这里是从第三行开始的回答内容。
                      </div>
                      <div className="my-2.5 text-left text-white/70">━━━━━━━━━━━━━━━</div>
                      <div className="text-left font-semibold text-white/95 text-[11px]">✦ GPT-5.5 · OpenAI ✦</div>
                    </div>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card className="overflow-hidden">
            <div
              className={`h-1 ${
                profileQ.data?.status === "failed"
                  ? "bg-destructive"
                  : profileQ.data?.active_profile === "safe_watch"
                    ? "bg-warning"
                    : "bg-success"
              }`}
            />
            <CardHeader className="gap-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-1">
                  <CardTitle className="flex items-center gap-2 text-base">
                    <ShieldAlert className="h-4 w-4 text-warning" />
                    运行模式
                  </CardTitle>
                  <CardDescription>
                    一键进入可持久、自愈的值守态，恢复时逐项还原进入前快照。
                  </CardDescription>
                </div>
                <SignalPill
                  tone={
                    profileQ.data?.status === "failed"
                      ? "danger"
                      : profileQ.data?.active_profile === "safe_watch"
                        ? "warn"
                        : "success"
                  }
                  label="当前"
                  value={
                    profileQ.isLoading
                      ? "读取中"
                      : profileQ.data?.active_profile === "safe_watch"
                        ? "值守"
                        : profileQ.data?.current_profile === "custom"
                          ? "自定义"
                          : "生产"
                  }
                />
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-2 text-xs sm:grid-cols-3">
                <div className="rounded-md border bg-muted/20 p-3">
                  <div className="text-muted-foreground">投递与任务</div>
                  <div className="mt-1 font-medium">
                    {profileQ.data?.active_profile === "safe_watch" ? "已暂停" : "按配置运行"}
                  </div>
                </div>
                <div className="rounded-md border bg-muted/20 p-3">
                  <div className="text-muted-foreground">资金动作</div>
                  <div className="mt-1 font-medium">
                    {profileQ.data?.active_profile === "safe_watch" ? "已拒绝" : "按能力闸运行"}
                  </div>
                </div>
                <div className="rounded-md border bg-muted/20 p-3">
                  <div className="text-muted-foreground">状态</div>
                  <div className="mt-1 font-medium">
                    {profileQ.data
                      ? RUNTIME_PROFILE_STATUS_LABEL[profileQ.data.status]
                      : "读取中"}
                  </div>
                </div>
              </div>

              {profileQ.data?.active_profile === "safe_watch" ? (
                <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs leading-5">
                  {profileQ.data.blind_spot}
                </div>
              ) : null}
              {profileQ.data?.last_error ? (
                <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
                  收敛失败：{profileQ.data.last_error}
                </div>
              ) : null}

              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="max-w-2xl text-xs leading-5 text-muted-foreground">
                  值守中 userbot 直通与命令入站继续纯观测落库，插件叶零投递。平台通知与内置管理命令保留，可随时查询并退出值守。
                </p>
                {profileQ.data?.active_profile === "safe_watch" ? (
                  <Button
                    variant="outline"
                    onClick={() => restoreProfile.mutate()}
                    loading={restoreProfile.isPending}
                    loadingText="正在恢复"
                    disabled={
                      profileQ.data.status === "restoring"
                      || profileQ.data.status === "applying"
                    }
                  >
                    恢复值守前快照
                  </Button>
                ) : (
                  <Button
                    onClick={() => previewSafeWatch.mutate()}
                    loading={previewSafeWatch.isPending}
                    loadingText="正在预检"
                    disabled={profileQ.isLoading || profileQ.data?.status === "applying"}
                  >
                    进入值守模式
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">平台能力</CardTitle>
              <CardDescription>
                可选平台模块热关闭 / 热启动。关闭只暂停入口与运行时资源，不删除配置、Token、规则或资金数据。
                userbot、审计、结算与补偿属于平台内核，不受这些开关影响。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {(capsQ.data?.modules ?? [
                {
                  key: "ai",
                  label: "AI",
                  desired_enabled: aiEnabled,
                  generation: 0,
                  runtime_state: aiEnabled ? "ready" : "stopped",
                },
              ]).map((mod) => {
                const pending =
                  saveCapability.isPending &&
                  saveCapability.variables?.key === mod.key;
                return (
                  <div
                    key={mod.key}
                    className="flex min-h-32 flex-col gap-3 rounded-md border border-border/70 bg-muted/20 p-3"
                  >
                    <div className="min-w-0 flex-1 space-y-1">
                      <div className="flex min-w-0 items-start justify-between gap-2">
                        <div className="flex min-w-0 flex-wrap items-center gap-2">
                          <span className="text-sm font-medium">{mod.label}</span>
                          <span className="text-[11px] text-muted-foreground">
                            gen {mod.generation}
                          </span>
                        </div>
                        <SignalPill
                          tone={
                            mod.desired_enabled
                              ? mod.runtime_state === "ready"
                                ? "success"
                                : "warn"
                              : "neutral"
                          }
                          label="状态"
                          value={runtimeStateLabel(String(mod.runtime_state))}
                          className="h-8 shrink-0 px-2"
                        />
                      </div>
                      <p className="text-xs leading-5 text-muted-foreground">
                        {mod.key === "ai" &&
                          "模型 Provider、AI 指令与插件 ctx.ai。关闭后 worker 不加载密钥与代理。"}
                        {mod.key === "interaction_bot" &&
                          "交互 Bot / 测试 Bot 与 interaction_bot 通道。管理 Bot 与 userbot 不受影响。"}
                        {mod.key === "webhooks" &&
                          "公开入站 Webhook 投递。关闭后外部 URL 立即 404，配置与 Token 保留。"}
                        {mod.key === "ledger" &&
                          "台账查询与操作面。ActionEvent 与派奖补偿主账继续写入。"}
                        {mod.key === "dispatch_debug" &&
                          "命中模拟与 router debug trace。普通日志与基础 Event Trace 不受影响。"}
                      </p>
                      {mod.last_error ? (
                        <p className="text-xs text-destructive">{mod.last_error}</p>
                      ) : null}
                    </div>
                    <div className="flex justify-end">
                      <Switch
                        checked={Boolean(mod.desired_enabled)}
                        disabled={pending || capsQ.isLoading}
                        onCheckedChange={(checked) =>
                          saveCapability.mutate({ key: mod.key, enabled: checked })
                        }
                        aria-label={`切换 ${mod.label}`}
                      />
                    </div>
                  </div>
                );
              })}
              {capsQ.data?.worker_convergence ? (
                <p className="text-[11px] leading-5 text-muted-foreground">
                  Worker 收敛：确认 {capsQ.data.worker_convergence.acked}/
                  {capsQ.data.worker_convergence.total_accounts}
                  {capsQ.data.worker_convergence.offline_or_timeout > 0
                    ? `，${capsQ.data.worker_convergence.offline_or_timeout} 个将由周期 reconcile 收敛`
                    : ""}
                  。
                </p>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">通知渠道</CardTitle>
              <CardDescription>设置系统事件的推送目标与通知机器人。</CardDescription>
            </CardHeader>
            <CardContent>
              <NotifyBots />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="proxy-identity" className="space-y-6">
          <ProxyManager />
          <DeviceProfileManager />
        </TabsContent>

        <TabsContent value="security" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">全局总闸（Kill Switch）</CardTitle>
              <CardDescription>
                开启后所有账号 worker 立即暂停，仅保留接收
              </CardDescription>
            </CardHeader>
            <CardContent className="flex items-center gap-4">
              <Switch
                checked={!!killQ.data?.enabled}
                onCheckedChange={(v) => {
                  if (v && !confirm("确认开启总闸？所有账号立即暂停！")) return;
                  killMut.mutate(v);
                }}
              />
              <span className="text-sm text-muted-foreground">
                当前：{killQ.data?.enabled ? "已暂停" : "正常运行"}
              </span>
            </CardContent>
          </Card>

          <div className="rounded-lg border bg-card">
            <div className="p-6">
              <button
                type="button"
                className="flex w-full items-center justify-between gap-2 text-left"
                onClick={() => setRateExpanded((v) => !v)}
              >
                <div>
                  <CardTitle className="text-base">频控模板</CardTitle>
                  <CardDescription>管理历史的速率限制模板，默认收起。</CardDescription>
                </div>
                <ChevronDown
                  className={`h-4 w-4 text-muted-foreground transition-transform ${rateExpanded ? "rotate-180" : ""}`}
                />
              </button>
            </div>
            {rateExpanded ? (
              <div className="border-t p-4">
                <RateTemplates />
              </div>
            ) : null}
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">时区设置</CardTitle>
              <CardDescription>
                全局时区，影响定时任务"下次触发/上次触发"等时间显示。默认使用 Asia/Shanghai。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex max-w-sm items-end gap-2">
                <div className="flex-1 space-y-1.5">
                  <Label>IANA 时区</Label>
                  <Input
                    value={timezone}
                    onChange={(e) => setTimezone(e.target.value)}
                    placeholder="如 Asia/Shanghai"
                  />
                  <p className="text-xs text-muted-foreground">
                    当前浏览器时区：<b>{Intl.DateTimeFormat().resolvedOptions().timeZone}</b>
                  </p>
                </div>
                <Button onClick={() => saveTimezone.mutate()} loading={saveTimezone.isPending}>
                  {!saveTimezone.isPending ? <Save className="mr-2 h-4 w-4" /> : null}
                  保存
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">运行日志与 Trace / Event Bus 设置</CardTitle>
              <CardDescription>
                控制运行日志、Trace、Event Bus、Inline 和 native_raw 保留策略。日志等级保存后立即影响新日志落库，0 天表示不自动删除。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid gap-3 md:grid-cols-4">
                <div className="space-y-1.5">
                  <Label>保留天数</Label>
                  <Input
                    inputMode="numeric"
                    value={logRetention.runtime_log_retention_days}
                    onChange={(e) =>
                      setLogRetention((v) => ({
                        ...v,
                        runtime_log_retention_days: e.target.value.replace(/[^0-9]/g, ""),
                      }))
                    }
                  />
                  <p className="text-xs text-muted-foreground">默认 30；0 = 不自动删除</p>
                </div>
                <div className="space-y-1.5">
                  <Label>消息正文最多字符</Label>
                  <Input
                    inputMode="numeric"
                    value={logRetention.runtime_log_max_message_chars}
                    onChange={(e) =>
                      setLogRetention((v) => ({
                        ...v,
                        runtime_log_max_message_chars: e.target.value.replace(/[^0-9]/g, ""),
                      }))
                    }
                  />
                  <p className="text-xs text-muted-foreground">默认 2000，最小 200</p>
                </div>
                <div className="space-y-1.5">
                  <Label>结构化详情最多字符</Label>
                  <Input
                    inputMode="numeric"
                    value={logRetention.runtime_log_max_detail_chars}
                    onChange={(e) =>
                      setLogRetention((v) => ({
                        ...v,
                        runtime_log_max_detail_chars: e.target.value.replace(/[^0-9]/g, ""),
                      }))
                    }
                  />
                  <p className="text-xs text-muted-foreground">默认 8000；0 = 不保存 detail</p>
                </div>
                <div className="space-y-1.5">
                  <Label>运行日志等级（即时生效）</Label>
                  <Select
                    value={logRetention.runtime_log_min_level}
                    onChange={(e) =>
                      setLogRetention((v) => ({
                        ...v,
                        runtime_log_min_level: e.target.value as RuntimeLogLevel,
                      }))
                    }
                  >
                    <option value="debug">debug（排障最详细）</option>
                    <option value="info">info（默认）</option>
                    <option value="warn">warn（只看告警和错误）</option>
                    <option value="error">error（只看错误）</option>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    debug 会记录插件排障细节；info 适合日常；warn/error 只保留异常。
                  </p>
                </div>
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-3">
                <LogToggleCard
                  label="写入 Trace"
                  description="关闭后保留旧运行日志；仅用于排查 Trace 存储异常。"
                  checked={logRetention.trace_enabled}
                  onCheckedChange={(checked) =>
                    setLogRetention((v) => ({ ...v, trace_enabled: checked }))
                  }
                />
                <LogToggleCard
                  label="Event Bus 投递"
                  description="关闭后回退旧交互规则链路，适合部署回滚观察。"
                  checked={logRetention.event_bus_delivery_enabled}
                  onCheckedChange={(checked) =>
                    setLogRetention((v) => ({ ...v, event_bus_delivery_enabled: checked }))
                  }
                />
                <LogToggleCard
                  label="Inline 更新"
                  description="关闭后不拉取 inline_query / chosen_inline_result。"
                  checked={logRetention.inline_updates_enabled}
                  onCheckedChange={(checked) =>
                    setLogRetention((v) => ({ ...v, inline_updates_enabled: checked }))
                  }
                />
              </div>
              <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
                <div className="space-y-1.5">
                  <Label>Trace 保留天数</Label>
                  <Input
                    inputMode="numeric"
                    value={logRetention.trace_retention_days}
                    onChange={(e) =>
                      setLogRetention((v) => ({
                        ...v,
                        trace_retention_days: e.target.value.replace(/[^0-9]/g, ""),
                      }))
                    }
                  />
                  <p className="text-xs text-muted-foreground">默认 30；0 = 不自动删除链路记录</p>
                </div>
                <div className="space-y-1.5">
                  <Label>Payload 快照保留天数</Label>
                  <Input
                    inputMode="numeric"
                    value={logRetention.trace_payload_snapshot_retention_days}
                    onChange={(e) =>
                      setLogRetention((v) => ({
                        ...v,
                        trace_payload_snapshot_retention_days: e.target.value.replace(/[^0-9]/g, ""),
                      }))
                    }
                  />
                  <p className="text-xs text-muted-foreground">默认 7；到期只清空快照，保留主链路</p>
                </div>
                <div className="flex min-w-0 flex-col gap-1.5">
                  <Label>保存完整 native_raw</Label>
                  <p className="text-xs leading-5 text-muted-foreground">默认关闭；仅用于短期深度排障</p>
                  <div className="mt-auto flex min-h-10 items-end justify-end pt-2">
                    <Switch
                      checked={logRetention.native_raw_persist_enabled}
                      onCheckedChange={(checked) =>
                        setLogRetention((v) => ({ ...v, native_raw_persist_enabled: checked }))
                      }
                    />
                  </div>
                </div>
                <div className="space-y-1.5">
                  <Label>native_raw 保留天数</Label>
                  <Input
                    inputMode="numeric"
                    value={logRetention.native_raw_retention_days}
                    onChange={(e) =>
                      setLogRetention((v) => ({
                        ...v,
                        native_raw_retention_days: e.target.value.replace(/[^0-9]/g, ""),
                      }))
                    }
                  />
                  <p className="text-xs text-muted-foreground">默认 1；当前默认不持久化完整内容</p>
                </div>
              </div>
              <div className="mt-3 flex justify-end">
                <Button onClick={() => saveLogRetention.mutate()} loading={saveLogRetention.isPending}>
                  {!saveLogRetention.isPending ? <Save className="mr-2 h-4 w-4" /> : null}
                  保存
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <SectionHeader
                icon={ShieldCheck}
                title="风控与预算"
                description="统一设置出款上限与 AI 账号预算。0 会按未限制处理，保存后新请求立即按后端风控读取。"
                meta={<SignalPill tone="warn" label="默认" value="未限制" />}
              />
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="space-y-3 rounded-md border border-border/70 bg-muted/20 p-3">
                <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <div className="text-sm font-medium">Payout 出款限制</div>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">
                      按你的业务币种或积分口径填写；先给单笔和日累计设上限，再根据真实流水放宽。
                    </p>
                  </div>
                  <SignalPill tone="primary" label="建议" value="先小额启用" />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <RiskLimitField
                    label="单笔上限"
                    value={payoutLimits.single_max}
                    suggestion="建议从 100-500 起步，确认结算稳定后再调高。"
                    onChange={(next) => setPayoutLimits((v) => ({ ...v, single_max: next }))}
                  />
                  <RiskLimitField
                    label="日累计上限"
                    value={payoutLimits.daily_max}
                    suggestion="建议不超过单笔上限的 5-10 倍，用于挡异常刷量。"
                    onChange={(next) => setPayoutLimits((v) => ({ ...v, daily_max: next }))}
                  />
                </div>
              </div>

              <div className="space-y-3 rounded-md border border-border/70 bg-muted/20 p-3">
                <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <div className="text-sm font-medium">AI 预算限制</div>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">
                      按账号统计，业务调用前预扣；诊断测活只记录 usage，不占用账号预算。
                    </p>
                  </div>
                  <SignalPill tone="primary" label="范围" value="每账号" />
                </div>
                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                  <RiskLimitField
                    label="每分钟调用"
                    value={llmLimits.per_minute}
                    suggestion="建议 20-60，防止循环触发或插件异常重试。"
                    onChange={(next) => setLlmLimits((v) => ({ ...v, per_minute: next }))}
                  />
                  <RiskLimitField
                    label="每日调用"
                    value={llmLimits.daily_requests}
                    suggestion="建议 500-2000，按账号活跃度调整。"
                    onChange={(next) => setLlmLimits((v) => ({ ...v, daily_requests: next }))}
                  />
                  <RiskLimitField
                    label="每日 Token"
                    value={llmLimits.daily_tokens}
                    suggestion="建议 200000-1000000；STT token 为估算值。"
                    onChange={(next) => setLlmLimits((v) => ({ ...v, daily_tokens: next }))}
                  />
                  <RiskLimitField
                    label="高价每日调用"
                    value={llmLimits.premium_daily}
                    suggestion="建议 20-100，只统计 cost_tier ≥ 3 的模型。"
                    onChange={(next) => setLlmLimits((v) => ({ ...v, premium_daily: next }))}
                  />
                </div>
              </div>

              <div className="flex justify-end">
                <Button onClick={() => saveRiskBudget.mutate()} loading={saveRiskBudget.isPending}>
                  {!saveRiskBudget.isPending ? <Save className="mr-2 h-4 w-4" /> : null}
                  保存风控与预算
                </Button>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="migration">
          <ConfigBackup />
        </TabsContent>
      </Tabs>
    </PageShell>
  );
}

function normalizeLimitInput(value: string): string {
  return value.replace(/[^0-9]/g, "");
}

function LogToggleCard({
  label,
  description,
  checked,
  onCheckedChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex min-h-36 flex-col rounded-md border border-border/70 bg-muted/20 p-3">
      <Label>{label}</Label>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{description}</p>
      <div className="mt-auto flex justify-end pt-4">
        <Switch
          checked={checked}
          aria-label={label}
          onCheckedChange={onCheckedChange}
        />
      </div>
    </div>
  );
}

function limitStatus(value: string): string {
  return Number(value) > 0 ? value : "未限制";
}

function RiskLimitField({
  label,
  value,
  suggestion,
  onChange,
}: {
  label: string;
  value: string;
  suggestion: string;
  onChange: (value: string) => void;
}) {
  const unrestricted = Number(value) <= 0;
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <Label>{label}</Label>
        <span
          className={`rounded-full border px-2 py-0.5 text-[11px] ${
            unrestricted
              ? "border-warning/30 bg-warning/10 text-warning"
              : "border-success/25 bg-success/10 text-success"
          }`}
        >
          {limitStatus(value)}
        </span>
      </div>
      <Input
        inputMode="numeric"
        value={value}
        onChange={(e) => onChange(normalizeLimitInput(e.target.value))}
      />
      <p className="text-xs leading-5 text-muted-foreground">{suggestion}</p>
    </div>
  );
}

function GuideInlineCard({
  expanded,
  currentStep,
  onToggle,
  onPrimary,
  onSkip,
}: {
  expanded: boolean;
  currentStep: number;
  onToggle: () => void;
  onPrimary: () => void;
  onSkip: () => void;
}) {
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const cmdPrefix = settingsQ.data?.command_prefix || ",";
  const step = {
    ...GUIDE_STEPS[currentStep],
    desc:
      currentStep === 1
        ? <>在系统设置里确定指令开头字符，比如 <CommandBadge>{cmdPrefix}ai</CommandBadge>。</>
        : GUIDE_STEPS[currentStep].desc,
  };
  const percent = ((currentStep + 1) / GUIDE_STEPS.length) * 100;

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
        新手指引：当前第 2 步，点击展开详情
      </Button>
    );
  }

  return (
    <div className="max-w-md rounded-2xl border bg-card/95 p-4 shadow-lg shadow-primary/10">
      <div className="mb-2 flex items-center justify-between text-xs text-muted-foreground">
        <span>新手指引</span>
        <button type="button" onClick={onToggle} className="hover:text-foreground">
          收起
        </button>
      </div>
      <div className="mb-2 text-sm font-semibold">{step.title}</div>
      <p className="text-xs leading-relaxed text-muted-foreground">{step.desc}</p>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary transition-all"
          style={{ width: `${percent}%` }}
        />
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button size="sm" onClick={onPrimary}>
          下一步：去插件中心 <ArrowRight className="ml-1 h-4 w-4" />
        </Button>
        <Button size="sm" variant="outline" onClick={onSkip}>
          跳过这步
        </Button>
      </div>
    </div>
  );
}
