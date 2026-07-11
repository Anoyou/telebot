import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  Bot,
  ChevronRight,
  CheckCircle2,
  Clock3,
  Loader2,
  MessageSquare,
  Minus,
  RotateCcw,
  Save,
  UserRound,
  X,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { getAccount, listAccountFeatures, toggleAccountFeature } from "@/api/accounts";
import { listLLMProviders } from "@/api/commands";
import {
  getPluginConfigActionJob,
  getFeatureMatrix,
  getPluginGlobalConfig,
  listPluginConfigActionJobs,
  setPluginGlobalConfig,
  startPluginConfigActionJob,
  updateAccountFeatureConfig,
  updateAccountFeatureDirectPassthrough,
  type PluginConfigActionJobStatus,
} from "@/api/features";
import { getSystemSettings } from "@/api/system";
import {
  buildScopedConfigValues,
  ConfigPreviewSection,
  ConfigScopeSection,
  schemaHasLLMSelect,
  type ConfigAction,
  type ConfigField,
  type ConfigSchema,
  withoutReadOnlyValues,
} from "@/components/plugin/ConfigDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { Switch } from "@/components/ui/switch";
import { getErrMsg } from "@/lib/api";
import { pluginUsageGuideWarning } from "@/lib/plugin-config-contract";
import {
  pluginContractRiskWarnings,
  pluginEventSubscriptionLabels,
  pluginOperationalCapabilityLabels,
  pluginSupportsDirectPassthrough,
} from "@/types/pluginContract";
import { featureConfigBackTarget, formatFeatureVersion } from "@/pages/Plugins/_shared/featureConfig";
import { featureRuntimeText, featureSwitchText } from "./_shared/featureStatus";

function isConfigSchema(schema: unknown): schema is ConfigSchema {
  const candidate = schema as Record<string, unknown> | null | undefined;
  return Boolean(
    candidate &&
      candidate.type === "object" &&
      candidate.properties &&
      typeof candidate.properties === "object" &&
      !Array.isArray(candidate.properties),
  );
}

function sameConfig(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function directPassthroughConfig(config: Record<string, unknown>): Record<string, unknown> {
  const raw = config.direct_passthrough;
  return raw && typeof raw === "object" && !Array.isArray(raw)
    ? raw as Record<string, unknown>
    : {};
}

const EMPTY_CONFIG: Record<string, unknown> = {};
const CONFIG_ACTION_TERMINAL_STATUSES = new Set(["succeeded", "failed"]);

function normalizeConfigActions(rawActions: unknown[]): ConfigAction[] {
  const seen = new Set<string>();
  const actions: ConfigAction[] = [];
  for (const raw of rawActions) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const action = raw as ConfigAction;
    const key = String(action.key || "").trim();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    actions.push({ ...action, key });
  }
  return actions;
}

function mergeConfigPatchIntoForm(
  patch: Record<string, unknown>,
  properties: Record<string, ConfigField>,
  setGlobalVals: Dispatch<SetStateAction<Record<string, unknown>>>,
  setAccountVals: Dispatch<SetStateAction<Record<string, unknown>>>,
) {
  const globalPatch: Record<string, unknown> = {};
  const accountPatch: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(patch)) {
    if (properties[key]?.level === "global") {
      globalPatch[key] = value;
    } else {
      accountPatch[key] = value;
    }
  }
  if (Object.keys(globalPatch).length > 0) {
    setGlobalVals((prev) => ({ ...prev, ...globalPatch }));
  }
  if (Object.keys(accountPatch).length > 0) {
    setAccountVals((prev) => ({ ...prev, ...accountPatch }));
  }
}

