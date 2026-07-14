import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowDown,
  ArrowUp,
  Bell,
  Bot,
  ChevronRight,
  Copy,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Search,
  Send,
  ShieldCheck,
  Trash2,
  UserPlus,
} from "lucide-react";
import { toast } from "sonner";

import {
  type ConfigField,
  type ConfigSchema,
  ConfigScopeSection,
} from "@/components/plugin/ConfigDialog";
import { queryKeys } from "@/lib/queryKeys";
import { TelegramHtmlPreview } from "@/components/TelegramHtmlPreview";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/misc";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { SignalPill } from "@/components/ui/status";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  listAccountFeatures,
} from "@/api/accounts";
import {
  createAccountBotUser,
  deleteAccountBotUser,
  getAccountBot,
  getInteractionBotConfig,
  listAccountBotUsers,
  restartAccountBotRuntime,
  saveInteractionBotComposite,
  testAccountBot,
  updateAccountBot,
  updateAccountBotUser,
  updateInteractionBotConfig,
} from "@/api/accountBots";
import { getFeatureMatrix } from "@/api/features";
import { listIgnoredPeers } from "@/api/ignored_peers";
import type {
  AccountBotInteractionConfig,
  AccountBotInteractionConfigUpdate,
  AccountBotInteractionRule,
  FeatureInteractionEntry,
  AccountBotRemotePluginPolicy,
  AccountBotRole,
  AccountBotUserCreate,
  IgnoredPeer,
} from "@/api/types";
import { getErrMsg } from "@/lib/api";
import { cn, formatDateTime } from "@/lib/utils";
import {
  pluginContractRiskWarnings,
  pluginEventLabel,
  pluginEventSubscriptionLabels,
  pluginOperationalCapabilityLabels,
  type PluginCapabilities,
  type PluginEventSubscription,
} from "@/types/pluginContract";

const MASKED_SECRET_PLACEHOLDER = "••••••••••••••••";

function localizeBotRuntimeError(message: string): string {
  if (message.includes("terminated by other getUpdates request") || message.includes("Conflict:")) {
    return "交互 Bot polling 冲突：同一个 Bbot token 正在被另一个实例监听。请确认它没有被其他账号、本地/Docker/VPS 中的另一套 TelePilot，或其他程序同时使用。";
  }
  return message;
}

const ROLE_META: Record<AccountBotRole, { label: string; desc: string }> = {
  viewer: { label: "viewer", desc: "只读查看" },
  operator: { label: "operator", desc: "启停常用功能" },
  admin: { label: "admin", desc: "危险操作确认" },
};

const HELP_PREVIEW = `/start  打开主菜单
/help 查看完整命令与角色说明
/status 查看账号、worker 与最近错误
/features 查看并启停账号功能
/commands 查看并启停自定义指令模板
/plugins 查看插件入口
  /plugins install <git-url> 安装远程插件（admin + 二次确认）
  /plugins update <name> 更新远程插件（admin + 二次确认）
  /plugins uninstall <name> 卸载远程插件（admin + 二次确认）
/rules 查看规则，scheduler 规则可手动执行
/logs 查看最近运行日志
/pause /resume 暂停或恢复账号
/restart 重启账号 worker（admin + 二次确认）`;

const REMOTE_POLICY_OPTIONS: Array<{
  key: keyof AccountBotRemotePluginPolicy;
  label: string;
  description: string;
}> = [
  {
    key: "enabled",
    label: "启用远程插件管理",
    description: "总开关。关闭后，Telegram 内所有远程插件高风险操作都会被拦截。",
  },
  {
    key: "install",
    label: "允许安装插件",
    description: "允许 admin 使用 /plugins install <git-url> 发起远程安装。",
  },
  {
    key: "update",
    label: "允许更新插件",
    description: "允许 admin 使用 /plugins update <name> 更新已安装插件。",
  },
  {
    key: "uninstall",
    label: "允许卸载插件",
    description: "允许 admin 使用 /plugins uninstall <name> 卸载插件。",
  },
  {
    key: "enable_disable",
    label: "允许启停第三方插件",
    description: "允许 admin 在 Telegram 内启用或停用非内置插件。",
  },
];

const DEFAULT_REMOTE_POLICY: AccountBotRemotePluginPolicy = {
  enabled: false,
  install: false,
  update: false,
  uninstall: false,
  enable_disable: false,
};

const DEFAULT_INTERACTION_DISABLED_MESSAGE = "本条互动规则已暂停，暂时不能开启。";
const DEFAULT_INTERACTION_RESPONSE_TEMPLATE = "已收到 {payer_name} 给 {receiver_name} 的转账 {amount}，互动流程已准备就绪。";
const DEFAULT_INTERACTION_MODULE_START_TEXT = "正在启动{规则名称}";
const DEFAULT_MATH10_START_KEYWORDS = "发十以内算数\n十以内算数\n开算数题";
const DEFAULT_INTERACTION_QUERY_COMMANDS = "。玩法\n。联动玩法";
const DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE = "<b>当前可用联动玩法</b>\n{items}";
const DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE = "{index}. <b>{name}</b>\n触发方式：{trigger}";
const DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE = "当前群暂无开启中的联动玩法。";
const RULE_CONTROLLED_MODULE_CONFIG_KEYS = new Set(["prize", "valid_seconds"]);
const DEFAULT_TRANSFER_NOTICE_TEMPLATE = [
  '<pre><code class="language-转账成功">付款人：{payer_name}',
  "{payer_user_id_line}",
  "收款人：{receiver_name}",
  "金额：{amount}",
  "{receiver_user_id_line}</code></pre>",
].join("\n");
const DEFAULT_DEBIT_NOTICE_TEMPLATE = [
  '<pre><code class="language-扣减成功">{payer_name} 扣减 {amount} 蝌蚪',
  "{receiver_name} 接收 {amount} 蝌蚪</code></pre>",
].join("\n");
const TRANSFER_NOTICE_TEMPLATE_SAMPLE_VALUES: Record<string, string> = {
  payer_name: "Alice",
  payer_user_id: "10001",
  payer_user_id_line: "付款人ID：10001",
  receiver_name: "Bob",
  receiver_user_id: "10002",
  receiver_user_id_line: "收款人ID：10002",
  amount: "88.00",
};

const DEFAULT_INTERACTION_BOT: AccountBotInteractionConfig = {
  enabled: false,
  chat_id: null,
  chat_ids: [],
  interaction_bot_token: null,
  clear_interaction_bot_token: false,
  has_interaction_bot_token: false,
  interaction_bot_username: null,
  interaction_bot_id: null,
  interaction_running: false,
  interaction_runtime_status: "stopped",
  interaction_last_update_id: null,
  interaction_last_error: null,
  interaction_debug: {
    payload: {},
    actions: [],
    guarded_actions: [],
    warnings: [],
  },
  trusted_bot_id: null,
  trusted_bot_ids: [],
  transfer_bot_id: null,
  transfer_bot_token: null,
  clear_transfer_bot_token: false,
  has_transfer_bot_token: false,
  trigger_mode: "payment",
  trigger_text: "转账成功",
  trigger_texts: ["转账成功"],
  module_start_keywords: [],
  receiver_user_id: null,
  receiver_text: "",
  amount: null,
  amount_match_mode: "eq",
  action: "notice",
  math_prize: 123,
  module_key: null,
  module_action: null,
  module_config: {},
  module_prize: null,
  module_start_text: null,
  user_cooldown_seconds: null,
  daily_limit_per_user: null,
  open_commands: [],
  close_commands: [],
  status_commands: [],
  query_commands: parseTextLines(DEFAULT_INTERACTION_QUERY_COMMANDS),
  query_response_template: DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE,
  query_item_template: DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE,
  query_empty_message: DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE,
  disabled_message: DEFAULT_INTERACTION_DISABLED_MESSAGE,
  valid_seconds: 600,
  concurrency: "chat",
  response_template: DEFAULT_INTERACTION_RESPONSE_TEMPLATE,
  transfer_notice_template: DEFAULT_TRANSFER_NOTICE_TEMPLATE,
  debit_notice_template: DEFAULT_DEBIT_NOTICE_TEMPLATE,
  rules: [],
};

type InteractionRuleForm = {
  id: string;
  name: string;
  enabled: boolean;
  chatIds: string;
  triggerMode: NonNullable<AccountBotInteractionRule["trigger_mode"]>;
  triggerTexts: string;
  moduleStartKeywords: string;
  receiverUserId: string;
  receiverText: string;
  amount: string;
  amountMatchMode: NonNullable<AccountBotInteractionRule["amount_match_mode"]>;
  action: AccountBotInteractionRule["action"];
  mathPrize: string;
  moduleKey: string;
  moduleAction: string;
  moduleSessionScope: NonNullable<AccountBotInteractionRule["module_session_scope"]>;
  participantPolicy: NonNullable<AccountBotInteractionRule["participant_policy"]>;
  moduleConfig: Record<string, unknown>;
  moduleStartText: string;
  userCooldownSeconds: string;
  dailyLimitPerUser: string;
  openCommands: string;
  closeCommands: string;
  statusCommands: string;
  disabledMessage: string;
  validSeconds: string;
  concurrency: NonNullable<AccountBotInteractionRule["concurrency"]>;
  responseTemplate: string;
};

type InteractionEntryOption = {
  featureKey: string;
  featureName: string;
  featureVersion?: string | null;
  featureConfig: Record<string, unknown>;
  featureConfigSchema?: Record<string, unknown> | null;
  featureUsage?: string | null;
  eventSubscriptions?: PluginEventSubscription[];
  capabilities?: PluginCapabilities;
  permissions?: string[];
  lintWarnings?: string[];
  entry: FeatureInteractionEntry;
  value: string;
  label: string;
};

type ResolvedInteractionEntry = InteractionEntryOption & {
  inferred: boolean;
};

type InteractionEntrySchema = ConfigSchema;

const PEER_KIND_LABEL: Record<string, string> = {
  private: "私聊",
  group: "普通群",
  supergroup: "超级群",
  channel: "频道",
};

function peerKindLabel(kind: string): string {
  return PEER_KIND_LABEL[kind] || kind || "会话";
}

function allowedPeerDisplayName(peer: IgnoredPeer): string {
  return peer.peer_label?.trim() || `${peerKindLabel(String(peer.peer_kind))} ${peer.peer_id}`;
}

function chatIdTextItems(value: string): string[] {
  return parseTextLines(value);
}

function RuleEditorSection({
  step,
  title,
  description,
  children,
}: {
  step: string;
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <details className="group rounded-lg border bg-muted/20">
      <summary className="flex cursor-pointer list-none items-start justify-between gap-3 p-3 [&::-webkit-details-marker]:hidden">
        <div className="flex min-w-0 items-start gap-3">
        <div className="grid h-7 w-7 shrink-0 place-items-center rounded-md border bg-background text-xs font-semibold text-muted-foreground">
          {step}
        </div>
        <div className="min-w-0">
          <div className="text-sm font-medium">{title}</div>
          {description ? (
            <div className="mt-0.5 text-xs text-muted-foreground">{description}</div>
          ) : null}
        </div>
        </div>
        <ChevronRight className="mt-1 h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
      </summary>
      <div className="space-y-3 border-t p-3">{children}</div>
    </details>
  );
}

