// 与后端 schema 对齐的关键类型（手写版）。OpenAPI 生成的 schema.ts 后续替换。

import type { PluginCapabilities, PluginEventSubscription } from "@/types/pluginContract";

// ===================== 鉴权 =====================
export interface LoginRequest {
  username: string;
  password: string;
  totp_code?: string | null;
  otp_token?: string | null;
  otp_code?: string | null;
  recovery_code?: string | null;
}
export interface LoginResponse {
  ok: boolean;
  require_totp: boolean;
  require_otp?: boolean;
  otp_token?: string | null;
  otp_delivery?: string | null;
  otp_message?: string | null;
  otp_ttl_seconds?: number | null;
  recovery_available?: boolean;
}
export interface CurrentUser {
  id: number;
  username: string;
  has_totp: boolean;
}

// ===================== 账号 =====================
export type AccountStatus =
  | "active"
  | "paused"
  | "floodwait"
  | "dead"
  | "login_required";

export interface ProxySummary {
  id: number;
  /** socks5 / http / mtproxy */
  type: string;
  host: string;
  port: number;
  /** 友好名字；后端目前给的是 host:port */
  label?: string | null;
  // ── 上次主动测的真实出口（30 min Redis 缓存）──
  /** ISO-2 国家代码（CN / US / JP），无缓存时 null */
  exit_country?: string | null;
  exit_ip?: string | null;
  /** epoch 秒；前端用来算"几分钟前测的" */
  probed_at?: number | null;
  /** 上次探测是否成功；null=从未探测 */
  probe_ok?: boolean | null;
}

export interface AccountSummary {
  id: number;
  phone: string;
  display_name: string | null;
  /** Telegram 数字 ID（client.get_me().id），新账号登录后回填，老账号 worker 上线时自动同步 */
  tg_user_id?: number | null;
  /** Telegram 用户名（不含 @），用户可能未设置或随时修改 */
  tg_username?: string | null;
  status: AccountStatus;
  tags?: string[] | null;
  enabled_features: number;
  cold_start_until: string | null;
  created_at: string;
  /** 该账号绑定的代理；null = 走主进程默认出口（DIRECT 或全局 TG_DEFAULT_PROXY） */
  proxy?: ProxySummary | null;
}

export interface AccountDetail extends AccountSummary {
  notes?: string | null;
  template_id?: number | null;
  proxy_id?: number | null;
  /** 设备伪装 profile id，决定 TG 设备列表里看到的 device_model / system_version / app_version */
  device_profile_id?: number | null;
}

export interface AccountStartLoginRequest {
  api_id: number;
  api_hash: string;
  phone: string;
  account_id?: number | null;
  proxy_id?: number | null;
  device_profile_id?: number | null;
}
export interface AccountStartLoginResponse {
  login_token: string;
  phone_code_hash?: string | null;
}
export interface AccountConfirmCodeRequest {
  login_token: string;
  code: string;
}
export interface AccountConfirm2FARequest {
  login_token: string;
  password: string;
}
export interface AccountConfirmResponse {
  account_id: number;
  require_2fa: boolean;
  display_name?: string | null;
}
export interface AccountUpdateRequest {
  display_name?: string | null;
  notes?: string | null;
  tags?: string[] | null;
  template_id?: number | null;
  proxy_id?: number | null;
  device_profile_id?: number | null;
}

// ===================== 账号 Bot 联动 =====================
export type AccountBotRole = "viewer" | "operator" | "admin";
export interface AccountBotRemotePluginPolicy {
  enabled: boolean;
  install: boolean;
  update: boolean;
  uninstall: boolean;
  enable_disable: boolean;
}

