import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Download,
  Upload,
  CheckCircle2,
  XCircle,
  AlertCircle,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Select } from "@/components/ui/select";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import { api } from "@/lib/api";
import { getErrMsg } from "@/lib/api";
import {
  confirmConfigBundle,
  dryRunConfigBundle,
  exportConfigBundle,
  listAccounts,
} from "@/api/accounts";
import type {
  ConfigBundleConfirmResponse,
  ConfigBundleDryRunResponse,
} from "@/api/types";

interface CategoryDef {
  key: string;
  label: string;
  desc: string;
  sensitive?: string[];
}

const CATEGORIES: CategoryDef[] = [
  {
    key: "system_settings",
    label: "系统设置",
    desc: "指令前缀等全局配置；Webhook Token 和恢复码仅在开启敏感导出时包含",
    sensitive: ["webhook_token", "recovery_code"],
  },
  { key: "command_templates", label: "自定义指令模板", desc: "所有回复/转发/AI 指令模板" },
  { key: "account_commands", label: "账号-指令绑定", desc: "每个账号启用了哪些指令" },
  { key: "llm_providers", label: "模型提供商", desc: "AI 模型提供商配置", sensitive: ["api_key"] },
  { key: "forward_rules", label: "消息转发规则", desc: "自动转发配置" },
  { key: "auto_reply_rules", label: "自动回复规则", desc: "自动回复配置" },
  { key: "rate_limit_templates", label: "风控模板", desc: "限速规则模板" },
  { key: "rate_limit_rules", label: "风控规则", desc: "账号级限速配置" },
  { key: "proxies", label: "代理配置", desc: "账号和模型提供商引用的代理", sensitive: ["password"] },
  { key: "device_profiles", label: "设备配置", desc: "账号登录使用的设备伪装参数" },
  { key: "feature_registry", label: "插件索引", desc: "插件配置依赖的功能登记信息" },
  {
    key: "plugin_global_configs",
    label: "插件全局配置",
    desc: "跨账号共享的插件配置",
    sensitive: ["插件密钥"],
  },
  { key: "feature_config", label: "插件功能配置", desc: "各账号的插件开关和配置" },
  { key: "account_settings", label: "账号设置", desc: "拟人化、标签等（不含登录信息）", sensitive: ["session", "api_id", "api_hash", "phone"] },
  { key: "humanize_configs", label: "拟人化配置", desc: "每个账号的延迟、打字和活跃时段" },
  { key: "ignored_peers", label: "允许会话", desc: "自动回复/转发允许通过的会话白名单" },
  { key: "notify_bots", label: "通知 Bot", desc: "通知机器人配置", sensitive: ["bot_token"] },
];

const BUNDLE_ENTITY_LABEL: Record<string, string> = {
  feature: "插件配置",
  rule: "插件规则",
  command_link: "指令绑定",
  ignored_peer: "允许会话",
};

