// 自动回复配置：列出该账号的 auto_reply rule，CRUD + 试运行
import { useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Plus, Pencil, Trash2, Play, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Select } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Spinner } from "@/components/ui/misc";
import type {
  AutoReplyMatch,
  AutoReplyCooldownScope,
  AutoReplyRuleConfig,
  AutoReplyScope,
  RuleDryRunResponse,
  RuleOut,
} from "@/api/types";
import { featureConfigBackTarget } from "@/pages/Plugins/_shared/featureConfig";
import {
  DryRunDialogShell,
  Field,
  RuleEditDialogShell,
  RuleFeatureToggleCard,
  RuleInfoBox,
  RulePageHeader,
  useRuleCrud,
} from "./_shared";

// rule.config 默认值
function defaultConfig(): AutoReplyRuleConfig {
  return {
    match: "keyword",
    patterns: [],
    scope: "private",
    reply: "",
    cooldown_seconds: 0,
    cooldown_scope: "chat",
    daily_limit_per_user: 0,
    usage_label: "",
    cooldown_notice_enabled: true,
    daily_limit_notice_enabled: true,
    case_sensitive: false,
    reply_to: true,    // 默认以引用形式回复
  };
}

function readConfig(c: Record<string, unknown> | undefined): AutoReplyRuleConfig {
  const def = defaultConfig();
  if (!c) return def;
  return { ...def, ...(c as Partial<AutoReplyRuleConfig>) };
}

interface FormState {
  name: string;
  enabled: boolean;
  priority: number;
  config: AutoReplyRuleConfig;
}

function emptyForm(): FormState {
  return { name: "", enabled: true, priority: 100, config: defaultConfig() };
}

