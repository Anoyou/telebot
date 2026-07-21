import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronDown, ChevronUp, Pencil, Plus, Save, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { SectionHeader, SignalPill } from "@/components/ui/status";

import {
  createSudoUser,
  deleteSudoUser,
  getSudoUsers,
  updateSudoUser,
} from "@/api/sudo";
import { listAccountCommands, listBuiltinCommands } from "@/api/commands";
import { getSystemSettings, patchSystemSettings } from "@/api/system";
import type { SudoUserResponse } from "@/types/sudo";
import { listAccounts } from "@/api/accounts";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";

const QK = ["sudo-users"] as const;

type FormState = {
  account_id: string;
  tg_user_id: string;
  display_name: string;
  allowed_chat_ids: string;
  allowed_commands: string;
  allow_all_chats: boolean;
  allow_all_commands: boolean;
};

type CommandCatalogItem = {
  name: string;
  aliases: string[];
  doc: string;
};

const EMPTY_FORM: FormState = {
  account_id: "",
  tg_user_id: "",
  display_name: "",
  allowed_chat_ids: "",
  allowed_commands: "",
  allow_all_chats: false,
  allow_all_commands: false,
};

const splitCsv = (value: string) =>
  value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);

const parseChatIds = (value: string) =>
  splitCsv(value).map((item) => Number(item));

const parseCommands = (value: string) => splitCsv(value);

const hasInvalidChatIds = (value: string) =>
  splitCsv(value).some((item) => !Number.isFinite(Number(item)));

const formatChatScope = (user: SudoUserResponse) => {
  if (user.allow_all_chats) return "全部（显式）";
  return user.allowed_chat_ids?.length
    ? user.allowed_chat_ids.join(", ")
    : "未授权";
};

const formatCommandScope = (user: SudoUserResponse) => {
  if (user.allow_all_commands) return "全部（显式）";
  return user.allowed_commands?.length
    ? user.allowed_commands.join(", ")
    : "未授权";
};

