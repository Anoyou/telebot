import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Loader2, Save } from "lucide-react";
import { toast } from "sonner";

import { listAccountFeatures, toggleAccountFeature } from "@/api/accounts";
import { getFeatureMatrix } from "@/api/features";
import { getSystemSettings } from "@/api/system";
import { CommandBadge } from "@/components/CommandBadge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/misc";
import { Switch } from "@/components/ui/switch";
import { getErrMsg } from "@/lib/api";
import { confirmDiscardChanges, useUnsavedChanges } from "@/lib/unsavedChanges";
import { featureConfigBackTarget, formatFeatureVersion } from "@/pages/Plugins/_shared/featureConfig";
import { featureRuntimeText } from "./_shared/featureStatus";

interface Game24Config {
  command: string;
  timeout: number;
}

function parseClampedInt(
  s: string,
  min: number,
  max: number,
): number | null {
  const cleaned = s.replace(/[^0-9]/g, "");
  if (!cleaned) return null;
  const n = parseInt(cleaned, 10);
  if (Number.isNaN(n)) return null;
  return Math.max(min, Math.min(max, n));
}

const DEFAULT_CONFIG: Game24Config = {
  command: "24d",
  timeout: 500,
};

export function Game24ConfigPage() {
  const params = useParams();
  const aid = Number(params.aid);
  const nav = useNavigate();
  const location = useLocation();
  const qc = useQueryClient();

  const featuresQ = useQuery({
    queryKey: ["account", aid, "features"],
    queryFn: () => listAccountFeatures(aid),
    enabled: !!aid,
  });
  const matrixQ = useQuery({
    queryKey: ["matrix"],
    queryFn: getFeatureMatrix,
  });

  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const cmdPrefix = settingsQ.data?.command_prefix || ",";

  const game24Feature = featuresQ.data?.find((f) => f.feature_key === "game24");
  const game24Info = matrixQ.data?.features.find((feature) => feature.key === "game24");
  const currentConfig = (game24Feature?.config ?? {}) as Partial<Game24Config>;

  const [command, setCommand] = useState(DEFAULT_CONFIG.command);
  const [timeoutInput, setTimeoutInput] = useState(
    String(DEFAULT_CONFIG.timeout),
  );
  const [dirty, setDirty] = useState(false);
  const [remoteConfigChanged, setRemoteConfigChanged] = useState(false);

  useEffect(() => {
    if (dirty) {
      setRemoteConfigChanged(true);
      return;
    }
    if (currentConfig.command !== undefined) {
      setCommand(currentConfig.command);
    }
    if (currentConfig.timeout !== undefined) {
      setTimeoutInput(String(currentConfig.timeout));
    }
    setDirty(false);
    setRemoteConfigChanged(false);
  }, [game24Feature?.config]);
  useUnsavedChanges(dirty);

  const saveMut = useMutation({
    mutationFn: async (config: Game24Config) => {
      const { api } = await import("@/lib/api");
      await api.patch(`/api/accounts/${aid}/features/game24`, {
        enabled: true,
        config,
      });
    },
    onSuccess: () => {
      toast.success("配置已保存（worker 热加载）");
      setDirty(false);
      setRemoteConfigChanged(false);
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const toggleMut = useMutation({
    mutationFn: (enabled: boolean) => toggleAccountFeature(aid, "game24", enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  function handleSave() {
    const timeout = parseClampedInt(timeoutInput, 30, 3600);
    if (timeout === null) {
      toast.error("答题限时不能为空");
      return;
    }
    saveMut.mutate({ command, timeout });
  }

  function resetForm() {
    setCommand(currentConfig.command ?? DEFAULT_CONFIG.command);
    setTimeoutInput(String(currentConfig.timeout ?? DEFAULT_CONFIG.timeout));
    setDirty(false);
    setRemoteConfigChanged(false);
  }

  if (!aid) return <p>账号 ID 不合法</p>;
  if (featuresQ.isLoading) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Spinner className="text-primary" />
      </div>
    );
  }

  const backTarget = featureConfigBackTarget(aid, location.search);

  return (
    <div className="space-y-6 pb-24">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="default" size="sm" className="gap-1.5 shadow-sm" onClick={() => confirmDiscardChanges(dirty) && nav(backTarget.backHref)}>
          <ArrowLeft className="h-4 w-4" /> {backTarget.backLabel}
        </Button>
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">24 点游戏</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <code>game24</code>
            <span>账号 #{aid}</span>
            <span className="rounded-full border border-primary/20 bg-primary/10 px-2 py-0.5 font-medium text-primary">
              当前版本 {formatFeatureVersion(game24Info?.version)}
            </span>
          </div>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">使用说明</CardTitle>
          <CardDescription>在群内发起 24 点答题，首个算对的人获得奖金。</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
            <ul className="mt-1.5 list-inside list-disc space-y-1 break-words">
              <li>在群内发送 <CommandBadge>{cmdPrefix}{command} 奖金金额</CommandBadge> 开始游戏（例：<CommandBadge>{cmdPrefix}{command} 2000</CommandBadge>）</li>
              <li>系统生成 4 个数字，第一个用算式答对的人获得奖金</li>
              <li>可用运算符：+ - * / ( )，也支持 x / ÷ / × 别名</li>
              <li>指令前缀跟随系统设置，可在系统配置中修改</li>
            </ul>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-base">功能总开关</CardTitle>
              <CardDescription>
                关闭后 24 点游戏不会响应当前账号的触发指令。
                {` · 运行状态：${featureRuntimeText(game24Feature)}`}
                {game24Feature?.last_error ? ` · 最近错误：${game24Feature.last_error}` : ""}
              </CardDescription>
            </div>
            <Switch
              checked={Boolean(game24Feature?.enabled)}
              disabled={toggleMut.isPending || !game24Feature}
              onCheckedChange={(enabled) => toggleMut.mutate(enabled)}
            />
          </div>
        </CardHeader>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">插件配置</CardTitle>
          <CardDescription>
            配置触发指令名和答题限时。修改后 worker 会自动热加载，无需重启。
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-6 pb-0 md:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="command">触发指令名</Label>
            <p className="text-xs text-muted-foreground">
              在系统指令前缀后输入此指令名触发游戏。默认
              <CommandBadge className="mx-1">24d</CommandBadge>
              ，即发送 <CommandBadge>{cmdPrefix}{command} 奖金金额</CommandBadge> 开始游戏。
            </p>
            <Input
              id="command"
              className="w-full font-mono"
              value={command}
              onChange={(e) => {
                setCommand(e.target.value.trim());
                setDirty(true);
              }}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="timeout">答题限时（秒）</Label>
            <p className="text-xs text-muted-foreground">
              超过此时间无人答对，游戏自动结束。默认 500 秒（约 8 分钟）。
            </p>
            <Input
              id="timeout"
              inputMode="numeric"
              className="w-full"
              value={timeoutInput}
              onChange={(e) => {
                setTimeoutInput(e.target.value.replace(/[^0-9]/g, ""));
                setDirty(true);
              }}
            />
          </div>

        </CardContent>
        <div className="static z-20 mt-4 rounded-b-lg border-t bg-background/95 px-4 py-3 shadow-[0_-8px_20px_rgba(15,23,42,0.06)] backdrop-blur supports-[backdrop-filter]:bg-background/85 sm:sticky sm:bottom-0 sm:px-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-sm">
              <div className="font-medium">配置操作</div>
              <div className="text-xs text-muted-foreground">
                {remoteConfigChanged
                  ? "服务端配置已变化；当前草稿已保留，保存会以草稿为准。"
                  : dirty
                    ? "有未保存修改，保存后 worker 会热加载。"
                    : "当前配置已同步。"}
              </div>
            </div>
            <div className="flex items-center gap-4">
              <Button disabled={!dirty || saveMut.isPending} onClick={handleSave}>
                {saveMut.isPending ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Save className="mr-2 h-4 w-4" />
                )}
                保存配置
              </Button>
              <Button type="button" variant="ghost" disabled={!dirty || saveMut.isPending} onClick={resetForm} className="px-0">
                撤销
              </Button>
            </div>
          </div>
        </div>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">插件预览</CardTitle>
          <CardDescription>该插件没有声明模板预览；建议后续补充题面和结算消息的渲染示例。</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="rounded-md border border-dashed bg-muted/20 px-3 py-4 text-sm text-muted-foreground">
            当前页面只配置触发指令和答题限时，游戏题面由运行时动态生成。
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
