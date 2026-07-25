// 系统设置 → 当前用户管理：修改密码 + (可选) 禁用 TOTP
//
// 不提供"用户列表"——本系统是单租户的超管模型，只有一个 web 用户；
// 真正需要换人时走数据库手动改 username + 密码即可。
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Copy, KeyRound, LogOut, Send, ShieldAlert, ShieldCheck, ShieldOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import { fetchMe, logout } from "@/lib/auth";
import { api, getErrMsg } from "@/lib/api";
import { getSystemSettings, patchSystemSettings } from "@/api/system";

interface TotpSetup {
  secret: string;
  otpauth_url: string;
}

type TotpMode = "always" | "after_failures";

export function UserAccount() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const meQ = useQuery({ queryKey: ["auth", "me"], queryFn: fetchMe });
  const settingsQ = useQuery({ queryKey: ["system", "settings"], queryFn: getSystemSettings });
  const loginSecurity = settingsQ.data?.login_security;

  // ── 修改密码 ────────────────────────────────────────────────
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [newPwd2, setNewPwd2] = useState("");

  const changeMut = useMutation({
    mutationFn: async () => {
      await api.post("/api/auth/change-password", {
        old_password: oldPwd,
        new_password: newPwd,
      });
    },
    onSuccess: () => {
      // 后端已清 cookie；提示后跳登录页
      toast.success("密码已修改，请用新密码重新登录");
      setOldPwd("");
      setNewPwd("");
      setNewPwd2("");
      qc.clear();
      // 用 hard reload 避免 React Query 还在用旧 token 再发请求
      setTimeout(() => {
        window.location.href = "/login";
      }, 800);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const logoutMut = useMutation({
    mutationFn: logout,
    onSettled: () => {
      qc.clear();
      nav("/login", { replace: true });
    },
  });

  const handleChange = () => {
    if (!oldPwd || !newPwd || !newPwd2) {
      toast.error("三项都要填");
      return;
    }
    if (newPwd.length < 8) {
      toast.error("新密码至少 8 位");
      return;
    }
    if (newPwd !== newPwd2) {
      toast.error("两次输入的新密码不一致");
      return;
    }
    if (oldPwd === newPwd) {
      toast.error("新密码不能与旧密码相同");
      return;
    }
    changeMut.mutate();
  };

  // ── 禁用 动态验证码（TOTP） ──────────────────────────────────────────────
  const [totpCode, setTotpCode] = useState("");
  const [totpSetup, setTotpSetup] = useState<TotpSetup | null>(null);
  const [totpVerifyCode, setTotpVerifyCode] = useState("");
  const [totpPolicy, setTotpPolicy] = useState<{ mode: TotpMode; threshold: string }>({
    mode: "after_failures",
    threshold: "5",
  });
  const disableTotpMut = useMutation({
    mutationFn: async () => {
      await api.post("/api/auth/totp/disable", { code: totpCode });
      await patchSystemSettings({ login_security: { totp_enabled: false } });
    },
    onSuccess: () => {
      toast.success("已禁用动态验证码（TOTP）");
      setTotpCode("");
      qc.invalidateQueries({ queryKey: ["auth", "me"] });
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const startTotpMut = useMutation({
    mutationFn: async () => {
      const { data } = await api.post<TotpSetup>("/api/auth/totp/enable");
      return data;
    },
    onSuccess: (data) => {
      setTotpSetup(data);
      setTotpVerifyCode("");
      toast.success("已生成 TOTP 密钥，请用验证器添加后输入 6 位码确认");
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const verifyTotpMut = useMutation({
    mutationFn: async () => {
      await api.post("/api/auth/totp/verify", { code: totpVerifyCode });
      await patchSystemSettings({
        login_security: {
          totp_enabled: true,
          totp_mode: totpPolicy.mode,
          totp_failed_attempt_threshold: Number(totpPolicy.threshold) || 5,
        },
      });
    },
    onSuccess: () => {
      toast.success("TOTP 已启用，登录验证开关已打开");
      setTotpSetup(null);
      setTotpVerifyCode("");
      qc.invalidateQueries({ queryKey: ["auth", "me"] });
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const patchLoginSecurityMut = useMutation({
    mutationFn: (patch: NonNullable<Parameters<typeof patchSystemSettings>[0]["login_security"]>) =>
      patchSystemSettings({ login_security: patch }),
    onSuccess: () => {
      toast.success("登录安全设置已保存");
      qc.invalidateQueries({ queryKey: ["system", "settings"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const [notifyOtp, setNotifyOtp] = useState({
    threshold: "5",
    windowSeconds: "900",
    ttlSeconds: "300",
    attempts: "3",
    recoveryTtl: "900",
  });

  useEffect(() => {
    if (!settingsQ.data?.login_security) return;
    const next = settingsQ.data.login_security;
    setTotpPolicy({
      mode: next.totp_mode === "always" ? "always" : "after_failures",
      threshold: String(next.totp_failed_attempt_threshold ?? 5),
    });
    setNotifyOtp({
      threshold: String(next.notify_otp_failed_attempt_threshold ?? 5),
      windowSeconds: String(next.notify_otp_fail_window_seconds ?? 900),
      ttlSeconds: String(next.notify_otp_ttl_seconds ?? 300),
      attempts: String(next.notify_otp_max_attempts ?? 3),
      recoveryTtl: String(next.recovery_code_ttl_seconds ?? 900),
    });
  }, [settingsQ.data?.login_security]);

  const saveNotifyOtp = () => {
    patchLoginSecurityMut.mutate({
      notify_otp_enabled: Boolean(loginSecurity?.notify_otp_enabled),
      notify_otp_failed_attempt_threshold: Number(notifyOtp.threshold) || 0,
      notify_otp_fail_window_seconds: Number(notifyOtp.windowSeconds) || 900,
      notify_otp_ttl_seconds: Number(notifyOtp.ttlSeconds) || 300,
      notify_otp_max_attempts: Number(notifyOtp.attempts) || 3,
      recovery_code_ttl_seconds: Number(notifyOtp.recoveryTtl) || 900,
    });
  };

  const saveTotpPolicy = () => {
    patchLoginSecurityMut.mutate({
      totp_enabled: Boolean(loginSecurity?.totp_enabled),
      totp_mode: totpPolicy.mode,
      totp_failed_attempt_threshold: Number(totpPolicy.threshold) || 5,
    });
  };

  const copyText = async (text: string, label: string) => {
    await navigator.clipboard.writeText(text);
    toast.success(`${label}已复制`);
  };

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          icon={KeyRound}
          title="当前用户"
          description={
            meQ.data ? (
              <>当前已登录：<span className="font-mono">{meQ.data.username}</span></>
            ) : (
              "加载中…"
            )
          }
          meta={
            <div className="flex flex-wrap items-center gap-2">
              {meQ.data ? (
                <SignalPill
                  tone={meQ.data.has_totp ? "success" : "warn"}
                  label="TOTP"
                  value={meQ.data.has_totp ? "已启用" : "未启用"}
                />
              ) : null}
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={() => logoutMut.mutate()}
                disabled={logoutMut.isPending}
              >
                <LogOut className="mr-1.5 h-4 w-4" />
                退出登录
              </Button>
            </div>
          }
        />
      </CardHeader>
      <CardContent className="space-y-6">
        {/* 修改密码 */}
        <div className="space-y-3 max-w-md">
          <h3 className="text-sm font-medium">修改密码</h3>
          <div className="space-y-2">
            <Label htmlFor="oldpwd">当前密码</Label>
            <Input
              id="oldpwd"
              type="password"
              autoComplete="current-password"
              value={oldPwd}
              onChange={(e) => setOldPwd(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="newpwd">新密码（≥ 8 位）</Label>
            <Input
              id="newpwd"
              type="password"
              autoComplete="new-password"
              value={newPwd}
              onChange={(e) => setNewPwd(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="newpwd2">确认新密码</Label>
            <Input
              id="newpwd2"
              type="password"
              autoComplete="new-password"
              value={newPwd2}
              onChange={(e) => setNewPwd2(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleChange();
              }}
            />
          </div>
          <Button onClick={handleChange} disabled={changeMut.isPending}>
            <KeyRound className="mr-2 h-4 w-4" />
            修改密码
          </Button>
          <p className="text-xs text-muted-foreground">
            修改成功后会强制下线，请用新密码重新登录
          </p>
        </div>

        <div className="space-y-4 border-t pt-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="text-sm font-medium">登录安全套件</h3>
              <p className="mt-1 max-w-2xl text-xs leading-5 text-muted-foreground">
                默认不强制二次验证。建议先确认服务器恢复码可用，再开启通知 Bot OTP 或 TOTP 登录验证。
              </p>
            </div>
            <div className="flex flex-wrap gap-1.5">
              <SignalPill
                tone={loginSecurity?.notify_otp_enabled ? "success" : "neutral"}
                label="通知 OTP"
                value={loginSecurity?.notify_otp_enabled ? "已开启" : "关闭"}
              />
              <SignalPill
                tone={loginSecurity?.totp_enabled ? "success" : "neutral"}
                label="TOTP 登录"
                value={
                  loginSecurity?.totp_enabled
                    ? loginSecurity.totp_mode === "always"
                      ? "每次验证"
                      : "失败后验证"
                    : "关闭"
                }
              />
            </div>
          </div>

          <section className="rounded-md border bg-muted/10 p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="space-y-1">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <ShieldCheck className="h-4 w-4 text-success" />
                  TOTP 登录验证
                </div>
                <p className="max-w-2xl text-xs leading-5 text-muted-foreground">
                  先绑定验证器密钥，再打开登录验证。关闭开关只是不再要求登录二次码，不会删除已绑定密钥。
                </p>
              </div>
              <Button
                variant={loginSecurity?.totp_enabled ? "default" : "outline"}
                disabled={patchLoginSecurityMut.isPending || startTotpMut.isPending}
                onClick={() => {
                  if (!meQ.data?.has_totp) {
                    startTotpMut.mutate();
                    return;
                  }
                  patchLoginSecurityMut.mutate({
                    totp_enabled: !loginSecurity?.totp_enabled,
                    totp_mode: totpPolicy.mode,
                    totp_failed_attempt_threshold: Number(totpPolicy.threshold) || 5,
                  });
                }}
              >
                {meQ.data?.has_totp
                  ? loginSecurity?.totp_enabled
                    ? "关闭登录验证"
                    : "开启登录验证"
                  : "绑定 TOTP"}
              </Button>
            </div>

            {totpSetup ? (
              <div className="mt-4 space-y-3 rounded-md border bg-background p-3">
                <div className="text-sm font-medium">添加到验证器</div>
                <div className="grid gap-3 lg:grid-cols-[1fr_1.5fr]">
                  <div className="space-y-1.5">
                    <Label>手动密钥</Label>
                    <div className="flex gap-2">
                      <Input readOnly value={totpSetup.secret} className="font-mono text-xs" />
                      <Button
                        type="button"
                        variant="outline"
                        size="icon"
                        onClick={() => copyText(totpSetup.secret, "TOTP 密钥")}
                      >
                        <Copy className="h-4 w-4" />
                      </Button>
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <Label>otpauth URL</Label>
                    <div className="flex gap-2">
                      <Input readOnly value={totpSetup.otpauth_url} className="font-mono text-xs" />
                      <Button
                        type="button"
                        variant="outline"
                        size="icon"
                        onClick={() => copyText(totpSetup.otpauth_url, "otpauth URL")}
                      >
                        <Copy className="h-4 w-4" />
                      </Button>
                    </div>
                  </div>
                </div>
                <div className="flex max-w-md items-end gap-2">
                  <div className="flex-1 space-y-1.5">
                    <Label htmlFor="totp-verify">验证器中的 6 位码</Label>
                    <Input
                      id="totp-verify"
                      inputMode="numeric"
                      maxLength={8}
                      value={totpVerifyCode}
                      onChange={(e) => setTotpVerifyCode(e.target.value.replace(/\D/g, ""))}
                      placeholder="6 位数字"
                    />
                  </div>
                  <Button
                    disabled={verifyTotpMut.isPending || totpVerifyCode.length < 6}
                    onClick={() => verifyTotpMut.mutate()}
                  >
                    确认启用
                  </Button>
                </div>
              </div>
            ) : null}

            {meQ.data?.has_totp ? (
              <div className="mt-4 grid gap-3 rounded-md border bg-background p-3 md:grid-cols-[minmax(0,1.2fr)_minmax(140px,0.5fr)_auto] md:items-end">
                <div className="space-y-1.5">
                  <Label htmlFor="totp-mode">登录验证策略</Label>
                  <Select
                    id="totp-mode"
                    value={totpPolicy.mode}
                    onChange={(e) =>
                      setTotpPolicy((v) => ({
                        ...v,
                        mode: e.target.value === "always" ? "always" : "after_failures",
                      }))
                    }
                  >
                    <option value="after_failures">连续输错后验证</option>
                    <option value="always">每次登录都验证</option>
                  </Select>
                  <p className="text-xs leading-5 text-muted-foreground">
                    失败后验证只在密码连续输错达到阈值后要求 TOTP，日常登录不打扰。
                  </p>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="totp-threshold">失败阈值</Label>
                  <Input
                    id="totp-threshold"
                    inputMode="numeric"
                    disabled={totpPolicy.mode === "always"}
                    value={totpPolicy.threshold}
                    onChange={(e) =>
                      setTotpPolicy((v) => ({
                        ...v,
                        threshold: e.target.value.replace(/\D/g, ""),
                      }))
                    }
                  />
                </div>
                <Button
                  variant="outline"
                  disabled={patchLoginSecurityMut.isPending}
                  onClick={saveTotpPolicy}
                >
                  保存策略
                </Button>
              </div>
            ) : null}

            {meQ.data?.has_totp ? (
              <div className="mt-4 flex max-w-xl flex-col gap-2 rounded-md border bg-background p-3 sm:flex-row sm:items-end">
                <div className="flex-1 space-y-1.5">
                  <Label htmlFor="totpcode">当前 TOTP 码</Label>
                  <Input
                    id="totpcode"
                    inputMode="numeric"
                    maxLength={8}
                    placeholder="6 位数字"
                    value={totpCode}
                    onChange={(e) => setTotpCode(e.target.value.replace(/\D/g, ""))}
                  />
                </div>
                <Button
                  variant="outline"
                  onClick={() => {
                    if (totpCode.length < 6) {
                      toast.error("TOTP 码至少 6 位");
                      return;
                    }
                    if (!confirm("确认移除 TOTP 密钥？移除后需要重新绑定验证器。")) return;
                    disableTotpMut.mutate();
                  }}
                  disabled={disableTotpMut.isPending}
                >
                  <ShieldOff className="mr-2 h-4 w-4" />
                  移除密钥
                </Button>
              </div>
            ) : null}
          </section>

          <section className="rounded-md border bg-muted/10 p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="space-y-1">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Send className="h-4 w-4 text-info" />
                  通知 Bot OTP 防爆破
                </div>
                <p className="max-w-2xl text-xs leading-5 text-muted-foreground">
                  密码失败达到阈值后，下一次正确密码会先给通知 Bot 发送一次性验证码。未配置通知 Bot 时会提示使用服务器恢复码。
                </p>
              </div>
              <Button
                variant={loginSecurity?.notify_otp_enabled ? "default" : "outline"}
                disabled={patchLoginSecurityMut.isPending}
                onClick={() =>
                  patchLoginSecurityMut.mutate({
                    notify_otp_enabled: !loginSecurity?.notify_otp_enabled,
                    notify_otp_failed_attempt_threshold: Number(notifyOtp.threshold) || 5,
                    notify_otp_fail_window_seconds: Number(notifyOtp.windowSeconds) || 900,
                    notify_otp_ttl_seconds: Number(notifyOtp.ttlSeconds) || 300,
                    notify_otp_max_attempts: Number(notifyOtp.attempts) || 3,
                    recovery_code_ttl_seconds: Number(notifyOtp.recoveryTtl) || 900,
                  })
                }
              >
                {loginSecurity?.notify_otp_enabled ? "关闭" : "开启"}
              </Button>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
              <div className="space-y-1.5">
                <Label>失败阈值</Label>
                <Input
                  inputMode="numeric"
                  value={notifyOtp.threshold}
                  onChange={(e) => setNotifyOtp((v) => ({ ...v, threshold: e.target.value.replace(/\D/g, "") }))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>计数窗口秒</Label>
                <Input
                  inputMode="numeric"
                  value={notifyOtp.windowSeconds}
                  onChange={(e) => setNotifyOtp((v) => ({ ...v, windowSeconds: e.target.value.replace(/\D/g, "") }))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>验证码秒</Label>
                <Input
                  inputMode="numeric"
                  value={notifyOtp.ttlSeconds}
                  onChange={(e) => setNotifyOtp((v) => ({ ...v, ttlSeconds: e.target.value.replace(/\D/g, "") }))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>尝试次数</Label>
                <Input
                  inputMode="numeric"
                  value={notifyOtp.attempts}
                  onChange={(e) => setNotifyOtp((v) => ({ ...v, attempts: e.target.value.replace(/\D/g, "") }))}
                />
              </div>
              <div className="space-y-1.5">
                <Label>恢复码秒</Label>
                <Input
                  inputMode="numeric"
                  value={notifyOtp.recoveryTtl}
                  onChange={(e) => setNotifyOtp((v) => ({ ...v, recoveryTtl: e.target.value.replace(/\D/g, "") }))}
                />
              </div>
            </div>
            <Button
              className="mt-3"
              variant="outline"
              disabled={patchLoginSecurityMut.isPending}
              onClick={saveNotifyOtp}
            >
              保存 OTP 参数
            </Button>
          </section>

          <section className="rounded-md border bg-muted/10 p-4">
            <div className="flex items-start gap-2">
              <ShieldAlert className="mt-0.5 h-4 w-4 text-warning" />
              <div className="space-y-1">
                <div className="text-sm font-medium">服务器一次性恢复码</div>
                <p className="max-w-2xl text-xs leading-5 text-muted-foreground">
                  无法收到通知 Bot OTP 或 TOTP 不可用时，在服务器执行 <code>make auth-recovery</code> 生成恢复码。恢复码仍需正确密码，只能成功使用一次。
                </p>
              </div>
            </div>
          </section>
        </div>
      </CardContent>
    </Card>
  );
}
