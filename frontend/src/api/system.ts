// 风控 / 系统 API 包装
import { api } from "@/lib/api";
import type {
  AccountRateLimitOut,
  AuditLogItem,
  BackendVersionInfo,
  CheckUpdateResult,
  UpdateJobStatus,
  UpdateTargetOptions,
  EventTraceDetail,
  EventTraceSummary,
  HealthOverview,
  HumanizeConfig,
  HumanizeUpdate,
  MessageFunelItem,
  PullUpdateResult,
  ResourceDashboard,
  RateLimitRuleConfig,
  RestartResult,
  StrictRequest,
  RuntimeLogItem,
  SystemConsoleLogsResponse,
  PlatformCapabilities,
  PlatformCapabilityPatchResult,
  PlatformModuleKey,
  SystemSettings,
  TemplateOut,
} from "@/api/types";

// ===================== 版本号（0.4.2 加） =====================
// 后端 GET /api/system/version 是 public 端点（无鉴权），用于前后端版本号对比。
// 不一致时 sidebar 顶部弹红条提示用户 make restart + 硬刷浏览器。
export async function getBackendVersion(): Promise<BackendVersionInfo> {
  const { data } = await api.get<BackendVersionInfo>("/api/system/version");
  return data;
}

// ===================== 风控 =====================
export async function getAccountRateLimit(
  aid: number,
): Promise<AccountRateLimitOut> {
  const { data } = await api.get<AccountRateLimitOut>(
    `/api/accounts/${aid}/rate-limit`,
  );
  return data;
}

export async function patchAccountRateLimit(
  aid: number,
  action: string,
  payload: Partial<RateLimitRuleConfig>,
): Promise<void> {
  await api.patch(`/api/accounts/${aid}/rate-limit/${action}`, payload);
}

export async function strictRateLimit(
  aid: number,
  payload: StrictRequest = {},
): Promise<void> {
  await api.post(`/api/accounts/${aid}/rate-limit/strict`, payload);
}

// ===================== 日志 =====================
export interface RuntimeLogQuery {
  account_id?: number | string;
  level?: string;
  /** event = 消息事件；plugin = 插件内部日志；system = worker 启停 / 错误 */
  source?: "system" | "event" | "plugin" | string;
  plugin_key?: string;
  keyword?: string;
  since?: string;
  limit?: number;
}
export async function listRuntimeLogs(
  q: RuntimeLogQuery = {},
): Promise<RuntimeLogItem[]> {
  const { data } = await api.get<RuntimeLogItem[]>("/api/logs/runtime", {
    params: q,
  });
  return data;
}

export interface SystemConsoleLogQuery {
  service?: "all" | "web" | "frontend" | "postgres" | "redis" | "updater" | string;
  keyword?: string;
  tail?: number;
}

export async function listSystemConsoleLogs(
  q: SystemConsoleLogQuery = {},
): Promise<SystemConsoleLogsResponse> {
  const { data } = await api.post<SystemConsoleLogsResponse>("/api/logs/system-console", q);
  return data;
}

// 操作日志（Dashboard 摘要 + 后续审计页用）
export interface AuditLogQuery {
  user_id?: number;
  action?: string;
  target?: string;
  keyword?: string;
  detail?: string;
  since?: string;
  limit?: number;
}
export async function listAuditLogs(
  q: AuditLogQuery = {},
): Promise<AuditLogItem[]> {
  const { data } = await api.get<AuditLogItem[]>("/api/logs/audit", {
    params: q,
  });
  return data;
}

export interface TraceQuery {
  account_id?: number | string;
  source_channel?: string;
  event_type?: string;
  chat_id?: number | string;
  message_id?: number | string;
  update_id?: number | string;
  sender_user_id?: number | string;
  plugin_key?: string;
  status?: string;
  trace_id?: string;
  reason_code?: string;
  keyword?: string;
  since?: string;
  until?: string;
  limit?: number;
}

export interface MessageFunelQuery extends TraceQuery {
  verdict?: "responded" | "no_response_normal" | "stuck" | "failed" | "";
}

export async function listEventTraces(
  q: TraceQuery = {},
): Promise<EventTraceSummary[]> {
  const { data } = await api.get<EventTraceSummary[]>("/api/logs/trace/events", {
    params: q,
  });
  return data;
}

export async function getMessageFunel(
  q: MessageFunelQuery = {},
): Promise<MessageFunelItem[]> {
  const { data } = await api.get<MessageFunelItem[]>("/api/logs/messages", {
    params: q,
  });
  return data;
}