export function GenericPluginConfigPage() {
  const params = useParams();
  const aid = Number(params.aid);
  const featureKey = params.featureKey ?? "";
  const nav = useNavigate();
  const location = useLocation();
  const qc = useQueryClient();

  const accountQ = useQuery({
    queryKey: ["account", aid],
    queryFn: () => getAccount(aid),
    enabled: !!aid,
  });
  const matrixQ = useQuery({
    queryKey: ["matrix"],
    queryFn: getFeatureMatrix,
  });
  const featuresQ = useQuery({
    queryKey: ["account", aid, "features"],
    queryFn: () => listAccountFeatures(aid),
    enabled: !!aid,
  });
  const globalConfigQ = useQuery({
    queryKey: ["plugin", "global", featureKey],
    queryFn: () => getPluginGlobalConfig(featureKey),
    enabled: !!featureKey,
  });
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });

  const feature = matrixQ.data?.features.find((item) => item.key === featureKey);
  const accountFeature = featuresQ.data?.find((item) => item.feature_key === featureKey);
  const schema = isConfigSchema(feature?.config_schema) ? feature.config_schema : null;
  const globalConfig = globalConfigQ.data ?? EMPTY_CONFIG;
  const accountConfig = accountFeature?.config ?? EMPTY_CONFIG;
  const supportsDirectPassthrough = pluginSupportsDirectPassthrough(feature?.capabilities);
  const directPassthroughEnabled = directPassthroughConfig(accountConfig).enabled === true;
  const commandPrefix = settingsQ.data?.command_prefix || ",";
  const llmProvidersQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
    enabled: Boolean(schema && schemaHasLLMSelect(schema)),
  });
  const configActions = useMemo(
    () => normalizeConfigActions([
      ...(Array.isArray(feature?.config_actions) ? feature.config_actions : []),
      ...(Array.isArray(schema?.["x-config-actions"]) ? schema["x-config-actions"] : []),
    ]),
    [feature?.config_actions, schema],
  );
  const actionTitleForKey = (actionKey: string) =>
    configActions.find((action) => action.key === actionKey)?.title || actionKey;

  const [globalVals, setGlobalVals] = useState<Record<string, unknown>>({});
  const [accountVals, setAccountVals] = useState<Record<string, unknown>>({});
  const [dirty, setDirty] = useState(false);
  const [activeActionJob, setActiveActionJob] = useState<{
    jobId: string;
    actionTitle: string;
    minimized: boolean;
    hidden: boolean;
  } | null>(null);
  const [finalizedActionJobs, setFinalizedActionJobs] = useState<Record<string, true>>({});

  useEffect(() => {
    if (!schema) return;
    const next = buildScopedConfigValues(schema, globalConfig, accountConfig);
    setGlobalVals(next.globalVals);
    setAccountVals(next.accountVals);
    setDirty(false);
  }, [schema, globalConfig, accountConfig]);

  const { globalFields, accountFields, previewFields } = useMemo(() => {
    const properties = schema?.properties ?? {};
    const isGuideField = (key: string) =>
      key === "usage_preview" ||
      key === "usage_guide" ||
      key === "usage_instructions" ||
      key === "ai_usage_guide" ||
      key === "template_placeholders" ||
      key === "template_preview" ||
      /_preview$/i.test(key);
    const isUsageOnlyField = (key: string) =>
      key === "usage_preview" ||
      key === "usage_guide" ||
      key === "usage_instructions" ||
      key === "ai_usage_guide" ||
      key === "template_placeholders";
    const entries = Object.entries(properties) as Array<[string, ConfigField]>;
    return {
      globalFields: entries.filter(
        ([key, field]) => !isGuideField(key) && field.level === "global",
      ),
      accountFields: entries.filter(
        ([key, field]) => !isGuideField(key) && field.level !== "global",
      ),
      previewFields: entries.filter(([key, field]) => !isUsageOnlyField(key) && !field["x-ui-hidden"]),
    };
  }, [schema]);
  const usageGuide = useMemo(
    () => buildUsageGuide({
      schema,
      usage: feature?.usage,
      values: { ...globalVals, ...accountVals },
      commandPrefix,
      interactionEntries: feature?.interaction_entries,
    }),
    [schema, feature?.usage, globalVals, accountVals, commandPrefix, feature?.interaction_entries],
  );
  const eventLabels = pluginEventSubscriptionLabels(feature?.event_subscriptions);
  const capabilityLabels = pluginOperationalCapabilityLabels({
    capabilities: feature?.capabilities,
    permissions: feature?.permissions,
    config_schema: feature?.config_schema,
    usage: feature?.usage,
  });
  const contractWarnings = pluginContractRiskWarnings({
    capabilities: feature?.capabilities,
    event_subscriptions: feature?.event_subscriptions,
    lint_warnings: feature?.lint_warnings,
  });

  const saveMut = useMutation({
    mutationFn: async () => {
      if (!schema) return;
      const properties = schema.properties;
      const editableGlobalVals = withoutReadOnlyValues(globalVals, properties, globalConfig);
      const editableAccountVals = withoutReadOnlyValues(accountVals, properties, accountConfig);

      if (globalFields.length > 0) {
        const globalOnlyVals: Record<string, unknown> = {};
        for (const [key] of globalFields) {
          if (key in editableGlobalVals) globalOnlyVals[key] = editableGlobalVals[key];
        }
        if (!sameConfig(globalOnlyVals, globalConfig)) {
          await setPluginGlobalConfig(featureKey, globalOnlyVals);
        }
      }

      if (accountFields.length > 0) {
        const accountOnlyVals: Record<string, unknown> = {};
        for (const [key] of accountFields) {
          if (key in editableAccountVals) accountOnlyVals[key] = editableAccountVals[key];
        }
        if (supportsDirectPassthrough) {
          accountOnlyVals.direct_passthrough = directPassthroughConfig(accountConfig);
        }
        if (!sameConfig(accountOnlyVals, accountConfig)) {
          await updateAccountFeatureConfig(aid, featureKey, accountOnlyVals);
        }
      }
    },
    onSuccess: () => {
      toast.success("配置已保存（worker 热加载）");
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["plugin", "global", featureKey] });
      qc.invalidateQueries({ queryKey: ["matrix"] });
      qc.invalidateQueries({ queryKey: ["message-templates", "catalog"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const toggleMut = useMutation({
    mutationFn: (enabled: boolean) => toggleAccountFeature(aid, featureKey, enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const directPassthroughMut = useMutation({
    mutationFn: (enabled: boolean) =>
      updateAccountFeatureDirectPassthrough(aid, featureKey, enabled),
    onSuccess: (_data, enabled) => {
      toast.success(enabled ? "裸直通已为当前账号开启" : "裸直通已为当前账号关闭");
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const actionJobQ = useQuery({
    queryKey: ["plugin-config-action-job", activeActionJob?.jobId],
    queryFn: () => getPluginConfigActionJob(activeActionJob?.jobId ?? ""),
    enabled: Boolean(activeActionJob?.jobId && !activeActionJob.hidden),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && CONFIG_ACTION_TERMINAL_STATUSES.has(status) ? false : 2000;
    },
  });
  const recentActionJobsQ = useQuery({
    queryKey: ["plugin-config-action-jobs", aid, featureKey],
    queryFn: () => listPluginConfigActionJobs(aid, featureKey, 10),
    enabled: Boolean(aid && featureKey),
    refetchInterval: (query) => {
      const jobs = query.state.data ?? [];
      return jobs.some((job) => !CONFIG_ACTION_TERMINAL_STATUSES.has(job.status)) ? 2000 : false;
    },
  });
  const latestActionJob = recentActionJobsQ.data?.[0];

  useEffect(() => {
    if (activeActionJob || !latestActionJob) return;
    if (CONFIG_ACTION_TERMINAL_STATUSES.has(latestActionJob.status)) return;
    setActiveActionJob({
      jobId: latestActionJob.job_id,
      actionTitle: actionTitleForKey(latestActionJob.action_key),
      minimized: false,
      hidden: false,
    });
  }, [activeActionJob, latestActionJob, configActions]);

  useEffect(() => {
    const job = actionJobQ.data;
    if (!job || finalizedActionJobs[job.job_id]) return;
    if (job.status === "succeeded") {
      const patch = job.config_patch ?? {};
      if (schema && Object.keys(patch).length > 0) {
        mergeConfigPatchIntoForm(
          patch,
          schema.properties,
          setGlobalVals,
          setAccountVals,
        );
        setDirty(false);
      }
      setFinalizedActionJobs((prev) => ({ ...prev, [job.job_id]: true }));
      toast.success("配置动作已完成，配置已自动保存");
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["plugin", "global", featureKey] });
      qc.invalidateQueries({ queryKey: ["plugin-config-action-jobs", aid, featureKey] });
    } else if (job.status === "failed") {
      setFinalizedActionJobs((prev) => ({ ...prev, [job.job_id]: true }));
      toast.error(job.error_message || job.message || "配置动作失败");
      qc.invalidateQueries({ queryKey: ["plugin-config-action-jobs", aid, featureKey] });
    }
  }, [actionJobQ.data, finalizedActionJobs, schema, qc, aid, featureKey]);

  if (!aid) return <p>账号 ID 不合法</p>;
  if (!featureKey) return <p>功能 key 不合法</p>;
  if (matrixQ.isLoading || featuresQ.isLoading || accountQ.isLoading || globalConfigQ.isLoading) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Spinner className="text-primary" />
      </div>
    );
  }

  const accountLabel =
    accountQ.data?.display_name ||
    (accountQ.data?.tg_username ? `@${accountQ.data.tg_username}` : `#${aid}`);
  const hasSchemaFields = Boolean(schema && Object.keys(schema.properties).length > 0);
  const backTarget = featureConfigBackTarget(aid, location.search);

  function resetForm() {
    if (!schema) return;
    const next = buildScopedConfigValues(schema, globalConfig, accountConfig);
    setGlobalVals(next.globalVals);
    setAccountVals(next.accountVals);
    setDirty(false);
  }

  async function handleConfigAction(action: ConfigAction, input: Record<string, unknown>) {
    if (!schema) return;
    const response = await startPluginConfigActionJob(aid, featureKey, action.key, {
      input,
      config: { ...globalVals, ...accountVals },
    });
    setActiveActionJob({
      jobId: response.job_id,
      actionTitle: action.title || action.key,
      minimized: false,
      hidden: false,
    });
    qc.invalidateQueries({ queryKey: ["plugin-config-action-jobs", aid, featureKey] });
    toast.success("配置动作已在后台开始执行");
  }

  return (
    <div className="space-y-6 pb-24">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="default" size="sm" className="gap-1.5 shadow-sm" onClick={() => nav(backTarget.backHref)}>
          <ArrowLeft className="h-4 w-4" /> {backTarget.backLabel}
        </Button>
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {feature?.display_name ?? featureKey}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <code>{featureKey}</code>
            <span>账号：{accountLabel}</span>
            {feature ? (
              <Badge variant={feature.is_builtin ? "secondary" : "outline"}>
                {feature.is_builtin ? "平台能力" : "第三方"}
              </Badge>
            ) : null}
            {feature ? (
              <span className="rounded-full border border-primary/20 bg-primary/10 px-2 py-0.5 font-medium text-primary">
                当前版本 {formatFeatureVersion(feature.version)}
              </span>
            ) : null}
          </div>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">使用说明</CardTitle>
          <CardDescription>{usageGuide.description}</CardDescription>
        </CardHeader>
        <CardContent>
          {usageGuide.missing ? (
            <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                <div className="font-medium">高级规范警告</div>
                <div className="mt-1 text-xs leading-5">
                  {usageGuide.warning}
                </div>
              </div>
            </div>
          ) : (
            <div className="space-y-3 rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
              <div className="whitespace-pre-wrap leading-relaxed text-foreground">
                {usageGuide.customText}
              </div>
              {usageGuide.commandExamples.length > 0 ? (
                <div>
                  <div className="mb-1 font-medium text-foreground">插件声明的指令参考</div>
                  <div className="space-y-1">
                    {usageGuide.commandExamples.map((item) => (
                      <div key={item} className="rounded border bg-background px-2 py-1 font-mono text-[11px] text-foreground">
                        {item}
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
              {usageGuide.notes.length > 0 ? (
                <ul className="list-inside list-disc space-y-1">
                  {usageGuide.notes.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">触发与权限</CardTitle>
          <CardDescription>来自 manifest 的触发入口、可用能力和风险提示。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="grid gap-3 md:grid-cols-3">
            <ContractSummaryBlock title="触发入口" items={eventLabels} empty="插件未声明触发入口" />
            <ContractSummaryBlock title="可用能力" items={capabilityLabels} empty="未声明可用能力" />
            <ContractSummaryBlock
              title="风险提示"
              items={contractWarnings}
              empty="未声明额外高风险能力"
              variant="destructive"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-base">功能总开关</CardTitle>
              <CardDescription>关闭后插件不会在当前账号运行；配置保存后会由 worker 热加载。</CardDescription>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant={accountFeature?.enabled ? "default" : "outline"}>
                  {featureSwitchText(accountFeature)}
                </Badge>
                <span>运行状态：{featureRuntimeText(accountFeature)}</span>
                {accountFeature?.last_error ? (
                  <span className="text-destructive">最近错误：{accountFeature.last_error}</span>
                ) : null}
                {accountFeature?.last_error ? (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 border-destructive/40 bg-destructive/10 px-2 text-destructive hover:bg-destructive/15 hover:text-destructive"
                    onClick={() => nav(`/logs?tab=plugins&account_id=${aid}&plugin_key=${encodeURIComponent(featureKey)}&status=failed`)}
                  >
                    查看日志
                  </Button>
                ) : null}
              </div>
            </div>
            <Switch
              checked={Boolean(accountFeature?.enabled)}
              disabled={toggleMut.isPending || !accountFeature}
              onCheckedChange={(enabled) => toggleMut.mutate(enabled)}
            />
          </div>
        </CardHeader>
        {supportsDirectPassthrough ? (
          <CardContent>
            <div className="flex flex-col gap-3 rounded-md border border-info/25 bg-info/5 px-3 py-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <div className="text-sm font-medium">账号级裸直通</div>
                  <Badge variant="outline">二次开关</Badge>
                </div>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  开启后，该插件可在标准 Event Bus 之前接收当前账号的原始消息。关闭只停用低延时直通，不影响插件的标准 Event Bus、指令或交互入口。
                </p>
                {!accountFeature?.enabled ? (
                  <p className="mt-1 text-xs text-warning">请先开启上方功能总开关。</p>
                ) : null}
              </div>
              <Switch
                checked={directPassthroughMut.isPending
                  ? Boolean(directPassthroughMut.variables)
                  : directPassthroughEnabled}
                disabled={!accountFeature?.enabled || directPassthroughMut.isPending}
                onCheckedChange={(enabled) => directPassthroughMut.mutate(enabled)}
                aria-label="账号级裸直通"
              />
            </div>
          </CardContent>
        ) : null}
      </Card>

      {latestActionJob ? (
        <RecentConfigActionJobCard
          job={latestActionJob}
          title={actionTitleForKey(latestActionJob.action_key)}
          onRestore={() =>
            setActiveActionJob({
              jobId: latestActionJob.job_id,
              actionTitle: actionTitleForKey(latestActionJob.action_key),
              minimized: false,
              hidden: false,
            })
          }
          onOpenLogs={() => nav(`/logs?source=plugin&account_id=${aid}&plugin_key=${encodeURIComponent(featureKey)}`)}
        />
      ) : null}

      {!hasSchemaFields ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">插件配置</CardTitle>
            <CardDescription>该功能没有可配置的 Schema 字段。</CardDescription>
          </CardHeader>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">插件配置</CardTitle>
            <CardDescription>字段由插件声明的 config_schema 渲染；保存后由 worker 热加载。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 pb-0">
            {globalFields.length > 0 ? (
              <ConfigScopeSection
                title="全局配置"
                description="所有账号共享，适合 Token、Provider、公共模板等跨账号配置。"
                fields={globalFields}
                values={globalVals}
                accountId={aid}
                commandPrefix={commandPrefix}
                llmProviders={llmProvidersQ.data}
                llmProvidersLoading={llmProvidersQ.isLoading || llmProvidersQ.isFetching}
                showPreviews={false}
                configActions={configActions}
                onConfigAction={handleConfigAction}
                onChange={(key, value) => {
                  setGlobalVals((prev) => ({ ...prev, [key]: value }));
                  setDirty(true);
                }}
              />
            ) : null}
            {accountFields.length > 0 ? (
              <ConfigScopeSection
                title="账号配置"
                description={`${accountLabel} 专属`}
                fields={accountFields}
                values={accountVals}
                accountId={aid}
                commandPrefix={commandPrefix}
                llmProviders={llmProvidersQ.data}
                llmProvidersLoading={llmProvidersQ.isLoading || llmProvidersQ.isFetching}
                showPreviews={false}
                configActions={configActions}
                onConfigAction={handleConfigAction}
                onChange={(key, value) => {
                  setAccountVals((prev) => ({ ...prev, [key]: value }));
                  setDirty(true);
                }}
              />
            ) : null}
          </CardContent>
          <div className="static z-20 mt-4 rounded-b-lg border-t bg-background/95 px-4 py-3 shadow-[0_-8px_20px_rgba(15,23,42,0.06)] backdrop-blur supports-[backdrop-filter]:bg-background/85 sm:sticky sm:bottom-0 sm:px-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="text-sm">
                <div className="font-medium">配置操作</div>
                <div className="text-xs text-muted-foreground">
                  {dirty ? "有未保存修改，保存后 worker 会热加载。" : "当前配置已同步。"}
                </div>
              </div>
              <div className="flex w-full flex-row items-center gap-2 sm:w-auto">
                <Button
                  className="min-w-0 flex-1 sm:flex-none sm:px-6"
                  onClick={() => saveMut.mutate()}
                  disabled={saveMut.isPending || !dirty}
                >
                  {saveMut.isPending ? (
                    <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4 shrink-0" />
                  )}
                  保存配置
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  disabled={!dirty || saveMut.isPending}
                  onClick={resetForm}
                  className="min-w-0 flex-1 border-foreground/25 bg-background shadow-sm hover:border-foreground/40 sm:flex-none sm:px-6"
                >
                  <RotateCcw className="h-4 w-4 shrink-0" />
                  撤销
                </Button>
              </div>
            </div>
          </div>
        </Card>
      )}

      {hasSchemaFields ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">插件预览</CardTitle>
            <CardDescription>
              插件可选声明的模板预览，使用模拟上下文渲染，不触发真实发送。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ConfigPreviewSection
              fields={previewFields}
              values={{ ...globalVals, ...accountVals }}
              commandPrefix={commandPrefix}
            />
            {!hasPreviewFields(previewFields) ? (
              <div className="rounded-md border border-dashed bg-muted/20 px-3 py-4 text-sm text-muted-foreground">
                当前插件没有声明预览字段。建议在 schema 中提供 <code>template_preview</code> 或 <code>*_preview</code>，便于用户确认最终消息效果。
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {activeActionJob && !activeActionJob.hidden ? (
        <ConfigActionJobWindow
          title={activeActionJob.actionTitle}
          job={actionJobQ.data}
          loading={actionJobQ.isLoading || actionJobQ.isFetching}
          minimized={activeActionJob.minimized}
          onOpenLogs={() => nav(`/logs?source=plugin&account_id=${aid}&plugin_key=${encodeURIComponent(featureKey)}`)}
          onMinimize={() => setActiveActionJob((prev) => prev ? { ...prev, minimized: true } : prev)}
          onRestore={() => setActiveActionJob((prev) => prev ? { ...prev, minimized: false } : prev)}
          onClose={() => setActiveActionJob((prev) => prev ? { ...prev, hidden: true } : prev)}
        />
      ) : null}
    </div>
  );
}

function RecentConfigActionJobCard({
  job,
  title,
  onRestore,
  onOpenLogs,
}: {
  job: PluginConfigActionJobStatus;
  title: string;
  onRestore: () => void;
  onOpenLogs: () => void;
}) {
  const terminal = CONFIG_ACTION_TERMINAL_STATUSES.has(job.status);
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex min-w-0 items-center gap-2 text-base">
              {terminal ? jobStatusIcon(job.status) : <Loader2 className="h-4 w-4 shrink-0 animate-spin text-primary" />}
              <span className="truncate">最近配置动作：{title}</span>
            </CardTitle>
            <CardDescription className="mt-1">
              {configActionJobSummaryMessage(job)}
            </CardDescription>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <Badge variant={jobStatusBadgeVariant(job.status)}>{configActionJobStatusText(job.status)}</Badge>
            <Button type="button" variant="outline" size="sm" className="border-primary/35 bg-primary/5 text-primary hover:bg-primary/10" onClick={onRestore}>
              查看过程
            </Button>
            <Button type="button" variant="outline" size="sm" className="border-primary/35 bg-primary/5 text-primary hover:bg-primary/10" onClick={onOpenLogs}>
              查看日志
            </Button>
          </div>
        </div>
      </CardHeader>
    </Card>
  );
}

function ContractSummaryBlock({
  title,
  items,
  empty,
  variant = "secondary",
}: {
  title: string;
  items: string[];
  empty: string;
  variant?: "secondary" | "destructive";
}) {
  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="mb-2 text-xs font-medium text-muted-foreground">{title}</div>
      {items.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {items.slice(0, 8).map((item) => (
            <Badge key={item} variant={variant} className="max-w-full break-all">
              {item}
            </Badge>
          ))}
          {items.length > 8 ? <Badge variant="outline">+{items.length - 8}</Badge> : null}
        </div>
      ) : (
        <div className="text-xs text-muted-foreground">{empty}</div>
      )}
    </div>
  );
}

function ConfigActionJobWindow({
  title,
  job,
  loading,
  minimized,
  onOpenLogs,
  onMinimize,
  onRestore,
  onClose,
}: {
  title: string;
  job?: PluginConfigActionJobStatus;
  loading: boolean;
  minimized: boolean;
  onOpenLogs: () => void;
  onMinimize: () => void;
  onRestore: () => void;
  onClose: () => void;
}) {
  const status = job?.status ?? "queued";
  const statusText = configActionJobStatusText(status);
  const terminal = CONFIG_ACTION_TERMINAL_STATUSES.has(status);
  const logs = job?.logs ?? [];
  const resultView = buildConfigActionResultView(job);
  if (typeof document === "undefined") return null;
  if (minimized) {
    return createPortal(
      <div className="fixed bottom-4 right-4 z-50 w-[min(92vw,360px)] rounded-md border bg-background shadow-lg">
        <button
          type="button"
          className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
          onClick={onRestore}
        >
          <span className="flex min-w-0 items-center gap-2">
            {terminal ? jobStatusIcon(status) : <Loader2 className="h-4 w-4 shrink-0 animate-spin text-primary" />}
            <span className="min-w-0 truncate text-sm font-medium">{title}</span>
          </span>
          <span className="shrink-0 text-xs text-muted-foreground">{statusText}</span>
        </button>
      </div>,
      document.body,
    );
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4 py-6">
      <div className="flex h-[min(84vh,720px)] w-[min(94vw,760px)] flex-col overflow-hidden rounded-md border bg-background shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <Bot className="h-4 w-4 shrink-0 text-primary" />
              <div className="min-w-0 truncate text-sm font-semibold">{title}</div>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge variant={jobStatusBadgeVariant(status)}>{statusText}</Badge>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Button type="button" variant="ghost" size="icon" className="h-8 w-8" onClick={onMinimize} aria-label="最小化">
              <Minus className="h-4 w-4" />
            </Button>
            <Button type="button" variant="ghost" size="icon" className="h-8 w-8" onClick={onClose} aria-label="关闭">
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-muted/20 px-4 py-4">
          <ConfigActionResultPanel view={resultView} terminal={terminal} />
          <ConfigActionExecutionDetails job={job} logs={logs} />
          {loading && !terminal ? (
            <div className="flex items-center gap-2 px-1 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              正在刷新进度
            </div>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2 border-t bg-background px-4 py-3">
          <div className="text-xs text-muted-foreground">
            关闭窗口不会停止后台执行。
          </div>
          <Button type="button" variant="outline" size="sm" className="border-primary/35 bg-primary/5 text-primary hover:bg-primary/10" onClick={onOpenLogs}>
            查看日志
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

type ConfigActionResultTone = "running" | "success" | "warning" | "error";

interface ConfigActionResultView {
  kind: "model-test" | "generic";
  title: string;
  statusLabel: string;
  tone: ConfigActionResultTone;
  summary: string;
  testMessage: string;
  assistantMessage: string;
  interpretation: string;
  provider: string;
  model: string;
  latencyMs: number | null;
  clientIdentity: string;
  finishedAt?: string | null;
}

function ConfigActionResultPanel({
  view,
  terminal,
}: {
  view: ConfigActionResultView;
  terminal: boolean;
}) {
  const metaItems = [
    view.provider ? `Provider：${view.provider}` : "",
    view.model ? `Model：${view.model}` : "",
    typeof view.latencyMs === "number" ? `${view.latencyMs} ms` : "",
    view.clientIdentity ? `客户端：${view.clientIdentity}` : "",
    view.finishedAt ? formatActionJobTime(view.finishedAt) : "",
  ].filter(Boolean);

  return (
    <div className="rounded-md border bg-background p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2">
          <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border bg-muted/30">
            {resultToneIcon(view.tone)}
          </div>
          <div className="min-w-0">
            <div className="text-sm font-semibold">{view.title}</div>
            <div className="mt-1 flex flex-wrap gap-x-2 gap-y-1 text-xs text-muted-foreground">
              {metaItems.length > 0 ? (
                metaItems.map((item) => (
                  <span key={item} className="max-w-full break-all">
                    {item}
                  </span>
                ))
              ) : (
                <span>{view.summary}</span>
              )}
            </div>
          </div>
        </div>
        <Badge variant={resultToneBadgeVariant(view.tone)}>{view.statusLabel}</Badge>
      </div>

      {view.kind === "model-test" ? (
        <div className="mt-4 space-y-3">
          <div className="flex justify-end">
            <div className="max-w-[82%] rounded-md bg-primary px-3 py-2 text-sm leading-6 text-primary-foreground">
              <div className="mb-1 inline-flex items-center gap-1 text-[11px] opacity-80">
                <UserRound className="h-3 w-3" />
                模拟用户消息
              </div>
              <div className="whitespace-pre-wrap break-words">{view.testMessage || "测试当前模型"}</div>
            </div>
          </div>
          <div className="flex justify-start">
            <div
              className={
                "max-w-[86%] rounded-md border px-3 py-2 text-sm leading-6 " +
                (view.tone === "error"
                  ? "border-destructive/30 bg-destructive/10 text-destructive"
                  : "bg-muted/40")
              }
            >
              <div className="mb-1 inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                <Bot className="h-3 w-3" />
                模型返回
              </div>
              {view.tone === "running" && !view.assistantMessage ? (
                <div className="flex items-center gap-2 text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                  正在等待模型返回
                </div>
              ) : (
                <div className="whitespace-pre-wrap break-words">
                  {view.assistantMessage || "没有拿到可展示文本。"}
                </div>
              )}
            </div>
          </div>
          {terminal || view.interpretation ? (
            <div className="rounded-md border bg-muted/20 px-3 py-2">
              <div className="mb-1 inline-flex items-center gap-1 text-xs font-medium text-muted-foreground">
                <MessageSquare className="h-3.5 w-3.5" />
                结果解读
              </div>
              <div className="whitespace-pre-wrap break-words text-sm leading-6">
                {view.interpretation || view.summary}
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <div className="mt-3 rounded-md border bg-muted/20 px-3 py-2 text-sm leading-6">
          {view.summary}
        </div>
      )}
    </div>
  );
}

function ConfigActionExecutionDetails({
  job,
  logs,
}: {
  job?: PluginConfigActionJobStatus;
  logs: PluginConfigActionJobStatus["logs"];
}) {
  return (
    <details className="rounded-md border bg-background [&[open]_.detail-chevron]:rotate-90">
      <summary className="cursor-pointer list-none px-3 py-2 text-sm font-medium">
        <span className="inline-flex flex-wrap items-center gap-2">
          <ChevronRight className="detail-chevron h-3.5 w-3.5 shrink-0 transition-transform" />
          执行细节
          <Badge variant="outline">{logs.length} 条日志</Badge>
          {job?.job_id ? <code className="text-[11px] text-muted-foreground">{job.job_id}</code> : null}
        </span>
      </summary>
      <div className="space-y-2 border-t bg-muted/20 p-3">
        {logs.length > 0 ? (
          logs.map((item) => (
            <ConfigActionChatLine
              key={item.id}
              level={item.level}
              message={item.message}
              ts={item.ts}
              detail={item.detail}
            />
          ))
        ) : (
          <div className="rounded-md border border-dashed bg-background px-3 py-4 text-center text-xs text-muted-foreground">
            暂无执行日志。
          </div>
        )}
      </div>
    </details>
  );
}

function ConfigActionChatLine({
  level,
  message,
  ts,
  detail,
  active = false,
}: {
  level: string;
  message: string;
  ts?: string | null;
  detail?: Record<string, unknown> | null;
  active?: boolean;
}) {
  const detailText = configActionLogDetailText(detail);
  return (
    <div className="flex gap-2">
      <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border bg-background">
        {active ? <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" /> : logLevelIcon(level)}
      </div>
      <div className="min-w-0 flex-1 rounded-md border bg-background px-3 py-2">
        <div className="break-words text-sm leading-5">{message || "状态更新"}</div>
        {detailText ? (
          <div className="mt-1 break-words font-mono text-[11px] leading-4 text-muted-foreground">
            {detailText}
          </div>
        ) : null}
        {ts ? <div className="mt-1 text-[11px] text-muted-foreground">{formatActionJobTime(ts)}</div> : null}
      </div>
    </div>
  );
}

function configActionJobStatusText(status: string): string {
  if (status === "queued") return "排队中";
  if (status === "running") return "执行中";
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "失败";
  return status || "未知";
}

function configActionJobSummaryMessage(job: PluginConfigActionJobStatus): string {
  const view = buildConfigActionResultView(job);
  if (view.kind === "model-test") {
    const parts = [
      view.statusLabel,
      view.model,
      typeof view.latencyMs === "number" ? `${view.latencyMs} ms` : "",
    ].filter(Boolean);
    return parts.join(" · ") || view.summary;
  }
  if (job.status === "succeeded") return "配置动作已完成，结果已写入配置。";
  if (job.status === "failed") return job.error_message || job.message || "配置动作失败";
  return job.message || configActionJobStatusText(job.status);
}

function jobStatusBadgeVariant(status: string): "default" | "secondary" | "destructive" | "outline" | "success" {
  if (status === "succeeded") return "success";
  if (status === "failed") return "destructive";
  if (status === "running") return "default";
  return "secondary";
}

function jobStatusIcon(status: string) {
  if (status === "succeeded") return <CheckCircle2 className="h-4 w-4 shrink-0 text-success" />;
  if (status === "failed") return <XCircle className="h-4 w-4 shrink-0 text-destructive" />;
  return <Clock3 className="h-4 w-4 shrink-0 text-muted-foreground" />;
}

function logLevelIcon(level: string) {
  const normalized = String(level || "").toLowerCase();
  if (normalized === "error") return <XCircle className="h-3.5 w-3.5 text-destructive" />;
  if (normalized === "warn" || normalized === "warning") return <AlertTriangle className="h-3.5 w-3.5 text-warning" />;
  return <CheckCircle2 className="h-3.5 w-3.5 text-success" />;
}

function resultToneIcon(tone: ConfigActionResultTone) {
  if (tone === "running") return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
  if (tone === "success") return <CheckCircle2 className="h-4 w-4 text-success" />;
  if (tone === "warning") return <AlertTriangle className="h-4 w-4 text-warning" />;
  return <XCircle className="h-4 w-4 text-destructive" />;
}

function resultToneBadgeVariant(tone: ConfigActionResultTone): "default" | "secondary" | "destructive" | "outline" | "success" {
  if (tone === "success") return "success";
  if (tone === "error") return "destructive";
  if (tone === "running") return "default";
  return "secondary";
}

function buildConfigActionResultView(job?: PluginConfigActionJobStatus): ConfigActionResultView {
  if (!job) {
    return {
      kind: "generic",
      title: "配置动作",
      statusLabel: "排队中",
      tone: "running",
      summary: "后台任务已创建，正在等待状态更新。",
      testMessage: "",
      assistantMessage: "",
      interpretation: "",
      provider: "",
      model: "",
      latencyMs: null,
      clientIdentity: "",
    };
  }

  const result = recordValue(job.result);
  const configPatch = recordValue(job.config_patch);
  const modelTestText = firstText(
    textValue(result?.model_test_result),
    textValue(configPatch?.model_test_result),
  );
  const hasModelTestShape = Boolean(
    job.action_key === "test_model_availability" ||
      modelTestText ||
      result?.response_preview ||
      result?.empty_response,
  );
  if (!hasModelTestShape) {
    const failed = job.status === "failed";
    return {
      kind: "generic",
      title: "配置动作结果",
      statusLabel: configActionJobStatusText(job.status),
      tone: !CONFIG_ACTION_TERMINAL_STATUSES.has(job.status) ? "running" : failed ? "error" : "success",
      summary: configActionJobSummaryMessageWithoutResultView(job),
      testMessage: "",
      assistantMessage: "",
      interpretation: "",
      provider: "",
      model: "",
      latencyMs: null,
      clientIdentity: "",
      finishedAt: job.ended_at || job.updated_at,
    };
  }

  const parsedStatus = labeledLineValue(modelTestText, ["状态"]);
  const parsedOk = parsedStatus.includes("可用") && !parsedStatus.includes("不可用");
  const parsedEmptyResponse = parsedStatus.includes("返回为空");
  const ok = boolValue(result?.ok) || parsedOk;
  const emptyResponse = boolValue(result?.empty_response) || parsedEmptyResponse;
  const terminal = CONFIG_ACTION_TERMINAL_STATUSES.has(job.status);
  const tone: ConfigActionResultTone = !terminal
    ? "running"
    : ok
      ? "success"
      : emptyResponse
        ? "warning"
        : "error";
  const statusLabel = !terminal
    ? "请求中"
    : ok
      ? "模型可用"
      : emptyResponse
        ? "返回为空"
        : "模型不可用";
  const provider = firstText(
    textValue(result?.provider),
    labeledLineValue(modelTestText, ["Provider"]),
  );
  const model = firstText(
    textValue(result?.model),
    labeledLineValue(modelTestText, ["Model"]),
  );
  const latencyMs = firstNumber(
    numberValue(result?.latency_ms),
    numberFromText(labeledLineValue(modelTestText, ["耗时"])),
  );
  const testMessage = firstText(
    textValue(result?.test_message),
    textValue(result?.test_prompt),
    labeledLineValue(modelTestText, ["测试语"]),
  );
  const clientIdentity = firstText(
    textValue(result?.client_identity),
    labeledLineValue(modelTestText, ["客户端标识"]),
  );
  const response = firstText(
    textValue(result?.response),
    textValue(result?.model_response),
    blockAfterLabel(modelTestText, ["模型实时返回", "模型返回"], ["结果解读", "错误"]),
    textValue(result?.response_preview),
  );
  const error = firstText(
    textValue(result?.error),
    labeledLineValue(modelTestText, ["错误"]),
    job.status === "failed" ? textValue(job.error_message) : "",
    job.status === "failed" ? textValue(job.message) : "",
  );
  const interpretation = firstText(
    blockAfterLabel(modelTestText, ["结果解读"], []),
    ok
      ? "模型返回了非空文本，说明 Provider 鉴权、模型路由、请求体和返回解析这条链路本次可用。"
      : emptyResponse
        ? "上游请求已完成，但没有拿到可展示文本；这不等同于 Provider 不可用。请换一句自然测试语，或检查上游是否只返回了被隐藏的思考内容。"
        : error
          ? "本次没有拿到可用模型文本。请优先检查 Provider 鉴权、额度/限流、模型名、base_url 与上游服务状态。"
          : "",
  );
  const summary = !terminal
    ? "已按真实聊天路径提交测试请求，正在等待模型返回。"
    : statusLabel;

  return {
    kind: "model-test",
    title: "测试对话",
    statusLabel,
    tone,
    summary,
    testMessage,
    assistantMessage: response || error,
    interpretation,
    provider,
    model,
    latencyMs,
    clientIdentity,
    finishedAt: job.ended_at || job.updated_at,
  };
}

function configActionJobSummaryMessageWithoutResultView(job: PluginConfigActionJobStatus): string {
  if (job.status === "succeeded") return "配置动作已完成，结果已写入配置。";
  if (job.status === "failed") return job.error_message || job.message || "配置动作失败";
  return job.message || configActionJobStatusText(job.status);
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function textValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function boolValue(value: unknown): boolean {
  return value === true;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function firstText(...values: string[]): string {
  for (const value of values) {
    const text = value.trim();
    if (text) return text;
  }
  return "";
}

function firstNumber(...values: Array<number | null>): number | null {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function numberFromText(value: string): number | null {
  const match = value.match(/\d+/);
  return match ? Number(match[0]) : null;
}

function labeledLineValue(text: string, labels: string[]): string {
  if (!text) return "";
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    for (const label of labels) {
      const fullWidth = `${label}：`;
      const halfWidth = `${label}:`;
      if (trimmed.startsWith(fullWidth)) return trimmed.slice(fullWidth.length).trim();
      if (trimmed.startsWith(halfWidth)) return trimmed.slice(halfWidth.length).trim();
    }
  }
  return "";
}

function blockAfterLabel(text: string, labels: string[], stopLabels: string[]): string {
  if (!text) return "";
  const lines = text.split(/\r?\n/);
  const out: string[] = [];
  let collecting = false;
  for (const line of lines) {
    const trimmed = line.trim();
    if (!collecting) {
      for (const label of labels) {
        const fullWidth = `${label}：`;
        const halfWidth = `${label}:`;
        if (trimmed.startsWith(fullWidth)) {
          const rest = trimmed.slice(fullWidth.length).trim();
          if (rest) out.push(rest);
          collecting = true;
          break;
        }
        if (trimmed.startsWith(halfWidth)) {
          const rest = trimmed.slice(halfWidth.length).trim();
          if (rest) out.push(rest);
          collecting = true;
          break;
        }
      }
      continue;
    }
    if (stopLabels.some((label) => trimmed.startsWith(`${label}：`) || trimmed.startsWith(`${label}:`))) {
      break;
    }
    out.push(line);
  }
  return out.join("\n").trim();
}

function configActionLogDetailText(detail?: Record<string, unknown> | null): string {
  if (!detail) return "";
  const hidden = new Set(["plugin_key", "action_key", "config_action_job_id", "component"]);
  const parts = Object.entries(detail)
    .filter(([key, value]) => !hidden.has(key) && value !== undefined && value !== null && value !== "")
    .map(([key, value]) => `${key}=${formatDetailValue(value)}`);
  return parts.slice(0, 6).join("  ");
}

function formatDetailValue(value: unknown): string {
  if (typeof value === "string") return value.length > 80 ? `${value.slice(0, 79)}…` : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function formatActionJobTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

interface UsageGuide {
  description: string;
  customText: string;
  commandExamples: string[];
  notes: string[];
  missing: boolean;
  warning: string;
}

function buildUsageGuide({
  schema,
  usage,
  values,
  commandPrefix,
  interactionEntries,
}: {
  schema: ConfigSchema | null;
  usage?: unknown;
  values: Record<string, unknown>;
  commandPrefix: string;
  interactionEntries?: Array<{ title?: string | null; description?: string | null; key?: string | null }>;
}): UsageGuide {
  const properties = schema?.properties ?? {};
  const command = configString(values.command ?? properties.command?.default) || "command";
  const usageVariables = buildUsageVariables(properties, values, commandPrefix || ",", command);
  const customText = renderUsageText(
    firstUsageGuideText([
      usage,
      schema?.["x-usage-guide"],
      schema?.["x-usage-instructions"],
      schema?.["x-usage-steps"],
      schema?.["x-help"],
      values.usage_preview ?? properties.usage_preview?.default,
      values.usage_guide ?? properties.usage_guide?.default,
      values.usage_instructions ?? properties.usage_instructions?.default,
      values.ai_usage_guide ?? properties.ai_usage_guide?.default,
      values.template_placeholders ?? properties.template_placeholders?.default,
    ]),
    usageVariables,
  );
  const aliasExamples = buildCommandExamples(properties, values, usageVariables.prefix, command);
  const missingWarning = pluginUsageGuideWarning({ config_schema: schema, usage });
  const interactionNotes = (interactionEntries ?? [])
    .map((entry) => {
      const title = entry.title || entry.key;
      const description = entry.description;
      if (!title && !description) return "";
      return `可交互：${[title, description].filter(Boolean).join("，")}`;
    })
    .filter(Boolean);

  const notes = [
    ...interactionNotes,
  ];

  return {
    description: missingWarning ? "该插件缺少自声明使用说明，需要插件开发者补齐。" : "来自插件 schema 的自声明使用说明。",
    customText,
    commandExamples: customText ? aliasExamples : [],
    notes,
    missing: Boolean(missingWarning),
    warning: missingWarning ?? "",
  };
}

function hasPreviewFields(fields: Array<[string, ConfigField]>): boolean {
  return fields.some(([key]) => key === "template_preview" || /_preview$/i.test(key));
}

function buildCommandExamples(
  properties: Record<string, ConfigField>,
  values: Record<string, unknown>,
  prefix: string,
  command: string,
): string[] {
  const knownArgs: Record<string, string> = {
    buy_aliases: "3 5",
    history_aliases: "5",
    sponsor_aliases: "10000",
    unsponsor_aliases: "10000",
    refund_aliases: "1",
  };
  const priority = [
    "help_aliases",
    "buy_aliases",
    "my_aliases",
    "pool_aliases",
    "hot_aliases",
    "stats_aliases",
    "history_aliases",
    "draw_aliases",
    "reset_aliases",
    "sponsor_aliases",
    "unsponsor_aliases",
    "refund_aliases",
  ];
  const aliasKeys = Object.keys(properties).filter((key) => /(^|_)aliases$/i.test(key));
  const orderedKeys = [
    ...priority.filter((key) => aliasKeys.includes(key)),
    ...aliasKeys.filter((key) => !priority.includes(key)).sort(),
  ];
  return orderedKeys
    .map((key) => {
      const alias = firstAlias(values[key] ?? properties[key]?.default);
      if (!alias) return "";
      const suffix = knownArgs[key] ? ` ${knownArgs[key]}` : "";
      return `${prefix}${command} ${alias}${suffix}`;
    })
    .filter(Boolean)
    .slice(0, 8);
}

function renderUsageText(value: string, variables: Record<string, string>): string {
  let text = normalizeUsageEscapes(value);
  for (const [key, replacement] of Object.entries(variables)) {
    text = text.replace(new RegExp(`\\{${escapeRegExp(key)}\\}`, "g"), replacement);
  }
  return text.trim();
}

function buildUsageVariables(
  properties: Record<string, ConfigField>,
  values: Record<string, unknown>,
  prefix: string,
  command: string,
): Record<string, string> {
  const variables: Record<string, string> = { prefix, command };
  for (const [key, field] of Object.entries(properties)) {
    if (isSensitiveUsageVariableKey(key)) continue;
    const text = usageVariableText(values[key] ?? field.default);
    if (text) variables[key] = text;
  }
  return variables;
}

function firstUsageGuideText(values: unknown[]): string {
  for (const value of values) {
    const text = formatUsageGuideValue(value).trim();
    if (text) return text;
  }
  return "";
}

function normalizeUsageEscapes(value: string): string {
  return value
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .replace(/\\r/g, "\n");
}

function formatUsageGuideValue(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value.map((item) => formatUsageGuideValue(item)).filter(Boolean).join("\n");
  }
  if (typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function usageVariableText(value: unknown): string {
  if (value == null || Array.isArray(value) || typeof value === "object") return "";
  return String(value);
}

function isSensitiveUsageVariableKey(key: string): boolean {
  return /(^|_)(api_key|access_token|auth_token|bot_token|token|tokens|secret|password|passwd|pwd)$/i.test(key);
}

function firstAlias(value: unknown): string {
  return configString(value).split(/\s+/).map((item) => item.trim()).find(Boolean) || "";
}

function configString(value: unknown): string {
  if (value == null) return "";
  if (Array.isArray(value)) return value.map((item) => String(item)).join(" ");
  return String(value);
}
