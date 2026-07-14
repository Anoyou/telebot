import { useMemo, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellRing, Bot, Link2, Pencil, Send } from "lucide-react";
import { toast } from "sonner";

import { getAccountBot } from "@/api/accountBots";
import { listAccounts } from "@/api/accounts";
import {
  createNotifyBot,
  deleteNotifyBot,
  listNotifyBots,
  testNotifyBot,
  updateNotifyBot,
} from "@/api/notify_bots";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/misc";
import { Select } from "@/components/ui/select";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import { Switch } from "@/components/ui/switch";
import { getErrMsg } from "@/lib/api";

const QK = ["notify-bots"] as const;

type FormState = {
  name: string;
  bot_token: string;
  default_chat_id: string;
  enabled: boolean;
  use_management_bot: boolean;
  source_account_id: string;
};

type EditTarget = {
  id: number;
  name: string;
  chatId: string;
};

const EMPTY_FORM: FormState = {
  name: "default",
  bot_token: "",
  default_chat_id: "",
  enabled: true,
  use_management_bot: false,
  source_account_id: "",
};

function chatTargetLabel(chatId: number): string {
  if (chatId > 0) return `私聊用户或 Bot 对话 · ${chatId}`;
  if (String(chatId).startsWith("-100")) return `超级群或频道 · ${chatId}`;
  return `群聊 · ${chatId}`;
}

function routeNameHint(name: string): string {
  if (name === "default") return "默认路由，用于启动通知和登录验证码";
  if (name === "alert") return "告警专用路由，用于账号 Worker 连续崩溃";
  return "自定义路由，仅在代码显式指定该名称时使用";
}