export function AutoReplyConfig() {
  const params = useParams();
  const nav = useNavigate();
  const location = useLocation();
  const aid = Number(params.aid);

  const crud = useRuleCrud({
    aid,
    ruleKind: "auto_reply",
    featureKey: "auto_reply",
  });

  // ===================== 编辑/新建 Dialog =====================
  const [editOpen, setEditOpen] = useState(false);
  const [editing, setEditing] = useState<RuleOut | null>(null);
  const [form, setForm] = useState<FormState>(emptyForm());
  // patterns 文本编辑（一行一条）
  const [patternsText, setPatternsText] = useState("");
  // group_ids 文本编辑（自由编辑，保存时再 split）
  const [groupIdsText, setGroupIdsText] = useState("");

  function openCreate() {
    setEditing(null);
    setForm(emptyForm());
    setPatternsText("");
    setGroupIdsText("");
    setEditOpen(true);
  }
  function openEdit(r: RuleOut) {
    setEditing(r);
    const cfg = readConfig(r.config);
    setForm({
      name: r.name,
      enabled: r.enabled,
      priority: r.priority,
      config: cfg,
    });
    setPatternsText((cfg.patterns || []).join("\n"));
    setGroupIdsText((cfg.group_ids || []).join("\n"));
    setEditOpen(true);
  }

  async function handleSave() {
    const patterns = patternsText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    const groupIds = groupIdsText
      .split(/[\s,，;；]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    const payload = {
      name: form.name.trim(),
      enabled: form.enabled,
      priority: form.priority,
      config: { ...form.config, patterns, group_ids: groupIds } as Record<
        string,
        unknown
      >,
    };
    if (!payload.name) return;
    await crud.saveRule({
      editing,
      payload,
      onSuccess: () => setEditOpen(false),
    });
  }

  // ===================== 试运行 Dialog =====================
  const [dryOpen, setDryOpen] = useState(false);
  const [dryRule, setDryRule] = useState<RuleOut | null>(null);
  const [drySample, setDrySample] = useState("");
  const [dryChat, setDryChat] = useState<"private" | "group">("private");
  const [dryChatId, setDryChatId] = useState("");
  const [dryResult, setDryResult] = useState<RuleDryRunResponse | null>(null);

  // 打开试运行：根据规则 scope 推导默认会话类型 + 默认 chat_id
  function openDryRun(rule: RuleOut) {
    setDryRule(rule);
    setDrySample("");
    setDryResult(null);
    const cfg = (rule.config || {}) as Record<string, unknown>;
    const scope = cfg.scope as string | undefined;
    if (scope === "private") {
      setDryChat("private");
    } else if (scope === "group_all" || scope === "group_specific") {
      setDryChat("group");
    }
    if (scope === "group_specific") {
      const gids = (cfg.group_ids as string[]) || [];
      setDryChatId(gids[0] ?? "");
    } else {
      setDryChatId("");
    }
    setDryOpen(true);
  }

  function handleDryRun() {
    if (!dryRule) return;
    crud.dryRun({
      rid: dryRule.id,
      payload: {
        sample_message: drySample,
        sample_chat_type: dryChat,
        sample_chat_id: dryChatId ? Number(dryChatId) : undefined,
      },
      onSuccess: (res) => setDryResult(res),
    });
  }

  if (!aid) return <p>账号 ID 不合法</p>;
  const backTarget = featureConfigBackTarget(aid, location.search);

  return (
    <div className="space-y-6">
      <RulePageHeader
        title={`自动回复配置 · #${aid}`}
        backLabel={backTarget.backLabel}
        backHref={backTarget.backHref}
      />

      <RuleInfoBox>
        <li>保存后立即生效，无需重启 worker。</li>
        <li>
          <b>仅响应别人发来的消息</b>（incoming）。用绑定的 userbot 账号自己发关键词
          <b>不会触发</b>——必须用其他账号在群里 / 私聊里发。
        </li>
        <li>
          正则模式支持捕获参数：模式 <code>^置顶\s+(\d+)$</code>，回复内容填
          <code>{"{prefix}"}pt {"{1}"}</code>，群友发 <code>置顶 12345</code> 时会由本账号代发白名单指令。
        </li>
        <li>
          不懂正则时选“变量模式”：模式写 <code>置顶 id=数字</code>，群友发
          <code>置顶 id=12345</code>，回复写 <code>{"{prefix}"}pt {"{id}"}</code>；游戏金额可写
          <code>我要猜骰 num=数字</code>。
        </li>
        <li>
          可选参数支持默认值：模式 <code>^我要猜骰\s*(\d+)?$</code>，回复内容填
          <code>。ct {"{1|1000}"}</code>，没带数字时自动使用 <code>1000</code>。
        </li>
        <li>
          频率限制支持按群或按用户冷却，也可以设置“每人每日上限”。例如每个群友一天最多 2 次：冷却对象选“每个用户”，冷却时间填 <code>6h</code>，每人每日上限填 2。
        </li>
        <li>
          自动命令成功后会把今日成功次数追加到结果底部；冷却中也会提示剩余 CD 和今日次数。规则名称或“提示名称”会进入文案，例如 <code>置顶促销</code> 会显示“今日已成功置顶促销 1/2 次”。
        </li>
        <li>
          管理员可在群里回复某个群友的消息发送 <code>命令前缀 + arcd</code> 重置相关会话/用户冷却和他的今日次数；也可发送 <code>命令前缀 + arcd 123456789</code> 按用户 ID 重置。
        </li>
        <li>
          不命中时去「日志中心」筛 source=plugin/worker 的 info
          条，会显示 <code>[event]</code> 收到了什么、<code>[auto_reply]</code> 跳过的具体原因。
        </li>
      </RuleInfoBox>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldCheck className="h-4 w-4" />
            自动指令白名单
          </CardTitle>
          <CardDescription>
            如果回复内容是指令，例如“我要玩 24 点”命中后回复 {"命令前缀 + 24d 100"}，
            需要先把 <code>24d</code> 加入本账号白名单；普通成员仍不能直接触发插件指令。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            variant="outline"
            onClick={() => nav(`/plugins/auto-command-whitelist?aid=${aid}`)}
          >
            配置自动指令白名单
          </Button>
        </CardContent>
      </Card>

      <RuleFeatureToggleCard
        enabled={crud.isFeatureEnabled}
        onToggle={crud.toggleFeature}
        state={crud.featureItem?.state}
        lastError={crud.featureItem?.last_error}
      />

      {/* 规则列表 */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base">规则</CardTitle>
              <CardDescription>支持关键词与正则；按优先级排序</CardDescription>
            </div>
            <Button onClick={openCreate}>
              <Plus className="mr-1 h-4 w-4" /> 新建规则
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {crud.rulesQ.isLoading ? (
            <div className="flex h-20 items-center justify-center">
              <Spinner className="text-primary" />
            </div>
          ) : crud.rulesQ.data && crud.rulesQ.data.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>启用</TableHead>
                  <TableHead>优先级</TableHead>
                  <TableHead>匹配</TableHead>
                  <TableHead>作用范围</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {crud.rulesQ.data.map((r) => {
                  const cfg = readConfig(r.config);
                  return (
                    <TableRow key={r.id}>
                      <TableCell className="font-medium">{r.name}</TableCell>
                      <TableCell>
                        <Badge variant={r.enabled ? "success" : "secondary"}>
                          {r.enabled ? "ON" : "OFF"}
                        </Badge>
                      </TableCell>
                      <TableCell>{r.priority}</TableCell>
                      <TableCell>
                        {cfg.match === "regex" ? "正则" : cfg.match === "template" ? "变量" : "关键词"}（
                        {cfg.patterns?.length ?? 0}）
                      </TableCell>
                      <TableCell>{scopeLabel(cfg.scope)}</TableCell>
                      <TableCell className="text-right">
                        <div className="inline-flex gap-1">
                          <Button size="sm" variant="ghost" onClick={() => openEdit(r)}>
                            <Pencil className="mr-1 h-3.5 w-3.5" /> 编辑
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => openDryRun(r)}>
                            <Play className="mr-1 h-3.5 w-3.5" /> 试运行
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-destructive"
                            onClick={() => {
                              if (confirm(`删除规则 ${r.name}？`)) crud.removeRule(r.id);
                            }}
                          >
                            <Trash2 className="mr-1 h-3.5 w-3.5" /> 删除
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          ) : (
            <p className="py-8 text-center text-sm text-muted-foreground">
              暂无规则，点击右上角「新建规则」
            </p>
          )}
        </CardContent>
      </Card>

      {/* 编辑 / 新建 */}
      <RuleEditDialogShell
        open={editOpen}
        onOpenChange={setEditOpen}
        editing={editing}
        description={"支持变量：{sender} {chat} {text} {prefix}；变量模式可写 置顶 id=数字，回复用 {id}；问号表示可选参数，例如 num=数字?。规则名称会用于冷却提示，也可单独填写提示名称。"}
        name={form.name}
        enabled={form.enabled}
        priority={form.priority}
        onNameChange={(v) => setForm({ ...form, name: v })}
        onEnabledChange={(v) => setForm({ ...form, enabled: v })}
        onPriorityChange={(v) => setForm({ ...form, priority: v })}
        onSave={handleSave}
        saving={crud.saving}
      >
        <div className="grid grid-cols-2 gap-3">
          <Field label="匹配类型">
            <Select
              value={form.config.match}
              onChange={(e) =>
                setForm({
                  ...form,
                  config: { ...form.config, match: e.target.value as AutoReplyMatch },
                })
              }
            >
              <option value="keyword">关键词</option>
              <option value="template">变量模式</option>
              <option value="regex">正则</option>
            </Select>
          </Field>
          <Field label="作用范围">
            <Select
              value={form.config.scope}
              onChange={(e) =>
                setForm({
                  ...form,
                  config: { ...form.config, scope: e.target.value as AutoReplyScope },
                })
              }
            >
              <option value="private">仅私聊</option>
              <option value="group_all">所有群</option>
              <option value="group_specific">指定群</option>
            </Select>
          </Field>
        </div>

        {form.config.scope === "group_specific" && (
          <Field label="指定群 ID（每行一个，或用空格 / 逗号分隔）">
            <Textarea
              rows={4}
              placeholder={
                "支持以下任一格式：\n" +
                "  -1001234567890   （Telethon 内部 id）\n" +
                "  1234567890       （从 t.me/c/<id> 复制）\n" +
                "  -1234567890      （basic group id）"
              }
              value={groupIdsText}
              onChange={(e) => setGroupIdsText(e.target.value)}
            />
          </Field>
        )}

        <Field label="模式（每行一条）">
          <Textarea
            rows={4}
            value={patternsText}
            onChange={(e) => setPatternsText(e.target.value)}
            placeholder={
              form.config.match === "regex"
                ? "例：^置顶\\s+(\\d+)$"
                : form.config.match === "template"
                  ? "例：置顶 id=数字\n我要猜骰 num=数字?"
                  : "例：你好\n在吗"
            }
          />
        </Field>

        <Field label="回复内容">
          <Textarea
            rows={3}
            value={form.config.reply}
            onChange={(e) =>
              setForm({ ...form, config: { ...form.config, reply: e.target.value } })
            }
            placeholder="支持变量：{sender}、{chat}、{text}、{prefix}"
          />
          <p className="text-xs leading-5 text-muted-foreground">
            正则模式可把捕获组写进回复内容，例如 <code>{"{prefix}"}pt {"{1}"}</code>；
            变量模式在“模式”里写 <code>置顶 id=数字</code>，群友消息应写 <code>置顶 id=12345</code>，回复内容写 <code>{"{prefix}"}pt {"{id}"}</code>。
            <code>num=数字</code> 会提取 <code>num=</code> 后面的数字；末尾加 <code>?</code> 表示这个参数可以不填；
            默认值写作 <code>{"{num|1000}"}</code>。若回复内容是指令，仍必须先加入自动指令白名单，并受本规则冷却限制。
          </p>
        </Field>

        <Field label="提示名称（用于冷却和上限文案）">
          <Input
            placeholder="例：置顶、猜骰、推广"
            value={form.config.usage_label ?? ""}
            onChange={(e) =>
              setForm({
                ...form,
                config: { ...form.config, usage_label: e.target.value },
              })
            }
          />
          <p className="text-xs leading-5 text-muted-foreground">
            留空时使用规则名称。填 <code>置顶促销</code> 后，成功和冷却提示会显示
            <code>今日已成功置顶促销 1/2 次</code>；达到上限时会显示不能再次使用置顶促销功能。
          </p>
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="冷却时间（0 不限制）">
            <Input
              inputMode="text"
              placeholder="0、30s、5m、6h、2d"
              value={(form.config.cooldown_seconds ?? 0).toString()}
              onChange={(e) =>
                setForm({
                  ...form,
                  config: {
                    ...form.config,
                    cooldown_seconds: e.target.value.trim(),
                  },
                })
              }
            />
            <p className="text-xs leading-5 text-muted-foreground">
              支持 s/m/h/d：例如 <code>30s</code>、<code>5m</code>、<code>6h</code>、<code>2d</code>。纯数字仍按秒计算。
            </p>
          </Field>
          <Field label="冷却对象">
            <Select
              value={form.config.cooldown_scope ?? "chat"}
              onChange={(e) =>
                setForm({
                  ...form,
                  config: {
                    ...form.config,
                    cooldown_scope: e.target.value as AutoReplyCooldownScope,
                  },
                })
              }
            >
              <option value="chat">当前会话</option>
              <option value="user">每个用户</option>
            </Select>
          </Field>
          <Field label="每人每日上限（0 不限制）">
            <Input
              inputMode="numeric"
              value={(form.config.daily_limit_per_user ?? 0).toString()}
              onChange={(e) =>
                setForm({
                  ...form,
                  config: {
                    ...form.config,
                    daily_limit_per_user: Number(
                      e.target.value.replace(/[^0-9]/g, "") || 0,
                    ),
                  },
                })
              }
            />
          </Field>
          <Field label="冷却中提示">
            <div className="flex h-10 items-center gap-2">
              <Switch
                checked={form.config.cooldown_notice_enabled !== false}
                onCheckedChange={(v) =>
                  setForm({
                    ...form,
                    config: { ...form.config, cooldown_notice_enabled: v },
                  })
                }
              />
              <span className="text-xs text-muted-foreground">
                显示剩余 CD 和今日次数
              </span>
            </div>
          </Field>
          <Field label="每日上限提示">
            <div className="flex h-10 items-center gap-2">
              <Switch
                checked={form.config.daily_limit_notice_enabled !== false}
                onCheckedChange={(v) =>
                  setForm({
                    ...form,
                    config: { ...form.config, daily_limit_notice_enabled: v },
                  })
                }
              />
              <span className="text-xs text-muted-foreground">
                达到上限时提醒联系管理员
              </span>
            </div>
          </Field>
          <Field label="区分大小写">
            <div className="flex h-10 items-center">
              <Switch
                checked={!!form.config.case_sensitive}
                onCheckedChange={(v) =>
                  setForm({
                    ...form,
                    config: { ...form.config, case_sensitive: v },
                  })
                }
              />
            </div>
          </Field>
          <Field label="以「引用」形式回复">
            <div className="flex h-10 items-center gap-2">
              <Switch
                checked={form.config.reply_to !== false}
                onCheckedChange={(v) =>
                  setForm({ ...form, config: { ...form.config, reply_to: v } })
                }
              />
              <span className="text-xs text-muted-foreground">
                开 = 引用触发消息；关 = 发新消息
              </span>
            </div>
          </Field>
          <Field label="白名单（每行一个 user_id，可选）">
            <Textarea
              rows={2}
              value={(form.config.whitelist || []).join("\n")}
              onChange={(e) =>
                setForm({
                  ...form,
                  config: {
                    ...form.config,
                    whitelist: e.target.value
                      .split("\n")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  },
                })
              }
            />
          </Field>
          <Field label="黑名单（每行一个 user_id，可选）">
            <Textarea
              rows={2}
              value={(form.config.blacklist || []).join("\n")}
              onChange={(e) =>
                setForm({
                  ...form,
                  config: {
                    ...form.config,
                    blacklist: e.target.value
                      .split("\n")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  },
                })
              }
            />
          </Field>
        </div>
      </RuleEditDialogShell>

      {/* 试运行 */}
      <DryRunDialogShell
        open={dryOpen}
        onOpenChange={setDryOpen}
        rule={dryRule}
        description="输入一条样例消息，验证规则是否命中、回复内容"
        onRun={handleDryRun}
        runDisabled={!drySample}
        pending={crud.dryRunPending}
        result={dryResult}
      >
        <Field label="样例消息">
          <Textarea
            rows={3}
            value={drySample}
            onChange={(e) => setDrySample(e.target.value)}
          />
        </Field>
        <Field label="会话类型">
          <Select
            value={dryChat}
            onChange={(e) => setDryChat(e.target.value as "private" | "group")}
          >
            <option value="private">私聊</option>
            <option value="group">群聊</option>
          </Select>
        </Field>
        {dryChat === "group" && (
          <Field label="样本群 ID（可选；留空 = 任意群，scope=group_specific 时自动取规则中第一项）">
            <Input
              inputMode="numeric"
              placeholder="例：-1001234567890 或 1234567890"
              value={dryChatId}
              onChange={(e) => setDryChatId(e.target.value.replace(/[^0-9-]/g, ""))}
            />
          </Field>
        )}
      </DryRunDialogShell>
    </div>
  );
}

function scopeLabel(s: AutoReplyScope): string {
  switch (s) {
    case "private":
      return "私聊";
    case "group_all":
      return "全部群";
    case "group_specific":
      return "指定群";
    default:
      return s;
  }
}