function AllowedPeerMultiSelect({
  aid,
  peers,
  selectedText,
  loading,
  onChange,
}: {
  aid: number;
  peers: IgnoredPeer[];
  selectedText: string;
  loading: boolean;
  onChange: (value: string) => void;
}) {
  const selected = new Set(chatIdTextItems(selectedText));
  const knownPeerIds = new Set(peers.map((peer) => String(peer.peer_id)));
  const unknownSelected = chatIdTextItems(selectedText).filter((id) => !knownPeerIds.has(id));
  const removeSelectedId = (id: string) => {
    onChange(chatIdTextItems(selectedText).filter((item) => item !== id).join("\n"));
  };
  const togglePeer = (peer: IgnoredPeer) => {
    const id = String(peer.peer_id);
    const next = chatIdTextItems(selectedText);
    const existingIndex = next.indexOf(id);
    if (existingIndex >= 0) {
      next.splice(existingIndex, 1);
    } else {
      next.push(id);
    }
    onChange(next.join("\n"));
  };

  if (loading) {
    return (
      <div className="flex h-10 items-center nested-surface-item border bg-background px-2 text-xs text-muted-foreground">
        <Spinner className="mr-2 h-3.5 w-3.5 text-primary" />
        正在读取已允许会话
      </div>
    );
  }

  if (peers.length === 0) {
    return (
      <div className="nested-surface-item border border-dashed bg-background px-3 py-2 text-xs leading-5 text-muted-foreground">
        暂无已允许会话。没有找到想选择的会话时，请先去{" "}
        <Link to={`/accounts/${aid}?tab=ignored`} className="font-medium text-primary hover:underline">
          账号详情页的允许会话
        </Link>{" "}
        添加。
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>从已允许会话选择</span>
        <span>{selected.size} 个已选</span>
      </div>
      <div className="nested-surface nested-surface-inset-2 flex max-h-24 flex-wrap gap-1.5 overflow-y-auto border bg-background">
        {peers.map((peer) => {
          const id = String(peer.peer_id);
          const active = selected.has(id);
          return (
            <button
              key={peer.id}
              type="button"
              className={cn(
                "nested-surface-item min-w-0 max-w-full border px-2 py-1.5 text-left text-xs transition-colors",
                active
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border bg-muted/30 text-muted-foreground [@media(hover:hover)]:hover:border-primary/40 [@media(hover:hover)]:hover:text-foreground",
              )}
              title={`${allowedPeerDisplayName(peer)} · ${id}`}
              onClick={() => togglePeer(peer)}
            >
              <span className="block max-w-[210px] truncate font-medium">
                {allowedPeerDisplayName(peer)}
              </span>
              <span className="mt-0.5 block font-mono text-[11px] opacity-75">
                {peerKindLabel(String(peer.peer_kind))} · {id}
              </span>
            </button>
          );
        })}
      </div>
      {unknownSelected.length > 0 ? (
        <div className="nested-surface-item flex flex-wrap gap-1.5 border border-warning/30 bg-warning/10 px-2 py-1.5 text-xs text-warning">
          {unknownSelected.map((id) => (
            <button
              key={id}
              type="button"
              className="rounded border border-warning/40 px-1.5 py-0.5 font-mono hover:bg-warning/15"
              title="从本规则移除这个未在允许会话中的 Chat ID"
              onClick={() => removeSelectedId(id)}
            >
              {id} ×
            </button>
          ))}
        </div>
      ) : null}
      <div className="text-xs leading-5 text-muted-foreground">
        没有找到想选择的会话？去{" "}
        <Link to={`/accounts/${aid}?tab=ignored`} className="font-medium text-primary hover:underline">
          账号详情页的允许会话
        </Link>{" "}
        添加后再回来选择。
      </div>
    </div>
  );
}

function InteractionContractBlock({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  const cleanItems = [...new Set(items.map((item) => item.trim()).filter(Boolean))];
  return (
    <div className="nested-surface-item min-w-0 border bg-muted/20 px-2 py-1.5 text-xs">
      <div className="mb-1 font-medium text-muted-foreground">{title}</div>
      {cleanItems.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {cleanItems.slice(0, 6).map((item) => (
            <Badge key={item} variant="secondary" className="max-w-full break-all px-1.5 text-[11px]">
              {item}
            </Badge>
          ))}
          {cleanItems.length > 6 ? <Badge variant="outline">+{cleanItems.length - 6}</Badge> : null}
        </div>
      ) : (
        <div className="text-muted-foreground">{empty}</div>
      )}
    </div>
  );
}

function interactionProfileLabel(profile?: string | null): string | null {
  if (profile === "session_game") return "群局抢答";
  if (profile === "challenge_game") return "对战玩法";
  if (profile === "reward_pool") return "奖池玩法";
  if (profile === "utility_trigger") return "工具触发";
  return null;
}

function interactionDispatchLabels(entry: FeatureInteractionEntry): string[] {
  const modes = Array.isArray(entry.dispatch_modes) ? entry.dispatch_modes : [];
  const labels: string[] = [];
  if (modes.includes("admin_command")) labels.push("管理员命令");
  if (modes.includes("public_keyword")) labels.push("群内玩法");
  if (!labels.length) {
    if (entry.launch_mode === "direct") return ["管理员命令"];
    if (entry.launch_mode === "hybrid") return ["管理员命令", "群内玩法"];
    return ["群内玩法"];
  }
  return labels;
}

function interactionChannelLabel(channel?: string | string[] | { prefer?: string[]; fallback?: boolean } | null): string {
  if (Array.isArray(channel)) {
    return channel.map((item) => interactionChannelLabel(item)).join(" / ");
  }
  if (typeof channel === "object" && channel !== null) {
    const preferred = Array.isArray(channel.prefer) ? channel.prefer : [];
    const label = preferred.length ? preferred.map((item) => interactionChannelLabel(item)).join(" / ") : "自动";
    return channel.fallback === false ? label : `${label} 回退`;
  }
  if (channel === "userbot_reply") return "人形";
  if (channel === "userbot") return "人形";
  if (channel === "interaction_bot") return "交互 Bot";
  if (channel === "bot") return "交互 Bot";
  if (channel === "bbot_notice") return "已移除通道";
  if (channel === "notice") return "已移除通道";
  if (channel === "auto") return "自动";
  return typeof channel === "string" && channel ? channel : "自动";
}

const INTERACTION_PROFILE_ORDER: Array<NonNullable<FeatureInteractionEntry["interaction_profile"]>> = [
  "session_game",
  "challenge_game",
  "reward_pool",
  "utility_trigger",
];

function defaultRuleForm(index = 0): InteractionRuleForm {
  const suffix = index + 1;
  return {
    id: `rule-${Date.now()}-${suffix}`,
    name: suffix === 1 ? "默认规则" : `规则 ${suffix}`,
    enabled: true,
    chatIds: "",
    triggerMode: "payment",
    triggerTexts: DEFAULT_INTERACTION_BOT.trigger_text,
    moduleStartKeywords: "",
    receiverUserId: "",
    receiverText: "",
    amount: "",
    amountMatchMode: "eq",
    action: "notice",
    mathPrize: "123",
    moduleKey: "game24",
    moduleAction: "",
    moduleSessionScope: "chat",
    participantPolicy: "open_race",
    moduleConfig: {},
    moduleStartText: DEFAULT_INTERACTION_MODULE_START_TEXT,
    userCooldownSeconds: "",
    dailyLimitPerUser: "",
    openCommands: "",
    closeCommands: "",
    statusCommands: "",
    disabledMessage: DEFAULT_INTERACTION_BOT.disabled_message || "",
    validSeconds: "600",
    concurrency: "chat",
    responseTemplate: DEFAULT_INTERACTION_BOT.response_template,
  };
}

function parseOptionalInt(value: string, label: string): number | null {
  const text = value.trim();
  if (!text) return null;
  if (!/^-?\d+$/.test(text)) {
    throw new Error(`${label} 必须是整数`);
  }
  const parsed = Number(text);
  if (!Number.isSafeInteger(parsed)) {
    throw new Error(`${label} 超出安全整数范围`);
  }
  return parsed;
}

function parseOptionalPositiveInt(value: string, label: string): number | null {
  const parsed = parseOptionalInt(value, label);
  if (parsed == null) return null;
  if (parsed <= 0) {
    throw new Error(`${label} 必须大于 0`);
  }
  return parsed;
}

function parseOptionalUserId(value: string, label: string): number | null {
  return parseOptionalPositiveInt(value, label);
}

function parseOptionalUserIds(value: string, label: string): number[] {
  const ids = value
    .split(/[\n,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const out: number[] = [];
  for (const id of ids) {
    const parsed = parseOptionalUserId(id, label);
    if (parsed == null) continue;
    if (!out.includes(parsed)) out.push(parsed);
  }
  return out;
}

function parseIntLines(value: string, label: string): number[] {
  const lines = value
    .split(/[\n,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const out: number[] = [];
  for (const line of lines) {
    const parsed = parseOptionalInt(line, label);
    if (parsed == null) continue;
    if (!out.includes(parsed)) out.push(parsed);
  }
  return out;
}

function parseTextLines(value: string): string[] {
  const out: string[] = [];
  for (const line of value.split(/[\n,，]+/)) {
    const item = line.trim();
    if (item && !out.includes(item)) out.push(item);
  }
  return out;
}

function renderTransferNoticeTemplatePreview(template: string): string {
  const source = template.trim() || DEFAULT_TRANSFER_NOTICE_TEMPLATE;
  return source.replace(/\{(\w+)\}/g, (match, key: string) => (
    TRANSFER_NOTICE_TEMPLATE_SAMPLE_VALUES[key] ?? match
  ));
}

function renderTemplateVariables(template: string, values: Record<string, string>): string {
  return template.replace(/\{([\w\u4e00-\u9fa5]+)\}/g, (match, key: string) => values[key] ?? match);
}

function renderInteractionQueryTemplatePreview(template: string, itemTemplate: string): string {
  const source = template.trim() || DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE;
  const itemSource = itemTemplate.trim() || DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE;
  const sampleItems = [
    {
      index: "1",
      name: "九宫格",
      trigger: "转账或关键词；关键词：<code>。ct num=数字</code>；转账通知",
      kind: "玩法 <code>dice_grid_hunt</code>",
      limit: "限时 <code>600</code> 秒",
      module_key: "dice_grid_hunt",
      module_action: "start_dice_grid_hunt",
      chat_id: "-1001234567890",
    },
    {
      index: "2",
      name: "置顶促销",
      trigger: "关键词；关键词：<code>促销 id=12345</code>",
      kind: "玩法 <code>pt_promote</code>",
      limit: "每用户 CD <code>12h</code>",
      module_key: "pt_promote",
      module_action: "promote_torrent",
      chat_id: "-1001234567890",
    },
  ].map((values) => renderTemplateVariables(itemSource, values)).join("\n");
  const sampleValues: Record<string, string> = {
    items: sampleItems,
    count: "2",
    closed_count: "0",
    chat_id: "-1001234567890",
  };
  return renderTemplateVariables(source, sampleValues);
}

function isInteractionEntrySchema(schema: unknown): schema is InteractionEntrySchema {
  const candidate = schema as Record<string, unknown> | null | undefined;
  return Boolean(
    candidate
    && candidate.type === "object"
    && candidate.properties
    && typeof candidate.properties === "object"
    && !Array.isArray(candidate.properties),
  );
}

function interactionSchemaProperties(entry?: FeatureInteractionEntry): Record<string, ConfigField> {
  if (!isInteractionEntrySchema(entry?.input_schema)) return {};
  return entry.input_schema.properties;
}

function controlledEntryFieldKeys(entry?: FeatureInteractionEntry): Set<string> {
  const keys = new Set<string>(RULE_CONTROLLED_MODULE_CONFIG_KEYS);
  const properties = interactionSchemaProperties(entry);
  if (!("valid_seconds" in properties)) {
    keys.delete("valid_seconds");
  }
  return keys;
}

function extraEntryConfigFields(entry?: FeatureInteractionEntry): Array<[string, ConfigField]> {
  const controlledKeys = controlledEntryFieldKeys(entry);
  return Object.entries(interactionSchemaProperties(entry)).filter(([key]) => !controlledKeys.has(key));
}

function configSchemaProperties(schema?: Record<string, unknown> | null): Record<string, ConfigField> {
  const candidate = schema as unknown as ConfigSchema | null | undefined;
  if (!candidate || candidate.type !== "object" || !candidate.properties || Array.isArray(candidate.properties)) {
    return {};
  }
  return candidate.properties;
}

function sanitizeFeatureConfigForSchema(
  config: Record<string, unknown>,
  schema?: Record<string, unknown> | null,
): { config: Record<string, unknown>; changed: boolean } {
  const candidate = schema as Record<string, unknown> | null | undefined;
  if (!candidate || candidate.type !== "object" || candidate.additionalProperties !== false) {
    return { config: { ...config }, changed: false };
  }
  const allowed = new Set(Object.keys(configSchemaProperties(schema)));
  const next = Object.fromEntries(Object.entries(config).filter(([key]) => allowed.has(key)));
  return { config: next, changed: Object.keys(next).length !== Object.keys(config).length };
}

function hasOwnConfigValue(config: Record<string, unknown> | undefined, key: string): boolean {
  return Boolean(config && Object.prototype.hasOwnProperty.call(config, key));
}

function interactionSchemaDefaults(entry?: FeatureInteractionEntry): Record<string, unknown> {
  const properties = interactionSchemaProperties(entry);
  const config: Record<string, unknown> = {};
  for (const [key, rawField] of Object.entries(properties)) {
    if (controlledEntryFieldKeys(entry).has(key)) continue;
    if ("default" in rawField) {
      config[key] = rawField.default;
    }
  }
  return config;
}

function stripControlledEntryConfig(config: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(config).filter(([key]) => !RULE_CONTROLLED_MODULE_CONFIG_KEYS.has(key)),
  );
}

function normalizeEntryConfigValue(field: ConfigField, value: unknown): unknown {
  if (value == null) return value;
  if (field.type === "integer" || field.type === "number") {
    if (typeof value === "number") return value;
    const text = String(value).trim();
    if (!text) return null;
    const parsed = Number(text);
    return Number.isFinite(parsed) ? parsed : value;
  }
  if (field.type === "boolean") {
    if (typeof value === "boolean") return value;
    const text = String(value).trim().toLowerCase();
    if (!text) return false;
    return ["1", "true", "yes", "on"].includes(text);
  }
  if (field.type === "array") {
    if (Array.isArray(value)) return value;
    const text = String(value).trim();
    if (!text) return [];
    const parts = text.split(",").map((item) => item.trim()).filter(Boolean);
    if (field.items?.type === "integer" || field.items?.type === "number") {
      return parts.map((item) => Number(item)).filter((item) => Number.isFinite(item));
    }
    return parts;
  }
  return value;
}

function buildEntryConfigValues(
  entry: FeatureInteractionEntry | undefined,
  rawConfig: Record<string, unknown>,
  preferredConfig?: Record<string, unknown>,
): Record<string, unknown> {
  const properties = interactionSchemaProperties(entry);
  const values: Record<string, unknown> = {};
  for (const [key, field] of Object.entries(properties)) {
    if (controlledEntryFieldKeys(entry).has(key)) continue;
    const value = hasOwnConfigValue(preferredConfig, key)
      ? preferredConfig?.[key]
      : hasOwnConfigValue(rawConfig, key)
        ? rawConfig[key]
        : field.default ?? null;
    values[key] = normalizeEntryConfigValue(field, value);
  }
  return values;
}

function mergeEntryConfigValues(
  entry: FeatureInteractionEntry | undefined,
  values: Record<string, unknown>,
): Record<string, unknown> {
  const properties = interactionSchemaProperties(entry);
  const next: Record<string, unknown> = {};
  for (const [key, field] of Object.entries(properties)) {
    if (controlledEntryFieldKeys(entry).has(key)) continue;
    const normalized = normalizeEntryConfigValue(field, values[key]);
    if (normalized !== null && normalized !== undefined && normalized !== "") {
      next[key] = normalized;
    }
  }
  return next;
}

function defaultModuleConfigFromEntry(
  entry?: FeatureInteractionEntry,
  featureConfig?: Record<string, unknown>,
): Record<string, unknown> {
  return buildEntryConfigValues(entry, interactionSchemaDefaults(entry), featureConfig);
}

function sameConfigValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function buildFeatureConfigUpdateForRule(
  rule: InteractionRuleForm,
  interactionEntries: InteractionEntryOption[],
  baseConfig: Record<string, unknown>,
): { pluginKey: string; config: Record<string, unknown> } | null {
  if (rule.action !== "module") return null;
  const selection = resolveRuleModuleSelection(rule, interactionEntries);
  if (!selection) return null;
  const globalFields = configSchemaProperties(selection.featureConfigSchema);
  const fields = extraEntryConfigFields(selection.entry).filter(([key]) => key in globalFields);

  const sanitized = sanitizeFeatureConfigForSchema(baseConfig, selection.featureConfigSchema);
  const nextConfig = { ...sanitized.config };
  let changed = sanitized.changed;
  if (fields.length <= 0) {
    return changed ? { pluginKey: selection.featureKey, config: nextConfig } : null;
  }
  for (const [key, field] of fields) {
    const normalized = normalizeEntryConfigValue(field, rule.moduleConfig[key]);
    if (normalized === null || normalized === undefined || normalized === "") {
      if (hasOwnConfigValue(nextConfig, key)) {
        delete nextConfig[key];
        changed = true;
      }
      continue;
    }
    if (!sameConfigValue(nextConfig[key], normalized)) {
      nextConfig[key] = normalized;
      changed = true;
    }
  }

  return changed ? { pluginKey: selection.featureKey, config: nextConfig } : null;
}

function interactionEntryHasField(entry: FeatureInteractionEntry | undefined, key: string): boolean {
  return key in interactionSchemaProperties(entry);
}

function defaultInteractionEntryForModule(
  entries: InteractionEntryOption[],
  moduleKey: string,
): InteractionEntryOption | undefined {
  const matches = entries.filter((item) => item.featureKey === moduleKey);
  return matches.length === 1 ? matches[0] : undefined;
}

function inferParticipantPolicy(
  entry: FeatureInteractionEntry | undefined,
  sessionScope: AccountBotInteractionRule["module_session_scope"] | string | null | undefined,
): InteractionRuleForm["participantPolicy"] {
  const entryPolicy = String(entry?.participant_policy || "").trim();
  if (entryPolicy === "solo_owner" || entryPolicy === "paid_pool" || entryPolicy === "notify_only") {
    return entryPolicy;
  }
  return sessionScope === "user" ? "solo_owner" : "open_race";
}

function filterInteractionEntries(
  entries: InteractionEntryOption[],
  profile: string,
): InteractionEntryOption[] {
  if (profile === "all") return entries;
  if (profile === "__ungrouped") {
    return entries.filter((item) => !item.entry.interaction_profile);
  }
  return entries.filter((item) => item.entry.interaction_profile === profile);
}

function uniqueIntValues(values: number[]): number[] {
  const out: number[] = [];
  for (const value of values) {
    if (!out.includes(value)) out.push(value);
  }
  return out;
}

function countDelimitedTextItems(value: string): number {
  return new Set(
    value
      .split(/[\n,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean),
  ).size;
}

function getRuleActionLabel(action: AccountBotInteractionRule["action"]): string {
  if (action === "module") return "启动插件";
  if (action === "math10") return "算数题";
  return "只发通知";
}

function getRuleTriggerModeLabel(mode: NonNullable<AccountBotInteractionRule["trigger_mode"]>): string {
  if (mode === "keyword") return "仅关键词";
  if (mode === "both") return "转账或关键词";
  return "仅转账通知";
}

function getRuleConcurrencyLabel(mode: NonNullable<AccountBotInteractionRule["concurrency"]>): string {
  if (mode === "user") return "按用户";
  if (mode === "none") return "不并发";
  return "按群聊";
}

function getModuleSessionScopeLabel(mode: NonNullable<AccountBotInteractionRule["module_session_scope"]>): string {
  if (mode === "user") return "按用户会话";
  if (mode === "none") return "不保存会话";
  return "按群会话";
}

function getParticipantPolicyLabel(mode: NonNullable<AccountBotInteractionRule["participant_policy"]>): string {
  if (mode === "solo_owner") return "仅付款/开局本人";
  if (mode === "paid_pool") return "仅付费玩家";
  if (mode === "notify_only") return "只通知";
  return "全群可参与";
}

function resolveRuleModuleSelection(
  rule: InteractionRuleForm,
  interactionEntries: InteractionEntryOption[],
): ResolvedInteractionEntry | undefined {
  const explicit = interactionEntries.find((item) =>
    item.featureKey === rule.moduleKey
    && item.entry.key === rule.moduleAction,
  );
  if (explicit) {
    return { ...explicit, inferred: false };
  }
  const inferred = defaultInteractionEntryForModule(interactionEntries, rule.moduleKey || "game24");
  if (!inferred) return undefined;
  return {
    ...inferred,
    inferred: true,
  };
}

function describeRuleModuleSelection(
  rule: InteractionRuleForm,
  selection?: ResolvedInteractionEntry,
  options?: { withVersion?: boolean },
): string {
  if (!selection) {
    return rule.moduleKey.trim() ? `插件 ${rule.moduleKey} / 入口未选` : "插件入口未选";
  }
  const entryLabel = selection.entry.title || selection.entry.label || selection.entry.key;
  const featureLabel = options?.withVersion
    ? `${selection.featureName} · ${formatInteractionFeatureVersion(selection.featureVersion)}`
    : selection.featureName;
  return selection.inferred || !rule.moduleAction.trim()
    ? `${featureLabel} / 自动推断 ${selection.entry.key}`
    : `${featureLabel} / ${entryLabel}`;
}

function formatInteractionFeatureVersion(version?: string | null): string {
  const value = (version || "").trim();
  if (!value) return "版本未知";
  return value.startsWith("v") ? value : `v${value}`;
}

function ruleFormFromRule(
  rule: AccountBotInteractionRule,
  index: number,
  fallbackChatIds: number[] = [],
  interactionEntries: InteractionEntryOption[] = [],
): InteractionRuleForm {
  const chatIds = rule.chat_ids?.length ? rule.chat_ids : fallbackChatIds;
  const prize = rule.action === "module" && rule.module_prize != null
    ? rule.module_prize
    : rule.math_prize || 123;
  const moduleKey = rule.module_key || "game24";
  const moduleAction = rule.module_action || "";
  const selectedEntry = interactionEntries.find((item) =>
    item.featureKey === moduleKey && item.entry.key === moduleAction,
  ) ?? defaultInteractionEntryForModule(interactionEntries, moduleKey);
  const moduleSessionScope = rule.module_session_scope || (
    selectedEntry?.entry.session_scope === "user" || selectedEntry?.entry.session_scope === "none"
      ? selectedEntry.entry.session_scope
      : "chat"
  );
  return {
    id: rule.id || `rule-${index + 1}`,
    name: rule.name || `规则 ${index + 1}`,
    enabled: rule.enabled !== false,
    chatIds: chatIds.join("\n"),
    triggerMode: rule.trigger_mode || "payment",
    triggerTexts: rule.trigger_texts?.length
      ? rule.trigger_texts.join("\n")
      : DEFAULT_INTERACTION_BOT.trigger_text,
    moduleStartKeywords: rule.module_start_keywords?.join("\n") || "",
    receiverUserId: rule.receiver_user_id == null ? "" : String(rule.receiver_user_id),
    receiverText: rule.receiver_text ?? "",
    amount: rule.amount == null ? "" : String(rule.amount),
    amountMatchMode: rule.amount_match_mode || "eq",
    action: rule.action || "notice",
    mathPrize: String(prize),
    moduleKey,
    moduleAction,
    moduleSessionScope,
    participantPolicy: rule.participant_policy || inferParticipantPolicy(selectedEntry?.entry, moduleSessionScope),
    moduleConfig: selectedEntry
      ? mergeEntryConfigValues(
          selectedEntry.entry,
          buildEntryConfigValues(
            selectedEntry.entry,
            stripControlledEntryConfig(rule.module_config ?? {}),
            selectedEntry.featureConfig,
          ),
        )
      : stripControlledEntryConfig(rule.module_config ?? {}),
    moduleStartText: rule.module_start_text ?? DEFAULT_INTERACTION_MODULE_START_TEXT,
    userCooldownSeconds: rule.user_cooldown_seconds ?? "",
    dailyLimitPerUser: rule.daily_limit_per_user == null ? "" : String(rule.daily_limit_per_user),
    openCommands: rule.open_commands?.join("\n") || "",
    closeCommands: rule.close_commands?.join("\n") || "",
    statusCommands: rule.status_commands?.join("\n") || "",
    disabledMessage: rule.disabled_message || DEFAULT_INTERACTION_BOT.disabled_message || "",
    validSeconds: String(rule.valid_seconds || 600),
    concurrency: rule.concurrency || "chat",
    responseTemplate: rule.response_template || DEFAULT_INTERACTION_BOT.response_template,
  };
}

function legacyRuleFromConfig(config: AccountBotInteractionConfig): AccountBotInteractionRule {
  return {
    id: "legacy-default",
    name: "默认规则",
    enabled: true,
    chat_ids: config.chat_ids?.length
      ? config.chat_ids
      : config.chat_id == null
        ? []
        : [config.chat_id],
    trigger_texts: config.trigger_texts?.length
      ? config.trigger_texts
      : [config.trigger_text || DEFAULT_INTERACTION_BOT.trigger_text],
    trigger_mode: config.trigger_mode || "payment",
    module_start_keywords: config.module_start_keywords ?? [],
    receiver_user_id: config.receiver_user_id ?? null,
    receiver_text: config.receiver_text ?? null,
    amount: config.amount ?? null,
    amount_match_mode: config.amount_match_mode || "eq",
    action: config.action || "notice",
    math_prize: config.math_prize || 123,
    module_key: config.module_key ?? null,
    module_action: config.module_action ?? null,
    module_session_scope: config.module_session_scope ?? null,
    participant_policy: config.participant_policy ?? null,
    module_prize: config.module_prize ?? null,
    module_start_text: config.module_start_text ?? null,
    user_cooldown_seconds: config.user_cooldown_seconds ?? null,
    daily_limit_per_user: config.daily_limit_per_user ?? null,
    open_commands: config.open_commands ?? [],
    close_commands: config.close_commands ?? [],
    status_commands: config.status_commands ?? [],
    disabled_message: config.disabled_message ?? DEFAULT_INTERACTION_BOT.disabled_message,
    valid_seconds: config.valid_seconds ?? 600,
    concurrency: config.concurrency ?? "chat",
    response_template: config.response_template || DEFAULT_INTERACTION_BOT.response_template,
  };
}

function ruleFromForm(
  form: InteractionRuleForm,
  index: number,
  interactionEntries: InteractionEntryOption[] = [],
): AccountBotInteractionRule {
  const triggerTexts = parseTextLines(form.triggerTexts);
  const name = form.name.trim() || `规则 ${index + 1}`;
  const action = form.action === "module" ? "module" : form.action === "math10" ? "math10" : "notice";
  const triggerMode = action === "notice" ? "payment" : form.triggerMode;
  const savesPaymentFilters = triggerMode !== "keyword";
  const savesUserLimits = action !== "notice";
  const mathPrize = parseOptionalPositiveInt(form.mathPrize, `${name} 奖金`) || 123;
  const moduleKey = form.moduleKey.trim() || "game24";
  const moduleAction = form.moduleAction.trim()
    || defaultInteractionEntryForModule(interactionEntries, moduleKey)?.entry.key
    || "";
  const inferredEntry = interactionEntries.find((item) => item.featureKey === moduleKey && item.entry.key === moduleAction)?.entry;
  const supportsModulePrize = interactionEntryHasField(inferredEntry, "prize");
  const moduleConfig = action === "module"
    ? mergeEntryConfigValues(inferredEntry, form.moduleConfig)
    : {};
  const moduleSessionScope = form.moduleSessionScope
    || (inferredEntry?.session_scope === "user" || inferredEntry?.session_scope === "none"
      ? inferredEntry.session_scope
      : "chat");
  const participantPolicy = form.participantPolicy || inferParticipantPolicy(inferredEntry, moduleSessionScope);
  return {
    id: form.id || `rule-${index + 1}`,
    name,
    enabled: form.enabled,
    chat_ids: parseIntLines(form.chatIds, `${name} Chat ID`),
    trigger_mode: triggerMode,
    trigger_texts: triggerTexts.length ? triggerTexts : [DEFAULT_INTERACTION_BOT.trigger_text],
    module_start_keywords: parseTextLines(form.moduleStartKeywords),
    receiver_user_id: savesPaymentFilters
      ? parseOptionalPositiveInt(form.receiverUserId, `${name} 指定收款人用户 ID`)
      : null,
    receiver_text: savesPaymentFilters ? form.receiverText.trim() || null : null,
    amount: savesPaymentFilters ? parseOptionalPositiveInt(form.amount, `${name} 金额过滤`) : null,
    amount_match_mode: form.amountMatchMode,
    action,
    math_prize: mathPrize,
    module_key: action === "module" ? moduleKey : null,
    module_action: action === "module" ? moduleAction || null : null,
    module_session_scope: action === "module" ? moduleSessionScope : null,
    participant_policy: action === "module" ? participantPolicy : null,
    module_config: moduleConfig,
    module_prize: action === "module" && supportsModulePrize
      ? mathPrize
      : null,
    module_start_text: action === "module" ? form.moduleStartText.trim() || null : null,
    user_cooldown_seconds: savesUserLimits ? form.userCooldownSeconds.trim() || null : null,
    daily_limit_per_user: savesUserLimits
      ? parseOptionalPositiveInt(form.dailyLimitPerUser, `${name} 每用户日上限`)
      : null,
    open_commands: parseTextLines(form.openCommands),
    close_commands: parseTextLines(form.closeCommands),
    status_commands: parseTextLines(form.statusCommands),
    disabled_message: form.disabledMessage.trim() || null,
    valid_seconds: parseOptionalPositiveInt(form.validSeconds, `${name} 参与有效期`) || 600,
    concurrency: action !== "notice" ? form.concurrency : "chat",
    response_template: form.responseTemplate.trim() || DEFAULT_INTERACTION_BOT.response_template,
  };
}

function InteractionRuleEditor({
  aid,
  rule,
  interactionEntries,
  allowedPeers,
  allowedPeersLoading,
  onPatch,
}: {
  aid: number;
  rule: InteractionRuleForm;
  interactionEntries: InteractionEntryOption[];
  allowedPeers: IgnoredPeer[];
  allowedPeersLoading: boolean;
  onPatch: (patch: Partial<InteractionRuleForm>) => void;
}) {
  const selectedModule = resolveRuleModuleSelection(rule, interactionEntries);
  const selectedInteractionEntry = selectedModule?.entry;
  const selectedEventLabels = pluginEventSubscriptionLabels(selectedModule?.eventSubscriptions);
  const selectedCapabilityLabels = pluginOperationalCapabilityLabels({
    capabilities: selectedModule?.capabilities,
    permissions: selectedModule?.permissions,
    usage: selectedModule?.featureUsage,
  });
  const selectedContractWarnings = pluginContractRiskWarnings({
    capabilities: selectedModule?.capabilities,
    event_subscriptions: selectedModule?.eventSubscriptions,
    lint_warnings: selectedModule?.lintWarnings,
  });
  const selectedEntryEvents = (selectedInteractionEntry?.events ?? []).map((event) => pluginEventLabel(event));
  const [entryProfileTab, setEntryProfileTab] = useState<string>("all");
  const effectiveTriggerMode = rule.action === "notice" ? "payment" : rule.triggerMode;
  const showsPaymentFields = effectiveTriggerMode !== "keyword";
  const showsKeywordFields = effectiveTriggerMode !== "payment" && rule.action !== "notice";
  const hasPaidThreshold = rule.amount.trim().length > 0;
  const showsPrize = (
    rule.action === "math10"
    || (rule.action === "module" && interactionEntryHasField(selectedInteractionEntry, "prize"))
  );
  const showsEntryParams = rule.action !== "notice";
  const moduleActionLabel = selectedModule
    ? selectedModule.entry.title || selectedModule.entry.label || selectedModule.entry.key
    : rule.moduleAction || "待选择";
  const moduleActionValue = rule.moduleAction.trim() || selectedModule?.entry.key || "";
  const ruleChatCount = countDelimitedTextItems(rule.chatIds);
  const keywordCount = countDelimitedTextItems(rule.moduleStartKeywords);
  const triggerTextCount = countDelimitedTextItems(rule.triggerTexts);
  const visibleEntries = filterInteractionEntries(interactionEntries, entryProfileTab);
  const availableProfileGroups = INTERACTION_PROFILE_ORDER.map((profile) => {
    const items = interactionEntries.filter((item) => item.entry.interaction_profile === profile);
    return {
      profile,
      label: interactionProfileLabel(profile) || profile,
      items,
    };
  }).filter((group) => group.items.length > 0);
  const visibleGroupedEntries = INTERACTION_PROFILE_ORDER.map((profile) => {
    const items = visibleEntries.filter((item) => item.entry.interaction_profile === profile);
    return {
      profile,
      label: interactionProfileLabel(profile) || profile,
      items,
    };
  }).filter((group) => group.items.length > 0);
  const visibleUngroupedEntries = visibleEntries.filter((item) => !item.entry.interaction_profile);
  const shouldUseEntryProfileTabs = availableProfileGroups.length > 0 && availableProfileGroups.length <= 4;
  const pluginParamCount = selectedInteractionEntry
    ? extraEntryConfigFields(selectedInteractionEntry).length
    : 0;
  const hasUserLimits = Boolean(rule.userCooldownSeconds.trim() || rule.dailyLimitPerUser.trim());

  const updateAction = (value: string) => {
    const nextAction: InteractionRuleForm["action"] = value === "module"
      ? "module"
      : value === "math10"
        ? "math10"
        : "notice";
    const patch: Partial<InteractionRuleForm> = { action: nextAction };
    if (nextAction === "notice") {
      patch.triggerMode = "payment";
    }
    if (nextAction === "math10") {
      patch.triggerMode = rule.triggerMode === "payment" ? "both" : rule.triggerMode;
      patch.moduleStartKeywords = rule.moduleStartKeywords.trim()
        ? rule.moduleStartKeywords
        : DEFAULT_MATH10_START_KEYWORDS;
    }
    if (nextAction === "module" && !rule.moduleAction) {
      const entryOption = defaultInteractionEntryForModule(interactionEntries, rule.moduleKey || "game24");
      if (entryOption) {
        patch.moduleKey = entryOption.featureKey;
        patch.moduleAction = entryOption.entry.key;
        patch.moduleSessionScope = (entryOption.entry.session_scope === "user" || entryOption.entry.session_scope === "none"
          ? entryOption.entry.session_scope
          : "chat") as InteractionRuleForm["moduleSessionScope"];
        patch.participantPolicy = inferParticipantPolicy(entryOption.entry, entryOption.entry.session_scope);
        patch.moduleConfig = defaultModuleConfigFromEntry(entryOption.entry, entryOption.featureConfig);
      }
    }
    onPatch(patch);
  };

  const applyEntryOption = (item: InteractionEntryOption) => {
    onPatch({
      moduleKey: item.featureKey,
      moduleAction: item.entry.key,
      moduleSessionScope: (item.entry.session_scope === "user" || item.entry.session_scope === "none"
        ? item.entry.session_scope
        : "chat") as InteractionRuleForm["moduleSessionScope"],
      participantPolicy: inferParticipantPolicy(item.entry, item.entry.session_scope),
      moduleConfig: defaultModuleConfigFromEntry(item.entry, item.featureConfig),
    });
  };

  const renderEntryOption = (item: InteractionEntryOption) => {
    const isActive = item.value === selectedModule?.value;
    const title = item.entry.title || item.entry.label || item.entry.key;
    const dispatchLabels = interactionDispatchLabels(item.entry);
    const entryEvents = (item.entry.events ?? []).map((event) => pluginEventLabel(event));
    const capabilityLabels = pluginOperationalCapabilityLabels({
      capabilities: item.capabilities,
      permissions: item.permissions,
      usage: item.featureUsage,
    });
    const adminChannel = item.entry.message_channels?.admin_command;
    const publicChannel = item.entry.message_channels?.public_keyword;
    return (
      <button
        key={item.value}
        type="button"
        className={cn(
          "nested-surface w-full border text-left transition-colors",
          isActive ? "border-primary bg-primary/5 shadow-sm" : "bg-background [@media(hover:hover)]:hover:border-primary/40 [@media(hover:hover)]:hover:bg-muted/30",
        )}
        onClick={() => applyEntryOption(item)}
      >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="line-clamp-2 break-words text-sm font-medium">
              {item.featureName}
            </div>
            <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">
              {title}
              {item.entry.description ? ` · ${item.entry.description}` : ""}
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {dispatchLabels.map((label) => (
                <Badge key={label} variant="outline" className="h-5 px-1.5 text-[11px]">
                  {label}
                </Badge>
              ))}
              {entryEvents.slice(0, 2).map((label) => (
                <Badge key={`event-${label}`} variant="secondary" className="h-5 px-1.5 text-[11px]">
                  Event:{label}
                </Badge>
              ))}
              {capabilityLabels.length > 0 ? (
                <Badge variant="outline" className="h-5 px-1.5 text-[11px]" title={capabilityLabels.join(" / ")}>
                  能力 {capabilityLabels.length}
                </Badge>
              ) : null}
              {adminChannel ? (
                <Badge variant="secondary" className="h-5 px-1.5 text-[11px]">
                  管理偏好:{interactionChannelLabel(adminChannel)}
                </Badge>
              ) : null}
              {publicChannel ? (
                <Badge variant="secondary" className="h-5 px-1.5 text-[11px]">
                  群内偏好:{interactionChannelLabel(publicChannel)}
                </Badge>
              ) : null}
              {item.entry.money_channel ? (
                <Badge variant="outline" className="h-5 px-1.5 text-[11px]">
                  转账:{interactionChannelLabel(item.entry.money_channel)}
                </Badge>
              ) : null}
            </div>
          </div>
          <Badge variant={isActive ? "secondary" : "outline"} className="shrink-0">
            {interactionProfileLabel(item.entry.interaction_profile) || "玩法"}
          </Badge>
        </div>
      </button>
    );
  };

  const triggerSummary = effectiveTriggerMode === "payment"
    ? "转账通知"
    : effectiveTriggerMode === "keyword"
      ? "关键词"
      : "转账或关键词";
  const executionSummary = rule.action === "module"
    ? describeRuleModuleSelection(rule, selectedModule)
    : getRuleActionLabel(rule.action);
  const limitSummary = rule.action === "notice"
    ? "无需发奖限制"
    : [
      showsPrize ? `奖金 ${rule.mathPrize || "默认"}` : null,
      rule.validSeconds ? `${rule.validSeconds} 秒有效` : "默认有效期",
      hasUserLimits ? "已限流" : "不限流",
    ].filter(Boolean).join(" · ");
  const triggerModeOptions = rule.action !== "notice"
    ? (
      <>
        <option value="payment">仅转账通知</option>
        <option value="keyword">仅关键词</option>
        <option value="both">转账或关键词</option>
      </>
    )
    : (
      <option value="payment">仅转账通知</option>
    );

  return (
    <div className="nested-surface min-w-0 space-y-4 border bg-background shadow-sm">
      <div className="nested-surface space-y-3 border bg-muted/20">
        <div className="grid min-w-0 gap-3">
          <div className="space-y-1.5">
            <Label>规则名称</Label>
            <Input
              value={rule.name}
              onChange={(e) => onPatch({ name: e.target.value })}
            />
          </div>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <div className="nested-surface-item border bg-background px-3 py-2">
            <div className="text-xs text-muted-foreground">监听群</div>
            <div className="mt-1 text-sm font-medium">
              {ruleChatCount > 0 ? `${ruleChatCount} 个群` : "未填写"}
            </div>
          </div>
          <div className="nested-surface-item border bg-background px-3 py-2">
            <div className="text-xs text-muted-foreground">触发</div>
            <div className="mt-1 text-sm font-medium">{triggerSummary}</div>
          </div>
          <div className="nested-surface-item border bg-background px-3 py-2">
            <div className="text-xs text-muted-foreground">执行</div>
            <div className="mt-1 line-clamp-2 break-words text-sm font-medium">{executionSummary}</div>
          </div>
          <div className="nested-surface-item border bg-background px-3 py-2">
            <div className="text-xs text-muted-foreground">奖励与限制</div>
            <div className="mt-1 line-clamp-2 break-words text-sm font-medium">{limitSummary}</div>
          </div>
        </div>
      </div>

      <RuleEditorSection
        step="1"
        title="触发"
        description="先决定交互 Bot 在哪些群里监听，以及群友用转账还是关键词触发。"
      >
        <div className="grid gap-3 lg:grid-cols-[minmax(240px,1fr)_168px_180px]">
          <div className="space-y-1.5">
            <Label>监听群</Label>
            <AllowedPeerMultiSelect
              aid={aid}
              peers={allowedPeers}
              selectedText={rule.chatIds}
              loading={allowedPeersLoading}
              onChange={(chatIds) => onPatch({ chatIds })}
            />
          </div>
          <div className="space-y-1.5">
            <Label>触发方式</Label>
            <Select
              value={effectiveTriggerMode}
              onChange={(e) => onPatch({ triggerMode: e.target.value as InteractionRuleForm["triggerMode"] })}
            >
              {triggerModeOptions}
            </Select>
            <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
              <Badge variant={ruleChatCount > 0 ? "secondary" : "destructive"}>
                {ruleChatCount > 0 ? `${ruleChatCount} 个群` : "缺少群"}
              </Badge>
              {showsPaymentFields ? (
                <Badge variant={hasPaidThreshold ? "secondary" : "outline"}>
                  {hasPaidThreshold ? `触发金额 ${rule.amount}` : "任意金额"}
                </Badge>
              ) : null}
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>命中后做什么</Label>
            <Select
              value={rule.action}
              onChange={(e) => updateAction(e.target.value)}
            >
              <option value="notice">只发通知</option>
              <option value="math10">发算数题</option>
              <option value="module">启动玩法</option>
            </Select>
          </div>
        </div>

        {showsKeywordFields ? (
          <div className="max-w-[720px] space-y-1.5">
            <Label>{rule.action === "math10" ? "算数题启动关键词" : "玩法启动关键词"}</Label>
            <Textarea
              rows={2}
              className="h-14 !min-h-14 resize-y py-2 text-xs leading-5"
              placeholder={rule.action === "math10" ? DEFAULT_MATH10_START_KEYWORDS : "开24点\n猜骰 num=数字"}
              value={rule.moduleStartKeywords}
              onChange={(e) => onPatch({ moduleStartKeywords: e.target.value })}
            />
            <div className="text-xs text-muted-foreground">
              一行一个关键词；需要带数字时写 <code>num=数字</code> 这类模板。
            </div>
          </div>
        ) : null}

        {showsPaymentFields ? (
          <details className="group nested-surface-item border bg-background px-3 py-2">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-medium [&::-webkit-details-marker]:hidden">
              <span>转账匹配细节</span>
              <span className="flex shrink-0 items-center gap-2 text-xs font-normal text-muted-foreground">
                {triggerTextCount > 0 ? `${triggerTextCount} 条通知词` : "默认通知词"}
                <ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" />
              </span>
            </summary>
            <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
              <div className="space-y-1.5 md:col-span-2">
                <Label>转账通知关键词</Label>
                <Textarea
                  rows={2}
                  className="h-14 !min-h-14 resize-y py-2 text-xs leading-5"
                  placeholder={"转账成功\n交易成功"}
                  value={rule.triggerTexts}
                  onChange={(e) => onPatch({ triggerTexts: e.target.value })}
                />
              </div>
              <div className="space-y-1.5">
                <Label>触发金额</Label>
                <Input
                  inputMode="numeric"
                  placeholder="留空表示任意金额"
                  value={rule.amount}
                  onChange={(e) => onPatch({ amount: e.target.value })}
                />
              </div>
              {hasPaidThreshold ? (
                <div className="space-y-1.5">
                  <Label>金额匹配</Label>
                  <Select
                    value={rule.amountMatchMode}
                    onChange={(e) => onPatch({ amountMatchMode: e.target.value as InteractionRuleForm["amountMatchMode"] })}
                  >
                    <option value="eq">等于触发金额</option>
                    <option value="gte">大于等于触发金额</option>
                  </Select>
                </div>
              ) : null}
              <div className="space-y-1.5">
                <Label>收款人用户 ID</Label>
                <Input
                  inputMode="numeric"
                  placeholder="留空默认当前账号"
                  value={rule.receiverUserId}
                  onChange={(e) => onPatch({ receiverUserId: e.target.value })}
                />
              </div>
              <div className="space-y-1.5">
                <Label>收款人用户名/名称</Label>
                <Input
                  placeholder="@username 或显示名"
                  value={rule.receiverText}
                  onChange={(e) => onPatch({ receiverText: e.target.value })}
                />
              </div>
            </div>
          </details>
        ) : null}
      </RuleEditorSection>

      <RuleEditorSection
        step="2"
        title="启动内容"
        description="命中规则后，决定是发通知、出一道内置题，还是启动插件玩法。"
      >
        {rule.action === "notice" ? (
          <div className="space-y-1.5">
            <Label>通知模板</Label>
            <Textarea
              rows={5}
              placeholder={DEFAULT_INTERACTION_RESPONSE_TEMPLATE}
              value={rule.responseTemplate}
              onChange={(e) => onPatch({ responseTemplate: e.target.value })}
            />
          </div>
        ) : null}

        {rule.action === "math10" ? (
          <div className="nested-surface-item border bg-background px-3 py-2 text-sm text-muted-foreground">
            触发后会由交互 Bot 发十以内算数题，适合用来测试监听和结果链路。
          </div>
        ) : null}

        {rule.action === "module" ? (
          <div className="space-y-3">
            <div className="nested-surface border bg-background">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-xs text-muted-foreground">当前会启动</div>
                  <div className="mt-1 break-words text-sm font-medium">{describeRuleModuleSelection(rule, selectedModule)}</div>
                  {selectedInteractionEntry?.description ? (
                    <div className="mt-1 text-xs text-muted-foreground">{selectedInteractionEntry.description}</div>
                  ) : null}
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    <InteractionContractBlock
                      title="触发事件"
                      items={selectedEntryEvents}
                      empty="入口未声明触发事件"
                    />
                    <InteractionContractBlock
                      title="触发入口"
                      items={selectedEventLabels}
                      empty="插件未声明触发入口"
                    />
                    <InteractionContractBlock
                      title="可用能力"
                      items={selectedCapabilityLabels}
                      empty="未声明可用能力"
                    />
                    <InteractionContractBlock
                      title="发送/模式"
                      items={[
                        selectedInteractionEntry?.launch_mode ? `launch:${selectedInteractionEntry.launch_mode}` : "",
                        selectedInteractionEntry?.session_scope ? `session:${selectedInteractionEntry.session_scope}` : "",
                        selectedInteractionEntry?.money_channel ? `money:${interactionChannelLabel(selectedInteractionEntry.money_channel)}` : "",
                      ].filter(Boolean)}
                      empty="使用默认交互模式"
                    />
                  </div>
                  {selectedContractWarnings.length > 0 ? (
                    <div className="nested-surface-item mt-2 border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs leading-5 text-destructive">
                      {selectedContractWarnings.map((item) => (
                        <div key={item}>{item}</div>
                      ))}
                    </div>
                  ) : null}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {selectedInteractionEntry?.interaction_profile ? (
                    <Badge variant="secondary">
                      {interactionProfileLabel(selectedInteractionEntry.interaction_profile)}
                    </Badge>
                  ) : null}
                  {selectedInteractionEntry?.preserve_command_trigger ? (
                    <Badge variant="outline">保留原命令</Badge>
                  ) : null}
                </div>
              </div>
            </div>

            <details className="group nested-surface border border-primary/35 bg-primary/5 shadow-sm" open={!selectedInteractionEntry}>
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-semibold text-primary [&::-webkit-details-marker]:hidden">
                <span className="min-w-0">
                  更换玩法入口
                  <span className="ml-2 font-normal text-muted-foreground">
                    当前：{moduleActionLabel}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-2 rounded-full border border-primary/25 bg-background px-2.5 py-1 text-xs font-medium text-foreground">
                  {interactionEntries.length} 个可选入口
                  <ChevronRight className="h-4 w-4 text-primary transition-transform group-open:rotate-90" />
                </span>
              </summary>
              <div className="mt-3 space-y-3">
                <div className="flex flex-wrap items-end justify-between gap-2">
                  <div className="text-xs text-muted-foreground">
                    玩法入口就是“这条规则命中后调用哪个插件玩法”。单入口插件通常不用改。
                  </div>
                  {!shouldUseEntryProfileTabs ? (
                    <Select
                      value={entryProfileTab}
                      onChange={(e) => setEntryProfileTab(e.target.value)}
                      className="w-full sm:w-[180px]"
                    >
                      <option value="all">全部类型</option>
                      {availableProfileGroups.map((group) => (
                        <option key={group.profile} value={group.profile}>
                          {group.label}
                        </option>
                      ))}
                      {interactionEntries.some((item) => !item.entry.interaction_profile) ? <option value="__ungrouped">未分类</option> : null}
                    </Select>
                  ) : null}
                </div>
                {shouldUseEntryProfileTabs ? (
                  <Tabs value={entryProfileTab} onValueChange={setEntryProfileTab} className="space-y-3">
                    <TabsList className="justify-start">
                      <TabsTrigger value="all">
                        全部
                      </TabsTrigger>
                      {availableProfileGroups.map((group) => (
                        <TabsTrigger
                          key={group.profile}
                          value={group.profile}
                        >
                          {group.label}
                        </TabsTrigger>
                      ))}
                      {interactionEntries.some((item) => !item.entry.interaction_profile) ? (
                        <TabsTrigger value="__ungrouped">
                          未分类
                        </TabsTrigger>
                      ) : null}
                    </TabsList>
                  </Tabs>
                ) : null}
                {visibleEntries.length <= 0 ? (
                  <div className="nested-surface-item border bg-muted/20 px-3 py-3 text-sm text-muted-foreground">
                    暂无可选交互入口。
                  </div>
                ) : entryProfileTab === "all" ? (
                  <div className="space-y-3">
                    {visibleGroupedEntries.map((group) => (
                      <div key={group.profile} className="space-y-2">
                        <div className="text-xs font-medium text-muted-foreground">{group.label}</div>
                        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                          {group.items.map(renderEntryOption)}
                        </div>
                      </div>
                    ))}
                    {visibleUngroupedEntries.length > 0 ? (
                      <div className="space-y-2">
                        <div className="text-xs font-medium text-muted-foreground">未分类</div>
                        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                          {visibleUngroupedEntries.map(renderEntryOption)}
                        </div>
                      </div>
                    ) : null}
                  </div>
                ) : (
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                    {visibleEntries.map(renderEntryOption)}
                  </div>
                )}
              </div>
            </details>

            <div className="space-y-1.5">
              <Label>启动占位消息</Label>
              <Input
                placeholder={DEFAULT_INTERACTION_MODULE_START_TEXT}
                value={rule.moduleStartText}
                onChange={(e) => onPatch({ moduleStartText: e.target.value })}
              />
              <div className="text-xs text-muted-foreground">
                可用 <code>{"{规则名称}"}</code> 或 <code>{"{rule_name}"}</code>，发送时会替换成当前规则名称。
              </div>
            </div>
          </div>
        ) : null}
      </RuleEditorSection>

      {showsEntryParams ? (
        <RuleEditorSection
          step="3"
          title="奖励与限流"
          description="这里只保留平台统一控制的金额、有效期和每用户限制。插件自己的特殊参数在高级区。"
        >
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
            {showsPrize ? (
              <div className="space-y-1.5">
                <Label>奖金</Label>
                <Input
                  inputMode="numeric"
                  value={rule.mathPrize}
                  onChange={(e) => onPatch({ mathPrize: e.target.value })}
                />
              </div>
            ) : null}
            <div className="space-y-1.5">
              <Label>参与有效期（秒）</Label>
              <Input
                inputMode="numeric"
                value={rule.validSeconds}
                onChange={(e) => onPatch({ validSeconds: e.target.value })}
              />
            </div>
            <div className="space-y-1.5">
              <Label>每用户 CD</Label>
              <Input
                placeholder="例如 6h，留空不限制"
                value={rule.userCooldownSeconds}
                onChange={(e) => onPatch({ userCooldownSeconds: e.target.value })}
              />
            </div>
            <div className="space-y-1.5">
              <Label>每用户日上限</Label>
              <Input
                inputMode="numeric"
                placeholder="例如 2，留空不限制"
                value={rule.dailyLimitPerUser}
                onChange={(e) => onPatch({ dailyLimitPerUser: e.target.value })}
              />
            </div>
            <div className="space-y-1.5">
              <Label>规则占用</Label>
              <Select
                value={rule.concurrency}
                onChange={(e) => onPatch({ concurrency: e.target.value as InteractionRuleForm["concurrency"] })}
              >
                <option value="chat">按群聊</option>
                <option value="user">按用户</option>
                <option value="none">不并发</option>
              </Select>
            </div>
            {rule.action === "module" ? (
              <div className="space-y-1.5">
                <Label>参与者范围</Label>
                <Select
                  value={rule.participantPolicy}
                  onChange={(e) => onPatch({ participantPolicy: e.target.value as InteractionRuleForm["participantPolicy"] })}
                >
                  <option value="open_race">全群可参与</option>
                  <option value="solo_owner">仅付款/开局本人</option>
                  <option value="paid_pool">仅付费玩家</option>
                  <option value="notify_only">只通知</option>
                </Select>
              </div>
            ) : null}
          </div>
          <div className="text-xs text-muted-foreground">
            每用户 CD 和日上限按付款人或消息发送者计算；规则占用决定能否并行启动，参与者范围决定活跃会话后谁能继续操作。
          </div>
        </RuleEditorSection>
      ) : null}

      {rule.action === "module" && selectedInteractionEntry ? (
        <details className="group nested-surface border bg-muted/20">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-medium [&::-webkit-details-marker]:hidden">
            <span>插件额外参数与技术详情</span>
            <span className="flex shrink-0 items-center gap-2 text-xs font-normal text-muted-foreground">
              {pluginParamCount > 0 ? `${pluginParamCount} 个插件参数` : "无额外参数"}
              <ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" />
            </span>
          </summary>
          <div className="mt-3 space-y-3">
            {pluginParamCount > 0 ? (
              <div className="space-y-2">
                <ConfigScopeSection
                  title="插件参数"
                  description="这里只显示该玩法入口额外声明的参数。"
                  fields={extraEntryConfigFields(selectedInteractionEntry)}
                  values={buildEntryConfigValues(selectedInteractionEntry, rule.moduleConfig)}
                  accountId={aid}
                  commandPrefix="."
                  onChange={(key, value) => {
                    const properties = interactionSchemaProperties(selectedInteractionEntry);
                    const next = buildEntryConfigValues(selectedInteractionEntry, rule.moduleConfig);
                    next[key] = normalizeEntryConfigValue(properties[key], value);
                    onPatch({ moduleConfig: mergeEntryConfigValues(selectedInteractionEntry, next) });
                  }}
                />
              </div>
            ) : (
              <div className="nested-surface-item border bg-background px-3 py-2 text-xs text-muted-foreground">
                该玩法入口没有额外插件参数。
              </div>
            )}
            <div className="grid gap-2 sm:grid-cols-2">
              <div className="nested-surface-item min-w-0 border bg-background px-3 py-2 text-xs">
                <div className="mb-1 font-medium">module_key</div>
                <code className="block truncate text-muted-foreground">{rule.moduleKey || selectedModule?.featureKey || "未选择"}</code>
              </div>
              <div className="nested-surface-item min-w-0 border bg-background px-3 py-2 text-xs">
                <div className="mb-1 font-medium">module_action</div>
                <code className="block truncate text-muted-foreground">{moduleActionValue || "保存时尝试推断"}</code>
              </div>
              <div className="nested-surface-item min-w-0 border bg-background px-3 py-2 text-xs sm:col-span-2">
                <div className="mb-1 font-medium">插件会话范围</div>
                <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_160px] sm:items-center">
                  <div className="text-muted-foreground">
                    当前为 {getModuleSessionScopeLabel(rule.moduleSessionScope)}，默认跟随插件入口声明。
                  </div>
                  <Select
                    value={rule.moduleSessionScope}
                    onChange={(e) => onPatch({ moduleSessionScope: e.target.value as InteractionRuleForm["moduleSessionScope"] })}
                  >
                    <option value="chat">按群会话</option>
                    <option value="user">按用户会话</option>
                    <option value="none">不保存会话</option>
                  </Select>
                </div>
              </div>
            </div>
          </div>
        </details>
      ) : null}

      <details className="group nested-surface border bg-muted/20">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-medium [&::-webkit-details-marker]:hidden">
          <span>高级设置与规则管理指令</span>
          <span className="flex shrink-0 items-center gap-2 text-xs font-normal text-muted-foreground">
            管理员临时开关时使用
            <ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" />
          </span>
        </summary>
        <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <div className="space-y-1.5">
            <Label>开启指令</Label>
            <Textarea
              rows={2}
              placeholder={"比如：\n开启24点\n打开游戏"}
              value={rule.openCommands}
              onChange={(e) => onPatch({ openCommands: e.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label>关闭指令</Label>
            <Textarea
              rows={2}
              placeholder={"比如：\n关闭24点\n暂停游戏"}
              value={rule.closeCommands}
              onChange={(e) => onPatch({ closeCommands: e.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label>状态指令</Label>
            <Textarea
              rows={2}
              placeholder="比如：24点状态"
              value={rule.statusCommands}
              onChange={(e) => onPatch({ statusCommands: e.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label>关闭提示</Label>
            <Textarea
              rows={2}
              placeholder={DEFAULT_INTERACTION_DISABLED_MESSAGE}
              value={rule.disabledMessage}
              onChange={(e) => onPatch({ disabledMessage: e.target.value })}
            />
          </div>
        </div>
      </details>
    </div>
  );
}

export function BotTab({
  aid,
  mode = "management",
}: {
  aid: number;
  mode?: "management" | "interaction";
}) {
  const qc = useQueryClient();
  const [enabled, setEnabled] = useState(false);
  const [token, setToken] = useState("");
  const [clearToken, setClearToken] = useState(false);
  const [remotePolicy, setRemotePolicy] = useState<AccountBotRemotePluginPolicy>(
    DEFAULT_REMOTE_POLICY,
  );
  const [transferEnabled, setTransferEnabled] = useState(false);
  const [interactionBotToken, setInteractionBotToken] = useState("");
  const [clearInteractionBotToken, setClearInteractionBotToken] = useState(false);
  const [trustedBotIdsText, setTrustedBotIdsText] = useState("");
  const [transferBotToken, setTransferBotToken] = useState("");
  const [clearTransferBotToken, setClearTransferBotToken] = useState(false);
  const [transferNoticeTemplate, setTransferNoticeTemplate] = useState(DEFAULT_TRANSFER_NOTICE_TEMPLATE);
  const [debitNoticeTemplate, setDebitNoticeTemplate] = useState(DEFAULT_DEBIT_NOTICE_TEMPLATE);
  const [interactionQueryCommands, setInteractionQueryCommands] = useState(DEFAULT_INTERACTION_QUERY_COMMANDS);
  const [interactionQueryResponseTemplate, setInteractionQueryResponseTemplate] = useState(DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE);
  const [interactionQueryItemTemplate, setInteractionQueryItemTemplate] = useState(DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE);
  const [interactionQueryEmptyMessage, setInteractionQueryEmptyMessage] = useState(DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE);
  const [interactionRules, setInteractionRules] = useState<InteractionRuleForm[]>([
    defaultRuleForm(0),
  ]);
  const [selectedInteractionRuleId, setSelectedInteractionRuleId] = useState<string | null>(null);
  const [newUser, setNewUser] = useState<AccountBotUserCreate>({
    tg_user_id: 0,
    display_name: "",
    role: "viewer",
    notify_enabled: true,
    enabled: true,
  });
  const [newUserTgId, setNewUserTgId] = useState("");
  const [interactionIdentityExpanded, setInteractionIdentityExpanded] = useState(false);
  const [interactionAdvancedExpanded, setInteractionAdvancedExpanded] = useState(false);
  const [mobileRuleEditorOpen, setMobileRuleEditorOpen] = useState(false);
  const [ruleSearch, setRuleSearch] = useState("");
  const [ruleStatusFilter, setRuleStatusFilter] = useState<"all" | "enabled" | "paused">("all");
  // 交互表单 dirty：后台 refetch 不得覆盖用户编辑中的输入。
  const interactionFormDirtyRef = useRef(false);
  const interactionInitAidRef = useRef<number | null>(null);
  const [remoteInteractionChanged, setRemoteInteractionChanged] = useState(false);

  const markInteractionDirty = () => {
    interactionFormDirtyRef.current = true;
  };

  const applyInteractionServerState = (data: AccountBotInteractionConfig, entries: typeof interactionEntries) => {
    setTransferEnabled(data.enabled);
    setInteractionBotToken("");
    setClearInteractionBotToken(false);
    const trustedBotIds = data.trusted_bot_ids?.length
      ? data.trusted_bot_ids
      : data.trusted_bot_id == null
        ? []
        : [data.trusted_bot_id];
    setTrustedBotIdsText(trustedBotIds.map((id) => String(id)).join("\n"));
    setTransferBotToken("");
    setClearTransferBotToken(false);
    setTransferNoticeTemplate(data.transfer_notice_template || DEFAULT_TRANSFER_NOTICE_TEMPLATE);
    setDebitNoticeTemplate(data.debit_notice_template || DEFAULT_DEBIT_NOTICE_TEMPLATE);
    setInteractionQueryCommands(
      data.query_commands?.length
        ? data.query_commands.join("\n")
        : DEFAULT_INTERACTION_QUERY_COMMANDS,
    );
    setInteractionQueryResponseTemplate(
      data.query_response_template || DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE,
    );
    setInteractionQueryItemTemplate(
      data.query_item_template || DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE,
    );
    setInteractionQueryEmptyMessage(
      data.query_empty_message || DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE,
    );
    const sourceRules = data.rules?.length
      ? data.rules
      : [legacyRuleFromConfig(data)];
    const fallbackChatIds = data.chat_ids?.length
      ? data.chat_ids
      : data.chat_id == null
        ? []
        : [data.chat_id];
    const nextRules = sourceRules.map((rule, index) => ruleFormFromRule(rule, index, fallbackChatIds, entries));
    setInteractionRules(nextRules);
    setSelectedInteractionRuleId((current) =>
      nextRules.some((rule) => rule.id === current)
        ? current
        : nextRules[0]?.id ?? null,
    );
    interactionFormDirtyRef.current = false;
    setRemoteInteractionChanged(false);
  };

  const botQ = useQuery({
    queryKey: ["account", aid, "bot"],
    queryFn: () => getAccountBot(aid),
    enabled: !!aid && mode === "management",
  });
  const usersQ = useQuery({
    queryKey: ["account", aid, "bot", "users"],
    queryFn: () => listAccountBotUsers(aid),
    enabled: !!aid && mode === "management",
  });
  const interactionQ = useQuery({
    queryKey: ["account", aid, "interaction-bot"],
    queryFn: () => getInteractionBotConfig(aid),
    enabled: !!aid && mode === "interaction",
  });
  const allowedPeersQ = useQuery({
    queryKey: queryKeys.ignoredPeers(aid),
    queryFn: () => listIgnoredPeers(aid),
    enabled: !!aid && mode === "interaction",
  });
  const matrixQ = useQuery({
    queryKey: queryKeys.featureMatrix,
    queryFn: getFeatureMatrix,
    enabled: mode === "interaction",
  });
  const featuresQ = useQuery({
    queryKey: ["account", aid, "features"],
    queryFn: () => listAccountFeatures(aid),
    enabled: !!aid && mode === "interaction",
  });
  const accountFeatureConfigByKey = new Map(
    (featuresQ.data ?? []).map((feature) => [feature.feature_key, feature.config ?? {}]),
  );
  const interactionEntries: InteractionEntryOption[] = (matrixQ.data?.features ?? []).flatMap((feature) =>
    (feature.interaction_entries ?? []).map((entry: FeatureInteractionEntry) => ({
      featureKey: feature.key,
      featureName: feature.display_name,
      featureVersion: feature.version,
      featureConfig: accountFeatureConfigByKey.get(feature.key) ?? {},
      featureConfigSchema: feature.config_schema ?? null,
      featureUsage: feature.usage,
      eventSubscriptions: feature.event_subscriptions,
      capabilities: feature.capabilities,
      permissions: feature.permissions,
      lintWarnings: feature.lint_warnings,
      entry,
      value: `${feature.key}:${entry.key}`,
      label: `${feature.display_name} / ${entry.title || entry.label || entry.key}`,
    })),
  );
  const interactionProfileGroups = INTERACTION_PROFILE_ORDER.map((profile) => {
    const items = interactionEntries.filter((item) => item.entry.interaction_profile === profile);
    return {
      profile,
      label: interactionProfileLabel(profile) || profile,
      items,
    };
  }).filter((group) => group.items.length > 0);
  const ungroupedInteractionEntries = interactionEntries.filter((item) => !item.entry.interaction_profile);

  useEffect(() => {
    if (botQ.data) {
      setEnabled(botQ.data.enabled);
      setClearToken(false);
      setToken("");
      setRemotePolicy(botQ.data.remote_plugin_policy ?? DEFAULT_REMOTE_POLICY);
    }
  }, [botQ.data?.enabled, botQ.data?.has_token, botQ.data?.remote_plugin_policy]);

  useEffect(() => {
    // 账号切换：强制用服务端状态重建表单。
    if (interactionInitAidRef.current !== aid) {
      interactionInitAidRef.current = aid;
      interactionFormDirtyRef.current = false;
      setRemoteInteractionChanged(false);
      if (interactionQ.data) {
        applyInteractionServerState(interactionQ.data, interactionEntries);
      }
      return;
    }
    if (!interactionQ.data) return;
    // 首次拿到 interaction 数据时初始化；之后 dirty 则不覆盖。
    if (!interactionFormDirtyRef.current) {
      applyInteractionServerState(interactionQ.data, interactionEntries);
      return;
    }
    // dirty 期间服务端数据变化：提示用户，不静默冲表单。
    setRemoteInteractionChanged(true);
    // 仅依赖 interaction 主数据；matrix/features 后到不再整表重置。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aid, interactionQ.data]);

  useEffect(() => {
    setSelectedInteractionRuleId((current) =>
      interactionRules.some((rule) => rule.id === current)
        ? current
        : interactionRules[0]?.id ?? null,
    );
  }, [interactionRules]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["account", aid, "bot"] });
    qc.invalidateQueries({ queryKey: ["account", aid, "bot", "users"] });
    qc.invalidateQueries({ queryKey: ["account", aid, "interaction-bot"] });
    qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
  };

  const saveMut = useMutation({
    mutationFn: () =>
      updateAccountBot(aid, {
        enabled,
        clear_token: clearToken,
        bot_token: token.trim() || null,
        remote_plugin_policy: remotePolicy,
      }),
    onSuccess: () => {
      toast.success("Bot 配置已保存");
      invalidate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const testMut = useMutation({
    mutationFn: () => testAccountBot(aid),
    onSuccess: () => toast.success("测试消息已发送"),
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const restartMut = useMutation({
    mutationFn: () => restartAccountBotRuntime(aid),
    onSuccess: () => {
      toast.success("Bot polling runtime 已重启");
      invalidate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const buildInteractionPayload = (overrides?: Partial<AccountBotInteractionConfigUpdate>): AccountBotInteractionConfigUpdate => {
    const existingInteractionBotToken = Boolean(interactionQ.data?.has_interaction_bot_token) && !clearInteractionBotToken;
    const nextInteractionBotToken = interactionBotToken.trim();
    const nextTransferBotToken = transferBotToken.trim();
    const trustedBotIds = parseOptionalUserIds(trustedBotIdsText, "转账结果通知 Bot ID");
    const rules = interactionRules.map((rule, index) => ruleFromForm(rule, index, interactionEntries));
    const chatIds = uniqueIntValues(rules.flatMap((rule) => rule.chat_ids ?? []));
    if (rules.length <= 0) {
      throw new Error("至少需要保留一条规则");
    }
    const firstRule = rules.find((rule) => rule.enabled) ?? rules[0];
    if (transferEnabled && !existingInteractionBotToken && !nextInteractionBotToken) {
      throw new Error("启用转账联动时必须填写交互 Bot Token");
    }
    if (transferEnabled && chatIds.length <= 0) {
      throw new Error("启用转账联动时至少需要在一条规则里填写监听群 Chat ID");
    }
    return {
      enabled: transferEnabled,
      chat_id: chatIds[0] ?? null,
      chat_ids: chatIds,
      interaction_bot_token: nextInteractionBotToken || null,
      clear_interaction_bot_token: clearInteractionBotToken,
      trusted_bot_id: trustedBotIds[0] ?? null,
      trusted_bot_ids: trustedBotIds,
      transfer_bot_token: nextTransferBotToken || null,
      clear_transfer_bot_token: clearTransferBotToken,
      trigger_mode: firstRule.trigger_mode,
      trigger_text: firstRule.trigger_texts?.[0] || DEFAULT_INTERACTION_BOT.trigger_text,
      trigger_texts: firstRule.trigger_texts?.length ? firstRule.trigger_texts : DEFAULT_INTERACTION_BOT.trigger_texts,
      module_start_keywords: firstRule.module_start_keywords ?? [],
      receiver_user_id: firstRule.receiver_user_id ?? null,
      receiver_text: firstRule.receiver_text ?? null,
      amount: firstRule.amount ?? null,
      amount_match_mode: firstRule.amount_match_mode,
      action: firstRule.action,
      math_prize: firstRule.math_prize || 123,
      module_key: firstRule.module_key ?? null,
      module_action: firstRule.module_action ?? null,
      participant_policy: firstRule.participant_policy ?? null,
      module_config: firstRule.module_config ?? {},
      module_prize: firstRule.module_prize ?? null,
      module_start_text: firstRule.module_start_text ?? null,
      user_cooldown_seconds: firstRule.user_cooldown_seconds ?? null,
      daily_limit_per_user: firstRule.daily_limit_per_user ?? null,
      open_commands: firstRule.open_commands ?? [],
      close_commands: firstRule.close_commands ?? [],
      status_commands: firstRule.status_commands ?? [],
      query_commands: parseTextLines(interactionQueryCommands),
      query_response_template: interactionQueryResponseTemplate.trim() || DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE,
      query_item_template: interactionQueryItemTemplate.trim() || DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE,
      query_empty_message: interactionQueryEmptyMessage.trim() || DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE,
      disabled_message: firstRule.disabled_message ?? null,
      valid_seconds: firstRule.valid_seconds ?? 600,
      concurrency: firstRule.concurrency ?? "chat",
      response_template: firstRule.response_template || DEFAULT_INTERACTION_BOT.response_template,
      transfer_notice_template: transferNoticeTemplate.trim() || DEFAULT_TRANSFER_NOTICE_TEMPLATE,
      debit_notice_template: debitNoticeTemplate.trim() || DEFAULT_DEBIT_NOTICE_TEMPLATE,
      rules,
      ...overrides,
    };
  };

  const buildPluginConfigUpdates = () => {
    const updates = new Map<string, Record<string, unknown>>();
    const currentConfigs = new Map(accountFeatureConfigByKey);
    const orderedRules = [
      ...interactionRules.filter((rule) => rule.id !== selectedInteractionRuleId),
      ...interactionRules.filter((rule) => rule.id === selectedInteractionRuleId),
    ];

    for (const rule of orderedRules) {
      const selection = resolveRuleModuleSelection(rule, interactionEntries);
      if (!selection) continue;
      const baseConfig = updates.get(selection.featureKey)
        ?? currentConfigs.get(selection.featureKey)
        ?? selection.featureConfig
        ?? {};
      const update = buildFeatureConfigUpdateForRule(rule, interactionEntries, baseConfig);
      if (update) {
        updates.set(update.pluginKey, update.config);
      }
    }

    return Array.from(updates.entries()).map(([pluginKey, config]) => ({ pluginKey, config }));
  };

  const saveInteractionConfig = async () => {
    const payload = buildInteractionPayload();
    const pluginConfigUpdates = buildPluginConfigUpdates();
    await saveInteractionBotComposite(aid, {
      interaction: payload,
      plugin_configs: pluginConfigUpdates.map((update) => ({
        plugin_key: update.pluginKey,
        config: update.config,
      })),
    });
    interactionFormDirtyRef.current = false;
    setRemoteInteractionChanged(false);
  };

  const saveTransferMut = useMutation({
    mutationFn: saveInteractionConfig,
    onSuccess: () => {
      toast.success("交互 Bot 配置已保存");
      invalidate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const saveInteractionBotMut = useMutation({
    mutationFn: saveInteractionConfig,
    onSuccess: () => {
      toast.success("交互 Bot 身份配置已保存");
      invalidate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const clearInteractionBotMut = useMutation({
    mutationFn: () => updateInteractionBotConfig(aid, buildInteractionPayload({
      interaction_bot_token: null,
      clear_interaction_bot_token: true,
    })),
    onSuccess: () => {
      toast.success("交互 Bot Token 已清空");
      setInteractionBotToken("");
      setClearInteractionBotToken(false);
      invalidate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const saveTransferResultBotMut = useMutation({
    mutationFn: saveInteractionConfig,
    onSuccess: () => {
      toast.success("转账结果通知 Bot 配置已保存");
      invalidate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const clearTransferResultBotMut = useMutation({
    mutationFn: () => updateInteractionBotConfig(aid, buildInteractionPayload({
      trusted_bot_id: null,
      trusted_bot_ids: [],
      transfer_bot_token: null,
      clear_transfer_bot_token: true,
    })),
    onSuccess: () => {
      toast.success("转账结果通知 Bot 配置已清空");
      setTrustedBotIdsText("");
      setTransferBotToken("");
      setClearTransferBotToken(false);
      invalidate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const updateInteractionRule = (
    index: number,
    patch: Partial<InteractionRuleForm>,
  ) => {
    markInteractionDirty();
    setInteractionRules((rules) =>
      rules.map((rule, i) => {
        const target = rules[index];
        const nextTarget = target ? { ...target, ...patch } : null;
        if (i === index) return nextTarget ?? { ...rule, ...patch };
        if (
          target
          && patch.moduleConfig !== undefined
          && patch.moduleKey === undefined
          && patch.moduleAction === undefined
          && rule.action === "module"
          && target.action === "module"
          && rule.moduleKey === target.moduleKey
          && rule.moduleAction === target.moduleAction
        ) {
          return { ...rule, moduleConfig: { ...patch.moduleConfig } };
        }
        return rule;
      }),
    );
  };

  const addInteractionRule = () => {
    markInteractionDirty();
    setInteractionRules((rules) => {
      const nextRule = defaultRuleForm(rules.length);
      setSelectedInteractionRuleId(nextRule.id);
      setMobileRuleEditorOpen(true);
      return [...rules, nextRule];
    });
  };

  const copyInteractionRule = (index: number) => {
    markInteractionDirty();
    setInteractionRules((rules) => {
      const source = rules[index];
      if (!source) return rules;
      const nextRule = {
        ...source,
        id: `rule-${Date.now()}-${index + 2}`,
        name: `${source.name || `规则 ${index + 1}`} 副本`,
      };
      const next = [...rules];
      next.splice(index + 1, 0, nextRule);
      setSelectedInteractionRuleId(nextRule.id);
      setMobileRuleEditorOpen(true);
      return next;
    });
  };

  const removeInteractionRule = (index: number) => {
    markInteractionDirty();
    setInteractionRules((rules) => {
      if (rules.length <= 1) {
        toast.error("至少需要保留一条规则");
        return rules;
      }
      const removedRule = rules[index];
      const next = rules.filter((_, i) => i !== index);
      if (removedRule?.id === selectedInteractionRuleId) {
        setSelectedInteractionRuleId(next[Math.min(index, next.length - 1)]?.id ?? null);
      }
      return next;
    });
  };

  const moveInteractionRule = (index: number, direction: -1 | 1) => {
    markInteractionDirty();
    setInteractionRules((rules) => {
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= rules.length) return rules;
      const next = [...rules];
      [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
      return next;
    });
  };

  const selectInteractionRule = (ruleId: string) => {
    setSelectedInteractionRuleId(ruleId);
    setMobileRuleEditorOpen(true);
  };

  const addUserMut = useMutation({
    mutationFn: () => {
      const tgUserId = parseOptionalPositiveInt(newUserTgId, "Telegram 用户 ID");
      if (tgUserId == null) throw new Error("请填写 Telegram 用户 ID");
      return createAccountBotUser(aid, {
        ...newUser,
        tg_user_id: tgUserId,
        display_name: newUser.display_name?.trim() || null,
      });
    },
    onSuccess: () => {
      toast.success("授权用户已添加");
      setNewUser({
        tg_user_id: 0,
        display_name: "",
        role: "viewer",
        notify_enabled: true,
        enabled: true,
      });
      setNewUserTgId("");
      invalidate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const updateUserMut = useMutation({
    mutationFn: (vars: { uid: number; patch: Partial<AccountBotUserCreate> }) =>
      updateAccountBotUser(aid, vars.uid, vars.patch),
    onSuccess: () => invalidate(),
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const deleteUserMut = useMutation({
    mutationFn: (uid: number) => deleteAccountBotUser(aid, uid),
    onSuccess: () => {
      toast.success("授权用户已删除");
      invalidate();
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const isLoading = mode === "interaction"
    ? interactionQ.isLoading || allowedPeersQ.isLoading || matrixQ.isLoading || featuresQ.isLoading
    : botQ.isLoading || usersQ.isLoading;
  const queryError = mode === "interaction"
    ? interactionQ.error || allowedPeersQ.error || matrixQ.error || featuresQ.error
    : botQ.error || usersQ.error;

  if (isLoading) {
    return (
      <div className="flex h-28 items-center justify-center">
        <Spinner className="text-primary" />
      </div>
    );
  }

  if (queryError) {
    return (
      <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
        配置加载失败：{getErrMsg(queryError)}
      </div>
    );
  }

  const bot = botQ.data;
  const users = usersQ.data ?? [];
  const hasInteractionToken = Boolean(interactionQ.data?.has_interaction_bot_token) && !clearInteractionBotToken;
  const hasTransferToken = Boolean(interactionQ.data?.has_transfer_bot_token) && !clearTransferBotToken;
  const hasRuleChatIds = interactionRules.some((rule) => rule.chatIds.trim());
  const transferReady =
    hasRuleChatIds
    && (hasInteractionToken || Boolean(interactionBotToken.trim()));
  const isInteractionConfigSaveDisabled =
    saveTransferMut.isPending || !interactionQ.data || (transferEnabled && !transferReady);
  const selectedInteractionRuleIndex = interactionRules.findIndex((rule) => rule.id === selectedInteractionRuleId);
  const selectedInteractionRuleIndexSafe = selectedInteractionRuleIndex >= 0 ? selectedInteractionRuleIndex : 0;
  const selectedInteractionRule = interactionRules[selectedInteractionRuleIndexSafe];
  const normalizedRuleSearch = ruleSearch.trim().toLocaleLowerCase();
  const filteredInteractionRules = interactionRules
    .map((rule, index) => ({ rule, index }))
    .filter(({ rule }) => {
      if (ruleStatusFilter === "enabled" && !rule.enabled) return false;
      if (ruleStatusFilter === "paused" && rule.enabled) return false;
      if (!normalizedRuleSearch) return true;
      return [
        rule.name,
        rule.moduleKey,
        rule.moduleAction,
        rule.moduleStartKeywords,
      ].some((value) => String(value || "").toLocaleLowerCase().includes(normalizedRuleSearch));
    });
  const enabledRuleCount = interactionRules.filter((rule) => rule.enabled).length;

  const managementStatus = (
    <div className="flex flex-wrap gap-2">
      <SignalPill tone={bot?.enabled ? "success" : "warn"} label="管理 Bot" value={bot?.enabled ? "已启用" : "未启用"} />
      <SignalPill tone={bot?.has_token ? "success" : "neutral"} label="Token" value={bot?.has_token ? "已配置" : "未配置"} />
      <SignalPill tone={users.length > 0 ? "primary" : "neutral"} label="授权用户" value={`${users.length} 人`} />
    </div>
  );

  const managementContent = (
    <div className="space-y-6">
      {managementStatus}
      <div className="grid gap-4 2xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,0.8fr)]">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Bot className="h-4 w-4" /> 管理 Bot 配置
              <Badge variant="destructive" className="ml-1">
                危险操作需 Telegram 内二次确认
              </Badge>
            </CardTitle>
            <CardDescription>
              管理 Bot 能通过 Telegram Bot 远程管理你账号。每个账号绑定一个普通 Bot，互相隔离授权和通知。Bot Token 不会回显。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="nested-surface-item border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
              安全提示：涉及重启、安装等危险操作时，需在 Telegram 内完成二次确认后才会执行。
            </div>
            <div className="nested-surface-item space-y-3 border border-destructive/30 bg-destructive/10 px-3 py-3">
              <div className="flex items-center gap-2">
                <div className="text-sm font-medium text-destructive">远程插件高风险开关（admin）</div>
                <Dialog>
                  <DialogTrigger asChild>
                    <button
                      type="button"
                      className="text-xs font-medium text-destructive underline underline-offset-2 hover:text-destructive-hover"
                    >
                      详情
                    </button>
                  </DialogTrigger>
                  <DialogContent className="max-h-[85vh] w-[calc(100vw-2rem)] max-w-xl overflow-y-auto rounded-xl">
                    <DialogHeader>
                      <DialogTitle className="text-base">远程插件高风险开关说明</DialogTitle>
                      <DialogDescription>
                        这是管理 Bot 里的高风险远程插件总闸，采用“Web 策略开关 + Telegram 内二次确认”双重防护。
                      </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-3 text-sm text-foreground/90">
                      <div><strong>总开关 enabled：</strong>关闭时所有高风险远程插件动作都不允许从 TG Bot 发起；开启后才会继续检查子开关。</div>
                      <div><strong>允许 install：</strong>控制 <code>/plugins install &lt;git-url&gt;</code>。</div>
                      <div><strong>允许 update：</strong>控制 <code>/plugins update &lt;name&gt;</code>。</div>
                      <div><strong>允许 uninstall：</strong>控制 <code>/plugins uninstall &lt;name&gt;</code>。</div>
                      <div><strong>允许第三方 enable/disable：</strong>控制第三方远程插件启停操作。</div>
                      <div><strong>权限边界：</strong>仅 <code>admin</code> 可操作；<code>viewer/operator</code> 会被拦截。</div>
                      <div><strong>执行机制：</strong>开关只代表“允许发起请求”，真正执行前仍需在 Telegram 内二次确认，且确认有时效并绑定发起人。</div>
                    </div>
                  </DialogContent>
                </Dialog>
              </div>
              <div className="text-xs text-destructive">
                默认全部关闭；即使开启后，Telegram 内仍需二次确认才会执行 install/update/uninstall/第三方启停。
              </div>
              <div className="grid gap-2 text-sm md:grid-cols-2">
                {REMOTE_POLICY_OPTIONS.map(({ key, label, description }) => (
                  <label
                    key={key}
                    className="flex items-start justify-between gap-3 rounded border border-destructive/25 bg-card px-3 py-2.5 text-foreground"
                  >
                    <span className="min-w-0">
                      <span className="block font-medium">{label}</span>
                      <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">
                        {description}
                      </span>
                    </span>
                    <Switch
                      className="mt-0.5 shrink-0"
                      checked={remotePolicy[key]}
                      onCheckedChange={(checked) =>
                        setRemotePolicy((prev) => ({
                          ...prev,
                          [key]: checked,
                        }))
                      }
                    />
                  </label>
                ))}
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>运行状态</Label>
                <div className="flex h-10 items-center gap-2 nested-surface-item border px-3 text-sm">
                  <Badge variant={bot?.enabled ? "default" : "secondary"}>
                    {bot?.enabled ? "已启用" : "未启用"}
                  </Badge>
                  <span className="font-mono text-muted-foreground">
                    {bot?.status ?? "disabled"}
                  </span>
                </div>
              </div>
              <div className="space-y-1.5">
                <Label>Bot 用户名（@开头的）</Label>
                <div className="flex h-10 items-center nested-surface-item border px-3 text-sm">
                  {bot?.username ? `@${bot.username}` : "保存 token 后自动读取"}
                </div>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="account-bot-token">Bot Token</Label>
              <div className="flex gap-2">
                <Input
                  id="account-bot-token"
                  type="password"
                  autoComplete="off"
                  placeholder={bot?.has_token ? MASKED_SECRET_PLACEHOLDER : "123456:ABC-DEF..."}
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
                <Button
                  type="button"
                  variant={clearToken ? "destructive" : "outline"}
                  onClick={() => setClearToken((v) => !v)}
                >
                  <KeyRound className="mr-1 h-4 w-4" />
                  {clearToken ? "将清空" : "清空"}
                </Button>
              </div>
            </div>

            {bot?.last_error ? (
              <div className="nested-surface-item border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                {localizeBotRuntimeError(bot.last_error)}
              </div>
            ) : null}

            <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
              <label className="flex items-center gap-2 text-sm">
                <Switch checked={enabled} onCheckedChange={setEnabled} />
                启用 管理 Bot
              </label>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  onClick={() => testMut.mutate()}
                  disabled={testMut.isPending || !bot?.has_token}
                >
                  {testMut.isPending ? (
                    <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                  ) : (
                    <Send className="mr-1 h-4 w-4" />
                  )}
                  测试发送
                </Button>
                <Button
                  variant="outline"
                  onClick={() => restartMut.mutate()}
                  disabled={restartMut.isPending}
                >
                  <RefreshCw className="mr-1 h-4 w-4" />
                  重启 runtime
                </Button>
                <Button onClick={() => saveMut.mutate()} disabled={saveMut.isPending}>
                  {saveMut.isPending ? (
                    <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="mr-1 h-4 w-4" />
                  )}
                  保存管理 Bot
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <ShieldCheck className="h-4 w-4" /> 可操作范围
            </CardTitle>
            <CardDescription>GUI 是完整控制台，Bot 覆盖高频远程操作。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="grid gap-2">
              {Object.entries(ROLE_META).map(([role, meta]) => (
                <div key={role} className="flex items-center justify-between nested-surface-item border px-3 py-2">
                  <span className="font-mono">{meta.label}</span>
                  <span className="text-muted-foreground">{meta.desc}</span>
                </div>
              ))}
            </div>
            <pre className="nested-surface-item overflow-x-auto bg-muted px-3 py-2 text-xs leading-5">
              {HELP_PREVIEW}
            </pre>
          </CardContent>
        </Card>
      </div>
    </div>
  );

  const floatingInteractionSaveButton = mode === "interaction" && typeof document !== "undefined"
    ? createPortal(
        <div className="pointer-events-none fixed bottom-[calc(5rem+env(safe-area-inset-bottom))] right-4 z-[55] flex justify-end sm:bottom-6 sm:right-8">
          <Button
            type="button"
            size="sm"
            className="pointer-events-auto h-10 rounded-full px-4 shadow-lg shadow-black/15"
            onClick={() => saveTransferMut.mutate()}
            disabled={isInteractionConfigSaveDisabled}
            title="保存规则"
            aria-label="保存规则"
          >
            {saveTransferMut.isPending ? (
              <Loader2 className="mr-1 h-4 w-4 animate-spin" />
            ) : (
              <Save className="mr-1 h-4 w-4" />
            )}
            保存规则
          </Button>
        </div>,
        document.body,
      )
    : null;

  const interactionContent = (
    <div className="space-y-4">
      {floatingInteractionSaveButton}
      <Card className="border-0 bg-transparent shadow-none">
        <CardContent className="flex flex-col gap-4 space-y-0 p-0">
          {interactionQ.data?.interaction_last_error ? (
            <div className="nested-surface-item border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {localizeBotRuntimeError(interactionQ.data.interaction_last_error)}
            </div>
          ) : null}

          <section className="nested-surface border border-warning/30 bg-warning/10 text-sm text-warning">
            <div className="flex items-start gap-2">
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                <div className="font-medium">可信插件风险提示</div>
                <div className="mt-1 text-xs leading-5">
                  插件和插件库由账号主人主动安装与启用。平台会完整下发匹配范围内的消息事件，并提供交互 Bot / UserBot 双通道操作能力；插件风险由安装者自行判断，TelePilot 负责提示风险、记录行为、展示告警和返回客观失败原因。
                </div>
              </div>
            </div>
          </section>

          {remoteInteractionChanged ? (
            <section className="nested-surface border border-primary/30 bg-primary/5 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="font-medium">远端配置已变化</div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    你正在编辑本地草稿；后台刷新不会覆盖当前输入。可选择用服务端最新配置覆盖本地。
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setRemoteInteractionChanged(false)}
                  >
                    继续编辑
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => {
                      if (interactionQ.data) {
                        applyInteractionServerState(interactionQ.data, interactionEntries);
                      }
                    }}
                  >
                    使用服务端配置
                  </Button>
                </div>
              </div>
            </section>
          ) : null}

          <section className="order-2 nested-surface space-y-4 border">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="text-sm font-medium">身份配置</div>
                <div className="text-xs text-muted-foreground">
                  交互 Bot 负责收更新与发互动消息；转账结果通知 Bot 只在测试模拟通知时需要 Token。
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={hasInteractionToken || interactionBotToken.trim() ? "secondary" : "destructive"}>
                  {hasInteractionToken || interactionBotToken.trim() ? "交互 Bot 已配置" : "交互 Bot 缺少 Token"}
                </Badge>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-8 gap-1.5"
                  onClick={() => setInteractionIdentityExpanded((value) => !value)}
                  aria-expanded={interactionIdentityExpanded}
                  aria-controls="interaction-identity-config"
                >
                  <ChevronRight
                    className={cn(
                      "h-4 w-4 transition-transform",
                      interactionIdentityExpanded && "rotate-90",
                    )}
                  />
                  {interactionIdentityExpanded ? "收起" : "展开"}
                </Button>
              </div>
            </div>

            {interactionIdentityExpanded ? (
            <div id="interaction-identity-config" className="grid gap-3 2xl:grid-cols-2">
              <div className="nested-surface space-y-3 border bg-muted/20">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="text-sm font-medium">交互 Bot</div>
                    <div className="text-xs text-muted-foreground">
                      用户 ID 会从 Token 自动读取，不需要手填；Telegram Bot API 发消息和收更新都只需要 Token。
                    </div>
                  </div>
                  <Badge variant={hasInteractionToken || interactionBotToken.trim() ? "secondary" : "destructive"}>
                    {hasInteractionToken || interactionBotToken.trim() ? "已配置" : "缺少 Token"}
                  </Badge>
                </div>
                <div className="grid gap-3">
                  <div className="space-y-1.5">
                    <Label>用户名（@开头的）</Label>
                    <div className="flex h-10 items-center nested-surface-item border bg-background px-3 text-sm">
                      {interactionQ.data?.interaction_bot_username
                        ? `@${interactionQ.data.interaction_bot_username}`
                        : "保存 Token 后自动读取"}
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <Label>Token</Label>
                    <Input
                      type="password"
                      autoComplete="off"
                      placeholder={interactionQ.data?.has_interaction_bot_token ? MASKED_SECRET_PLACEHOLDER : "123456:ABC-DEF..."}
                      value={interactionBotToken}
                      onChange={(e) => {
                        markInteractionDirty();
                        setInteractionBotToken(e.target.value);
                      }}
                    />
                  </div>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
                  <Button
                    type="button"
                    variant="outline"
                    className="sm:min-w-[132px]"
                    onClick={() => saveInteractionBotMut.mutate()}
                    disabled={saveInteractionBotMut.isPending || !interactionQ.data}
                  >
                    {saveInteractionBotMut.isPending ? (
                      <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                    ) : (
                      <Save className="mr-1 h-4 w-4" />
                    )}
                    保存交互 Bot
                  </Button>
                  <Button
                    type="button"
                    variant="destructive"
                    className="sm:min-w-[132px]"
                    onClick={() => clearInteractionBotMut.mutate()}
                    disabled={clearInteractionBotMut.isPending || !interactionQ.data}
                  >
                    <KeyRound className="mr-1 h-4 w-4" />
                    清空交互 Bot
                  </Button>
                </div>
              </div>

              <div className="nested-surface space-y-3 border bg-muted/20">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="text-sm font-medium">真实转账结果通知 Bot</div>
                    <div className="text-xs text-muted-foreground">
                      这里填写群里实际发送到账结果的 Bot ID；多个群使用不同通知 Bot 时可以全部加入信任列表。
                    </div>
                  </div>
                  <Badge variant={hasTransferToken || transferBotToken.trim() || trustedBotIdsText.trim() ? "secondary" : "outline"}>
                    可选
                  </Badge>
                </div>
                <div className="grid gap-3">
                  <div className="space-y-1.5">
                    <Label>真实通知 Bot ID 列表（一行一个，不是@开头的）</Label>
                    <Textarea
                      rows={3}
                      inputMode="numeric"
                      placeholder={"例如：\n6920251805\n6929566752"}
                      value={trustedBotIdsText}
                      onChange={(e) => {
                        markInteractionDirty();
                        setTrustedBotIdsText(e.target.value);
                      }}
                    />
                    <div className="text-xs text-muted-foreground">
                      参考群内转账结果消息的发送者 ID；保存时会自动去重，测试模拟 Token 识别出的 Bot 会额外加入信任。
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <Label>测试模拟通知 Bot Token</Label>
                    <Input
                      type="password"
                      autoComplete="off"
                      placeholder={interactionQ.data?.has_transfer_bot_token ? MASKED_SECRET_PLACEHOLDER : "测试模拟通知时填写"}
                      value={transferBotToken}
                      onChange={(e) => {
                        markInteractionDirty();
                        setTransferBotToken(e.target.value);
                      }}
                    />
                    <div className="text-xs text-muted-foreground">
                      {interactionQ.data?.transfer_bot_id
                        ? `当前测试模拟 Bot ID：${interactionQ.data.transfer_bot_id}。系统会同时信任真实通知 Bot ID 和这个测试 Bot ID。`
                        : "没有测试模拟 Token 时，系统只监听群里真实出现的转账结果通知。"}
                    </div>
                  </div>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
                  <Button
                    type="button"
                    variant="outline"
                    className="sm:min-w-[132px]"
                    onClick={() => saveTransferResultBotMut.mutate()}
                    disabled={saveTransferResultBotMut.isPending || !interactionQ.data}
                  >
                    {saveTransferResultBotMut.isPending ? (
                      <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                    ) : (
                      <Save className="mr-1 h-4 w-4" />
                    )}
                    保存通知 Bot
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    className="sm:min-w-[132px]"
                    onClick={() => clearTransferResultBotMut.mutate()}
                    disabled={clearTransferResultBotMut.isPending || !interactionQ.data}
                  >
                    <KeyRound className="mr-1 h-4 w-4" />
                    清空通知 Bot
                  </Button>
                </div>
              </div>
            </div>
            ) : null}
          </section>

          <section className="order-1 nested-surface space-y-3 border">
            <div className={cn(
              "flex flex-wrap items-center justify-between gap-2",
              mobileRuleEditorOpen && "hidden xl:flex",
            )}>
              <div className="min-w-0">
                <div className="text-sm font-medium">规则列表</div>
                <div className="text-xs text-muted-foreground">
                  先在左侧挑规则，再到右侧按触发、启动内容、奖励限制配置当前规则。
                </div>
              </div>
              <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:justify-end">
                <label className="nested-surface-item flex min-w-0 flex-1 items-center justify-between gap-3 border bg-background px-3 py-2 sm:flex-none">
                  <span className="min-w-0">
                    <span className="flex items-center gap-2 text-sm font-medium">
                      交互功能总开关
                      <Badge variant={transferEnabled ? "secondary" : "outline"}>
                        {transferEnabled ? "已开启" : "已关闭"}
                      </Badge>
                    </span>
                    <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">
                      关闭后停用全部规则和交互 Bot，已有配置会保留。
                    </span>
                  </span>
                  <Switch
                    checked={transferEnabled}
                    onCheckedChange={(value) => {
                      markInteractionDirty();
                      setTransferEnabled(value);
                    }}
                    aria-label="交互功能总开关"
                  />
                </label>
                <Button
                  type="button"
                  variant="outline"
                  className="flex-1 sm:flex-none sm:min-w-[116px]"
                  onClick={addInteractionRule}
                >
                  <Plus className="mr-1 h-4 w-4" />
                  新增规则
                </Button>
              </div>
            </div>

            <details className={cn(
              "nested-surface-item overflow-hidden border bg-muted/20",
              mobileRuleEditorOpen && "hidden xl:block",
            )}>
                <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
                  玩法查询设置
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    群内查询指令、列表模板和空状态提示
                  </span>
                </summary>
                <div className="grid gap-3 border-t p-3 xl:grid-cols-[minmax(180px,250px)_minmax(260px,1fr)_minmax(260px,360px)]">
                  <div className="space-y-1.5">
                    <Label>玩法查询指令</Label>
                    <Textarea
                      rows={2}
                      className="h-16 !min-h-16 resize-y py-2 text-xs leading-5"
                      placeholder={DEFAULT_INTERACTION_QUERY_COMMANDS}
                      value={interactionQueryCommands}
                      onChange={(e) => {
                        markInteractionDirty();
                        setInteractionQueryCommands(e.target.value);
                      }}
                    />
                    <div className="text-xs leading-5 text-muted-foreground">
                      一行一个指令；留空则不开放群内玩法查询。
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <Label>玩法查询消息模板</Label>
                    <Textarea
                      rows={3}
                      className="min-h-[78px] resize-y py-2 text-xs leading-5"
                      placeholder={DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE}
                      value={interactionQueryResponseTemplate}
                      onChange={(e) => {
                        markInteractionDirty();
                        setInteractionQueryResponseTemplate(e.target.value);
                      }}
                    />
                    <div className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                      <span><code>{"{items}"}</code>：由右侧单项模板生成</span>
                      <span><code>{"{count}"}</code>：开启数量</span>
                      <span><code>{"{closed_count}"}</code>：临时关闭数量</span>
                      <span><code>{"{chat_id}"}</code>：当前群 ID</span>
                    </div>
                    <div className="space-y-1.5">
                      <Label>玩法列表单项模板</Label>
                      <Textarea
                        rows={3}
                        className="min-h-[78px] resize-y py-2 text-xs leading-5"
                        placeholder={DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE}
                        value={interactionQueryItemTemplate}
                        onChange={(e) => {
                          markInteractionDirty();
                          setInteractionQueryItemTemplate(e.target.value);
                        }}
                      />
                    </div>
                    <div className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                      <span><code>{"{index}"}</code>：序号</span>
                      <span><code>{"{name}"}</code>：规则名称</span>
                      <span><code>{"{trigger}"}</code>：触发方式</span>
                      <span><code>{"{kind}"}</code>：玩法类型</span>
                      <span><code>{"{limit}"}</code>：限制摘要</span>
                      <span><code>{"{module_key}"}</code>：插件 key</span>
                    </div>
                    <div className="space-y-1.5">
                      <Label>无可用玩法提示</Label>
                      <Input
                        value={interactionQueryEmptyMessage}
                        placeholder={DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE}
                        onChange={(e) => {
                          markInteractionDirty();
                          setInteractionQueryEmptyMessage(e.target.value);
                        }}
                      />
                    </div>
                  </div>
                  <div className="nested-surface border bg-background text-xs">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                      <div className="font-medium">查询预览</div>
                      <span className="text-[11px] text-muted-foreground">示例变量渲染</span>
                    </div>
                    <TelegramHtmlPreview
                      value={renderInteractionQueryTemplatePreview(interactionQueryResponseTemplate, interactionQueryItemTemplate)}
                      mode="html"
                      title="交互 Bot"
                      caption="玩法查询"
                      hints={[
                        { label: "count", value: "2" },
                        { label: "closed", value: "0" },
                        { label: "chat", value: "-1001234567890" },
                      ]}
                    />
                  </div>
                </div>
            </details>

            <div className="grid gap-3 xl:h-[calc(100vh-18rem)] xl:min-h-[560px] xl:max-h-[860px] xl:grid-cols-[minmax(300px,380px)_minmax(0,1fr)] xl:items-stretch">
              <div className={cn(
                "nested-surface nested-surface-inset-2 space-y-2 border bg-muted/20 xl:h-full xl:overflow-y-auto",
                mobileRuleEditorOpen && "hidden xl:block",
              )}>
                <div className="sticky top-0 z-10 space-y-2 bg-muted/95 pb-1 backdrop-blur-sm">
                  <div className="relative">
                    <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                    <Input
                      value={ruleSearch}
                      onChange={(event) => setRuleSearch(event.target.value)}
                      placeholder="搜索规则或插件"
                      className="h-8 pl-8"
                    />
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <Tabs value={ruleStatusFilter} onValueChange={(value) => setRuleStatusFilter(value as typeof ruleStatusFilter)}>
                      <TabsList className="w-auto flex-nowrap">
                        <TabsTrigger value="all" className="min-w-0 px-2">全部</TabsTrigger>
                        <TabsTrigger value="enabled" className="min-w-0 px-2">启用</TabsTrigger>
                        <TabsTrigger value="paused" className="min-w-0 px-2">暂停</TabsTrigger>
                      </TabsList>
                    </Tabs>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {enabledRuleCount}/{interactionRules.length} 启用
                    </span>
                  </div>
                </div>

                {filteredInteractionRules.map(({ rule, index }) => {
                  const resolvedModule = resolveRuleModuleSelection(rule, interactionEntries);
                  const effectiveTriggerMode = rule.action === "notice" ? "payment" : rule.triggerMode;
                  const isSelected = rule.id === selectedInteractionRule?.id;
                  const chatCount = countDelimitedTextItems(rule.chatIds);
                  const keywordCount = countDelimitedTextItems(rule.moduleStartKeywords);
                  const actionTone = rule.action === "module" ? "default" : rule.action === "math10" ? "secondary" : "outline";
                  const moduleSummary = rule.action === "module"
                    ? describeRuleModuleSelection(rule, resolvedModule, { withVersion: true })
                    : rule.action === "notice"
                      ? "命中后只发送通知消息"
                      : keywordCount > 0
                        ? `${keywordCount} 条启动关键词`
                        : "未填写启动关键词";
                  const sessionSummary = rule.action === "module"
                    ? `${getModuleSessionScopeLabel(rule.moduleSessionScope)} · ${getParticipantPolicyLabel(rule.participantPolicy)}`
                    : `${getRuleConcurrencyLabel(rule.concurrency)} · ${rule.validSeconds || "不限"} 秒有效`;
                  return (
                    <div
                      key={rule.id}
                      className={cn(
                        "group nested-surface nested-surface-inset-2 border bg-background transition-colors",
                        isSelected ? "border-primary/50 bg-primary/5 shadow-sm ring-1 ring-primary/10" : "border-border/70 hover:border-primary/25",
                      )}
                    >
                      <div className="flex items-start gap-2">
                        <button
                          type="button"
                          className="min-w-0 flex-1 text-left"
                          onClick={() => selectInteractionRule(rule.id)}
                        >
                          <div className="flex items-start gap-2.5">
                            <div className={cn(
                              "grid h-8 w-8 shrink-0 place-items-center nested-surface-item border text-xs font-semibold",
                              isSelected ? "border-primary/40 bg-primary/10 text-primary" : "border-border bg-muted/40 text-muted-foreground",
                            )}>
                              {index + 1}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="flex min-w-0 items-center gap-1.5">
                                <div className="min-w-0 flex-1 truncate text-sm font-semibold">
                                  {rule.name || `规则 ${index + 1}`}
                                </div>
                                <Badge
                                  variant={rule.enabled ? "success" : "secondary"}
                                  className="h-5 shrink-0 px-1.5 text-[11px]"
                                >
                                  {rule.enabled ? "启用" : "暂停"}
                                </Badge>
                                {isSelected ? (
                                  <Badge variant="outline" className="h-5 shrink-0 px-1.5 text-[11px]">
                                    当前
                                  </Badge>
                                ) : null}
                              </div>
                              <div className="mt-1 flex min-w-0 flex-wrap gap-1.5">
                                <Badge variant={actionTone} className="h-5 px-1.5 text-[11px]">
                                  {getRuleActionLabel(rule.action)}
                                </Badge>
                                <Badge
                                  variant={effectiveTriggerMode === "payment" ? "secondary" : "outline"}
                                  className="h-5 px-1.5 text-[11px]"
                                >
                                  {getRuleTriggerModeLabel(effectiveTriggerMode)}
                                </Badge>
                              </div>
                              <div className="mt-1 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                                <span className="whitespace-nowrap">群 {chatCount > 0 ? chatCount : "未填"}</span>
                                <span className="whitespace-nowrap">关键词 {keywordCount}</span>
                                <span className="min-w-0 truncate">{sessionSummary}</span>
                              </div>
                              <div className="mt-1 line-clamp-2 break-words text-xs text-muted-foreground">
                                {moduleSummary}
                              </div>
                            </div>
                          </div>
                        </button>

                        <div className="flex shrink-0 flex-col items-end gap-1">
                          <div className="flex h-7 items-center nested-surface-item bg-muted/40 px-1.5">
                            <Switch
                              id={`interaction-rule-enabled-${rule.id}`}
                              checked={rule.enabled}
                              onCheckedChange={(checked) => updateInteractionRule(index, { enabled: checked })}
                              aria-label={`${rule.name || `规则 ${index + 1}`} ${rule.enabled ? "已启用" : "已暂停"}`}
                            />
                          </div>
                          <div className={cn(
                            "grid grid-cols-4 gap-0.5 transition-opacity",
                            isSelected
                              ? "opacity-100"
                              : "opacity-70 xl:opacity-0 xl:group-hover:opacity-100 xl:group-focus-within:opacity-100",
                          )}>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            title="上移"
                            aria-label="上移规则"
                            onClick={() => moveInteractionRule(index, -1)}
                            disabled={index === 0}
                          >
                            <ArrowUp className="h-4 w-4" />
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            title="下移"
                            aria-label="下移规则"
                            onClick={() => moveInteractionRule(index, 1)}
                            disabled={index === interactionRules.length - 1}
                          >
                            <ArrowDown className="h-4 w-4" />
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            title="复制"
                            aria-label="复制规则"
                            onClick={() => copyInteractionRule(index)}
                          >
                            <Copy className="h-4 w-4" />
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            title="删除"
                            aria-label="删除规则"
                            onClick={() => removeInteractionRule(index)}
                            disabled={interactionRules.length <= 1}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })}
                {filteredInteractionRules.length === 0 ? (
                  <div className="nested-surface-item border border-dashed bg-background px-3 py-8 text-center text-sm text-muted-foreground">
                    没有符合当前筛选的规则
                  </div>
                ) : null}
              </div>

              <div className={cn(
                "min-w-0 xl:h-full xl:overflow-y-auto",
                !mobileRuleEditorOpen && "hidden xl:block",
              )}>
                {selectedInteractionRule ? (
                  <div className="space-y-3">
                    <div className="sticky top-0 z-20 flex min-h-12 items-center gap-2 rounded-md border bg-background/95 px-2 py-2 backdrop-blur-sm xl:hidden">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="shrink-0"
                        onClick={() => setMobileRuleEditorOpen(false)}
                      >
                        <ArrowLeft className="mr-1 h-4 w-4" />
                        规则列表
                      </Button>
                      <div className="flex min-w-0 flex-col justify-center">
                        <div className="truncate text-sm font-semibold leading-5">
                          {selectedInteractionRule.name || `规则 ${selectedInteractionRuleIndexSafe + 1}`}
                        </div>
                        <div className="truncate text-xs leading-4 text-muted-foreground">
                          第 {selectedInteractionRuleIndexSafe + 1} 条，共 {interactionRules.length} 条
                        </div>
                      </div>
                    </div>
                    <InteractionRuleEditor
                      aid={aid}
                      rule={selectedInteractionRule}
                      interactionEntries={interactionEntries}
                      allowedPeers={allowedPeersQ.data ?? []}
                      allowedPeersLoading={allowedPeersQ.isLoading}
                      onPatch={(patch) => updateInteractionRule(selectedInteractionRuleIndexSafe, patch)}
                    />
                  </div>
                ) : (
                  <div className="nested-surface border bg-muted/20 text-sm text-muted-foreground">
                    当前没有可编辑的规则。
                  </div>
                )}
              </div>
            </div>
          </section>

          <section className="order-3 nested-surface space-y-3 border">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="text-sm font-medium">高级设置</div>
                <div className="text-xs text-muted-foreground">
                  这里放测试通知模板和保存入口，不影响现有保存逻辑。
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={hasTransferToken || transferBotToken.trim() ? "secondary" : "outline"}>
                  {hasTransferToken || transferBotToken.trim() ? "通知模板可联调" : "模板可先预设"}
                </Badge>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-8 gap-1.5"
                  onClick={() => setInteractionAdvancedExpanded((value) => !value)}
                  aria-expanded={interactionAdvancedExpanded}
                  aria-controls="interaction-advanced-config"
                >
                  <ChevronRight
                    className={cn(
                      "h-4 w-4 transition-transform",
                      interactionAdvancedExpanded && "rotate-90",
                    )}
                  />
                  {interactionAdvancedExpanded ? "收起" : "展开"}
                </Button>
              </div>
            </div>
            {interactionAdvancedExpanded ? (
              <>
                <div id="interaction-advanced-config" className="space-y-1.5">
                  <Label>转账测试通知模板</Label>
                  <Textarea
                    rows={5}
                    placeholder={DEFAULT_TRANSFER_NOTICE_TEMPLATE}
                    value={transferNoticeTemplate}
                    onChange={(e) => {
                      markInteractionDirty();
                      setTransferNoticeTemplate(e.target.value);
                    }}
                  />
                  <div className="grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                    <span><code>{"{payer_name}"}</code>：付款人显示名</span>
                    <span><code>{"{payer_user_id}"}</code>：付款人用户 ID</span>
                    <span><code>{"{receiver_name}"}</code>：收款人显示名</span>
                    <span><code>{"{amount}"}</code>：转账金额</span>
                    <span><code>{"{receiver_user_id}"}</code>：收款人用户 ID</span>
                    <span className="sm:col-span-2"><code>{"{payer_user_id_line}"}</code>：有付款人 ID 时渲染为“付款人ID：数字”，没有时自动留空</span>
                    <span className="sm:col-span-2"><code>{"{receiver_user_id_line}"}</code>：有收款人 ID 时渲染为“收款人ID：数字”，没有时自动留空</span>
                  </div>
                  <div className="nested-surface border bg-background text-xs">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                      <div className="font-medium">消息预览</div>
                      <span className="text-[11px] text-muted-foreground">使用示例变量渲染</span>
                    </div>
                    <TelegramHtmlPreview
                      value={renderTransferNoticeTemplatePreview(transferNoticeTemplate)}
                      mode="html"
                      title="转账通知 Bot"
                      caption="transfer notice"
                      hints={[
                        { label: "payer", value: TRANSFER_NOTICE_TEMPLATE_SAMPLE_VALUES.payer_name },
                        { label: "receiver", value: TRANSFER_NOTICE_TEMPLATE_SAMPLE_VALUES.receiver_name },
                        { label: "amount", value: TRANSFER_NOTICE_TEMPLATE_SAMPLE_VALUES.amount },
                      ]}
                    />
                  </div>
                </div>
                <div className="space-y-1.5">
                  <Label>扣款测试通知模板</Label>
                  <Textarea
                    rows={4}
                    placeholder={DEFAULT_DEBIT_NOTICE_TEMPLATE}
                    value={debitNoticeTemplate}
                    onChange={(e) => {
                      markInteractionDirty();
                      setDebitNoticeTemplate(e.target.value);
                    }}
                  />
                  <div className="nested-surface border bg-background text-xs">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                      <div className="font-medium">消息预览</div>
                      <span className="text-[11px] text-muted-foreground">使用示例变量渲染</span>
                    </div>
                    <TelegramHtmlPreview
                      value={renderTransferNoticeTemplatePreview(debitNoticeTemplate.trim() ? debitNoticeTemplate : DEFAULT_DEBIT_NOTICE_TEMPLATE)}
                      mode="html"
                      title="转账通知 Bot"
                      caption="debit notice"
                      hints={[
                        { label: "payer", value: TRANSFER_NOTICE_TEMPLATE_SAMPLE_VALUES.payer_name },
                        { label: "receiver", value: TRANSFER_NOTICE_TEMPLATE_SAMPLE_VALUES.receiver_name },
                        { label: "amount", value: TRANSFER_NOTICE_TEMPLATE_SAMPLE_VALUES.amount },
                      ]}
                    />
                  </div>
                </div>

                <div className="nested-surface-item bg-muted px-3 py-2 text-xs leading-5 text-muted-foreground">
                  群里回复任意消息发送 <code>+123</code> 会生成带 <code>language-转账成功</code> 标识的 HTML 代码块；发送 <code>-123</code> 会生成带 <code>language-扣减成功</code> 标识的扣款通知。
                  没有测试用的转账通知结果 Bot 的 Token 时，交互 Bot 只监听群里真实出现的转账结果通知。
                </div>
              </>
            ) : null}
          </section>
        </CardContent>
      </Card>
    </div>
  );

  const usersContent = (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Bell className="h-4 w-4" /> 管理 Bot 授权用户
          </CardTitle>
          <CardDescription>
            未授权用户默认无响应。授权用户发 /start 后会记录 last_chat_id，用于通知和测试发送。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="nested-surface grid gap-3 border lg:grid-cols-[180px_minmax(0,1fr)_150px_120px]">
            <div className="space-y-1.5">
              <Label>Telegram 用户 ID</Label>
              <Input
                inputMode="numeric"
                placeholder="123456789"
                value={newUserTgId}
                onChange={(e) => setNewUserTgId(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label>备注名</Label>
              <Input
                placeholder="例如：我 / 运维同事"
                value={newUser.display_name ?? ""}
                onChange={(e) => setNewUser((v) => ({ ...v, display_name: e.target.value }))}
              />
            </div>
            <div className="space-y-1.5">
              <Label>角色</Label>
              <Select
                value={newUser.role}
                onChange={(e) =>
                  setNewUser((v) => ({ ...v, role: e.target.value as AccountBotRole }))
                }
              >
                {Object.keys(ROLE_META).map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </Select>
            </div>
            <div className="flex items-end">
              <Button
                className="w-full"
                onClick={() => addUserMut.mutate()}
                disabled={addUserMut.isPending}
              >
                <UserPlus className="mr-1 h-4 w-4" />
                添加
              </Button>
            </div>
          </div>

          <div className="space-y-3 md:hidden">
            {users.map((u) => (
              <div key={u.id} className="nested-surface space-y-3 border">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-mono text-sm">{u.tg_user_id}</div>
                    <div className="text-xs text-muted-foreground">
                      chat: {u.last_chat_id ?? "未记录"}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="shrink-0 text-destructive"
                    onClick={() => deleteUserMut.mutate(u.id)}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label>备注名</Label>
                    <Input
                      defaultValue={u.display_name ?? ""}
                      placeholder="无"
                      onBlur={(e) => {
                        if (e.target.value === (u.display_name ?? "")) return;
                        updateUserMut.mutate({
                          uid: u.id,
                          patch: { display_name: e.target.value || null },
                        });
                      }}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label>角色</Label>
                    <Select
                      value={u.role}
                      onChange={(e) =>
                        updateUserMut.mutate({
                          uid: u.id,
                          patch: { role: e.target.value as AccountBotRole },
                        })
                      }
                    >
                      {Object.keys(ROLE_META).map((role) => (
                        <option key={role} value={role}>
                          {role}
                        </option>
                      ))}
                    </Select>
                  </div>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="nested-surface-item flex items-center justify-between border px-3 py-2 text-sm">
                    <span>通知</span>
                    <Switch
                      checked={u.notify_enabled}
                      onCheckedChange={(v) =>
                        updateUserMut.mutate({ uid: u.id, patch: { notify_enabled: v } })
                      }
                    />
                  </label>
                  <label className="nested-surface-item flex items-center justify-between border px-3 py-2 text-sm">
                    <span>启用</span>
                    <Switch
                      checked={u.enabled}
                      onCheckedChange={(v) =>
                        updateUserMut.mutate({ uid: u.id, patch: { enabled: v } })
                      }
                    />
                  </label>
                </div>
                <div className="text-xs text-muted-foreground">
                  创建于 {formatDateTime(u.created_at)}
                </div>
              </div>
            ))}
            {users.length === 0 ? (
              <div className="nested-surface-item border px-3 py-8 text-center text-sm text-muted-foreground">
                还没有授权用户
              </div>
            ) : null}
          </div>

          <Table className="hidden table-fixed md:table">
            <colgroup>
              <col className="w-[22%]" />
              <col className="w-[18%]" />
              <col className="w-[16%]" />
              <col className="w-[12%]" />
              <col className="w-[14%]" />
              <col className="w-[18%]" />
            </colgroup>
            <TableHeader>
              <TableRow>
                <TableHead>用户</TableHead>
                <TableHead>备注</TableHead>
                <TableHead>角色</TableHead>
                <TableHead className="text-center">通知</TableHead>
                <TableHead className="text-center">启用</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((u) => (
                <TableRow key={u.id}>
                  <TableCell>
                    <div className="font-mono">{u.tg_user_id}</div>
                    <div className="text-xs text-muted-foreground">
                      chat: {u.last_chat_id ?? "未记录"}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Input
                      defaultValue={u.display_name ?? ""}
                      placeholder="无"
                      onBlur={(e) => {
                        if (e.target.value === (u.display_name ?? "")) return;
                        updateUserMut.mutate({
                          uid: u.id,
                          patch: { display_name: e.target.value || null },
                        });
                      }}
                    />
                  </TableCell>
                  <TableCell>
                    <Select
                      value={u.role}
                      onChange={(e) =>
                        updateUserMut.mutate({
                          uid: u.id,
                          patch: { role: e.target.value as AccountBotRole },
                        })
                      }
                    >
                      {Object.keys(ROLE_META).map((role) => (
                        <option key={role} value={role}>
                          {role}
                        </option>
                      ))}
                    </Select>
                  </TableCell>
                  <TableCell className="text-center">
                    <Switch
                      checked={u.notify_enabled}
                      onCheckedChange={(v) =>
                        updateUserMut.mutate({ uid: u.id, patch: { notify_enabled: v } })
                      }
                    />
                  </TableCell>
                  <TableCell className="text-center">
                    <Switch
                      checked={u.enabled}
                      onCheckedChange={(v) =>
                        updateUserMut.mutate({ uid: u.id, patch: { enabled: v } })
                      }
                    />
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex items-center justify-end gap-2">
                      <span className="hidden text-xs text-muted-foreground xl:inline">
                        {formatDateTime(u.created_at)}
                      </span>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-destructive"
                        onClick={() => deleteUserMut.mutate(u.id)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              {users.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="h-20 text-center text-muted-foreground">
                    还没有授权用户
                  </TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
  );

  if (mode === "interaction") {
    return interactionContent;
  }

  return (
    <div className="space-y-6">
      {managementContent}
      {usersContent}
    </div>
  );
}
