// 登录页：支持 TOTP 二次输入 + 首次部署兜底注册流程
//
// iOS / iPadOS 注意事项：
//   ``<input type="password">`` 在 iOS 上**强制使用系统键盘**（系统级安全策略），
//   第三方输入法（搜狗、百度等）无法工作。变通方案：在密码框右侧加一个"显示密码"
//   按钮（type 切到 "text"）——切到 text 后系统不限制输入法，用户可用第三方输入法
//   输完后再切回隐藏。这是 web 应用的通用做法（GitHub / Google 都这么做）。
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Eye, EyeOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { getErrCode, getErrMsg } from "@/lib/api";
import { login, register } from "@/lib/auth";
import { BrandLogo } from "@/components/BrandLogo";

type Mode = "login" | "register";

export function Login() {
  const nav = useNavigate();
  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [totpCode, setTotpCode] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [needOtp, setNeedOtp] = useState(false);
  const [otpToken, setOtpToken] = useState("");
  const [otpCode, setOtpCode] = useState("");
  const [otpMessage, setOtpMessage] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");

  const resetLoginChallenge = () => {
    setNeedTotp(false);
    setNeedOtp(false);
    setTotpCode("");
    setOtpToken("");
    setOtpCode("");
    setOtpMessage("");
    setRecoveryCode("");
  };

  // 登录 mutation：返回 require_totp 时切换到第二步
  const loginMut = useMutation({
    mutationFn: () =>
      login({
        username,
        password,
        totp_code: needTotp ? totpCode : null,
        otp_token: needOtp ? otpToken || null : null,
        otp_code: needOtp ? otpCode || null : null,
        recovery_code: needOtp || needTotp ? recoveryCode || null : null,
      }),
    onSuccess: (res) => {
      if (res.require_totp && !needTotp) {
        setNeedTotp(true);
        toast.info("请输入二次验证码（TOTP）");
        return;
      }
      if (res.require_otp) {
        setNeedOtp(true);
        setOtpToken(res.otp_token || "");
        setOtpCode("");
        setOtpMessage(res.otp_message || "请输入通知 Bot 收到的登录验证码");
        toast.info(res.otp_message || "请输入通知 Bot 登录验证码");
        return;
      }
      toast.success("登录成功");
      nav("/", { replace: true });
    },
    onError: (err) => {
      const code = getErrCode(err);
      // 后端约定：系统尚未创建用户时返回 NO_USER → 引导注册
      if (code === "NO_USER" || code === "USER_NOT_INITIALIZED") {
        toast.info("尚未创建管理员账号，请先注册");
        setMode("register");
        return;
      }
      if (code === "TOTP_REQUIRED") {
        setNeedTotp(true);
        toast.info("请输入二次验证码（TOTP）");
        return;
      }
      if (code === "OTP_INVALID") {
        setOtpCode("");
      }
      if (code === "RECOVERY_CODE_INVALID") {
        setRecoveryCode("");
      }
      toast.error(getErrMsg(err));
    },
  });

  const registerMut = useMutation({
    mutationFn: () => register(username, password),
    onSuccess: (res) => {
      // register 直接返回 LoginResponse，避免二次 login 请求
      if (res.require_totp) {
        setMode("login");
        setNeedTotp(true);
        toast.info("注册成功，请输入二次验证码（TOTP）");
        return;
      }
      toast.success("注册并登录成功");
      nav("/", { replace: true });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!username || !password) {
      toast.error("请填写用户名和密码");
      return;
    }
    if (needOtp && !recoveryCode.trim() && (!otpToken || !otpCode.trim())) {
      toast.error(otpToken ? "请输入通知 Bot 验证码或服务器恢复码" : "请输入服务器恢复码");
      return;
    }
    if (needTotp && !totpCode.trim() && !recoveryCode.trim()) {
      toast.error("请输入 TOTP 动态验证码，或使用服务器恢复码");
      return;
    }
    if (mode === "login") loginMut.mutate();
    else registerMut.mutate();
  };

  const isLogin = mode === "login";
  const submitting = loginMut.isPending || registerMut.isPending;

  return (
    <div className="grid min-h-screen place-items-center bg-background p-6">
      <div className="w-full max-w-sm space-y-5">
        <div className="flex items-center justify-center gap-3">
          <div className="grid h-11 w-11 place-items-center">
            <BrandLogo className="h-11 w-11 shadow-sm" />
          </div>
          <div>
            <div className="text-2xl font-bold tracking-tight">TelePilot</div>
            <p className="text-xs font-medium text-muted-foreground">管理控制台</p>
          </div>
        </div>
        <Card>
          <CardHeader>
            <CardTitle>{isLogin ? "登录" : "首次部署 · 创建管理员"}</CardTitle>
            <CardDescription>
              {isLogin
                ? "使用管理员账号进入工作台"
                : "本系统仅有一个超级管理员，密码请妥善保管"}
            </CardDescription>
          </CardHeader>
          <form onSubmit={onSubmit}>
            <CardContent className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="username">用户名</Label>
                <Input
                  id="username"
                  autoComplete="username"
                  value={username}
                  onChange={(e) => {
                    setUsername(e.target.value);
                    resetLoginChallenge();
                  }}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="password">密码</Label>
                {/*
                  密码框 + 右侧显示/隐藏按钮：iOS Safari 在 type="password" 时强制系统键盘，
                  切到 type="text" 后第三方输入法（搜狗 / 百度等）才能工作。
                */}
                <div className="relative">
                  <Input
                    id="password"
                    type={showPassword ? "text" : "password"}
                    autoComplete={isLogin ? "current-password" : "new-password"}
                    // 切到 text 时关掉自动大写 / 自动更正避免污染密码
                    autoCapitalize={showPassword ? "none" : undefined}
                    autoCorrect={showPassword ? "off" : undefined}
                    spellCheck={showPassword ? false : undefined}
                    className="pr-10"
                    value={password}
                    onChange={(e) => {
                      setPassword(e.target.value);
                      resetLoginChallenge();
                    }}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                    aria-label={showPassword ? "隐藏密码" : "显示密码"}
                    title={
                      showPassword
                        ? "隐藏密码（切回安全的系统键盘）"
                        : "显示密码（iOS 上可用第三方输入法）"
                    }
                    // tabindex=-1 让 Tab 不停在按钮上，密码 → 提交直接 enter
                    tabIndex={-1}
                  >
                    {showPassword ? (
                      <EyeOff className="h-4 w-4" />
                    ) : (
                      <Eye className="h-4 w-4" />
                    )}
                  </button>
                </div>
                {showPassword && (
                  <p className="text-[11px] text-warning">
                    密码已显示；输完后建议点击眼睛图标隐藏
                  </p>
                )}
              </div>
              {isLogin && needTotp && (
                <div className="space-y-1.5">
                  <Label htmlFor="totp">二次验证码 (TOTP)</Label>
                  <Input
                    id="totp"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    value={totpCode}
                    onChange={(e) => setTotpCode(e.target.value)}
                    placeholder="6 位数字"
                  />
                </div>
              )}
              {isLogin && needOtp && (
                <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
                  {otpMessage || "登录尝试较多，需要额外验证。"}
                </div>
              )}
              {isLogin && needOtp && otpToken && (
                <div className="space-y-1.5">
                  <Label htmlFor="login-otp">通知 Bot 登录验证码</Label>
                  <Input
                    id="login-otp"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    value={otpCode}
                    onChange={(e) => setOtpCode(e.target.value)}
                    placeholder="6 位数字"
                  />
                </div>
              )}
              {isLogin && (needOtp || needTotp) && (
                <div className="space-y-1.5">
                  <Label htmlFor="recovery-code">服务器一次性恢复码</Label>
                  <Input
                    id="recovery-code"
                    autoComplete="one-time-code"
                    value={recoveryCode}
                    onChange={(e) => setRecoveryCode(e.target.value)}
                    placeholder="TP-XXXXX-XXXXX-XXXXX-XXXXX"
                  />
                  <p className="text-[11px] leading-relaxed text-muted-foreground">
                    无法收到验证码时，可在服务器执行恢复码命令后填写；仍需正确密码。
                  </p>
                </div>
              )}
            </CardContent>
            <CardFooter className="flex flex-col gap-2">
              <Button type="submit" className="w-full" disabled={submitting}>
                {submitting ? "提交中…" : isLogin ? "登录" : "注册并登录"}
              </Button>
              <button
                type="button"
                className="text-xs text-muted-foreground hover:underline"
                onClick={() => {
                  setMode(isLogin ? "register" : "login");
                  resetLoginChallenge();
                }}
              >
                {isLogin ? "首次部署？点此创建管理员" : "已有账号？返回登录"}
              </button>
            </CardFooter>
          </form>
        </Card>
      </div>
    </div>
  );
}