export function NotifyBots() {
  const qc = useQueryClient();
  const listQ = useQuery({ queryKey: QK, queryFn: listNotifyBots });
  const accountsQ = useQuery({ queryKey: ["accounts"], queryFn: listAccounts });
  const accountBotQueries = useQueries({
    queries: (accountsQ.data ?? []).map((account) => ({
      queryKey: ["account", account.id, "bot"],
      queryFn: () => getAccountBot(account.id),
      staleTime: 30_000,
    })),
  });
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [editTarget, setEditTarget] = useState<EditTarget | null>(null);

  const managementSources = (accountsQ.data ?? []).map((account, index) => ({
    account,
    bot: accountBotQueries[index]?.data,
  }));
  const sourceNameById = useMemo(
    () =>
      new Map(
        managementSources.map(({ account, bot }) => [
          account.id,
          bot?.username
            ? `${account.display_name || `账号 #${account.id}`} · @${bot.username}`
            : account.display_name || `账号 #${account.id}`,
        ]),
      ),
    [managementSources],
  );

  const createMut = useMutation({
    mutationFn: () =>
      createNotifyBot({
        name: form.name.trim(),
        bot_token: form.use_management_bot ? undefined : form.bot_token.trim(),
        source_account_id: form.use_management_bot
          ? Number(form.source_account_id)
          : undefined,
        default_chat_id: Number(form.default_chat_id),
        enabled: form.enabled,
      }),
    onSuccess: () => {
      toast.success("已创建通知路由");
      setForm(EMPTY_FORM);
      qc.invalidateQueries({ queryKey: QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const toggleMut = useMutation({
    mutationFn: async (args: { id: number; enabled: boolean }) =>
      updateNotifyBot(args.id, { enabled: args.enabled }),
    onSuccess: () => {
      toast.success("已更新通知路由");
      qc.invalidateQueries({ queryKey: QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const targetMut = useMutation({
    mutationFn: async (target: EditTarget) => {
      const value = target.chatId.trim();
      if (!/^-?\d+$/.test(value)) throw new Error("Chat ID 必须是整数");
      return updateNotifyBot(target.id, { default_chat_id: Number(value) });
    },
    onSuccess: () => {
      toast.success("已更新接收目标");
      setEditTarget(null);
      qc.invalidateQueries({ queryKey: QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const delMut = useMutation({
    mutationFn: async (id: number) => deleteNotifyBot(id),
    onSuccess: () => {
      toast.success("已删除通知路由");
      qc.invalidateQueries({ queryKey: QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const testMut = useMutation({
    mutationFn: async (id: number) => testNotifyBot(id),
    onSuccess: () => toast.success("测试消息已发送"),
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const canCreate = useMemo(() => {
    if (!form.name.trim() || !form.default_chat_id.trim()) return false;
    if (!/^-?\d+$/.test(form.default_chat_id.trim())) return false;
    if (form.use_management_bot) return /^\d+$/.test(form.source_account_id);
    return Boolean(form.bot_token.trim());
  }, [form]);

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          title="通知 Bot"
          description="单向发送系统通知，不接收命令。可使用独立 Token，也可以安全引用某个账号的管理 Bot。"
          meta={
            <SignalPill
              tone={(listQ.data?.length ?? 0) > 0 ? "success" : "neutral"}
              label="通知路由"
              value={`${listQ.data?.length ?? 0} 条`}
            />
          }
        />
      </CardHeader>
      <CardContent className="space-y-5">
        <section className="nested-surface grid gap-3 border md:grid-cols-3">
          <div>
            <div className="text-sm font-medium">服务启动</div>
            <div className="mt-1 text-xs leading-5 text-muted-foreground">发送当前 TelePilot 版本已启动。</div>
          </div>
          <div>
            <div className="text-sm font-medium">登录安全</div>
            <div className="mt-1 text-xs leading-5 text-muted-foreground">密码风控命中后发送一次性登录验证码。</div>
          </div>
          <div>
            <div className="text-sm font-medium">账号告警</div>
            <div className="mt-1 text-xs leading-5 text-muted-foreground">账号 Worker 连续崩溃并停止时发送告警。</div>
          </div>
        </section>

        <section className="nested-surface space-y-4 border">
          <div>
            <div className="text-sm font-medium">新建通知路由</div>
            <div className="mt-1 text-xs text-muted-foreground">
              路由决定“用哪个 Bot 发到哪个会话”。推荐先建一条 <code>default</code>。
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="notify-route-name">路由名称</Label>
              <Input
                id="notify-route-name"
                placeholder="default / alert"
                value={form.name}
                onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
              />
              <div className="text-xs leading-5 text-muted-foreground">
                <code>default</code> 接收启动通知和登录验证码，<code>alert</code> 可单独承接崩溃告警。
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="notify-chat-id">默认接收 Chat ID</Label>
              <Input
                id="notify-chat-id"
                inputMode="numeric"
                placeholder="私聊填 1682400007，群或频道通常为负数"
                value={form.default_chat_id}
                onChange={(e) => setForm((p) => ({ ...p, default_chat_id: e.target.value }))}
              />
              <div className="text-xs leading-5 text-muted-foreground">
                这是通知接收目标，不是 Bot ID。私聊通常是正数用户 ID，群聊为负数，超级群或频道通常以 <code>-100</code> 开头。
              </div>
            </div>
          </div>

          <div className="nested-surface-item space-y-3 border px-3 py-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Link2 className="h-4 w-4" /> 引用账号管理 Bot
                </div>
                <div className="mt-1 max-w-2xl text-xs leading-5 text-muted-foreground">
                  开启后直接读取所选账号已加密保存的管理 Bot Token，不复制凭据，也不会启动第二个 polling。
                </div>
              </div>
              <Switch
                checked={form.use_management_bot}
                onCheckedChange={(checked) =>
                  setForm((p) => ({
                    ...p,
                    use_management_bot: checked,
                    bot_token: checked ? "" : p.bot_token,
                  }))
                }
              />
            </div>

            {form.use_management_bot ? (
              <div className="space-y-1.5">
                <Label htmlFor="notify-account-bot">选择管理 Bot</Label>
                <Select
                  id="notify-account-bot"
                  value={form.source_account_id}
                  onChange={(e) => setForm((p) => ({ ...p, source_account_id: e.target.value }))}
                >
                  <option value="">请选择已配置管理 Bot 的账号</option>
                  {managementSources.map(({ account, bot }) => (
                    <option key={account.id} value={account.id} disabled={!bot?.has_token}>
                      {account.display_name || `账号 #${account.id}`}
                      {bot?.username ? ` · @${bot.username}` : ""}
                      {!bot?.has_token ? " · 未配置 Token" : ""}
                    </option>
                  ))}
                </Select>
              </div>
            ) : (
              <div className="space-y-1.5">
                <Label htmlFor="notify-bot-token">独立 Bot Token</Label>
                <Input
                  id="notify-bot-token"
                  type="password"
                  autoComplete="off"
                  placeholder="123456:ABC-DEF..."
                  value={form.bot_token}
                  onChange={(e) => setForm((p) => ({ ...p, bot_token: e.target.value }))}
                />
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-3 border-t pt-4">
            <label className="flex items-center gap-2 text-sm">
              <Switch
                checked={form.enabled}
                onCheckedChange={(v) => setForm((p) => ({ ...p, enabled: v }))}
              />
              创建后立即启用
            </label>
            <Button
              className="ml-auto"
              onClick={() => createMut.mutate()}
              disabled={!canCreate || createMut.isPending}
            >
              新建通知路由
            </Button>
          </div>
        </section>

        {listQ.isLoading ? (
          <div className="flex h-20 items-center justify-center">
            <Spinner className="text-primary" />
          </div>
        ) : (
          <div className="space-y-2">
            {(listQ.data || []).map((row) => (
              <div key={row.id} className="nested-surface-item flex flex-col gap-3 border px-3 py-3">
                <div className="flex flex-col gap-3 md:flex-row md:items-center">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="font-medium">{row.name}</div>
                      <Badge variant={row.name === "default" || row.name === "alert" ? "secondary" : "outline"}>
                        {row.name === "default" ? "默认" : row.name === "alert" ? "告警" : "自定义"}
                      </Badge>
                      <Badge variant={row.credential_source === "account_bot" ? "default" : "outline"}>
                        {row.credential_source === "account_bot" ? "引用管理 Bot" : "独立 Token"}
                      </Badge>
                    </div>
                    <div className="mt-1 text-xs leading-5 text-muted-foreground">{routeNameHint(row.name)}</div>
                    <div className="mt-1 text-xs leading-5 text-muted-foreground">
                      {chatTargetLabel(row.default_chat_id)}
                      {row.source_account_id
                        ? ` · ${sourceNameById.get(row.source_account_id) || `账号 #${row.source_account_id}`}`
                        : ` · Token ${row.has_token ? "已配置" : "未配置"}`}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Switch
                      checked={row.enabled}
                      onCheckedChange={(v) => toggleMut.mutate({ id: row.id, enabled: v })}
                    />
                    <Button
                      variant="secondary"
                      onClick={() => testMut.mutate(row.id)}
                      disabled={!row.enabled || !row.has_token || testMut.isPending}
                    >
                      <Send className="mr-1 h-4 w-4" /> 测试
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() =>
                        setEditTarget({ id: row.id, name: row.name, chatId: String(row.default_chat_id) })
                      }
                    >
                      <Pencil className="mr-1 h-4 w-4" /> 修改接收目标
                    </Button>
                    <Button
                      variant="destructive"
                      onClick={() => {
                        if (!confirm(`确认删除通知路由 ${row.name} ?`)) return;
                        delMut.mutate(row.id);
                      }}
                      disabled={delMut.isPending}
                    >
                      删除
                    </Button>
                  </div>
                </div>
              </div>
            ))}
            {(listQ.data || []).length === 0 ? (
              <div className="nested-surface-item flex items-center gap-2 border px-3 py-4 text-sm text-muted-foreground">
                <BellRing className="h-4 w-4" /> 暂无通知路由，建议先创建 default。
              </div>
            ) : null}
          </div>
        )}
      </CardContent>

      <Dialog open={editTarget !== null} onOpenChange={(open) => !open && setEditTarget(null)}>
        <DialogContent className="w-[calc(100vw-2rem)] max-w-md rounded-xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-base">
              <Bot className="h-4 w-4" /> 修改通知接收目标
            </DialogTitle>
            <DialogDescription>
              修改路由 {editTarget?.name || ""} 的默认 Chat ID。不会更换 Bot Token。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="edit-notify-chat-id">Chat ID</Label>
            <Input
              id="edit-notify-chat-id"
              autoFocus
              inputMode="numeric"
              value={editTarget?.chatId ?? ""}
              onChange={(e) =>
                setEditTarget((current) => (current ? { ...current, chatId: e.target.value } : current))
              }
            />
            <div className="text-xs leading-5 text-muted-foreground">
              私聊填正数用户 ID。群聊、超级群或频道通常是负数。
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditTarget(null)}>
              取消
            </Button>
            <Button
              onClick={() => editTarget && targetMut.mutate(editTarget)}
              disabled={!editTarget?.chatId.trim() || targetMut.isPending}
            >
              保存接收目标
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
