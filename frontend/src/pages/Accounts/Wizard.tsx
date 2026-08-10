// 账号绑定 4 步向导：API 凭据 → 验证码 → (可选)2FA → 完成（可复制其他账号配置）
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ChevronRight } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import {
  cloneConfig,
  getAccount,
  listAccounts,
  login2fa,
  loginCode,
  loginStart,
} from "@/api/accounts";
import { listProxies } from "@/api/proxies";
import { getErrCode, getErrMsg } from "@/lib/api";
import {
  isSupportedProxyType,
  proxySelectionNeedsLoadedList,
  proxySelectionIssue,
  visibleProxyOptions,
} from "@/lib/proxySupport";
import { cn } from "@/lib/utils";

// 中文错误码翻译表（PRD 列出的几种）
const ERR_MAP: Record<string, string> = {
  CODE_INVALID: "验证码错误",
  CODE_EXPIRED: "验证码已过期，请重新获取",
  PASSWORD_INVALID: "二步验证密码错误",
  FLOOD_WAIT: "Telegram 限流，请稍后再试",
  PHONE_INVALID: "手机号格式不正确",
  SESSION_EXPIRED: "登录会话已过期，请重新开始",
  ACCOUNT_PHONE_MISMATCH: "重登手机号必须与当前账号一致",
  ACCOUNT_IDENTITY_MISMATCH: "登录到的 Telegram 用户与当前账号不一致，已拒绝覆盖",
  PROXY_NOT_FOUND: "所选代理已不存在，请重新选择",
  PROXY_TYPE_UNSUPPORTED: "所选代理已停止支持，请更换代理",
};
function readableError(err: unknown): string {
  const code = getErrCode(err);
  if (code && ERR_MAP[code]) return ERR_MAP[code];
  return getErrMsg(err);
}

type Step = 1 | 2 | 3 | 4;