export function SudoManagement() {
  const qc = useQueryClient();
  const listQ = useQuery<SudoUserResponse[]>({
    queryKey: QK,
    queryFn: () => getSudoUsers(),
  });
  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: () => listAccounts(),
  });
  const builtinCommandsQ = useQuery({
    queryKey: ["commands", "builtin"],
    queryFn: () => listBuiltinCommands(),
  });
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });

  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [addFormOpen, setAddFormOpen] = useState(false);
  const selectedAccountId = useMemo(() => {
    const id = Number(form.account_id);
    return Number.isInteger(id) && id > 0 ? id : null;
  }, [form.account_id]);
  const accountCommandsQ = useQuery({
    queryKey: ["account", selectedAccountId, "commands", "sudo"],
    queryFn: () => listAccountCommands(selectedAccountId as number),
    enabled: selectedAccountId !== null,
  });

  const builtinCommandItems = useMemo<CommandCatalogItem[]>(
    () =>
      (builtinCommandsQ.data || []).map((cmd) => ({
        name: cmd.name,
        aliases: cmd.aliases || [],
        doc: cmd.doc || "内置指令",
      })),
    [builtinCommandsQ.data],
  );
  const enabledTemplateCommandItems = useMemo<CommandCatalogItem[]>(
    () =>
      (accountCommandsQ.data || [])
        .filter((item) => item.enabled)
        .map((item) => ({
          name: item.template.name,
          aliases: item.template.aliases || [],
        doc:
          item.template.description ||
          `自定义指令模板：${item.template.type}`,
        })),
    [accountCommandsQ.data],
  );
  const selectedCommands = useMemo(
    () => new Set(parseCommands(form.allowed_commands)),
    [form.allowed_commands],
  );

  const createMut = useMutation({
    mutationFn: () =>
      createSudoUser({
        account_id: Number(form.account_id),
        tg_user_id: Number(form.tg_user_id),
        display_name: form.display_name.trim() || undefined,
        allow_all_chats: form.allow_all_chats,
        allow_all_commands: form.allow_all_commands,
        allowed_chat_ids: form.allow_all_chats
          ? []
          : parseChatIds(form.allowed_chat_ids),
        allowed_commands: form.allow_all_commands
          ? []
          : parseCommands(form.allowed_commands),
      }),
    onSuccess: () => {
      toast.success("已创建 Sudo 用户");
      setForm(EMPTY_FORM);
      setAddFormOpen(false);
      qc.invalidateQueries({ queryKey: QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const updateMut = useMutation({
    mutationFn: async (id: number) => {
      await updateSudoUser(id, {
        display_name: form.display_name.trim() || undefined,
        allow_all_chats: form.allow_all_chats,
        allow_all_commands: form.allow_all_commands,
        allowed_chat_ids: form.allow_all_chats
          ? []
          : parseChatIds(form.allowed_chat_ids),
        allowed_commands: form.allow_all_commands
          ? []
          : parseCommands(form.allowed_commands),
      });
    },
    onSuccess: () => {
      toast.success("已更新");
      setEditingId(null);
      setForm(EMPTY_FORM);
      setAddFormOpen(false);
      qc.invalidateQueries({ queryKey: QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const deleteMut = useMutation({
    mutationFn: async (id: number) => deleteSudoUser(id),
    onSuccess: () => {
      toast.success("已删除");
      qc.invalidateQueries({ queryKey: QK });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const sudoEnabledMut = useMutation({
    mutationFn: (enabled: boolean) => patchSystemSettings({ sudo_enabled: enabled }),
    onSuccess: () => {
      toast.success("Sudo 总开关已保存，worker 将热加载");
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const startEdit = (user: SudoUserResponse) => {
    setEditingId(user.id);
    setAddFormOpen(true);
    setForm({
      account_id: String(user.account_id),
      tg_user_id: String(user.tg_user_id),
      display_name: user.display_name || "",
      allowed_chat_ids: user.allowed_chat_ids?.join(", ") || "",
      allowed_commands: user.allowed_commands?.join(", ") || "",
      allow_all_chats: user.allow_all_chats,
      allow_all_commands: user.allow_all_commands,
    });
  };

  const cancelEdit = () => {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setAddFormOpen(false);
  };

  const toggleAllowedCommand = (command: string) => {
    if (!command || form.allow_all_commands) return;
    setForm((current) => {
      const commands = parseCommands(current.allowed_commands);
      const next = commands.includes(command)
        ? commands.filter((item) => item !== command)
        : [...commands, command];
      return {
        ...current,
        allowed_commands: next.join(", "),
      };
    });
  };

  const handleDelete = (id: number) => {
    if (!window.confirm("确定要删除这个 Sudo 用户吗？此操作不可撤销。")) return;
    deleteMut.mutate(id);
  };

  const canSave = useMemo(() => {
    if (!form.account_id || !form.tg_user_id) return false;
    if (isNaN(Number(form.account_id)) || isNaN(Number(form.tg_user_id)))
      return false;
    if (!form.allow_all_chats && hasInvalidChatIds(form.allowed_chat_ids))
      return false;
    const hasChatScope =
      form.allow_all_chats || parseChatIds(form.allowed_chat_ids).length > 0;
    const hasCommandScope =
      form.allow_all_commands || parseCommands(form.allowed_commands).length > 0;
    if (!hasChatScope || !hasCommandScope) return false;
    return true;
  }, [form]);

  if (listQ.isLoading || accountsQ.isLoading) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Spinner className="text-primary" />
      </div>
    );
  }

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          title="Sudo 用户管理"
          description="授权其他 Telegram 用户通过独立前缀触发指令。默认不授予任何对话或指令权限。"
          meta={
            <SignalPill
              tone={(listQ.data?.length ?? 0) > 0 ? "warn" : "neutral"}
              label="已授权"
              value={`${listQ.data?.length ?? 0} 人`}
            />
          }
        />
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="rounded-lg border bg-muted/20 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold">Sudo 总开关</h3>
              <p className="mt-1 text-xs text-muted-foreground">
                关闭时所有 sudo 触发都会静默忽略。开启后也只允许在账号自身 chat（收藏夹）里触发，不在群组或普通私聊里响应。
              </p>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs text-muted-foreground">
                {settingsQ.data?.sudo_enabled ? "已开启" : "已关闭"}
              </span>
              <Switch
                checked={!!settingsQ.data?.sudo_enabled}
                disabled={settingsQ.isLoading || sudoEnabledMut.isPending}
                onCheckedChange={(checked) => sudoEnabledMut.mutate(checked)}
              />
            </div>
          </div>
        </div>

        {/* 创建/编辑表单 */}
        {!addFormOpen ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="w-full justify-between sm:w-auto"
            onClick={() => setAddFormOpen(true)}
          >
            <span className="inline-flex items-center">
              <Plus className="mr-1 h-4 w-4" />
              添加 Sudo 用户
            </span>
            <ChevronDown className="ml-3 h-4 w-4 text-muted-foreground" />
          </Button>
        ) : (
        <div className="space-y-4 rounded-lg border p-4">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold">
              {editingId ? "编辑 Sudo 用户" : "添加 Sudo 用户"}
            </h3>
            {!editingId && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-8 px-2 text-muted-foreground"
                onClick={() => {
                  setForm(EMPTY_FORM);
                  setAddFormOpen(false);
                }}
              >
                <ChevronUp className="mr-1 h-4 w-4" />
                收起
              </Button>
            )}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>账号 *</Label>
              <Select
                value={form.account_id}
                onChange={(e) =>
                  setForm((f) => ({ ...f, account_id: e.target.value }))
                }
              >
                <option value="">请选择账号</option>
                {accountsQ.data?.map((acc) => (
                  <option key={acc.id} value={acc.id}>
                    {acc.display_name || acc.phone}
                  </option>
                ))}
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label>Telegram 用户 ID *</Label>
              <Input
                type="number"
                value={form.tg_user_id}
                onChange={(e) =>
                  setForm((f) => ({ ...f, tg_user_id: e.target.value }))
                }
                placeholder="123456789"
              />
            </div>

            <div className="space-y-1.5">
              <Label>显示名称</Label>
              <Input
                value={form.display_name}
                onChange={(e) =>
                  setForm((f) => ({ ...f, display_name: e.target.value }))
                }
                placeholder="可选"
              />
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-3">
                <Label>允许的对话 ID（逗号分隔）</Label>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={form.allow_all_chats}
                    onChange={(e) =>
                      setForm((f) => ({
                        ...f,
                        allow_all_chats: e.target.checked,
                      }))
                    }
                  />
                  允许所有对话
                </label>
              </div>
              <Input
                value={form.allowed_chat_ids}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    allowed_chat_ids: e.target.value,
                  }))
                }
                disabled={form.allow_all_chats}
                placeholder="不填=不授权，如: -100123, -100456"
              />
            </div>

            <div className="space-y-2 sm:col-span-2">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <Label>允许的指令</Label>
                  <p className="text-xs text-muted-foreground">
                    在下方指令卡片里点击即可启用/取消；已启用的卡片会高亮。
                  </p>
                </div>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={form.allow_all_commands}
                    onChange={(e) =>
                      setForm((f) => ({
                        ...f,
                        allow_all_commands: e.target.checked,
                      }))
                    }
                  />
                  允许所有指令
                </label>
              </div>
            </div>
          </div>

          <div className="space-y-3 rounded-lg border bg-background/70 p-3 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="text-sm font-medium">允许的指令 / 当前可用的 sudo 指令</p>
                <p className="text-xs text-muted-foreground">
                  白名单填写不带前缀的触发词；别名触发也需要单独授权。
                </p>
              </div>
              <Badge variant="outline">
                {form.allow_all_commands
                  ? "已允许全部"
                  : `已选 ${selectedCommands.size} 个`}
              </Badge>
            </div>

            <CommandCatalog
              title="内置指令"
              loading={builtinCommandsQ.isLoading}
              items={builtinCommandItems}
              disabled={form.allow_all_commands}
              selectedCommands={selectedCommands}
              onToggle={toggleAllowedCommand}
              emptyText="暂无内置指令"
            />

            <CommandCatalog
              title="该账号已启用的自定义指令"
              loading={accountCommandsQ.isLoading}
              items={enabledTemplateCommandItems}
              disabled={form.allow_all_commands || selectedAccountId === null}
              selectedCommands={selectedCommands}
              onToggle={toggleAllowedCommand}
              emptyText={
                selectedAccountId === null
                  ? "选择账号后显示该账号已启用的自定义指令"
                  : "该账号暂无已启用的自定义指令"
              }
            />

            <div className="rounded-md border border-dashed bg-muted/25 px-3 py-2 text-xs leading-5 text-muted-foreground">
              插件配置页里的自定义触发词不一定属于指令模板；若没有出现在这里，可以先到对应插件配置页确认触发词。
            </div>

            {!form.allow_all_commands && selectedCommands.size > 0 && (
              <div className="flex flex-wrap gap-1.5 border-t pt-3">
                {[...selectedCommands].map((command) => (
                  <button
                    key={command}
                    type="button"
                    className="inline-flex items-center gap-1 rounded-full border bg-background px-2 py-0.5 font-mono text-xs hover:bg-muted"
                    onClick={() => toggleAllowedCommand(command)}
                  >
                    {command}
                    <X className="h-3 w-3" />
                  </button>
                ))}
              </div>
            )}
          </div>

          <p className="text-xs text-muted-foreground">
            需要同时配置对话范围和指令范围；勾选“允许所有”才会授予全部权限。
          </p>

          <div className="flex gap-2">
            <Button
              onClick={() => {
                if (editingId) {
                  updateMut.mutate(editingId);
                } else {
                  createMut.mutate();
                }
              }}
              disabled={!canSave || createMut.isPending || updateMut.isPending}
            >
              {editingId ? (
                <Save className="mr-1 h-4 w-4" />
              ) : (
                <Plus className="mr-1 h-4 w-4" />
              )}
              {editingId ? "更新" : "添加"}
            </Button>
            {editingId && (
              <Button variant="outline" onClick={cancelEdit}>
                <X className="mr-1 h-4 w-4" />
                取消
              </Button>
            )}
          </div>
        </div>
        )}

        {/* 列表 */}
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">Sudo 用户列表</h3>
          {!listQ.data || listQ.data.length === 0 ? (
            <p className="text-sm text-muted-foreground">暂无 Sudo 用户</p>
          ) : (
            <div className="space-y-2">
              {listQ.data.map((user) => (
                <div
                  key={user.id}
                  className="flex items-center justify-between rounded-lg border p-3"
                >
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm">
                        TG User ID: {user.tg_user_id}
                      </span>
                      {user.display_name && (
                        <Badge variant="secondary">{user.display_name}</Badge>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      账号 ID: {user.account_id}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      允许对话: {formatChatScope(user)}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      允许指令: {formatCommandScope(user)}
                    </p>
                  </div>
                  <div className="flex gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => startEdit(user)}
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => handleDelete(user.id)}
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function CommandCatalog({
  title,
  loading,
  items,
  disabled,
  emptyText,
  selectedCommands,
  onToggle,
}: {
  title: string;
  loading: boolean;
  items: CommandCatalogItem[];
  disabled: boolean;
  emptyText: string;
  selectedCommands: Set<string>;
  onToggle: (command: string) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <p className="text-xs font-medium text-muted-foreground">{title}</p>
        <Badge variant="secondary">{items.length}</Badge>
      </div>

      {loading ? (
        <p className="text-xs text-muted-foreground">正在加载指令...</p>
      ) : items.length === 0 ? (
        <p className="text-xs text-muted-foreground">{emptyText}</p>
      ) : (
        <div className="grid gap-2">
          {items.map((item) => (
            <div
              key={item.name}
              className={cn(
                "grid gap-2 rounded-md border px-3 py-2 transition-colors md:grid-cols-[minmax(0,1fr)_minmax(16rem,1.4fr)] md:items-center",
                selectedCommands.has(item.name)
                  ? "border-primary/45 bg-primary/10"
                  : "border-border/70 bg-muted/25",
              )}
            >
              <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                <Button
                  type="button"
                  size="sm"
                  variant={selectedCommands.has(item.name) ? "default" : "outline"}
                  disabled={disabled}
                  className="h-8 gap-1 rounded-full px-3 font-mono text-xs"
                  onClick={() => onToggle(item.name)}
                >
                  {selectedCommands.has(item.name) && <Check className="h-3 w-3" />}
                  {item.name}
                </Button>
                {item.aliases.map((alias) => (
                  <Button
                    key={`${item.name}:${alias}`}
                    type="button"
                    size="sm"
                    variant={selectedCommands.has(alias) ? "secondary" : "outline"}
                    disabled={disabled}
                    className="h-8 gap-1 rounded-full px-2.5 font-mono text-xs text-muted-foreground"
                    onClick={() => onToggle(alias)}
                  >
                    {selectedCommands.has(alias) && <Check className="h-3 w-3" />}
                    {alias}
                  </Button>
                ))}
              </div>
              <p className="min-w-0 text-xs leading-5 text-muted-foreground md:text-right">
                {item.doc}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
