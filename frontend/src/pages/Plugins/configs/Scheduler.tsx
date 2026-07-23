import { useMemo, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { History, Pencil, Play, Plus, Trash2, Zap } from "lucide-react";
import { toast } from "sonner";

import { getSystemSettings } from "@/api/system";
import { listAccounts } from "@/api/accounts";
import {
  executeRule,
} from "@/api/features";
import type {
  RuleDryRunResponse,
  RuleExecuteResponse,
  RuleOut,
  SchedulerRuleConfig,
} from "@/api/types";
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
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/misc";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { getErrMsg } from "@/lib/api";
import { normalizeSchedulerTarget } from "@/lib/schedulerTarget";
import { formatDateTime } from "@/lib/utils";
import { DryRunDetail } from "@/components/DryRunDetail";
import { featureConfigBackTarget } from "@/pages/Plugins/_shared/featureConfig";
import {
  Field,
  RuleInfoBox,
  RuleEditDialogShell,
  RulePageHeader,
  useRuleCrud,
} from "./_shared";

function defaultConfig(commandPrefix = ","): SchedulerRuleConfig {
  return {
    kind: "cron",
    cron: "*/5 * * * *",
    fire_at: "",
    interval_sec: 300,
    enabled: true,
    action: {
      type: "send_message",
      target_chat_id: "",
      text: "tick",
      command: `${commandPrefix}help`,
      provider_id: 0,
      prompt: "今天要做什么？",
      system_prompt: "你是简洁有用的中文助手。",
      max_tokens: 256,
      delete_after: null,
    },
    next_fire: null,
  };
}

function readConfig(c: Record<string, unknown> | undefined, commandPrefix = ","): SchedulerRuleConfig {
  return { ...defaultConfig(commandPrefix), ...(c as Partial<SchedulerRuleConfig> | undefined) };
}

function configForSave(config: SchedulerRuleConfig): Record<string, unknown> {
  const {
    next_fire: _nextFire,
    last_fire: _lastFire,
    last_result: _lastResult,
    last_error: _lastError,
    ...rest
  } = config as SchedulerRuleConfig & Record<string, unknown>;
  delete rest._last_cron;
  delete rest._cron_seconds_mode;
  delete rest._cron_timezone;
  delete rest._config_dirty;
  const action = { ...config.action };
  const targetRequired = ["send_message", "call_llm"].includes(action.type);
  const target = normalizeSchedulerTarget(action.target_chat_id, targetRequired);
  if (target === undefined) {
    delete action.target_chat_id;
  } else {
    action.target_chat_id = target;
  }
  return { ...rest, action };
}

interface FormState {
  name: string;
  enabled: boolean;
  priority: number;
  config: SchedulerRuleConfig;
}

function emptyForm(commandPrefix = ","): FormState {
  return { name: "", enabled: true, priority: 100, config: defaultConfig(commandPrefix) };
}

export function SchedulerConfig() {
  const params = useParams();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const aidFromPath = Number(params.aid);
  const aidFromQuery = Number(searchParams.get("aid"));
  const aid =
    Number.isFinite(aidFromPath) && aidFromPath > 0
      ? aidFromPath
      : Number.isFinite(aidFromQuery) && aidFromQuery > 0
        ? aidFromQuery
        : 0;
  const fromAccountRoute = Number.isFinite(aidFromPath) && aidFromPath > 0;
  const nav = useNavigate();
  const accountBackTarget = featureConfigBackTarget(aid, location.search);

  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });

  const tzQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const tz = tzQ.data?.timezone || "Asia/Shanghai";
  const cmdPrefix = tzQ.data?.command_prefix || ",";
  // Scheduler 不需要 featureKey（没有"功能总开关"语义）
  const crud = useRuleCrud({ aid, ruleKind: "scheduler" });

  const [editOpen, setEditOpen] = useState(false);
  const [editing, setEditing] = useState<RuleOut | null>(null);
  const [form, setForm] = useState<FormState>(() => emptyForm(cmdPrefix));
  const cronPreview = useMemo(
    () => buildCronPreview(form.config.cron || "", tz),
    [form.config.cron, tz],
  );

  function openCreate() {
    setEditing(null);
    setForm(emptyForm(cmdPrefix));
    setEditOpen(true);
  }

  function openEdit(r: RuleOut) {
    setEditing(r);
    setForm({
      name: r.name,
      enabled: r.enabled,
      priority: r.priority,
      config: readConfig(r.config, cmdPrefix),
    });
    setEditOpen(true);
  }

  async function handleSave() {
    if (!form.name.trim()) {
      toast.error("规则名称必填");
      return;
    }
    const cfg = form.config;
    if (cfg.kind === "cron" && !(cfg.cron || "").trim()) {
      toast.error("cron 表达式必填");
      return;
    }
    if (cfg.kind === "once" && !(cfg.fire_at || "").trim()) {
      toast.error("once 模式 fire_at 必填");
      return;
    }
    if (cfg.kind === "interval" && Number(cfg.interval_sec || 0) <= 0) {
      toast.error("interval_sec 必须 > 0");
      return;
    }
    if (!cfg.action?.type) {
      toast.error("action.type 必填");
      return;
    }
    let saveConfig: Record<string, unknown>;
    try {
      saveConfig = configForSave(cfg);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "目标聊天格式无效");
      return;
    }
    if (cfg.action.type === "send_message" && !(cfg.action.text || "").trim()) {
      toast.error("send_message 的 text 必填");
      return;
    }
    if (
      cfg.action.type === "run_command" &&
      !(cfg.action.command || cfg.action.text || "").trim()
    ) {
      toast.error("run_command 的 command 必填");
      return;
    }
    if (cfg.action.type === "call_llm") {
      if (!cfg.action.provider_id) {
        toast.error("call_llm 的 provider_id 必填");
        return;
      }
      if (!(cfg.action.prompt || "").trim()) {
        toast.error("call_llm 的 prompt 必填");
        return;
      }
    }
    if (cfg.action.delete_after != null && cfg.action.delete_after > 3600) {
      toast.error("delete_after 上限为 3600 秒");
      return;
    }

    await crud.saveRule({
      editing,
      payload: {
        name: form.name.trim(),
        enabled: form.enabled,
        priority: form.priority,
        config: saveConfig,
      },
      onSuccess: () => setEditOpen(false),
    });
  }

  const [dryOpen, setDryOpen] = useState(false);
  const [dryRule, setDryRule] = useState<RuleOut | null>(null);
  const [dryResult, setDryResult] = useState<RuleDryRunResponse | null>(null);

  function openDryRun(rule: RuleOut) {
    setDryRule(rule);
    setDryResult(null);
    setDryOpen(true);
  }

  function handleDryRun() {
    if (!dryRule) return;
    crud.dryRun({
      rid: dryRule.id,
      payload: {
        sample_message: "scheduler dry-run",
        sample_chat_type: "private",
      },
      onSuccess: (res) => setDryResult(res),
    });
  }

  // executeRule 是 Scheduler 独有，不进 useRuleCrud
  const [execOpen, setExecOpen] = useState(false);
  const [execRule, setExecRule] = useState<RuleOut | null>(null);
  const [execResult, setExecResult] = useState<RuleExecuteResponse | null>(null);

  function openExec(rule: RuleOut) {
    setExecRule(rule);
    setExecResult(null);
    setExecOpen(true);
  }

  const execMut = useMutation({
    mutationFn: () => executeRule(aid, "scheduler", execRule!.id),
    onSuccess: (res) => {
      setExecResult(res);
      if (res.ok) {
        crud.rulesQ.refetch();
      }
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  if (!aid) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">定时任务</h1>
          <p className="text-sm text-muted-foreground">
            选择账号后管理该账号的定时任务规则。
          </p>
        </div>
        <Card>
          <CardHeader>
            <CardTitle className="text-base">选择账号</CardTitle>
            <CardDescription>
              定时任务按账号隔离运行，每个账号独立维护规则。
            </CardDescription>
          </CardHeader>
          <CardContent>
            {accountsQ.isLoading ? (
              <div className="flex h-20 items-center justify-center">
                <Spinner className="text-primary" />
              </div>
            ) : accountsQ.data && accountsQ.data.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {accountsQ.data.map((a) => (
                  <Button
                    key={a.id}
                    variant="outline"
                    onClick={() => setSearchParams({ aid: String(a.id) })}
                  >
                    {a.display_name || a.phone || `账号 #${a.id}`}
                  </Button>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                暂无可用账号，请先绑定账号。
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <RulePageHeader
        title={`定时任务 · 账号 #${aid}`}
        backLabel={fromAccountRoute ? accountBackTarget.backLabel : "返回定时任务"}
        backHref={fromAccountRoute ? accountBackTarget.backHref : "/operations/scheduler"}
      />

      <RuleInfoBox>
        <li>定时任务按账号隔离运行，每个账号独立维护规则。</li>
        <li>支持 cron 定时、once 单次和 interval 间隔触发。</li>
        <li>动作可以发送消息、执行指令或调用 AI 模型；是否执行由每条规则自己的启用状态控制。</li>
      </RuleInfoBox>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base">基础能力状态</CardTitle>
              <CardDescription>
                定时任务调度器随 worker 初始化运行；是否执行由每条规则自己的启用状态控制。
              </CardDescription>
            </div>
            <span className="rounded-full bg-success/15 px-3 py-1 text-xs font-medium text-success">
              随 worker 启动
            </span>
          </div>
        </CardHeader>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-3">
            <div>
              <CardTitle className="text-base">规则</CardTitle>
              <CardDescription>
                支持 cron 定时 / once 单次 / interval 间隔，触发动作：发送消息 / 执行指令 / 调用 LLM
              </CardDescription>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                variant="outline"
                onClick={() => nav(`/logs?view=messages&source_channel=scheduler&event_type=scheduler_fire&account_id=${aid}`)}
              >
                <History className="mr-1 h-4 w-4" /> 运行历史
              </Button>
              <Button onClick={openCreate}>
                <Plus className="mr-1 h-4 w-4" /> 新建规则
              </Button>
            </div>
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
                  <TableHead>触发</TableHead>
                  <TableHead>动作</TableHead>
                  <TableHead>下次触发</TableHead>
                  <TableHead>上次触发</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {crud.rulesQ.data.map((r) => {
                  const cfg = readConfig(r.config, cmdPrefix);
                  return (
                    <TableRow key={r.id}>
                      <TableCell className="font-medium">{r.name}</TableCell>
                      <TableCell>
                        <Badge variant={r.enabled ? "success" : "secondary"}>
                          {r.enabled ? "ON" : "OFF"}
                        </Badge>
                      </TableCell>
                      <TableCell>{r.priority}</TableCell>
                      <TableCell>{triggerLabel(cfg)}</TableCell>
                      <TableCell>
                        <div className="flex flex-col gap-0.5">
                          <span>
                            {ACTION_TYPE_LABELS[cfg.action?.type || "send_message"] ||
                              cfg.action?.type}
                          </span>
                          {cfg.action?.delete_after ? (
                            <span className="text-xs text-muted-foreground">
                              自动删除: {cfg.action.delete_after}s
                            </span>
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell className="text-xs font-mono">
                        {formatDateTime(cfg.next_fire, tz)}
                      </TableCell>
                      <TableCell className="text-xs font-mono">
                        {formatDateTime(cfg.last_fire, tz)}
                      </TableCell>
                      <TableCell>
                        {cfg.last_result ? (
                          <div className="flex flex-col gap-0.5">
                            <Badge
                              variant={cfg.last_result === "ok" ? "success" : "destructive"}
                            >
                              {cfg.last_result === "ok" ? "成功" : "失败"}
                            </Badge>
                            {cfg.last_error ? (
                              <span
                                className="max-w-[160px] truncate text-xs text-destructive"
                                title={cfg.last_error}
                              >
                                {cfg.last_error}
                              </span>
                            ) : null}
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">未执行</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="inline-flex gap-1">
                          <Button size="sm" variant="ghost" onClick={() => openEdit(r)}>
                            <Pencil className="mr-1 h-3.5 w-3.5" /> 编辑
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => openDryRun(r)}>
                            <Play className="mr-1 h-3.5 w-3.5" /> 试运行
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => openExec(r)}>
                            <Zap className="mr-1 h-3.5 w-3.5" /> 立即运行一次
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
            <p className="text-sm text-muted-foreground">暂无规则，点击"新建规则"。</p>
          )}
        </CardContent>
      </Card>

      <RuleEditDialogShell
        open={editOpen}
        onOpenChange={setEditOpen}
        editing={editing}
        description="保存后由 worker 热更新，无需重启。"
        maxWidthClass="max-w-2xl"
        name={form.name}
        enabled={form.enabled}
        priority={form.priority}
        onNameChange={(v) => setForm((s) => ({ ...s, name: v }))}
        onEnabledChange={(v) => setForm((s) => ({ ...s, enabled: v }))}
        onPriorityChange={(v) => setForm((s) => ({ ...s, priority: v }))}
        onSave={handleSave}
        saving={crud.saving}
      >
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <Field label="触发类型">
            <Select
              value={form.config.kind}
              onChange={(e) =>
                setForm((s) => ({
                  ...s,
                  config: {
                    ...s.config,
                    kind: e.target.value as SchedulerRuleConfig["kind"],
                  },
                }))
              }
            >
              <option value="cron">cron 定时</option>
              <option value="once">once 单次</option>
              <option value="interval">interval 间隔</option>
            </Select>
            {form.config.kind === "cron" ? <CronExamples /> : null}
          </Field>
          {form.config.kind === "cron" ? (
            <Field label="cron 表达式">
              <Input
                value={form.config.cron || ""}
                onChange={(e) =>
                  setForm((s) => ({
                    ...s,
                    config: { ...s.config, cron: e.target.value },
                  }))
                }
                placeholder="*/5 * * * *"
              />
              <CronPreview preview={cronPreview} timezone={tz} />
            </Field>
          ) : null}
          {form.config.kind === "once" ? (
            <Field label="触发时间">
              <Input
                value={form.config.fire_at || ""}
                onChange={(e) =>
                  setForm((s) => ({
                    ...s,
                    config: { ...s.config, fire_at: e.target.value },
                  }))
                }
                placeholder="2026-05-10T15:30:00+08:00"
              />
            </Field>
          ) : null}
          {form.config.kind === "interval" ? (
            <Field label="间隔秒数">
              <Input
                type="number"
                value={form.config.interval_sec || 0}
                onChange={(e) =>
                  setForm((s) => ({
                    ...s,
                    config: {
                      ...s.config,
                      interval_sec: Number(e.target.value || 0),
                    },
                  }))
                }
                placeholder="300"
              />
            </Field>
          ) : null}
        </div>

        <div className="space-y-3 rounded-md border p-3">
          <Field label="动作类型">
            <Select
              value={form.config.action.type}
              onChange={(e) =>
                setForm((s) => ({
                  ...s,
                  config: {
                    ...s.config,
                    action: {
                      ...s.config.action,
                      type: e.target.value as SchedulerRuleConfig["action"]["type"],
                    },
                  },
                }))
              }
            >
              <option value="send_message">发送消息</option>
              <option value="run_command">执行指令</option>
              <option value="call_llm">调用 LLM</option>
            </Select>
          </Field>

          {form.config.action.type === "send_message" ||
          form.config.action.type === "call_llm" ? (
            <Field label="目标聊天（数字 ID 或 @username）">
              <Input
                type="text"
                placeholder="8395686237、-1001234567890 或 @qingbaobu"
                value={form.config.action.target_chat_id ?? ""}
                onChange={(e) =>
                  setForm((s) => ({
                    ...s,
                    config: {
                      ...s.config,
                      action: {
                        ...s.config.action,
                        target_chat_id: e.target.value,
                      },
                    },
                  }))
                }
              />
            </Field>
          ) : null}

          {form.config.action.type === "send_message" ? (
            <>
              <Field label="消息内容">
                <Textarea
                  value={form.config.action.text || ""}
                  onChange={(e) =>
                    setForm((s) => ({
                      ...s,
                      config: {
                        ...s.config,
                        action: { ...s.config.action, text: e.target.value },
                      },
                    }))
                  }
                  rows={4}
                />
              </Field>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <Field label="自动删除（秒）">
                  <Input
                    type="number"
                    min={0}
                    max={3600}
                    value={form.config.action.delete_after ?? 0}
                    onChange={(e) =>
                      setForm((s) => ({
                        ...s,
                        config: {
                          ...s.config,
                          action: {
                            ...s.config.action,
                            delete_after: Number(e.target.value) || 0,
                          },
                        },
                      }))
                    }
                    placeholder="0 = 不删除"
                  />
                  <p className="text-xs text-muted-foreground">
                    发送后多少秒自动删除，0 或留空 = 不删除，上限 3600
                  </p>
                </Field>
              </div>
            </>
          ) : null}

          {form.config.action.type === "run_command" ? (
            <Field label="指令">
              <Input
                value={form.config.action.command || ""}
                onChange={(e) =>
                  setForm((s) => ({
                    ...s,
                    config: {
                      ...s.config,
                      action: { ...s.config.action, command: e.target.value },
                    },
                  }))
                }
                placeholder={`${cmdPrefix}ai 今天天气`}
              />
            </Field>
          ) : null}

          {form.config.action.type === "call_llm" ? (
            <>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <Field label="服务商 ID">
                  <Input
                    type="number"
                    value={form.config.action.provider_id || 0}
                    onChange={(e) =>
                      setForm((s) => ({
                        ...s,
                        config: {
                          ...s.config,
                          action: {
                            ...s.config.action,
                            provider_id: Number(e.target.value || 0),
                          },
                        },
                      }))
                    }
                  />
                </Field>
                <Field label="最大 Token 数">
                  <Input
                    type="number"
                    value={form.config.action.max_tokens || 256}
                    onChange={(e) =>
                      setForm((s) => ({
                        ...s,
                        config: {
                          ...s.config,
                          action: {
                            ...s.config.action,
                            max_tokens: Number(e.target.value || 0),
                          },
                        },
                      }))
                    }
                  />
                </Field>
              </div>
              <Field label="系统提示词">
                <Textarea
                  value={form.config.action.system_prompt || ""}
                  onChange={(e) =>
                    setForm((s) => ({
                      ...s,
                      config: {
                        ...s.config,
                        action: { ...s.config.action, system_prompt: e.target.value },
                      },
                    }))
                  }
                  rows={2}
                />
              </Field>
              <Field label="提示词">
                <Textarea
                  value={form.config.action.prompt || ""}
                  onChange={(e) =>
                    setForm((s) => ({
                      ...s,
                      config: {
                        ...s.config,
                        action: { ...s.config.action, prompt: e.target.value },
                      },
                    }))
                  }
                  rows={4}
                />
              </Field>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <Field label="自动删除（秒）">
                  <Input
                    type="number"
                    min={0}
                    max={3600}
                    value={form.config.action.delete_after ?? 0}
                    onChange={(e) =>
                      setForm((s) => ({
                        ...s,
                        config: {
                          ...s.config,
                          action: {
                            ...s.config.action,
                            delete_after: Number(e.target.value) || 0,
                          },
                        },
                      }))
                    }
                    placeholder="0 = 不删除"
                  />
                  <p className="text-xs text-muted-foreground">
                    发送后多少秒自动删除，0 或留空 = 不删除，上限 3600
                  </p>
                </Field>
              </div>
            </>
          ) : null}
        </div>
      </RuleEditDialogShell>

      <Dialog open={dryOpen} onOpenChange={setDryOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>试运行</DialogTitle>
            <DialogDescription>{dryRule ? `规则：${dryRule.name}` : ""}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <Button
              onClick={handleDryRun}
              disabled={!dryRule || crud.dryRunPending}
            >
              {crud.dryRunPending ? "运行中..." : "执行试运行"}
            </Button>
            {dryResult ? (
              <>
                <div className="rounded-md border p-3 space-y-1">
                  <div>
                    匹配：<b>{dryResult.matched ? "是" : "否"}</b>
                  </div>
                  {dryResult.output && (
                    <div className="text-xs text-muted-foreground">{dryResult.output}</div>
                  )}
                </div>
                <DryRunDetail detail={dryResult.detail} />
              </>
            ) : null}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={execOpen} onOpenChange={setExecOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>立即执行</DialogTitle>
            <DialogDescription>
              {execRule ? `规则：${execRule.name} — 将真实发送消息/执行动作` : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <Button onClick={() => execMut.mutate()} disabled={!execRule || execMut.isPending}>
              {execMut.isPending ? "执行中..." : "立即执行"}
            </Button>
            {execResult ? (
              <div className="rounded-md border p-3 space-y-1">
                <div>
                  结果：
                  <b
                    className={
                      execResult.ok
                        ? "text-success"
                        : "text-destructive"
                    }
                  >
                    {execResult.ok ? "成功" : "失败"}
                  </b>
                </div>
                {execResult.error && (
                  <div className="text-xs text-destructive">错误：{execResult.error}</div>
                )}
              </div>
            ) : null}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

interface CronPreviewResult {
  ok: boolean;
  error?: string;
  fieldHint: string;
  summary: string;
  next: Date[];
}

const CRON_EXAMPLES = [
  { expr: "*/5 * * * *", label: "每5分钟" },
  { expr: "0 9 * * 1-5", label: "工作日9点" },
  { expr: "0 5 11 * * *", label: "每天11:05:00" },
  { expr: "0 0 1 * *", label: "每月1号零点" },
  { expr: "*/30 * * * *", label: "每30分钟" },
];

function CronExamples() {
  return (
    <div className="mt-2 space-y-1 text-xs text-muted-foreground">
      <div>示例：</div>
      {CRON_EXAMPLES.map((item) => (
        <div key={item.expr} className="flex items-center gap-2">
          <code className="shrink-0 rounded bg-muted px-1">{item.expr}</code>
          <span>{item.label}</span>
        </div>
      ))}
    </div>
  );
}

function CronPreview({ preview, timezone }: { preview: CronPreviewResult; timezone: string }) {
  return (
    <div
      className={[
        "mt-2 rounded-md border px-3 py-2 text-xs",
        preview.ok
          ? "border-success/25 bg-success/10 text-success"
          : "border-destructive/30 bg-destructive/5 text-destructive",
      ].join(" ")}
    >
      <div className="font-medium">{preview.ok ? preview.summary : "cron 表达式无效"}</div>
      <div className="mt-1 text-muted-foreground">{preview.fieldHint}</div>
      {preview.ok ? (
        <div className="mt-2 space-y-1">
          {preview.next.map((item, idx) => (
            <div key={`${item.getTime()}-${idx}`} className="font-mono">
              {idx === 0 ? "下一次：" : `第 ${idx + 1} 次：`}
              {formatCronPreviewDate(item, timezone)}
            </div>
          ))}
        </div>
      ) : (
        <div className="mt-1">{preview.error || "请检查字段数量或范围。"}</div>
      )}
    </div>
  );
}

function triggerLabel(cfg: SchedulerRuleConfig): string {
  if (cfg.kind === "once") return `单次 @ ${cfg.fire_at || "-"}`;
  if (cfg.kind === "interval") return `每 ${cfg.interval_sec || 0} 秒`;
  return cfg.cron || "(无效 cron)";
}

const ACTION_TYPE_LABELS: Record<string, string> = {
  send_message: "发送消息",
  run_command: "执行指令",
  call_llm: "调用 LLM",
};

function buildCronPreview(expr: string, timezone: string): CronPreviewResult {
  const raw = expr.trim();
  if (!raw) {
    return {
      ok: false,
      fieldHint: "支持 5 字段：分 时 日 月 周；6/7 字段：秒 分 时 日 月 周 [年]",
      summary: "",
      next: [],
      error: "cron 表达式不能为空。",
    };
  }

  const parts = raw.split(/\s+/);
  const fieldHint =
    parts.length === 5
      ? "字段解释：分 时 日 月 周"
      : parts.length === 6
        ? "字段解释：秒 分 时 日 月 周"
        : parts.length === 7
          ? "字段解释：秒 分 时 日 月 周 年"
          : "支持 5 字段或 6/7 字段 cron";
  if (![5, 6, 7].includes(parts.length)) {
    return {
      ok: false,
      fieldHint,
      summary: "",
      next: [],
      error: `当前是 ${parts.length} 个字段；请输入 5、6 或 7 个字段。`,
    };
  }

  const offset = parts.length === 5 ? 0 : 1;
  const second = parts.length === 5 ? parseCronField("0", 0, 59) : parseCronField(parts[0], 0, 59);
  const minute = parseCronField(parts[offset], 0, 59);
  const hour = parseCronField(parts[offset + 1], 0, 23);
  const day = parseCronField(parts[offset + 2], 1, 31, { allowQuestion: true });
  const month = parseCronField(parts[offset + 3], 1, 12, { names: MONTH_NAMES });
  const weekday = parseCronField(parts[offset + 4], 0, 7, { allowQuestion: true, names: WEEKDAY_NAMES, mapSevenToZero: true });
  const year = parts.length === 7 ? parseCronField(parts[6], 1970, 2099) : null;
  const fields = [second, minute, hour, day, month, weekday, year].filter(Boolean) as ParsedCronField[];
  const invalid = fields.find((f) => f.error);
  if (invalid) {
    return {
      ok: false,
      fieldHint,
      summary: "",
      next: [],
      error: invalid.error,
    };
  }

  const next = computeNextCronDates({
    second,
    minute,
    hour,
    day,
    month,
    weekday,
    year,
  });
  if (!next.length) {
    return {
      ok: false,
      fieldHint,
      summary: "",
      next: [],
      error: "未来一年内没有匹配时间，请检查日期、星期或年份限制。",
    };
  }

  return {
    ok: true,
    fieldHint,
    summary: `${describeCron(parts)}；按 ${timezone || "Asia/Shanghai"} 预览`,
    next,
  };
}

interface ParsedCronField {
  raw: string;
  values: number[];
  restricted: boolean;
  error?: string;
}

const MONTH_NAMES: Record<string, number> = {
  JAN: 1,
  FEB: 2,
  MAR: 3,
  APR: 4,
  MAY: 5,
  JUN: 6,
  JUL: 7,
  AUG: 8,
  SEP: 9,
  OCT: 10,
  NOV: 11,
  DEC: 12,
};

const WEEKDAY_NAMES: Record<string, number> = {
  SUN: 0,
  MON: 1,
  TUE: 2,
  WED: 3,
  THU: 4,
  FRI: 5,
  SAT: 6,
};

function parseCronField(
  rawField: string,
  min: number,
  max: number,
  options: {
    allowQuestion?: boolean;
    names?: Record<string, number>;
    mapSevenToZero?: boolean;
  } = {},
): ParsedCronField {
  const raw = rawField.trim().toUpperCase();
  const allValues = range(min, options.mapSevenToZero ? max - 1 : max);
  if (raw === "*" || (options.allowQuestion && raw === "?")) {
    return { raw, values: allValues, restricted: false };
  }

  const values = new Set<number>();
  for (const piece of raw.split(",")) {
    const parsed = parseCronPiece(piece, min, max, options);
    if (typeof parsed === "string") {
      return { raw, values: [], restricted: true, error: parsed };
    }
    parsed.forEach((value) => values.add(value));
  }

  return {
    raw,
    values: [...values].sort((a, b) => a - b),
    restricted: true,
  };
}

function parseCronPiece(
  piece: string,
  min: number,
  max: number,
  options: {
    names?: Record<string, number>;
    mapSevenToZero?: boolean;
  },
): number[] | string {
  const [base, stepRaw] = piece.split("/");
  const step = stepRaw ? Number(stepRaw) : 1;
  if (!Number.isInteger(step) || step <= 0) return `步长无效：${piece}`;

  let start: number;
  let end: number;
  if (base === "*") {
    start = min;
    end = max;
  } else if (base.includes("-")) {
    const [left, right] = base.split("-");
    start = parseCronNumber(left, options);
    end = parseCronNumber(right, options);
  } else {
    start = parseCronNumber(base, options);
    end = stepRaw ? max : start;
  }

  if (options.mapSevenToZero) {
    if (start === 7) start = 0;
    if (end === 7 && base !== "*") end = 6;
  }
  if (!Number.isInteger(start) || !Number.isInteger(end)) return `字段无效：${piece}`;
  if (start < min || end > max || start > end) return `字段超出范围：${piece}`;

  const out: number[] = [];
  for (let value = start; value <= end; value += step) {
    out.push(options.mapSevenToZero && value === 7 ? 0 : value);
  }
  return out;
}

function parseCronNumber(raw: string, options: { names?: Record<string, number> }): number {
  const named = options.names?.[raw.toUpperCase()];
  if (named !== undefined) return named;
  return Number(raw);
}

function range(min: number, max: number): number[] {
  return Array.from({ length: max - min + 1 }, (_, idx) => min + idx);
}

function computeNextCronDates(fields: {
  second: ParsedCronField;
  minute: ParsedCronField;
  hour: ParsedCronField;
  day: ParsedCronField;
  month: ParsedCronField;
  weekday: ParsedCronField;
  year: ParsedCronField | null;
}): Date[] {
  const out: Date[] = [];
  const cursor = new Date(Date.now() + 1000);
  cursor.setMilliseconds(0);
  const maxDays = 366;

  for (let dayOffset = 0; dayOffset <= maxDays && out.length < 5; dayOffset += 1) {
    const date = new Date(cursor);
    date.setHours(0, 0, 0, 0);
    date.setDate(date.getDate() + dayOffset);
    const year = date.getFullYear();
    const month = date.getMonth() + 1;
    const dom = date.getDate();
    const dow = date.getDay();

    if (fields.year && !fields.year.values.includes(year)) continue;
    if (!fields.month.values.includes(month)) continue;
    if (!dayMatches(fields.day, fields.weekday, dom, dow)) continue;

    for (const hour of fields.hour.values) {
      for (const minute of fields.minute.values) {
        for (const second of fields.second.values) {
          const candidate = new Date(date);
          candidate.setHours(hour, minute, second, 0);
          if (candidate <= cursor) continue;
          out.push(candidate);
          if (out.length >= 5) return out;
        }
      }
    }
  }
  return out;
}

function dayMatches(day: ParsedCronField, weekday: ParsedCronField, dom: number, dow: number): boolean {
  const dayOk = day.values.includes(dom);
  const weekOk = weekday.values.includes(dow);
  if (day.restricted && weekday.restricted) return dayOk || weekOk;
  return dayOk && weekOk;
}

function describeCron(parts: string[]): string {
  if (parts.length === 5) {
    const [minute, hour, day, month, weekday] = parts;
    if (minute.startsWith("*/") && hour === "*" && day === "*" && month === "*" && weekday === "*") {
      return `每 ${minute.slice(2)} 分钟触发`;
    }
    if (day === "*" && month === "*" && weekday === "*") return `每天 ${padCron(hour)}:${padCron(minute)} 触发`;
    return "已解析为 5 字段 cron";
  }
  const [second, minute, hour, day, month, weekday] = parts;
  if (day === "*" && month === "*" && weekday === "*") {
    return `每天 ${padCron(hour)}:${padCron(minute)}:${padCron(second)} 触发`;
  }
  return "已解析为 6/7 字段 cron";
}

function padCron(raw: string): string {
  return /^\d+$/.test(raw) ? raw.padStart(2, "0") : raw;
}

function formatCronPreviewDate(date: Date, timezone?: string): string {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: timezone || undefined,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}