export async function getEventTrace(traceId: string): Promise<EventTraceDetail> {
  const { data } = await api.get<EventTraceDetail>(
    `/api/logs/trace/events/${encodeURIComponent(traceId)}`,
  );
  return data;
}

// ===================== 系统设置 =====================
export async function getSystemSettings(): Promise<SystemSettings> {
  const { data } = await api.get<SystemSettings>("/api/system/settings");
  return data;
}
export type SystemSettingsPatch = Partial<Omit<SystemSettings, "login_security" | "ui_preferences">> & {
  login_security?: Partial<NonNullable<SystemSettings["login_security"]>>;
  ui_preferences?: Partial<NonNullable<SystemSettings["ui_preferences"]>>;
};

export async function patchSystemSettings(
  payload: SystemSettingsPatch,
): Promise<SystemSettings> {
  const { data } = await api.patch<SystemSettings>(
    "/api/system/settings",
    payload,
  );
  return data;
}

// ===================== 平台能力热插拔 =====================
export async function getPlatformCapabilities(): Promise<PlatformCapabilities> {
  const { data } = await api.get<PlatformCapabilities>("/api/system/capabilities");
  return data;
}

export async function patchPlatformCapability(
  moduleKey: PlatformModuleKey | string,
  enabled: boolean,
): Promise<PlatformCapabilityPatchResult> {
  const { data } = await api.patch<PlatformCapabilityPatchResult>(
    `/api/system/capabilities/${moduleKey}`,
    { enabled },
  );
  return data;
}

// ===================== 风控模板 =====================
export async function listRateTemplates(): Promise<TemplateOut[]> {
  const { data } = await api.get<TemplateOut[]>("/api/rate-templates");
  return data;
}

export async function createRateTemplate(payload: {
  name: string;
  is_default?: boolean;
}): Promise<TemplateOut> {
  const { data } = await api.post<TemplateOut>("/api/rate-templates", payload);
  return data;
}

export async function deleteRateTemplate(id: number): Promise<void> {
  await api.delete(`/api/rate-templates/${id}`);
}

// ===================== 拟人化 humanize =====================
// 后端是 PUT 但语义是 PATCH（仅传非空字段，未传字段保持不变）
export async function getHumanize(aid: number): Promise<HumanizeConfig> {
  const { data } = await api.get<HumanizeConfig>(
    `/api/accounts/${aid}/humanize`,
  );
  return data;
}

export async function patchHumanize(
  aid: number,
  body: HumanizeUpdate,
): Promise<HumanizeConfig> {
  const { data } = await api.put<HumanizeConfig>(
    `/api/accounts/${aid}/humanize`,
    body,
  );
  return data;
}

// ===================== 系统健康概览（Dashboard 用）=====================
export async function getHealthOverview(): Promise<HealthOverview> {
  const { data } = await api.get<HealthOverview>("/api/system/health-overview");
  return data;
}

export async function getResourceDashboard(): Promise<ResourceDashboard> {
  const { data } = await api.get<ResourceDashboard>("/api/system/resource-dashboard");
  return data;
}

// ===================== 检查更新 / 拉取 / 重启 =====================
// 全局 axios timeout 是 15s；检查/拉取会触发后端同步 git 操作，需 per-request 放宽。
// 这里只覆盖这几个慢调用，不动 lib/api.ts 的全局默认。update-jobs 轮询保持默认 15s。
export interface AppUpdateTarget {
  remote?: string;
  branch?: string;
}

export async function checkUpdate(target?: AppUpdateTarget): Promise<CheckUpdateResult> {
  const { data } = await api.post<CheckUpdateResult>(
    "/api/system/check-update",
    target,
    { timeout: 150_000 },
  );
  return data;
}
export async function pullUpdate(target?: AppUpdateTarget): Promise<PullUpdateResult> {
  const { data } = await api.post<PullUpdateResult>(
    "/api/system/pull-update",
    target,
    { timeout: 60_000 },
  );
  return data;
}
export async function getUpdateJob(jobId: string): Promise<UpdateJobStatus> {
  const { data } = await api.get<UpdateJobStatus>(`/api/system/update-jobs/${jobId}`);
  return data;
}
export async function getUpdateTargetOptions(remote?: string): Promise<UpdateTargetOptions> {
  const { data } = await api.get<UpdateTargetOptions>("/api/system/update-target-options", {
    params: remote ? { remote } : undefined,
    timeout: 60_000,
  });
  return data;
}
export async function restartApp(): Promise<RestartResult> {
  const { data } = await api.post<RestartResult>("/api/system/restart");
  return data;
}