export interface AccountBotConfig {
  account_id: number;
  enabled: boolean;
  status: string;
  has_token: boolean;
  remote_plugin_policy: AccountBotRemotePluginPolicy;
  username?: string | null;
  last_update_id?: number | null;
  last_error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AccountBotConfigUpdate {
  bot_token?: string | null;
  clear_token?: boolean;
  enabled?: boolean | null;
  remote_plugin_policy?: Partial<AccountBotRemotePluginPolicy> | null;
}

export interface AccountBotUser {
  id: number;
  account_id: number;
  tg_user_id: number;
  display_name?: string | null;
  role: AccountBotRole;
  notify_enabled: boolean;
  last_chat_id?: number | null;
  enabled: boolean;
  created_at: string;
  updated_at?: string | null;
}

export interface AccountBotUserCreate {
  tg_user_id: number;
  display_name?: string | null;
  role: AccountBotRole;
  notify_enabled?: boolean;
  enabled?: boolean;
}

export interface AccountBotUserUpdate {
  display_name?: string | null;
  role?: AccountBotRole;
  notify_enabled?: boolean;
  enabled?: boolean;
}
export interface AccountBotTestResponse {
  ok: boolean;
  sent: number;
  message: string;
}

export interface AccountBotTestRequest {
  text?: string | null;
  chat_id?: number | null;
  bot_token_override?: string | null;
}

export interface AccountBotInteractionRule {
  id: string;
  name: string;
  enabled: boolean;
  chat_ids?: number[];
  trigger_mode?: "payment" | "keyword" | "both";
  trigger_texts?: string[];
  module_start_keywords?: string[];
  receiver_user_id?: number | null;
  receiver_text?: string | null;
  amount?: number | null;
  amount_match_mode?: "eq" | "gte";
  action: "notice" | "math10" | "module";
  math_prize: number;
  module_key?: string | null;
  module_action?: string | null;
  module_session_scope?: "chat" | "user" | "none" | null;
  participant_policy?: "open_race" | "solo_owner" | "paid_pool" | "notify_only" | null;
  module_config?: Record<string, unknown> | null;
  module_prize?: number | null;
  module_start_text?: string | null;
  user_cooldown_seconds?: string | null;
  daily_limit_per_user?: number | null;
  open_commands?: string[];
  close_commands?: string[];
  status_commands?: string[];
  disabled_message?: string | null;
  valid_seconds?: number | null;
  concurrency?: "chat" | "user" | "none";
  response_template: string;
}

export interface AccountBotInteractionConfig {
  enabled: boolean;
  chat_id?: number | null;
  chat_ids?: number[];
  transfer_test_chat_ids?: number[];
  interaction_bot_token?: string | null;
  clear_interaction_bot_token?: boolean;
  has_interaction_bot_token?: boolean;
  interaction_bot_username?: string | null;
  interaction_bot_id?: number | null;
  interaction_running?: boolean;
  interaction_runtime_status?: "running" | "stopped";
  interaction_last_update_id?: number | null;
  interaction_last_error?: string | null;
  interaction_chat_memberships?: AccountBotChatMembership[];
  interaction_debug?: AccountBotInteractionDebugSnapshot;
  trusted_bot_id?: number | null;
  trusted_bot_ids?: number[];
  transfer_bot_id?: number | null;
  transfer_bot_token?: string | null;
  clear_transfer_bot_token?: boolean;
  has_transfer_bot_token?: boolean;
  transfer_test_chat_memberships?: AccountBotChatMembership[];
  trigger_mode?: "payment" | "keyword" | "both";
  trigger_text: string;
  trigger_texts?: string[];
  module_start_keywords?: string[];
  receiver_user_id?: number | null;
  receiver_text?: string | null;
  amount?: number | null;
  amount_match_mode?: "eq" | "gte";
  action: "notice" | "math10" | "module";
  math_prize: number;
  module_key?: string | null;
  module_action?: string | null;
  module_session_scope?: "chat" | "user" | "none" | null;
  participant_policy?: "open_race" | "solo_owner" | "paid_pool" | "notify_only" | null;
  module_config?: Record<string, unknown> | null;
  module_prize?: number | null;
  module_start_text?: string | null;
  user_cooldown_seconds?: string | null;
  daily_limit_per_user?: number | null;
  open_commands?: string[];
  close_commands?: string[];
  status_commands?: string[];
  query_commands?: string[];
  query_response_template?: string;
  query_item_template?: string;
  query_empty_message?: string;
  disabled_message?: string | null;
  valid_seconds?: number | null;
  concurrency?: "chat" | "user" | "none";
  response_template: string;
  transfer_notice_template: string;
  debit_notice_template: string;
  rules?: AccountBotInteractionRule[];
}

export interface AccountBotChatMembership {
  chat_id: number;
  status: "joined" | "not_joined" | "unknown";
  detail?: string | null;
}

export interface AccountBotInteractionDebugSnapshot {
  ts?: number | null;
  stage?: string | null;
  chat_id?: number | null;
  message_id?: number | null;
  update_id?: number | null;
  payload?: Record<string, unknown>;
  actions?: Record<string, unknown>[];
  guarded_actions?: Record<string, unknown>[];
  warnings?: Record<string, unknown>[];
  error?: string | null;
}

export interface AccountBotInteractionSettlement {
  mode?: "announce_only" | "manual" | "auto";
  amount?: number | null;
  currency?: string | null;
  winner_user_id?: number | null;
  winner_name?: string | null;
  payout_account_label?: string | null;
  status?: "pending" | "announced" | "settled" | "failed";
  data?: Record<string, unknown>;
}

export type AccountBotInteractionConfigUpdate = Pick<
  AccountBotInteractionConfig,
  | "enabled"
  | "chat_id"
  | "chat_ids"
  | "transfer_test_chat_ids"
  | "interaction_bot_token"
  | "clear_interaction_bot_token"
  | "trusted_bot_id"
  | "trusted_bot_ids"
  | "transfer_bot_token"
  | "clear_transfer_bot_token"
  | "trigger_mode"
  | "trigger_text"
  | "trigger_texts"
  | "module_start_keywords"
  | "receiver_user_id"
  | "receiver_text"
  | "amount"
  | "amount_match_mode"
  | "action"
  | "math_prize"
  | "module_key"
  | "module_action"
  | "module_session_scope"
  | "participant_policy"
  | "module_config"
  | "module_prize"
  | "module_start_text"
  | "user_cooldown_seconds"
  | "daily_limit_per_user"
  | "open_commands"
  | "close_commands"
  | "status_commands"
  | "query_commands"
  | "query_response_template"
  | "query_item_template"
  | "query_empty_message"
  | "disabled_message"
  | "valid_seconds"
  | "concurrency"
  | "response_template"
  | "transfer_notice_template"
  | "debit_notice_template"
  | "rules"
>;

export type AccountBotTransferNoticeConfig = AccountBotInteractionConfig;

// ===================== 设备伪装 =====================
export interface DeviceProfileOut {
  id: number;
  name: string;
  device_model: string;
  system_version: string;
  app_version: string;
  lang_code: string;
  system_lang_code: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface DeviceProfileCreate {
  name: string;
  device_model: string;
  system_version: string;
  app_version: string;
  lang_code?: string;
  system_lang_code?: string;
  is_default?: boolean;
}

export interface DeviceProfileUpdate {
  name?: string;
  device_model?: string;
  system_version?: string;
  app_version?: string;
  lang_code?: string;
  system_lang_code?: string;
  is_default?: boolean;
}
export interface AccountCloneConfigRequest {
  from_account_id: number;
  features: string[];
}

// ===================== Config Bundle =====================
export interface ConfigBundleSourceAccount {
  id: number;
  label: string;
}

export interface ConfigBundleFeatureItem {
  feature_key: string;
  enabled: boolean;
  config: Record<string, unknown>;
}

export interface ConfigBundleRuleItem {
  feature_key: string;
  name: string;
  enabled: boolean;
  priority: number;
  config: Record<string, unknown>;
}

export interface ConfigBundleCommandLinkItem {
  template_id: number;
  template_name: string;
  aliases: string[];
  type: string;
  enabled: boolean;
}

export interface ConfigBundleIgnoredPeerItem {
  peer_id: number;
  peer_kind: string;
  peer_label?: string | null;
}

export interface ConfigBundleExport {
  version: "1";
  source_account: ConfigBundleSourceAccount;
  rules: ConfigBundleRuleItem[];
  features: Record<string, ConfigBundleFeatureItem>;
  command_links: ConfigBundleCommandLinkItem[];
  ignored_peers: ConfigBundleIgnoredPeerItem[];
}

export interface ConfigBundleDiffCounts {
  add: number;
  skip: number;
  conflict: number;
}

export interface ConfigBundleDiffItem {
  entity: "feature" | "rule" | "command_link" | "ignored_peer";
  key: string;
  action: "add" | "skip" | "conflict";
  conflict_kind?: "overridable" | "blocked" | null;
  fields: string[];
  note?: string | null;
}

export interface ConfigBundleDryRunResponse {
  version: "1";
  source_account: ConfigBundleSourceAccount;
  target_account: ConfigBundleSourceAccount;
  size_bytes: number;
  counts: ConfigBundleDiffCounts;
  items: ConfigBundleDiffItem[];
  warnings: string[];
  preview_signature?: string | null;
}

export interface ConfigBundleConfirmResponse {
  version: "1";
  source_account: ConfigBundleSourceAccount;
  target_account: ConfigBundleSourceAccount;
  imported: number;
  skipped: number;
  conflicts: number;
  warnings: string[];
  preview_signature?: string | null;
}

// ===================== 功能 =====================
export type FeatureState = "active" | "failed" | "disabled";
export type FeatureCategory = "interactive" | "automation" | "utility";

export interface FeatureInteractionEntry {
  key: string;
  title?: string | null;
  label?: string | null;
  description?: string | null;
  interaction_profile?: "session_game" | "challenge_game" | "reward_pool" | "utility_trigger" | string | null;
  launch_mode?: "bridge" | "direct" | "hybrid" | string | null;
  dispatch_modes?: Array<"admin_command" | "public_keyword" | string>;
  message_channels?: Partial<Record<"admin_command" | "public_keyword" | string, string | string[] | { prefer?: string[]; fallback?: boolean }>>;
  money_channel?: "userbot_reply" | string | null;
  events?: Array<"payment_confirmed" | "keyword" | "message" | "callback_query" | "session_close" | "all_messages" | string>;
  session_scope?: "chat" | "user" | "none" | string | null;
  participant_policy?: "open_race" | "solo_owner" | "paid_pool" | "notify_only" | string | null;
  preserve_command_trigger?: boolean | null;
  command_fallback?: {
    enabled?: boolean;
    command?: string | null;
    mode?: string | null;
  } | null;
  payload_contract?: Record<string, unknown> | null;
  result_contract?: Record<string, unknown> | null;
  settlement?: Record<string, unknown> | null;
  input_schema?: Record<string, unknown> | null;
}

export interface FeatureInfo {
  key: string;
  display_name: string;
  is_builtin: boolean;
  source_type?: "local" | "remote" | string;
  source_label?: string | null;
  orphan?: boolean;
  signature_ok?: boolean | null;
  version?: string | null;
  usage?: string | null;
  config_schema?: Record<string, unknown> | null;
  config_actions?: Record<string, unknown>[];
  category?: FeatureCategory | string | null;
  interaction_profile?: "session_game" | "challenge_game" | "reward_pool" | "utility_trigger" | string | null;
  interaction_entries?: FeatureInteractionEntry[];
  event_subscriptions?: PluginEventSubscription[];
  capabilities?: PluginCapabilities;
  permissions?: string[];
  experimental: boolean;
  update_available?: boolean;
  latest_version?: string | null;
  last_update_check_at?: string | null;
  last_update_check_error?: string | null;
  lint_warnings?: string[];
}
export interface AccountFeatureItem {
  feature_key: string;
  enabled: boolean;
  state: FeatureState;
  last_error?: string | null;
  config: Record<string, unknown>;
}
export interface AccountFeatureToggle {
  enabled: boolean;
  config?: Record<string, unknown> | null;
}
export interface FeatureMatrixRow {
  id: number;
  name: string;
  features: Record<string, FeatureState>;
  feature_enabled?: Record<string, boolean>;
}
export interface FeatureMatrixResponse {
  features: FeatureInfo[];
  accounts: FeatureMatrixRow[];
}

// ===================== 规则 =====================
export interface RuleOut {
  id: number;
  account_id: number;
  feature_key: string;
  name: string;
  enabled: boolean;
  priority: number;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}
export interface RuleCreate {
  name: string;
  enabled?: boolean;
  priority?: number;
  config?: Record<string, unknown>;
}
export interface RuleUpdate {
  name?: string;
  enabled?: boolean;
  priority?: number;
  config?: Record<string, unknown>;
}
export interface RuleDryRunRequest {
  sample_message: string;
  sample_chat_type?: "private" | "group" | "channel";
  sample_chat_id?: number;
}
export interface RuleDryRunResponse {
  matched: boolean;
  output?: string | null;
  detail?: Record<string, unknown> | null;
}
export interface RuleExecuteResponse {
  ok: boolean;
  error?: string | null;
}
export interface RuleCopyRequest {
  rule_ids: number[];
  target_account_ids: number[];
}

// ===================== 风控 =====================
export type RatePolicy = "drop" | "queue" | "backoff" | "pause" | "notify";

export interface RateLimitRuleConfig {
  action: string;
  per_second?: number | null;
  per_minute?: number | null;
  per_hour?: number | null;
  per_day?: number | null;
  same_peer_per_minute?: number | null;
  policy: RatePolicy;
  backoff_base_seconds: number;
  backoff_max_seconds: number;
  enabled: boolean;
}
export interface AccountRateLimitOut {
  template_id: number | null;
  rules: RateLimitRuleConfig[];
}
export interface UsageBucket {
  action: string;
  used: number;
  limit: number | null;
  pct: number;
  warn?: boolean;
}
export interface UsageResponse {
  window: string;
  buckets: UsageBucket[];
  active_overrides: Array<Record<string, unknown>>;
}
export interface StrictRequest {
  multiplier?: number;
  ttl_seconds?: number;
}

export interface TemplateOut {
  id: number;
  name: string;
  is_default: boolean;
  created_at: string;
}

// ===================== 代理 =====================
export type ProxyType = "socks5" | "http" | "https" | "mtproxy";

export interface ProxyOut {
  id: number;
  type: ProxyType;
  host: string;
  port: number;
  username: string | null;
  has_password: boolean;
}

export interface ProxyCreate {
  type: ProxyType;
  host: string;
  port: number;
  username?: string | null;
  password?: string | null;
}

export interface ProxyUpdate {
  type?: ProxyType;
  host?: string;
  port?: number;
  username?: string | null;
  password?: string | null;
  clear_password?: boolean;
}

export interface ProxyTestResult {
  ok: boolean;
  latency_ms?: number | null;
  exit_ip?: string | null;
  country?: string | null;
  region?: string | null;
  city?: string | null;
  error?: string | null;
}

/** ``GET /api/proxies/{id}/usage`` 单条引用——告诉用户"删了会断哪些"。 */
export interface ProxyUsageItem {
  /** "account" | "llm_provider" */
  kind: string;
  id: number;
  name?: string | null;
  /** account 给 phone；llm 给 default_model */
  extra?: string | null;
}

export interface ProxyUsageResponse {
  accounts: ProxyUsageItem[];
  llm_providers: ProxyUsageItem[];
  total: number;
}

// ===================== 网络环境 =====================
export interface NetworkInfo {
  ip: string | null;
  country: string | null;
  region: string | null;
  city: string | null;
  org: string | null;
  cached_at: number;
  fresh: boolean;
  error?: string | null;
}

// ===================== 自动回复 rule.config =====================
export type AutoReplyMatch = "keyword" | "template" | "regex";
export type AutoReplyScope = "private" | "group_all" | "group_specific";
export type AutoReplyCooldownScope = "chat" | "user";

export interface AutoReplyRuleConfig {
  match: AutoReplyMatch;
  patterns: string[];
  scope: AutoReplyScope;
  group_ids?: string[];
  // 正则模式可在 reply 中使用 {1} / {name} / {1|默认值}
  reply: string;
  cooldown_seconds?: number | string;
  cooldown_scope?: AutoReplyCooldownScope;
  daily_limit_per_user?: number;
  usage_label?: string;
  cooldown_notice_enabled?: boolean;
  daily_limit_notice_enabled?: boolean;
  cooldown_message_template?: string;
  daily_limit_message_template?: string;
  daily_limit_success_message_template?: string;
  daily_limit_final_message_template?: string;
  whitelist?: string[];
  blacklist?: string[];
  case_sensitive?: boolean;
  reply_to?: boolean;     // true = 以引用形式回复（默认）；false = 直接发新消息
}

// ===== Sprint2 #5 =====
// 与后端 ``builtin/forward/manifest.py:config_schema`` 对齐的 rule.config 结构
//
// source_kind：
//   - all       —— 任何 incoming 消息都进流水线
//   - peers     —— 仅 source_peers 中的 chat_id 命中（支持 -100 / -bare / bare 等价展开）
//   - keyword   —— 文本（小写）包含 keyword 时命中；空 keyword 视为不命中
//   - duplicate —— 复读检测：同一 chat 内 ≥N 个不同用户发送相同文本时触发，每日去重
//
// mode：
//   - forward_native  —— 原生转发，保留原作者署名
//   - copy_text       —— 复制文本，不显示原作者，可加 header 前缀
//   - quote           —— 引用包装，自动 "📨 来自 X" 前缀
//   - link_only       —— 公开超级群可点链接（私群退化为可读字符串）
export type ForwardSourceKind = "all" | "peers" | "keyword" | "duplicate";
export type ForwardMode =
  | "forward_native"
  | "copy_text"
  | "quote"
  | "link_only";

export interface ForwardRuleConfig {
  source_kind: ForwardSourceKind;
  /** chat_id 列表；前端用 string[] 编辑，提交时转成 number[] */
  source_peers?: number[];
  keyword?: string;
  /** duplicate 模式：时间窗口（秒），默认 60 */
  duplicate_window?: number;
  /** duplicate 模式：不同用户数阈值（同一用户多次发送只算1人），默认 3 */
  duplicate_threshold?: number;
  /** 必填：目标 chat_id（Telethon 形式） */
  target_chat_id: number;
  mode: ForwardMode;
  /** 默认 true；false 时跳过含媒体消息（仅文本通过） */
  include_media?: boolean;
  /** copy / quote / link_only 模式下的固定前缀 */
  header?: string;
}

// ===== 自动复读 =====
// 与插件库 ``autorepeat`` 插件 manifest 的 config_schema 对齐的 rule.config 结构
export interface AutorepeatRuleConfig {
  /** 必填：监控的群组 chat_id（Telethon marked ID 格式） */
  target_chat_id: number;
  /** 时间窗口（秒），默认 300 */
  time_window?: number;
  /** 触发复读所需的不同用户数，默认 5 */
  min_users?: number;
}

export interface CodexImageConfig {
  /** Codex Access Token，用于鉴权 */
  access_token: string;
  /** 模型名称，默认 gpt-5.4 */
  model?: string;
  /** 最大等待时间（秒），默认 600 */
  max_wait_seconds?: number;
}

// ===================== 日志 =====================
export interface RuntimeLogItem {
  id: number;
  account_id: number | null;
  level: string;
  message: string;
  created_at: string;
  source?: string | null;
  detail?: Record<string, unknown> | null;
}

export interface SystemConsoleLogsResponse {
  ok: boolean;
  source: string;
  services: string[];
  tail: number;
  lines: string[];
  error?: string | null;
}

// 操作日志（Web 端写操作）
export interface AuditLogItem {
  id: number;
  ts: string;
  user_id: number | null;
  action: string;
  target?: string | null;
  detail?: Record<string, unknown> | null;
}

export interface NativeRawMeta {
  enabled?: boolean;
  source?: string | null;
  driver?: string | null;
  object?: string | null;
  stored_in_trace?: boolean;
  size_bytes?: number;
  reason_code?: string | null;
  [key: string]: unknown;
}

export interface EventSpanItem {
  id: number;
  span_id: string;
  trace_id: string;
  parent_span_id?: string | null;
  phase: string;
  component?: string | null;
  plugin_key?: string | null;
  entry_key?: string | null;
  status: string;
  reason_code?: string | null;
  message?: string | null;
  detail?: Record<string, unknown> | null;
  started_at: string;
  ended_at?: string | null;
  duration_ms?: number | null;
}

export interface EventActionItem {
  id: number;
  action_id: string;
  trace_id: string;
  plugin_key?: string | null;
  action_type: string;
  requested_send_via?: string | null;
  actual_send_via?: string | null;
  target_chat_id?: number | null;
  target_message_id?: number | null;
  status: string;
  telegram_message_id?: number | null;
  inline_result_count?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  detail?: Record<string, unknown> | null;
  created_at: string;
}

export interface EventTraceSummary {
  id: number;
  trace_id: string;
  account_id?: number | null;
  source_channel?: string | null;
  event_type: string;
  chat_id?: number | null;
  message_id?: number | null;
  update_id?: number | null;
  callback_query_id?: string | null;
  sender_user_id?: number | null;
  sender_name?: string | null;
  chat_title?: string | null;
  text_preview?: string | null;
  inline_query?: string | null;
  chosen_inline_result_id?: string | null;
  chosen_inline_query?: string | null;
  status: string;
  started_at: string;
  ended_at?: string | null;
  duration_ms?: number | null;
  native_raw_meta?: NativeRawMeta | null;
  plugin_count: number;
  plugin_keys: string[];
  action_count: number;
  error_count: number;
}

export interface EventProbeItem {
  label: string;
  path?: string;
  value?: unknown;
  note?: string;
}

export interface EventProbeSuggestion {
  title: string;
  reason?: string;
  manifest?: Record<string, unknown>;
  action?: Record<string, unknown>;
  level?: string;
  capability?: string;
  stored_in_trace?: boolean;
  reason_code?: string | null;
}

export interface EventProbeRoutingItem {
  phase?: string;
  plugin_key?: string | null;
  entry_key?: string | null;
  matched?: boolean;
  status?: string;
  reason_code?: string | null;
  message?: string | null;
  filters?: unknown;
}

export interface EventProbeReport {
  version: number;
  headline: string;
  summary: Record<string, unknown>;
  field_paths: EventProbeItem[];
  message_facts: EventProbeItem[];
  subscription_suggestions: EventProbeSuggestion[];
  action_suggestions: EventProbeSuggestion[];
  capability_hints: EventProbeSuggestion[];
  routing: EventProbeRoutingItem[];
  warnings: string[];
}

export interface EventTraceDetail extends EventTraceSummary {
  raw_summary?: Record<string, unknown> | null;
  payload_snapshot?: Record<string, unknown> | null;
  probe_report?: EventProbeReport | null;
  spans: EventSpanItem[];
  actions: EventActionItem[];
  related_runtime_logs: RuntimeLogItem[];
}

export type MessageFunelStage = "pass" | "skip" | "stuck" | "fail" | "none";
export type MessageVerdict = "responded" | "no_response_normal" | "stuck" | "failed";

export interface MessageFunel {
  received: MessageFunelStage;
  routed: MessageFunelStage;
  ran: MessageFunelStage;
  sent: MessageFunelStage;
  verdict: MessageVerdict;
  stuck_at?: "routed" | "ran" | "sent" | null;
  reason_code?: string | null;
  reason_text: string;
  next_step: string;
}

export interface MessageFunelItem extends EventTraceSummary {
  funel: MessageFunel;
  verdict: MessageVerdict;
  stuck_at?: "routed" | "ran" | "sent" | null;
  reason_code?: string | null;
  reason_text: string;
  next_step: string;
}

// ===================== 系统设置 =====================
export interface SystemSettings {
  command_prefix: string;
  /** 开启时账号本人也必须带系统前缀；关闭后仅账号本人可裸命令触发 */
  command_prefix_required?: boolean;
  kill_switch?: boolean;
  /** 全局 AI 能力开关；关闭后不加载模型 provider，也不注入插件 ctx.ai */
  ai_enabled?: boolean;
  sudo_enabled?: boolean;
  /** 群聊纯命令回声防误触检查前 N 条消息；0 = 关闭 */
  command_echo_guard_previous_messages?: number;
  /** IANA 时区标识，如 "Asia/Shanghai"；默认 Asia/Shanghai */
  timezone?: string;
  remote_plugin_update_check?: {
    enabled: boolean;
    interval_minutes: number;
  };
  app_update_target?: {
    remote: string;
    branch: string;
  };
  login_security?: {
    notify_otp_enabled: boolean;
    notify_otp_failed_attempt_threshold: number;
    notify_otp_fail_window_seconds: number;
    notify_otp_ttl_seconds: number;
    notify_otp_max_attempts: number;
    totp_enabled: boolean;
    totp_mode: "always" | "after_failures";
    totp_failed_attempt_threshold: number;
    recovery_code_ttl_seconds: number;
  };
  llm_limits?: {
    per_minute: number;
    daily_requests: number;
    daily_tokens: number;
    premium_daily: number;
  };
  payout_limits?: {
    single_max: number;
    daily_max: number;
  };
  log_retention?: {
    trace_enabled: boolean;
    event_bus_delivery_enabled: boolean;
    inline_updates_enabled: boolean;
    runtime_log_retention_days: number;
    runtime_log_max_message_chars: number;
    runtime_log_max_detail_chars: number;
    runtime_log_min_level: "debug" | "info" | "warn" | "error";
    trace_retention_days: number;
    trace_payload_snapshot_retention_days: number;
    native_raw_persist_enabled: boolean;
    native_raw_retention_days: number;
  };
}

// ===================== 系统健康概览（Dashboard 用）=====================
//
// 与后端 ``app/api/system_health.py`` 对齐
export interface DbStatus {
  ok: boolean;
  /** PostgreSQL 16.x 字串；失败时 null */
  version?: string | null;
  error?: string | null;
}

export interface AlembicStatus {
  /** true = DB 当前版本就是代码 head；false = 需要跑 alembic upgrade head */
  ok: boolean;
  /** DB 里 alembic_version 表存的版本号 */
  current?: string | null;
  /** 代码仓库里 alembic 链的最新版本 */
  head?: string | null;
  /** 已写但还没 apply 的迁移版本号（按时间序） */
  pending: string[];
  error?: string | null;
}

export interface RedisStatus {
  ok: boolean;
  error?: string | null;
}

export interface ProvidersHealthStatus {
  total: number;
  with_api_key: number;
  with_proxy: number;
  /** {modality: count}，如 {"text":2,"vision":1} */
  by_modality: Record<string, number>;
  /** {cost_tier_str: count}，如 {"1":1,"2":2,"3":1} */
  by_cost_tier: Record<string, number>;
}

export interface ProxiesHealthStatus {
  total: number;
  /** {type: count}，如 {"socks5":2,"http":1,"mtproxy":1} */
  by_type: Record<string, number>;
  /** 被任意 LLMProvider.proxy_id 引用的代理数量（去重） */
  used_by_llm: number;
}

export interface WorkersHealthStatus {
  total: number;
  /** {status: count}，如 {"active":3,"paused":1,"login_required":1} */
  by_status: Record<string, number>;
  runtime_total: number;
  runtime_alive: number;
  runtime_desired_running: number;
  runtime_desired_running_alive: number;
  runtime_failing: number;
}

export interface HealthOverview {
  db: DbStatus;
  alembic: AlembicStatus;
  redis: RedisStatus;
  providers: ProvidersHealthStatus;
  proxies: ProxiesHealthStatus;
  workers: WorkersHealthStatus;
}

export interface HostResourceStatus {
  cpu_percent?: number | null;
  memory_used_percent?: number | null;
  memory_total_mb?: number | null;
  disk_used_percent?: number | null;
  disk_free_gb?: number | null;
  sampled_at: number;
  uptime_seconds?: number | null;
}

export interface ProcessResourceStatus {
  pid?: number | null;
  cpu_percent?: number | null;
  rss_mb?: number | null;
  uss_mb?: number | null;
}

export interface WorkerRuntimeResourceStatus extends ProcessResourceStatus {
  account_id: number;
  alive: boolean;
  desired: string;
  fail_count: number;
}

export interface ContainerResourceStatus {
  id?: string | null;
  name: string;
  service?: string | null;
  cpu_percent?: number | null;
  memory_mb?: number | null;
  memory_limit_mb?: number | null;
  memory_percent?: number | null;
}

export interface RuntimeLogStatsStatus {
  last_5m_total: number;
  last_5m_warn: number;
  last_5m_error: number;
}

export interface ResourceDashboard {
  host: HostResourceStatus;
  main_process: ProcessResourceStatus;
  project_total: ProcessResourceStatus;
  app_uptime_seconds?: number | null;
  other_processes: ProcessResourceStatus[];
  containers: ContainerResourceStatus[];
  container_total: ProcessResourceStatus;
  container_probe_error?: string | null;
  workers: WorkerRuntimeResourceStatus[];
  worker_alive: number;
  worker_desired_running: number;
  logs: RuntimeLogStatsStatus;
}

// 通用 list 包装（部分接口直接返数组，但为了后续兼容预留）
export interface ListResponse<T> {
  items: T[];
  total?: number;
}

// ===================== Sprint2 #1：拟人化 humanize =====================
// 与后端 ``HumanizeOut`` / ``HumanizeUpdate`` 对齐
//   - active_window_*：``HH:MM[:SS]`` 字符串，``null`` = 不限活跃时段
//   - typing_probability / jitter_pct：百分比 0-100
//   - typing_min_ms <= typing_max_ms 由前端校验
export interface HumanizeConfig {
  jitter_pct: number;
  typing_simulate: boolean;
  typing_min_ms: number;
  typing_max_ms: number;
  typing_probability: number;
  read_before_reply: boolean;
  active_window_start?: string | null;
  active_window_end?: string | null;
  cold_start_days: number;
}
export type HumanizeUpdate = Partial<HumanizeConfig>;

// ==================== Sprint2 #3 Ignored Peers ====================
//
// peer 类型：
//   - private    1 对 1 私聊（chat_id 为正整数）
//   - group      普通群（旧版小群，chat_id 为负数但非 -100 开头）
//   - supergroup 超级群（chat_id 形如 -1001234567890）
//   - channel    频道（chat_id 形如 -1001234567890）
export type PeerKind = "private" | "group" | "supergroup" | "channel";

/** 已配置允许会话的 peer 一行（GET / POST 响应） */
export interface IgnoredPeer {
  id: number;
  account_id: number;
  /** Telethon chat_id；可正可负（supergroup 形如 -100xxx） */
  peer_id: number;
  peer_kind: PeerKind | string;
  peer_label: string | null;
  added_at: string;
}

/** 加入允许会话名单的入参 */
export interface IgnoredPeerCreate {
  peer_id: number;
  peer_kind?: PeerKind | string;
  peer_label?: string | null;
}

/**
 * worker 内存里"最近 50 个 incoming peer"的一条。
 * - 重启 worker 后清空
 * - worker 离线时后端返回空数组
 */
export interface RecentPeerItem {
  peer_id: number;
  peer_kind: PeerKind | string;
  peer_label: string | null;
  /** epoch 秒（time.time()），前端做相对时间显示 */
  ts: number;
}

/**
 * GET /recent-peers 包裹响应：把 "worker 是否在跑" 单独传一个布尔，
 * 这样前端可以区分"worker 离线导致空"vs"worker 在跑只是没收到 incoming"
 * 这两种 items=[] 的语义。
 */
export interface RecentPeersResponse {
  worker_alive: boolean;
  items: RecentPeerItem[];
}

// ==================== Sprint2 #2 Custom Commands ====================
//
// 4 种命令类型：
//   - reply_text   收到 → 编辑原消息为文本（支持 {args} 占位）
//   - forward_to   收到 → 转发被引用消息到指定 chat_id
//   - run_plugin   占位：调插件方法（V1 暂未实装）
//   - ai           收到 → 调 LLM provider → 编辑回原消息
export type CommandTemplateType =
  | "reply_text"
  | "forward_to"
  | "run_plugin"
  | "ai";

/** 命令模板出参（与 GET /api/commands/templates 对齐） */
export interface CommandTemplateOut {
  id: number;
  /** ,name 触发名；命令前缀在系统设置里改 */
  name: string;
  type: CommandTemplateType;
  /** 按 type 不同结构；前端按 type 切表单 */
  config: Record<string, unknown>;
  description: string | null;
  aliases: string[];
  created_at: string;
}

/** 新建模板入参（POST /api/commands/templates） */
export interface CommandTemplateCreate {
  name: string;
  aliases?: string[];
  type: CommandTemplateType;
  config: Record<string, unknown>;
  description?: string | null;
}

/** PATCH 更新（任意字段可选） */
export interface CommandTemplateUpdate {
  name?: string;
  aliases?: string[];
  type?: CommandTemplateType;
  config?: Record<string, unknown>;
  description?: string | null;
}

// 各 type 对应的 config 形状（仅做编辑/校验参考；后端 schema 校验是权威）
export interface ReplyTextConfig {
  /** 命令文本；支持 {args} 占位，被 ,name xxx yyy 的剩余参数替换 */
  text: string;
}

export interface ForwardToConfig {
  /** 目标会话的 chat_id（int / 字符串都可）；留空 / 缺省 = 转到触发消息所在的 chat */
  target_chat_id?: number | string | null;
  /** 转发成功后多少秒删命令消息；0 / 缺省 = 不删；上限 3600 */
  delete_after?: number | null;
}

export interface RunPluginConfig {
  plugin_key: string;
  method?: string;
  args?: unknown[];
}

export interface AICommandConfig {
  /** AI 命令能力模式：chat 默认问答；search 强制联网；image/video 对齐 TeleBox 二级指令 */
  mode?: "chat" | "search" | "image" | "video";
  /** 关联的 LLMProvider.id（fixed 模式下的固定 provider；auto 模式下没命中规则也用它兜底） */
  provider_id?: number;
  /** 单次覆盖 provider.default_model；空 = 用 provider 默认 */
  model?: string;
  /** 拼 prompt 时引用被回复消息内容 */
  quote_replied?: boolean;
  system_prompt?: string;
  max_tokens?: number;
  /** 采样温度，0 更稳定，2 更发散；空表示不覆盖 provider 默认值 */
  temperature?: number;
  /** 推理模型的思考预算；当前主要用于 OpenAI Chat/Responses 协议 */
  reasoning_effort?: "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
  /** 单次 API 调用超时时间，单位秒 */
  timeout_seconds?: number;
  // ── 路由（Sprint2 #2 路由扩展）──
  /**
   * fixed = 永远用 provider_id（V1 行为，默认）
   * auto  = 看消息内容自动选 provider；规则全不命中时回退 fallback / classifier
   */
  routing_mode?: "fixed" | "auto";
  /** auto 模式下规则与分类器都失败时使用的 provider id；缺省 = 用 provider_id 自身 */
  routing_fallback_provider_id?: number;
  /** auto 模式下分类器 provider id；指定后路由器规则未命中时调一个轻量小模型分类 */
  classifier_provider_id?: number;
  // ── 输出格式（决定 TG 里编辑成什么样）──
  /**
   * Telegram 解析模式；默认 html
   * - html      Telethon 内置；支持 <b> <blockquote expandable> 等，能实现折叠引用块
   * - markdown  Telegram 经典 Markdown v1（telethon 接受 'md'）
   * - plain     不解析任何格式
   *
   * 老数据里可能存 'markdownv2'，后端读时自动归一到 'html'（telethon 1.36 不识别 v2）。
   */
  output_format?: "html" | "markdown" | "plain";
  /** 输出模板字符串；null = 用默认（PRESET_SIMPLE） */
  output_template?: string | null;
  /** 是否对占位符的值做对应格式的转义；默认 true。html 模式下会转义 & < > */
  escape_values?: boolean;
  /** 使用模型工具联网搜索；目前仅 OpenAI Responses API 生效 */
  web_search?: boolean;
  /** 联网搜索上下文强度 */
  web_search_context_size?: "low" | "medium" | "high";
  /** edit = 原地编辑命令消息；send_new = 删除命令并另发一条 */
  send_mode?: "edit" | "send_new";
  /** image 模式后端：codex_image 复用图片生成插件；llm 调用 Provider 原生生图 */
  image_backend?: "codex_image" | "llm";
}

/** 账号详情 → 命令 tab 一行：模板内容 + 该账号是否启用 */
export interface AccountCommandItem {
  template: CommandTemplateOut;
  enabled: boolean;
}

/** AI 总览：账号启用 AI 命令的批量统计 */
export interface AICommandEnablementSummary {
  total_accounts: number;
  enabled_accounts: number;
  ai_templates: number;
}

// ===== Sprint4 Wave1 =====
export type Sprint4Wave1TypesMarker = "command-aliases";

// ── LLM Provider ──
export type LLMProviderKind = "openai" | "anthropic" | "ollama";

/**
 * API 协议（与 provider 厂商解耦；同一个反代 base_url 可能只支持其中某种）：
 * - chat_completions    POST /chat/completions    OpenAI 经典协议
 * - responses           POST /responses           OpenAI 2024 出的新协议
 * - anthropic_messages  POST /v1/messages         Anthropic 协议
 *
 * 国内常见反代（如 anyrouter）有的只接 responses 而拒 chat_completions，
 * 切到对应 api_format 即可解决报 404 / "模型不支持" 一类问题。
 */
export type LLMApiFormat = "chat_completions" | "responses" | "anthropic_messages";
export type LLMReasoningEffort = "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
export type LLMWebSearchApiFormat = "auto" | LLMApiFormat;
export type LLMProtocolProfile = "standard" | "claude_code_proxy";
/**
 * 客户端身份档案（0.57.0 阶段 A）：控制 UA 与身份相关的安全请求头，
 * 与 protocol_profile（协议语义 / beta 头）相互独立。auto 按本次实际协议解析。
 */
export type LLMClientIdentityProfile =
  | "auto"
  | "minimal"
  | "openai_sdk"
  | "codex_cli"
  | "codex_desktop"
  | "claude_code"
  | "claude_desktop"
  | "grok_cli";

/**
 * LLMProvider 下挂的一个候选模型条目（与后端 ProviderModel 对齐）。
 *
 * - id 是模型 ID（如 ``gpt-5.5`` / ``claude-haiku-4-5``）
 * - enabled = 该模型是否会出现在下游"自定义命令 ai 子表单"的展开式 select 里
 * - custom = true 表示用户手动加的；false 表示从 ``GET /v1/models`` fetch 拉的
 * - label 是可选的展示名（默认就用 id）
 */
export interface ProviderModel {
  id: string;
  enabled: boolean;
  custom: boolean;
  label?: string | null;
  supports_tools?: boolean | null;
  supports_images?: boolean | null;
  supports_temperature?: boolean | null;
  reasoning_efforts?: LLMReasoningEffort[] | null;
}

/**
 * 模态分类（与后端 ALL_LLM_MODALITIES 对齐）：
 * - text       纯文本 LLM（绝大多数）
 * - vision     视觉多模态（图文输入 → 文本输出，如 GPT-4V / Claude Vision）
 * - audio      音频多模态（语音转写 / TTS，如 Whisper / GPT-4o realtime）
 * - multimodal 全模态（图、音、视频同时输入，如 GPT-4o / Gemini-Pro）
 */
export type LLMModality = "text" | "vision" | "audio" | "multimodal";

/**
 * 路由标签集合（与后端 ALL_LLM_TAGS 对齐）：
 * - chat / code / math / translate / vision    擅长领域
 * - long_context                               大上下文（≥ 64K token）
 * - reason / smart                             复杂推理 / 旗舰
 * - cheap / fast                               量大优先 / 低延迟
 * - classify                                   适合做"路由分类器"的轻量小模型
 */
export type LLMTag =
  | "chat"
  | "code"
  | "math"
  | "translate"
  | "vision"
  | "long_context"
  | "reason"
  | "smart"
  | "cheap"
  | "fast"
  | "classify";

/** GET /api/commands/llm-providers 出参；不含明文 api_key */
export interface LLMProviderOut {
  id: number;
  name: string;
  provider: LLMProviderKind | string;
  has_api_key: boolean;
  base_url: string | null;
  default_model: string;
  /** API 协议；老数据可能缺，前端按 chat_completions 兜底 */
  api_format?: LLMApiFormat | string;
  /** Anthropic Messages 请求兼容档案；其他协议固定为 standard */
  protocol_profile?: LLMProtocolProfile | string;
  /** 联网搜索时的协议覆盖；auto = OpenAI/chat_completions 在联网时临时走 responses */
  web_search_api_format?: LLMWebSearchApiFormat | string;
  /** 客户端身份档案；auto 按本次实际协议解析。与 protocol_profile 相互独立 */
  client_identity_profile?: LLMClientIdentityProfile | string;
  /** 模态；老数据可能缺，前端按 "text" 兜底 */
  modality?: LLMModality | string;
  /** 路由标签；老数据可能为空数组 */
  tags?: string[];
  /** 1=便宜 / 2=中 / 3=旗舰；老数据按 2 兜底 */
  cost_tier?: number;
  /** 运维备注 */
  notes?: string | null;
  /** 出口代理 id；null = 直连（DIRECT） */
  proxy_id?: number | null;
  /** 候选模型清单 */
  models?: ProviderModel[];
  created_at: string;
}

export interface LLMProviderApiKeyReveal {
  api_key: string;
}

export interface LLMProviderCreate {
  name: string;
  provider: LLMProviderKind;
  /** 空 / undefined → 不设；下发后由后端 Fernet 加密 */
  api_key?: string | null;
  base_url?: string | null;
  default_model: string;
  api_format?: LLMApiFormat;
  /** 仅在 api_format=anthropic_messages 时生效 */
  protocol_profile?: LLMProtocolProfile;
  web_search_api_format?: LLMWebSearchApiFormat;
  /** 客户端身份档案；auto 按本次实际协议解析。与 protocol_profile 相互独立 */
  client_identity_profile?: LLMClientIdentityProfile;
  modality?: LLMModality;
  tags?: string[];
  cost_tier?: number;
  notes?: string | null;
  /** 出口代理；不传 / null = 直连 */
  proxy_id?: number | null;
  /** 候选模型清单；通常新建时留空，建完用"Fetch 模型列表"按钮自动填 */
  models?: ProviderModel[];
}

/**
 * PATCH provider；api_key 行为：
 * - 缺省 / undefined → 不动
 * - "" 空串       → 清空
 * - 非空字符串    → 替换并加密
 *
 * 路由字段（modality / tags / cost_tier / notes）：缺省 / undefined = 不动。
 *
 * proxy 切换语义：
 * - 想换成另一条 proxy：``proxy_id: <id>``，``clear_proxy`` 不传或 false
 * - 想切回 DIRECT（不走代理）：``clear_proxy: true``，``proxy_id`` 可不传
 * - 不动：两个都不传
 */
export interface LLMProviderUpdate {
  name?: string;
  provider?: LLMProviderKind;
  api_key?: string | null;
  base_url?: string | null;
  default_model?: string;
  api_format?: LLMApiFormat;
  /** 仅在 api_format=anthropic_messages 时生效 */
  protocol_profile?: LLMProtocolProfile;
  web_search_api_format?: LLMWebSearchApiFormat;
  /** 客户端身份档案；auto 按本次实际协议解析。与 protocol_profile 相互独立 */
  client_identity_profile?: LLMClientIdentityProfile;
  modality?: LLMModality;
  tags?: string[];
  cost_tier?: number;
  notes?: string | null;
  proxy_id?: number | null;
  clear_proxy?: boolean;
  /** 整体替换式 PATCH——给 list（含空 list）就覆盖；undefined = 不动 */
  models?: ProviderModel[];
}

/** ``POST /api/commands/llm-providers/{pid}/fetch-models`` 出参 */
export interface FetchModelsResponse {
  /** 从 ``GET {base_url}/models`` 拉到的模型条数 */
  fetched: number;
  /** 合并后最新 provider 出参 */
  provider: LLMProviderOut;
}

/** ``POST /api/commands/llm-providers/fetch-models-preview`` 入参；
 *  用编辑表单当前值（不必先保存）预览 fetch /models 的结果。 */
export interface FetchModelsPreviewRequest {
  provider: LLMProviderKind;
  api_format?: LLMApiFormat;
  base_url?: string | null;
  /** 可空——若 pid 给了且 api_key 留空，后端会回落到 DB 里已存的 */
  api_key?: string | null;
  proxy_id?: number | null;
  /** 已落库 provider 的 id（编辑模式才有）；用来回落到已存 api_key */
  pid?: number | null;
}

/** ``POST /api/commands/llm-providers/fetch-models-preview`` 出参 */
export interface FetchModelsPreviewResponse {
  fetched: number;
  ids: string[];
}

export interface QuickVerifyProviderRequest {
  base_url: string;
  api_key?: string | null;
  api_format: LLMApiFormat;
  protocol_profile?: LLMProtocolProfile;
  client_identity_profile?: LLMClientIdentityProfile;
  model?: string | null;
  reasoning_effort?: LLMReasoningEffort | null;
  proxy_id?: number | null;
  system_prompt?: string;
  message?: string;
  max_tokens?: number;
  timeout_seconds?: number;
}

export interface QuickVerifyProviderResult {
  ok: boolean;
  model?: string | null;
  requested_model?: string | null;
  latency_ms?: number;
  response?: string | null;
  error?: string | null;
  requires_model?: boolean;
  models: string[];
  api_format: LLMApiFormat;
  streaming?: boolean;
  stream_fallback?: boolean;
  input_tokens?: number;
  output_tokens?: number;
  base_url?: string;
  provider?: LLMProviderKind;
  suggested_name?: string;
}

export type QuickVerifyProviderStreamEvent =
  | {
      type: "discovery";
      model: string;
      models: string[];
      api_format: LLMApiFormat;
    }
  | {
      type: "start";
      model: string;
      streaming: true;
      api_format: LLMApiFormat;
    }
  | {
      type: "delta";
      model: string;
      delta: string;
    }
  | ({ type: "done" | "error" } & QuickVerifyProviderResult);

export interface DetectProviderProtocolsRequest {
  provider: LLMProviderKind;
  base_url?: string | null;
  api_key?: string | null;
  proxy_id?: number | null;
  pid?: number | null;
  model?: string | null;
  /** 阶段 B：可选自然提示词；不传用稳定默认 */
  system_prompt?: string | null;
  message?: string | null;
}

export interface ProtocolProbeResult {
  ok: boolean;
  status_code?: number | null;
  latency_ms: number;
  error?: string | null;
  /** 阶段 B：本次探测所用客户端身份 */
  client_identity_profile?: string | null;
  /** 探测阶段：network / credentials / protocol / identity */
  stage?: string | null;
  /** 结构化错误分类 */
  error_category?: string | null;
  /** 脱敏修复建议 */
  suggestion?: string | null;
}

/** 某协议下按身份顺序的单次身份尝试结果 */
export interface ProtocolIdentityAttempt {
  api_format: string;
  client_identity_profile: string;
  ok: boolean;
  status_code?: number | null;
  latency_ms: number;
  error_category?: string | null;
  error?: string | null;
  suggestion?: string | null;
}

export interface DetectProviderProtocolsResponse {
  chat_completions: ProtocolProbeResult;
  responses: ProtocolProbeResult;
  anthropic_messages: ProtocolProbeResult;
  models: ProtocolProbeResult;
  recommended_api_format?: LLMApiFormat | string | null;
  /** 阶段 B：推荐客户端身份 */
  recommended_client_identity_profile?: LLMClientIdentityProfile | string | null;
  /** 阶段 B：每协议身份尝试列表 */
  identity_attempts?: ProtocolIdentityAttempt[];
  recommended_web_search_api_format: LLMWebSearchApiFormat | string;
  note?: string | null;
}

/** ``POST /api/commands/llm-providers/{pid}/test-model`` 入参 */
export interface TestModelRequest {
  model: string;
}

/** ``POST /api/commands/llm-providers/{pid}/test-model`` 出参 */
export interface TestModelResponse {
  ok: boolean;
  /** 总耗时（毫秒） */
  latency_ms: number;
  /** API 实际返回的 model 名（可能带日期后缀） */
  model?: string | null;
  /** 返回 text 的前 80 字符；让用户一眼看出"模型确实回话了" */
  preview?: string | null;
  /** 失败时的错误消息（已脱敏） */
  error?: string | null;
}

export interface ChatTestTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatTestModelsRequest {
  models: string[];
  message: string;
  history?: ChatTestTurn[];
  history_by_model?: Record<string, ChatTestTurn[]>;
  system_prompt?: string;
  max_tokens?: number;
  timeout_seconds?: number;
  api_format_override?: LLMApiFormat | null;
  client_identity_profile_override?: LLMClientIdentityProfile | null;
}

export interface ChatTestModelResult {
  ok: boolean;
  requested_model: string;
  model?: string | null;
  latency_ms: number;
  response?: string | null;
  preview?: string | null;
  input_tokens: number;
  output_tokens: number;
  empty_response: boolean;
  error?: string | null;
  client_identity_profile?: string | null;
  effective_api_format?: string | null;
  streaming?: boolean;
  stream_fallback?: boolean;
}

export interface ChatTestModelsResponse {
  provider_id: number;
  provider_name: string;
  results: ChatTestModelResult[];
}

// ===== 0.57.0 阶段 C：全量已启用模型测活 =====
export interface FullLivenessPreviewRequest {
  max_tokens?: number;
  global_concurrency?: number;
  only_provider_ids?: number[] | null;
}

export interface LivenessProviderPlan {
  provider_id: number;
  provider_name: string;
  enabled_models: string[];
  executable: boolean;
  skipped_reason?: string | null;
}

export interface FullLivenessPreviewResponse {
  provider_total: number;
  executable_provider_total: number;
  enabled_model_total: number;
  task_total: number;
  max_tokens: number;
  max_output_tokens: number;
  global_concurrency: number;
  provider_concurrency: number;
  needs_confirmation: boolean;
  providers: LivenessProviderPlan[];
}

export interface FullLivenessRunRequest {
  system_prompt?: string;
  message?: string;
  max_tokens?: number;
  timeout_seconds?: number;
  global_concurrency?: number;
  confirm_large_run?: boolean;
  only_provider_ids?: number[] | null;
  only_models?: string[] | null;
}

export interface LivenessResultItem {
  provider_id: number;
  provider_name: string;
  model_id: string;
  status: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  preview?: string | null;
  error?: string | null;
  error_category?: string | null;
  suggestion?: string | null;
  client_identity_profile?: string | null;
  effective_api_format?: string | null;
  skipped: boolean;
}

export interface FullLivenessRunResponse {
  run_id: string;
  status: "queued" | "running" | "completed" | "cancelled";
  task_total: number;
  completed: number;
  healthy: number;
  failed: number;
  skipped: number;
  cancelled: number;
  results: LivenessResultItem[];
  error?: string | null;
}

export interface FullLivenessRunStartResponse {
  run_id: string;
  status: "queued" | "running";
  task_total: number;
}

// ===== Sprint4 #2C =====
export type SchedulerKind = "cron" | "once" | "interval";
export type SchedulerActionType = "send_message" | "run_command" | "call_llm";

export interface SchedulerActionConfig {
  type: SchedulerActionType;
  target_chat_id?: number | string;
  target_chat_id_resolved?: number;
  target_chat_resolved_ref?: string;
  text?: string;
  command?: string;
  provider_id?: number;
  model?: string;
  prompt?: string;
  system_prompt?: string;
  max_tokens?: number;
  /** 触发后多少秒自动删除发送的消息，0 或留空 = 不删除，上限 3600 */
  delete_after?: number | null;
}

export interface SchedulerRuleConfig {
  kind: SchedulerKind;
  cron?: string;
  fire_at?: string;
  interval_sec?: number;
  action: SchedulerActionConfig;
  enabled?: boolean;
  next_fire?: string | null;
  last_fire?: string | null;
  last_result?: "ok" | "error" | string;
  last_error?: string | null;
}

export interface SchedulerFeatureConfig {
  allowed_command_whitelist?: string[];
}

// ===== Sprint4 #2D =====
export interface NotifyBotOut {
  id: number;
  name: string;
  default_chat_id: number;
  enabled: boolean;
  has_token: boolean;
  credential_source: "direct" | "account_bot";
  source_account_id?: number | null;
  created_at: string;
  updated_at: string;
}

export interface NotifyBotCreate {
  name: string;
  bot_token?: string | null;
  source_account_id?: number | null;
  default_chat_id: number;
  enabled?: boolean;
}

export interface NotifyBotUpdate {
  name?: string;
  default_chat_id?: number;
  enabled?: boolean;
  bot_token?: string;
  clear_token?: boolean;
  source_account_id?: number | null;
}

export interface NotifyBotTestRequest {
  text?: string;
}

// ==================== Patch 0.4.1 ====================
// 内置命令的元数据（只读）。前端在「自定义命令模板」编辑器里展示，
// 让用户知道哪些 name / alias 已被占用，避免起名撞车。
export interface BuiltinCommandItem {
  name: string;
  aliases: string[];
  doc: string;
}

// ==================== Patch 0.4.2 ====================
// GET /api/system/version 响应（public 端点，无鉴权）。
// 前端启动 + 每 60s 拉一次，对比 lib/version.ts 的 APP_VERSION；
// 不一致就在 GlobalAlertBar 弹红条提示「前后端版本不一致，请 make restart」。
export interface BackendVersionInfo {
  version: string;
  stage: string | null;
}

// ===================== 检查更新 =====================
export interface CheckUpdateResult {
  has_update: boolean;
  /** false = 当前环境无法在进程内自动检查（容器内无 updater / 无工作树）；
   *  此时 has_update 不代表真实结论，应展示"无法自动检查"。 */
  can_check?: boolean | null;
  current_commit: string | null;
  remote_commit: string | null;
  ahead: number;
  remote?: string | null;
  branch?: string | null;
  changed_files: string[];
  runtime_mode?: string | null;
  update_executor?: string | null;
  action_required?:
    | "none"
    | "docs_only"
    | "frontend"
    | "backend"
    | "mixed"
    | "updater"
    | "full_update"
    | "manual"
    | "unsupported"
    | "restart"
    | null;
  plan_label?: string | null;
  plan_detail?: string | null;
  components?: string[] | null;
  services?: string[] | null;
  requires_full_update?: boolean | null;
  requires_backup?: boolean | null;
  requires_migration?: boolean | null;
  can_apply?: boolean | null;
  manual_command?: string | null;
  error: string | null;
}
export interface PullUpdateResult {
  success: boolean;
  new_commit: string | null;
  summary: string | null;
  job_id?: string | null;
  status?: string | null;
  remote?: string | null;
  branch?: string | null;
  runtime_mode?: string | null;
  update_executor?: string | null;
  action_required?:
    | "none"
    | "docs_only"
    | "frontend"
    | "backend"
    | "mixed"
    | "updater"
    | "full_update"
    | "manual"
    | "unsupported"
    | "restart"
    | null;
  plan_label?: string | null;
  plan_detail?: string | null;
  components?: string[] | null;
  services?: string[] | null;
  requires_full_update?: boolean | null;
  requires_backup?: boolean | null;
  requires_migration?: boolean | null;
  can_apply?: boolean | null;
  manual_command?: string | null;
  error: string | null;
}
export interface RestartResult {
  success: boolean;
  error: string | null;
}

export interface UpdateJobStatus {
  ok: boolean;
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "unsupported" | "unknown" | string;
  created_at?: number | null;
  started_at?: number | null;
  finished_at?: number | null;
  returncode?: number | null;
  remote?: string | null;
  branch?: string | null;
  new_commit?: string | null;
  summary?: string | null;
  error?: string | null;
  progress?: number | null;
  phase?: string | null;
  detail?: string | null;
  logs: string[];
  plan?: Record<string, unknown> | null;
}

export interface UpdateTargetOptions {
  ok: boolean;
  remotes: string[];
  branches: string[];
  remote?: string | null;
  error?: string | null;
}

// ═══════════════ 客户端身份 UA 版本配置（0.57.0 收口） ═══════════════
export interface ClientIdentityVersionItem {
  key: string;
  label: string;
  current: string;
  default: string;
  registry?: string | null;
  detectable: boolean;
}

export interface ClientIdentityVersionsResponse {
  items: ClientIdentityVersionItem[];
}

export interface ClientIdentityVersionDetectItem {
  key: string;
  current: string;
  latest?: string | null;
  up_to_date?: boolean | null;
  error?: string | null;
}

export interface ClientIdentityVersionDetectResponse {
  items: ClientIdentityVersionDetectItem[];
}

export interface ClientIdentityVersionsUpdateRequest {
  overrides: Record<string, string>;
}