export function ConfigBackup() {
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [includeSensitive, setIncludeSensitive] = useState(false);
  const [importResult, setImportResult] = useState<{
    imported: number;
    skipped: number;
    warnings: string[];
    affected_accounts: number[];
    reloaded_accounts: number[];
    restart_required: boolean;
  } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const bundleFileRef = useRef<HTMLInputElement>(null);
  const [bundleSourceAid, setBundleSourceAid] = useState("");
  const [bundleTargetAid, setBundleTargetAid] = useState("");
  const [bundleResult, setBundleResult] = useState<ConfigBundleDryRunResponse | null>(null);
  const [bundleConfirmResult, setBundleConfirmResult] = useState<ConfigBundleConfirmResponse | null>(null);
  const [bundleFile, setBundleFile] = useState<File | null>(null);
  const [applyConflicts, setApplyConflicts] = useState(false);
  const [confirmChatIdConflicts, setConfirmChatIdConflicts] = useState(false);
  const appliedSourceParamRef = useRef<string | null>(null);

  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });

  const sourceParam = searchParams.get("source");
  useEffect(() => {
    const accounts = accountsQ.data ?? [];
    if (!sourceParam || accounts.length === 0 || appliedSourceParamRef.current === sourceParam) {
      return;
    }

    appliedSourceParamRef.current = sourceParam;
    const sourceAid = Number(sourceParam);
    if (Number.isInteger(sourceAid) && accounts.some((a) => a.id === sourceAid)) {
      setBundleSourceAid(String(sourceAid));
    }
  }, [accountsQ.data, sourceParam]);

  const toggleCategory = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const selectAll = () => {
    if (selected.size === CATEGORIES.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(CATEGORIES.map((c) => c.key)));
    }
  };

  const exportMut = useMutation({
    mutationFn: async () => {
      const res = await api.post("/api/system/export-config", {
        categories: Array.from(selected),
        includeSensitive,
      }, { responseType: "blob" });
      // 从 Content-Disposition 提取文件名
      const disposition = res.headers["content-disposition"] || "";
      const match = disposition.match(/filename="?(.+?)"?(?:;|$)/);
      const filename = match ? match[1] : `telepilot-config-${new Date().toISOString().slice(0, 10)}.json`;
      // 触发下载
      const url = URL.createObjectURL(res.data as Blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
    onSuccess: () => toast.success("配置已导出"),
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const importMut = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      const { data } = await api.post("/api/system/import-config", form, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      return data as {
        imported: number;
        skipped: number;
        warnings: string[];
        affected_accounts: number[];
        reloaded_accounts: number[];
        restart_required: boolean;
      };
    },
    onSuccess: async (data) => {
      setImportResult(data);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["system"] }),
        queryClient.invalidateQueries({ queryKey: ["accounts"] }),
        queryClient.invalidateQueries({ queryKey: ["account"] }),
        queryClient.invalidateQueries({ queryKey: ["matrix"] }),
        queryClient.invalidateQueries({ queryKey: ["llm-providers"] }),
        queryClient.invalidateQueries({ queryKey: ["cmd-tpl"] }),
        queryClient.invalidateQueries({ queryKey: ["rate-templates"] }),
        queryClient.invalidateQueries({ queryKey: ["proxies"] }),
        queryClient.invalidateQueries({ queryKey: ["device-profiles"] }),
        queryClient.invalidateQueries({ queryKey: ["notify-bots"] }),
      ]);
      if (data.restart_required) {
        toast.warning(
          `导入已提交：${data.imported} 条成功；部分 Worker 未确认热重载，请重启应用`,
        );
      } else {
        toast.success(`导入完成：${data.imported} 条成功，${data.skipped} 条跳过`);
      }
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const exportBundleMut = useMutation({
    mutationFn: async () => {
      const res = await exportConfigBundle(Number(bundleSourceAid));
      const disposition = res.headers["content-disposition"] || "";
      const match = disposition.match(/filename="?(.+?)"?(?:;|$)/);
      const filename = match ? match[1] : `telepilot-config-bundle-${bundleSourceAid}.json`;
      const url = URL.createObjectURL(res.data as Blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
    onSuccess: () => toast.success("Config Bundle 已导出"),
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const dryRunBundleMut = useMutation({
    mutationFn: async (file: File) =>
      dryRunConfigBundle(Number(bundleTargetAid), file, {
        applyConflicts,
        confirmChatIdConflicts,
      }),
    onSuccess: (data) => {
      setBundleResult(data);
      toast.success(
        `dry-run 完成：新增 ${data.counts.add}，跳过 ${data.counts.skip}，冲突 ${data.counts.conflict}`,
      );
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const confirmBundleMut = useMutation({
    mutationFn: async () => {
      if (!bundleFile) throw new Error("请先上传 bundle 并完成 dry-run");
      if (!bundleResult?.preview_signature) throw new Error("预览已失效，请重新 dry-run");
      return confirmConfigBundle(Number(bundleTargetAid), bundleFile, {
        applyConflicts,
        confirmChatIdConflicts,
        previewSignature: bundleResult.preview_signature,
      });
    },
    onSuccess: (data) => {
      setBundleConfirmResult(data);
      toast.success(
        `confirm 完成：写入 ${data.imported}，跳过 ${data.skipped}，冲突 ${data.conflicts}`,
      );
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      setImportResult(null);
      importMut.mutate(file);
      // 清空 file input 以支持重复选择同一文件
      e.target.value = "";
    },
    [importMut],
  );

  const handleBundleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      setBundleResult(null);
      setBundleConfirmResult(null);
      setBundleFile(file);
      setApplyConflicts(false);
      setConfirmChatIdConflicts(false);
      dryRunBundleMut.mutate(file);
      e.target.value = "";
    },
    [dryRunBundleMut],
  );

  useEffect(() => {
    setBundleResult(null);
    setBundleConfirmResult(null);
  }, [bundleTargetAid]);

  useEffect(() => {
    if (bundleResult) {
      setBundleResult(null);
      setBundleConfirmResult(null);
    }
  }, [applyConflicts, confirmChatIdConflicts]);

  return (
    <>
      <Card>
      <CardHeader>
        <SectionHeader
          title="备份与恢复"
          description="导出或导入系统配置（可选是否包含敏感数据）"
          meta={
            <SignalPill
              tone={selected.size > 0 ? "primary" : "neutral"}
              label="已选分类"
              value={`${selected.size}/${CATEGORIES.length}`}
            />
          }
        />
      </CardHeader>
      <CardContent className="space-y-6">
        {/* 导出区域 */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <Label className="text-sm font-medium">选择导出类别</Label>
            <Button variant="ghost" size="sm" onClick={selectAll} className="text-xs">
              {selected.size === CATEGORIES.length ? "取消全选" : "全选"}
            </Button>
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {CATEGORIES.map((cat) => (
              <label
                key={cat.key}
                className="flex items-start gap-2 rounded-md border px-3 py-2 cursor-pointer hover:bg-muted/50 transition-colors"
              >
                <input
                  type="checkbox"
                  checked={selected.has(cat.key)}
                  onChange={() => toggleCategory(cat.key)}
                  className="mt-0.5"
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-sm font-medium">{cat.label}</span>
                    {cat.sensitive && (
                      <span className="rounded bg-warning/15 px-1 text-[10px] text-warning">
                        含敏感数据
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">{cat.desc}</p>
                </div>
              </label>
            ))}
          </div>

          {/* 敏感数据开关 */}
          <div className="flex items-center justify-between rounded-md border px-3 py-2">
            <div>
              <Label className="text-sm">包含敏感数据</Label>
              <p className="text-xs text-muted-foreground">
                session、api_key、token、密码等加密字段
              </p>
            </div>
            <Switch
              checked={includeSensitive}
              onCheckedChange={setIncludeSensitive}
            />
          </div>

          {includeSensitive && (
            <div className="flex items-start gap-2 rounded-md border px-3 py-2 text-xs alert-warning">
              <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
              <span>导出后请妥善保管文件。导入端需使用相同的 MASTER_KEY 才能解密敏感数据。</span>
            </div>
          )}

          <Button
            onClick={() => exportMut.mutate()}
            disabled={selected.size === 0 || exportMut.isPending}
            className="gap-1.5"
          >
            <Download className="h-4 w-4" />
            {exportMut.isPending ? "导出中..." : `导出配置（${selected.size} 项）`}
          </Button>
        </div>

        {/* 分隔线 */}
        <div className="border-t" />

        {/* 导入区域 */}
        <div className="space-y-3">
          <Label className="text-sm font-medium">导入配置</Label>
          <p className="text-xs text-muted-foreground">
            上传之前导出的版本化 JSON 配置包。恢复会按依赖顺序在单个事务中完成，
            同一自然标识的配置项将被跳过。
          </p>

          <input
            ref={fileRef}
            type="file"
            accept=".json"
            onChange={handleFileChange}
            className="hidden"
          />

          <Button
            variant="outline"
            onClick={() => fileRef.current?.click()}
            disabled={importMut.isPending}
            className="gap-1.5"
          >
            <Upload className="h-4 w-4" />
            {importMut.isPending ? "导入中..." : "选择文件导入"}
          </Button>

          {/* 导入结果 */}
          {importResult && (
            <div className="rounded-md border px-3 py-2 space-y-2">
              <div className="flex items-center gap-4 text-sm">
                <span className="flex items-center gap-1 text-success">
                  <CheckCircle2 className="h-4 w-4" />
                  成功 {importResult.imported}
                </span>
                <span className="flex items-center gap-1 text-muted-foreground">
                  <XCircle className="h-4 w-4" />
                  跳过 {importResult.skipped}
                </span>
              </div>
              {importResult.warnings.length > 0 && (
                <div className="space-y-1 text-xs text-warning">
                  {importResult.warnings.slice(0, 5).map((w, i) => (
                    <p key={i}>{w}</p>
                  ))}
                  {importResult.warnings.length > 5 && (
                    <p>... 还有 {importResult.warnings.length - 5} 条警告</p>
                  )}
                </div>
              )}
              {importResult.affected_accounts.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  已通知 {importResult.reloaded_accounts.length}/
                  {importResult.affected_accounts.length} 个账号 Worker 刷新配置
                </p>
              )}
              {importResult.restart_required && (
                <div className="flex items-start gap-2 text-xs text-warning">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>部分运行时未确认刷新。请重启应用后再验证恢复结果。</span>
                </div>
              )}
            </div>
          )}
        </div>
      </CardContent>
      </Card>

      <Card>
      <CardHeader>
        <SectionHeader
          title="账号配置包（Config Bundle）"
          description="把 A 账号的规则、插件配置、自定义指令绑定、允许会话打包成 JSON，再给 B 账号套用；上传后会先演练对比，确认写入才会真正落库。"
        />
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-3 rounded-md border bg-muted/30 px-3 py-3">
          <div>
            <div className="text-sm font-medium">第 1 步：从一个账号导出配置包</div>
            <p className="text-xs text-muted-foreground">
              适合“我已经把 1 号账号调好了，想把同一套规则和允许会话复制给 2 号账号”的场景。不会导出 session、API key、Bot Token 等敏感密钥。
            </p>
          </div>
          <div className="space-y-1.5 max-w-md">
            <Label>源账号（从谁那里复制配置）</Label>
            <Select value={bundleSourceAid} onChange={(e) => setBundleSourceAid(e.target.value)}>
              <option value="">-- 选择账号 --</option>
              {accountsQ.data?.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.display_name || a.phone}
                </option>
              ))}
            </Select>
          </div>
          <Button
            onClick={() => exportBundleMut.mutate()}
            disabled={!bundleSourceAid || exportBundleMut.isPending}
            className="gap-1.5"
          >
            <Download className="h-4 w-4" />
            {exportBundleMut.isPending ? "导出中..." : "导出账号配置包"}
          </Button>
        </div>

        <div className="border-t" />

        <div className="space-y-3 rounded-md border bg-muted/30 px-3 py-3">
          <div>
            <div className="text-sm font-medium">第 2 步：上传配置包，先看会改什么</div>
            <p className="text-xs text-muted-foreground">
              dry-run 就是“先排练一遍”：系统会告诉你目标账号会新增哪些配置、跳过哪些配置、哪些地方有冲突，但这一步不会写入数据库。
            </p>
          </div>
          <div className="space-y-1.5 max-w-md">
            <Label>目标账号（要把配置复制给谁）</Label>
            <Select value={bundleTargetAid} onChange={(e) => setBundleTargetAid(e.target.value)}>
              <option value="">-- 选择账号 --</option>
              {accountsQ.data?.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.display_name || a.phone}
                </option>
              ))}
            </Select>
          </div>
          <input
            ref={bundleFileRef}
            type="file"
            accept=".json,application/json"
            onChange={handleBundleFileChange}
            className="hidden"
          />
          <Button
            variant="outline"
            onClick={() => bundleFileRef.current?.click()}
            disabled={!bundleTargetAid || dryRunBundleMut.isPending}
            className="gap-1.5"
          >
            <Upload className="h-4 w-4" />
            {dryRunBundleMut.isPending ? "分析中..." : "上传配置包并预览差异"}
          </Button>

          {bundleResult && (
            <div className="space-y-3 rounded-md border px-3 py-3">
              <div className="text-sm font-medium">预览结果：现在还没有写入，只是在告诉你会发生什么</div>
              <div className="flex flex-wrap gap-3 text-sm">
                <span className="flex items-center gap-1 text-success">
                  <CheckCircle2 className="h-4 w-4" />
                  新增 {bundleResult.counts.add}
                </span>
                <span className="flex items-center gap-1 text-muted-foreground">
                  <XCircle className="h-4 w-4" />
                  跳过 {bundleResult.counts.skip}
                </span>
                <span className="flex items-center gap-1 text-destructive">
                  <AlertCircle className="h-4 w-4" />
                  冲突 {bundleResult.counts.conflict}
                </span>
              </div>
              <div className="text-xs text-muted-foreground">
                bundle 大小 {Math.round(bundleResult.size_bytes / 1024)} KB
              </div>
              {bundleResult.warnings.length > 0 && (
                <div className="space-y-1 text-xs text-warning">
                  {bundleResult.warnings.map((w, i) => (
                    <p key={i}>{w}</p>
                  ))}
                </div>
              )}
              <div className="max-h-72 space-y-1 overflow-auto text-xs">
                {bundleResult.items.slice(0, 40).map((item, i) => (
                  <div
                    key={`${item.entity}-${item.key}-${i}`}
                    className={[
                      "rounded border px-2 py-1",
                      item.action === "conflict"
                        ? "border-destructive/25 bg-destructive/10 text-destructive"
                        : item.action === "add"
                          ? "border-success/25 bg-success/10 text-success"
                          : "border-border text-muted-foreground",
                    ].join(" ")}
                  >
                    <span className="font-medium">
                      {BUNDLE_ENTITY_LABEL[item.entity] ?? item.entity}
                    </span>
                    {" · "}
                    <span>{item.key}</span>
                    {" · "}
                    <span>{item.action}</span>
                    {item.fields.length > 0 && (
                      <span className="ml-2">[{item.fields.join(", ")}]</span>
                    )}
                    {item.note && <span className="ml-2">{item.note}</span>}
                  </div>
                ))}
              </div>
              {bundleResult.items.length > 40 && (
                <p className="text-xs text-muted-foreground">
                  ... 还有 {bundleResult.items.length - 40} 条结果
                </p>
              )}
              <div className="space-y-3 border-t pt-3">
                <div className="flex items-center justify-between rounded-md border px-3 py-2">
                  <div>
                    <Label className="text-sm">应用冲突项</Label>
                    <p className="text-xs text-muted-foreground">
                      大白话：如果目标账号已经有同名配置，是否用配置包里的版本覆盖它。关闭时只复制全新的配置，不碰已有冲突项。
                    </p>
                  </div>
                  <Switch checked={applyConflicts} onCheckedChange={setApplyConflicts} />
                </div>
                <div className="flex items-center justify-between rounded-md border px-3 py-2">
                  <div>
                    <Label className="text-sm">确认 chat_id 冲突</Label>
                    <p className="text-xs text-muted-foreground">
                      大白话：群 ID / 聊天 ID 变了可能会把消息发到不同群，所以这里必须单独确认，防止误发。
                    </p>
                  </div>
                  <Switch
                    checked={confirmChatIdConflicts}
                    onCheckedChange={setConfirmChatIdConflicts}
                    disabled={!applyConflicts}
                  />
                </div>
                <Button
                  onClick={() => confirmBundleMut.mutate()}
                  disabled={
                    !bundleFile ||
                    !bundleTargetAid ||
                    !bundleResult?.preview_signature ||
                    confirmBundleMut.isPending
                  }
                  className="gap-1.5"
                >
                  <CheckCircle2 className="h-4 w-4" />
                  {confirmBundleMut.isPending ? "写入中..." : "确认写入目标账号"}
                </Button>
                {bundleConfirmResult && (
                  <div className="rounded-md border px-2 py-2 text-xs text-muted-foreground">
                    已写入 {bundleConfirmResult.imported}，跳过 {bundleConfirmResult.skipped}，冲突{" "}
                    {bundleConfirmResult.conflicts}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </CardContent>
      </Card>
    </>
  );
}
