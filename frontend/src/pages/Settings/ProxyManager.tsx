// 代理库管理：列表 + 新建（含 type/host/port/账密）+ 测试连通性 + 反查"被谁用了" + 删除
// 在 Settings 页里以一个 Card 形式嵌入。
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Activity,
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  Pencil,
  Plus,
  Trash2,
  Users,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/misc";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import {
  createProxy,
  deleteProxy,
  getProxyUsage,
  listProxies,
  patchProxy,
  testProxy,
} from "@/api/proxies";
import type {
  ProxyOut,
  ProxyTestResult,
  ProxyType,
  ProxyUpdate,
  ProxyUsageResponse,
} from "@/api/types";
import { getErrMsg } from "@/lib/api";

const MASKED_SECRET_PLACEHOLDER = "••••••••••••••••";

const TYPE_OPTIONS: { value: ProxyType; label: string }[] = [
  { value: "socks5", label: "SOCKS5" },
  { value: "http", label: "HTTP" },
  { value: "https", label: "HTTPS" },
  { value: "mtproxy", label: "MTProxy" },
];

type ProxyEditDraft = {
  type: ProxyType;
  host: string;
  port: string;
  username: string;
  password: string;
  clear_password: boolean;
};

function flagOf(country?: string | null): string {
  if (!country || country.length !== 2) return "🌐";
  const cp = (s: string) => 0x1f1e6 + (s.toUpperCase().charCodeAt(0) - 65);
  try {
    return String.fromCodePoint(cp(country[0]), cp(country[1]));
  } catch {
    return "🌐";
  }
}