export function AccountWizard() {
  const nav = useNavigate();
  const [searchParams] = useSearchParams();
  const qc = useQueryClient();
  const reloginAid = Number(searchParams.get("relogin") || 0) || null;
  const isRelogin = reloginAid != null;

  const [step, setStep] = useState<Step>(1);

  // 第一步表单
  const [apiId, setApiId] = useState("");
  const [apiHash, setApiHash] = useState("");
  const [phone, setPhone] = useState("");
  const [proxyId, setProxyId] = useState("");

  // 后端返回的临时 token；仅放组件 state，刷新即丢失
  const [loginToken, setLoginToken] = useState<string | null>(null);

  // 第二/三步表单
  const [smsCode, setSmsCode] = useState("");
  const [twoFa, setTwoFa] = useState("");

  // 完成后的目标账号 ID
  const [createdAid, setCreatedAid] = useState<number | null>(null);

  const reloginAccountQ = useQuery({
    queryKey: ["account", reloginAid],
    queryFn: () => getAccount(reloginAid!),
    enabled: isRelogin,
  });

  // 代理列表（用于第 1 步下拉）
  const proxiesQ = useQuery({
    queryKey: ["proxies"],
    queryFn: listProxies,
    enabled: step === 1,
  });
  const proxies = proxiesQ.data || [];
  const selectedProxyIssue = proxiesQ.isSuccess
    ? proxySelectionIssue(proxies, proxyId)
    : null;
  const proxyListRequired = proxySelectionNeedsLoadedList(proxyId, proxiesQ.isSuccess);
  const proxySelectionBlocked = proxyListRequired || selectedProxyIssue !== null;
  const proxyOptions = useMemo(
    () => visibleProxyOptions(proxies, proxyId),
    [proxies, proxyId],
  );

  // 复制其他账号配置（可选）
  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
    enabled: step === 4 && !isRelogin,
  });
  const [cloneFrom, setCloneFrom] = useState<string>("");

  useEffect(() => {
    if (!reloginAccountQ.data) return;
    setPhone(reloginAccountQ.data.phone);
    setProxyId(reloginAccountQ.data.proxy_id ? String(reloginAccountQ.data.proxy_id) : "");
  }, [reloginAccountQ.data]);

  // ===================== mutations =====================
  const startMut = useMutation({
    mutationFn: () =>
      loginStart({
        api_id: Number(apiId),
        api_hash: apiHash.trim(),
        phone: phone.trim(),
        account_id: reloginAid,
        proxy_id: proxyId ? Number(proxyId) : null,
      }),
    onSuccess: (res) => {
      setLoginToken(res.login_token);
      setStep(2);
      toast.success("已发送验证码，请到 Telegram 接收");
    },
    onError: (err) => toast.error(readableError(err)),
  });

  const codeMut = useMutation({
    mutationFn: () =>
      loginCode({ login_token: loginToken!, code: smsCode.trim() }),
    onSuccess: (res) => {
      if (res.require_2fa) {
        setStep(3);
        toast.info("该账号已启用两步验证，请输入密码");
      } else {
        setCreatedAid(res.account_id);
        qc.invalidateQueries({ queryKey: ["accounts"] });
        qc.invalidateQueries({ queryKey: ["account", res.account_id] });
        toast.success(isRelogin ? "重新登录成功，已覆盖当前账号 session" : "绑定成功");
        if (isRelogin) {
          nav(`/accounts/${res.account_id}`, { replace: true });
        } else {
          setStep(4);
        }
      }
    },
    onError: (err) => toast.error(readableError(err)),
  });

  const twoFaMut = useMutation({
    mutationFn: () =>
      login2fa({ login_token: loginToken!, password: twoFa }),
    onSuccess: (res) => {
      setCreatedAid(res.account_id);
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["account", res.account_id] });
      toast.success(isRelogin ? "重新登录成功，已覆盖当前账号 session" : "绑定成功");
      if (isRelogin) {
        nav(`/accounts/${res.account_id}`, { replace: true });
      } else {
        setStep(4);
      }
    },
    onError: (err) => toast.error(readableError(err)),
  });

  const cloneMut = useMutation({
    mutationFn: () =>
      cloneConfig(createdAid!, Number(cloneFrom), [
        "scheduler",
      ]),
    onSuccess: () => {
      toast.success("已复制配置");
      nav(`/accounts/${createdAid}`);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const stepInfo = useMemo(
    () =>
      [
        { n: 1, t: "API 凭据" },
        { n: 2, t: "验证码" },
        { n: 3, t: "两步密码（可选）" },
        { n: 4, t: "完成" },
      ] as const,
    [],
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => nav(-1)}>
          <ArrowLeft className="mr-1 h-4 w-4" /> 返回
        </Button>
        <h1 className="text-2xl font-semibold tracking-tight">
          {isRelogin ? `重新登录账号 #${reloginAid}` : "新增账号"}
        </h1>
      </div>

      {isRelogin ? (
        <Card className="border-warning/30 bg-warning/10">
          <CardContent className="pt-4 text-sm text-warning">
            这次不会新建账号，也不会删除原配置。成功后只覆盖当前账号的
            session、API 凭据和运行状态，允许会话、插件规则、指令绑定都会保留。
          </CardContent>
        </Card>
      ) : null}

      {/* 步进条 */}
      <ol className="flex items-center gap-2 text-sm">
        {stepInfo.map((s, idx) => (
          <li key={s.n} className="flex items-center gap-2">
            <span
              className={cn(
                "flex h-6 w-6 items-center justify-center rounded-full border text-xs",
                step === s.n && "border-primary bg-primary text-primary-foreground",
                step > s.n && "border-success bg-success text-white",
              )}
            >
              {step > s.n ? <Check className="h-3.5 w-3.5" /> : s.n}
            </span>
            <span
              className={cn(
                "text-muted-foreground",
                step === s.n && "text-foreground font-medium",
              )}
            >
              {s.t}
            </span>
            {idx < stepInfo.length - 1 && (
              <ChevronRight className="h-4 w-4 text-muted-foreground" />
            )}
          </li>
        ))}
      </ol>

      {/* Step 1 */}
      {step === 1 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              步骤 1 · {isRelogin ? "重新录入 API 凭据" : "API 凭据"}
            </CardTitle>
            <CardDescription>
              在{" "}
              <a
                href="https://my.telegram.org"
                target="_blank"
                rel="noreferrer"
                className="text-primary hover:underline"
              >
                my.telegram.org
              </a>{" "}
              申请 API ID / Hash
              {isRelogin ? "。MASTER_KEY 变更后需要重新输入，系统会用新密钥保存。" : ""}
            </CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>API ID</Label>
              <Input value={apiId} onChange={(e) => setApiId(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>API Hash</Label>
              <Input
                value={apiHash}
                onChange={(e) => setApiHash(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label>手机号</Label>
              <Input
                placeholder="+8613800000000"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                readOnly={isRelogin}
              />
              {isRelogin ? (
                <p className="text-xs text-muted-foreground">
                  重登模式会锁定原手机号，避免把其他 Telegram 账号覆盖到当前配置上。
                </p>
              ) : null}
            </div>
            <div className="space-y-1.5">
              <Label>出口代理（可选）</Label>
              <Select
                value={proxyId}
                onChange={(e) => setProxyId(e.target.value)}
              >
                <option value="">直连（不走代理）</option>
                {selectedProxyIssue === "missing" ? (
                  <option value={proxyId} disabled>
                    代理 #{proxyId} 已不存在，请重新选择
                  </option>
                ) : null}
                {proxyOptions.map((p) => (
                  <option
                    key={p.id}
                    value={String(p.id)}
                    disabled={!isSupportedProxyType(p.type)}
                  >
                    [{p.type}] {p.host}:{p.port}
                    {p.username ? ` @${p.username}` : ""}
                    {!isSupportedProxyType(p.type) ? "（已停止支持，请更换）" : ""}
                  </option>
                ))}
              </Select>
              {proxyListRequired ? (
                <p className="text-xs text-destructive">
                  {proxiesQ.isError
                    ? "代理列表加载失败，无法核对当前绑定。请重试或改选直连后继续。"
                    : "正在核对当前代理，请稍候。"}
                </p>
              ) : selectedProxyIssue ? (
                <p className="text-xs text-destructive">
                  当前代理{selectedProxyIssue === "missing" ? "已不存在" : "已停止支持"}，请选择 DIRECT 或受支持代理后继续。
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  若代理列表为空，先到「系统设置 → 代理与标识」创建
                </p>
              )}
            </div>
            <div className="sm:col-span-2 flex justify-end">
              <Button
                onClick={() => {
                  if (!apiId || !apiHash || !phone) {
                    toast.error("请填写 API ID/Hash 与手机号");
                    return;
                  }
                  if (proxySelectionBlocked) {
                    toast.error("请先确认或更换当前代理");
                    return;
                  }
                  startMut.mutate();
                }}
                disabled={startMut.isPending || proxySelectionBlocked}
              >
                {startMut.isPending ? "发送中…" : isRelogin ? "发送重登验证码" : "下一步"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Step 2 */}
      {step === 2 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">步骤 2 · 输入验证码</CardTitle>
            <CardDescription>
              我们已向 {phone} 发送了验证码（在 Telegram 客户端查看）
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5 max-w-xs">
              <Label>验证码</Label>
              <Input
                inputMode="numeric"
                maxLength={6}
                value={smsCode}
                onChange={(e) => setSmsCode(e.target.value)}
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setStep(1)}>
                上一步
              </Button>
              <Button
                onClick={() => smsCode && codeMut.mutate()}
                disabled={codeMut.isPending || !smsCode}
              >
                {codeMut.isPending ? "提交中…" : isRelogin ? "完成重登" : "下一步"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Step 3 */}
      {step === 3 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              步骤 3 · 输入两步验证密码
            </CardTitle>
            <CardDescription>
              该账号开启了 Telegram 两步验证，需要输入密码
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5 max-w-xs">
              <Label>密码</Label>
              <Input
                type="password"
                value={twoFa}
                onChange={(e) => setTwoFa(e.target.value)}
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setStep(2)}>
                上一步
              </Button>
              <Button
                onClick={() => twoFa && twoFaMut.mutate()}
                disabled={twoFaMut.isPending || !twoFa}
              >
                {twoFaMut.isPending ? "提交中…" : "完成"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Step 4 */}
      {step === 4 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">步骤 4 · 完成</CardTitle>
            <CardDescription>
              账号已绑定（ID #{createdAid}），可选择从已有账号复制功能配置
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5 max-w-md">
              <Label>从其他账号复制配置（可选）</Label>
              {accountsQ.isLoading ? (
                <div className="flex h-10 items-center">
                  <Spinner className="text-primary" />
                </div>
              ) : (
                <Select
                  value={cloneFrom}
                  onChange={(e) => setCloneFrom(e.target.value)}
                >
                  <option value="">-- 不复制 --</option>
                  {accountsQ.data
                    ?.filter((a) => a.id !== createdAid)
                    .map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.display_name || a.phone}
                      </option>
                    ))}
                </Select>
              )}
            </div>
            <div className="flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => nav(`/accounts/${createdAid}`)}
              >
                跳过
              </Button>
              <Button
                disabled={!cloneFrom || cloneMut.isPending}
                onClick={() => cloneMut.mutate()}
              >
                {cloneMut.isPending ? "复制中…" : "复制并完成"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