export function ProxyManager() {
  const qc = useQueryClient();
  const proxiesQ = useQuery({ queryKey: ["proxies"], queryFn: listProxies });

  // 新建表单
  const [type, setType] = useState<ProxyType>("socks5");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("1080");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const createMut = useMutation({
    mutationFn: () =>
      createProxy({
        type,
        host: host.trim(),
        port: Number(port),
        username: username.trim() || null,
        password: password || null,
      }),
    onSuccess: () => {
      toast.success("已创建");
      setHost("");
      setUsername("");
      setPassword("");
      qc.invalidateQueries({ queryKey: ["proxies"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteProxy(id),
    onSuccess: () => {
      toast.success("已删除");
      qc.invalidateQueries({ queryKey: ["proxies"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState<ProxyEditDraft | null>(null);

  const updateMut = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: ProxyUpdate }) =>
      patchProxy(id, payload),
    onSuccess: (_, vars) => {
      toast.success("已保存");
      setEditingId(null);
      setEditDraft(null);
      setTestResults((m) => {
        const { [vars.id]: _, ...rest } = m;
        return rest;
      });
      qc.invalidateQueries({ queryKey: ["proxies"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["system", "health-overview"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  // 每条 proxy 的测试结果 inline 显示
  const [testResults, setTestResults] = useState<
    Record<number, ProxyTestResult | "loading">
  >({});

  // 展开"被谁用了"的代理 id；同一时间只展开一条够用了
  const [expandedUsage, setExpandedUsage] = useState<number | null>(null);

  async function handleTest(p: ProxyOut) {
    setTestResults((m) => ({ ...m, [p.id]: "loading" }));
    try {
      const res = await testProxy(p.id);
      setTestResults((m) => ({ ...m, [p.id]: res }));
      // 后端写了 Redis 缓存——账号列表会复用，所以顺手 invalidate 一下
      qc.invalidateQueries({ queryKey: ["accounts"] });
      if (res.ok) {
        toast.success(
          `测试通过：${flagOf(res.country)} ${res.country || "?"} · ${res.latency_ms}ms`,
        );
      } else {
        toast.error(`测试失败：${res.error || "未知错误"}`);
      }
    } catch (err) {
      toast.error(getErrMsg(err));
      setTestResults((m) => {
        const { [p.id]: _, ...rest } = m;
        return rest;
      });
    }
  }

  // 删除前先拉一遍 usage——给出"会断哪些"的明确警告
  async function handleDelete(p: ProxyOut) {
    let usage: ProxyUsageResponse | null = null;
    try {
      usage = await getProxyUsage(p.id);
    } catch {
      // usage 探测失败也不阻塞——后端 delete 接口本身会拒被引用的代理
    }
    let confirmText = `确认删除代理 ${p.host}:${p.port}？`;
    if (usage && usage.total > 0) {
      const parts: string[] = [];
      if (usage.accounts.length > 0) {
        parts.push(`${usage.accounts.length} 个账号`);
      }
      if (usage.llm_providers.length > 0) {
        parts.push(`${usage.llm_providers.length} 个 LLM`);
      }
      confirmText =
        `代理 ${p.host}:${p.port} 正被 ${parts.join(" + ")}使用——\n` +
        `直接删除会让这些功能立刻断网。\n\n` +
        `继续？（强烈建议先去那些地方改换代理）`;
    }
    if (!confirm(confirmText)) return;
    deleteMut.mutate(p.id);
  }

  function startEdit(p: ProxyOut) {
    setEditingId(p.id);
    setEditDraft({
      type: p.type,
      host: p.host,
      port: String(p.port),
      username: p.username || "",
      password: "",
      clear_password: false,
    });
  }

  function cancelEdit() {
    setEditingId(null);
    setEditDraft(null);
  }

  function saveEdit(p: ProxyOut) {
    if (!editDraft) return;
    const nextPort = Number(editDraft.port);
    if (!editDraft.host.trim() || !editDraft.port || nextPort <= 0 || nextPort > 65535) {
      toast.error("请填写有效的主机和端口");
      return;
    }
    updateMut.mutate({
      id: p.id,
      payload: {
        type: editDraft.type,
        host: editDraft.host.trim(),
        port: nextPort,
        username: editDraft.username.trim() || null,
        password: editDraft.clear_password ? null : editDraft.password || null,
        clear_password: editDraft.clear_password,
      },
    });
  }

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          title="网络代理模板"
          description="公用代理池：在绑定 TG 账号或账号详情中可选用其中一个；带「测试」按钮验证连通性 + 出口归属地"
          meta={
            <SignalPill
              tone={(proxiesQ.data?.length ?? 0) > 0 ? "primary" : "neutral"}
              label="代理数"
              value={proxiesQ.data?.length ?? 0}
            />
          }
        />
      </CardHeader>
      <CardContent className="space-y-4">
        {/* 新建 */}
        <div className="grid grid-cols-1 gap-2 md:grid-cols-[120px_1fr_100px_1fr_1fr_auto]">
          <div className="space-y-1.5">
            <Label className="text-xs">类型</Label>
            <Select
              value={type}
              onChange={(e) => setType(e.target.value as ProxyType)}
            >
              {TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">主机</Label>
            <Input
              placeholder="10.10.8.33 或 http://10.10.8.33:6152"
              value={host}
              onChange={(e) => setHost(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">端口</Label>
            <Input
              inputMode="numeric"
              value={port}
              onChange={(e) => setPort(e.target.value.replace(/[^0-9]/g, ""))}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">用户名（可选）</Label>
            <Input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">密码（可选）</Label>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div className="flex items-end">
            <Button
              onClick={() => createMut.mutate()}
              disabled={
                !host.trim() ||
                !port ||
                Number(port) <= 0 ||
                createMut.isPending
              }
              className="w-full md:w-auto"
            >
              <Plus className="mr-1 h-4 w-4" /> 新建
            </Button>
          </div>
        </div>

        {/* 列表 */}
        {proxiesQ.isLoading ? (
          <div className="flex h-16 items-center justify-center">
            <Spinner className="text-primary" />
          </div>
        ) : proxiesQ.data && proxiesQ.data.length > 0 ? (
          <ul className="divide-y rounded-md border">
            {proxiesQ.data.map((p) => {
              const tr = testResults[p.id];
              const isLoading = tr === "loading";
              const result = isLoading ? null : (tr as ProxyTestResult | undefined);
              const isEditing = editingId === p.id && editDraft !== null;
              return (
                <li key={p.id} className="space-y-1 px-3 py-2 text-sm">
                  {isEditing ? (
                    <div className="space-y-2 rounded-md border bg-muted/20 p-3">
                      <div className="grid grid-cols-1 gap-2 md:grid-cols-[120px_1fr_100px_1fr_1fr]">
                        <div className="space-y-1.5">
                          <Label className="text-xs">类型</Label>
                          <Select
                            value={editDraft.type}
                            onChange={(e) =>
                              setEditDraft((v) => v ? { ...v, type: e.target.value as ProxyType } : v)
                            }
                          >
                            {TYPE_OPTIONS.map((o) => (
                              <option key={o.value} value={o.value}>
                                {o.label}
                              </option>
                            ))}
                          </Select>
                        </div>
                        <div className="space-y-1.5">
                          <Label className="text-xs">主机</Label>
                          <Input
                            placeholder="可直接粘贴 http://host:port"
                            value={editDraft.host}
                            onChange={(e) =>
                              setEditDraft((v) => v ? { ...v, host: e.target.value } : v)
                            }
                          />
                        </div>
                        <div className="space-y-1.5">
                          <Label className="text-xs">端口</Label>
                          <Input
                            inputMode="numeric"
                            value={editDraft.port}
                            onChange={(e) =>
                              setEditDraft((v) =>
                                v ? { ...v, port: e.target.value.replace(/[^0-9]/g, "") } : v,
                              )
                            }
                          />
                        </div>
                        <div className="space-y-1.5">
                          <Label className="text-xs">用户名</Label>
                          <Input
                            value={editDraft.username}
                            onChange={(e) =>
                              setEditDraft((v) => v ? { ...v, username: e.target.value } : v)
                            }
                          />
                        </div>
                        <div className="space-y-1.5">
                          <Label className="text-xs">新密码</Label>
                          <Input
                            type="password"
                            placeholder={p.has_password ? MASKED_SECRET_PLACEHOLDER : "可选"}
                            value={editDraft.password}
                            disabled={editDraft.clear_password}
                            onChange={(e) =>
                              setEditDraft((v) => v ? { ...v, password: e.target.value } : v)
                            }
                          />
                        </div>
                      </div>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <label className="flex items-center gap-2 text-xs text-muted-foreground">
                          <input
                            type="checkbox"
                            checked={editDraft.clear_password}
                            onChange={(e) =>
                              setEditDraft((v) =>
                                v ? { ...v, clear_password: e.target.checked, password: "" } : v,
                              )
                            }
                          />
                          清空已保存密码
                        </label>
                        <div className="flex items-center gap-1">
                          <Button
                            size="sm"
                            onClick={() => saveEdit(p)}
                            loading={updateMut.isPending}
                          >
                            {!updateMut.isPending ? (
                              <Check className="mr-1 h-4 w-4" />
                            ) : null}
                            保存
                          </Button>
                          <Button size="sm" variant="outline" onClick={cancelEdit}>
                            <X className="mr-1 h-4 w-4" />
                            取消
                          </Button>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <span className="rounded bg-secondary px-1.5 py-0.5 text-xs font-mono uppercase">
                          {p.type}
                        </span>
                        <span className="font-mono">
                          {p.host}:{p.port}
                        </span>
                        {p.username ? (
                          <span className="text-xs text-muted-foreground">
                            @ {p.username}
                            {p.has_password ? " · 含密码" : ""}
                          </span>
                        ) : null}
                      </div>
                      <div className="flex items-center gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() =>
                            setExpandedUsage((cur) => (cur === p.id ? null : p.id))
                          }
                          title="查看哪些账号 / LLM 在使用本代理"
                        >
                          {expandedUsage === p.id ? (
                            <ChevronDown className="h-4 w-4" />
                          ) : (
                            <ChevronRight className="h-4 w-4" />
                          )}
                          <Users className="ml-1 h-4 w-4" />
                          <span className="ml-1">被谁用</span>
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleTest(p)}
                          loading={isLoading}
                        >
                          {!isLoading ? (
                            <Activity className="h-4 w-4" />
                          ) : null}
                          <span className="ml-1">测试</span>
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => startEdit(p)}
                          title="编辑代理"
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={deleteMut.isPending}
                          onClick={() => handleDelete(p)}
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      </div>
                    </div>
                  )}
                  {result ? (
                    result.ok ? (
                      <div className="text-xs text-success">
                        ✓ 通过 · {result.latency_ms}ms ·{" "}
                        {flagOf(result.country)} {result.country || "?"}
                        {result.city ? ` · ${result.city}` : ""}
                        {result.exit_ip ? (
                          <span className="ml-1 font-mono text-muted-foreground">
                            ({result.exit_ip})
                          </span>
                        ) : null}
                      </div>
                    ) : (
                      <div className="text-xs text-destructive">
                        ✗ {result.error || "未知错误"}
                      </div>
                    )
                  ) : null}
                  {expandedUsage === p.id ? <UsageBlock proxyId={p.id} /> : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="rounded-md border border-dashed py-6 text-center text-xs text-muted-foreground">
            尚无代理。绑定 TG 账号时如需走代理，先在此新建
          </p>
        )}
      </CardContent>
    </Card>
  );
}

// ── 折叠展开行：展示某条代理被谁用了 ─────────────────────────────
function UsageBlock({ proxyId }: { proxyId: number }) {
  const qc = useQueryClient();
  const usageQ = useQuery({
    queryKey: ["proxy-usage", proxyId],
    queryFn: () => getProxyUsage(proxyId),
    // 用户可能刚改完账号绑定，希望立刻看到——staleTime=0 + 每次展开都 refetch
    staleTime: 0,
  });
  if (usageQ.isLoading) {
    return (
      <div className="mt-1 flex h-8 items-center justify-center text-xs text-muted-foreground">
        <Spinner className="mr-1" size="sm" />
        加载中…
      </div>
    );
  }
  if (usageQ.isError) {
    return (
      <div className="mt-1 rounded-md border border-destructive/30 bg-destructive/5 px-2 py-1.5 text-xs text-destructive">
        ⚠ 查询失败：{getErrMsg(usageQ.error)}
        <Button
          variant="ghost"
          size="sm"
          className="ml-2 h-5 px-2 text-xs"
          onClick={() => usageQ.refetch()}
        >
          重试
        </Button>
      </div>
    );
  }
  const u = usageQ.data;
  if (!u || u.total === 0) {
    return (
      <div className="mt-1 flex items-center justify-between rounded-md border border-dashed bg-muted/30 px-2 py-1.5 text-xs text-muted-foreground">
        <span>尚无引用——可以放心删除。</span>
        <Button
          variant="ghost"
          size="sm"
          className="h-5 px-1 text-xs"
          onClick={() => {
            qc.invalidateQueries({ queryKey: ["proxy-usage", proxyId] });
          }}
          title="重新查询（刚改完账号绑定时用）"
        >
          <Loader2
            className={
              "h-3 w-3 " + (usageQ.isFetching ? "animate-spin" : "")
            }
          />
        </Button>
      </div>
    );
  }
  return (
    <div className="mt-1 space-y-1.5 rounded-md border bg-muted/30 px-2 py-1.5 text-xs">
      {u.accounts.length > 0 ? (
        <div>
          <div className="text-muted-foreground">
            被 <strong>{u.accounts.length}</strong> 个账号使用：
          </div>
          <div className="mt-0.5 flex flex-wrap gap-1.5">
            {u.accounts.map((a) => (
              <Badge key={`a${a.id}`} variant="outline" className="text-xs">
                #{a.id} {a.name || "(无名)"}
                {a.extra ? (
                  <span className="ml-1 text-muted-foreground">{a.extra}</span>
                ) : null}
              </Badge>
            ))}
          </div>
        </div>
      ) : null}
      {u.llm_providers.length > 0 ? (
        <div>
          <div className="text-muted-foreground">
            被 <strong>{u.llm_providers.length}</strong> 个 LLM provider 使用：
          </div>
          <div className="mt-0.5 flex flex-wrap gap-1.5">
            {u.llm_providers.map((l) => (
              <Badge key={`l${l.id}`} variant="outline" className="text-xs">
                #{l.id} {l.name || "(无名)"}
                {l.extra ? (
                  <span className="ml-1 text-muted-foreground">· {l.extra}</span>
                ) : null}
              </Badge>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
