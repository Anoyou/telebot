// Codex 图片生成配置：按账号管理 access_token / model / max_wait
import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Eye, EyeOff, Save } from "lucide-react";
import { toast } from "sonner";

import { listAccountFeatures, toggleAccountFeature } from "@/api/accounts";
import { getSystemSettings } from "@/api/system";
import { CommandBadge } from "@/components/CommandBadge";
import { Button } from "@/components/ui/button";
import { TelegramHtmlPreview } from "@/components/TelegramHtmlPreview";
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
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { getErrMsg } from "@/lib/api";
import { confirmDiscardChanges, useUnsavedChanges } from "@/lib/unsavedChanges";
import { featureConfigBackTarget } from "@/pages/Plugins/_shared/featureConfig";
import { featureRuntimeText } from "./_shared/featureStatus";

const MASKED_SECRET_PLACEHOLDER = "••••••••••••••••";

interface CodexImageConfig {
  command: string;
  access_token: string;
  model: string;
  image_model: string;
  max_wait_seconds: number;
  status_interval_seconds: number;
  message_template: string;
  image_size: string;
  aspect_ratio: string;
  image_format: string;
  delete_command_message: boolean;
  show_revised_prompt: boolean;
  reasoning_effort: string;
  custom_instructions: string;
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

const DEFAULT_CONFIG: CodexImageConfig = {
  command: "cximg",
  access_token: "",
  model: "gpt-5.5",
  image_model: "auto",
  max_wait_seconds: 600,
  status_interval_seconds: 20,
  message_template:
    "<b>🎨 Codex 图片生成</b>\n<b>状态:</b> {status}\n<b>提示词:</b> {prompt}\n<b>主模型:</b> {model} · <b>图片模型:</b> {image_model}\n<b>尺寸:</b> {image_size} · <b>比例:</b> {aspect_ratio} · <b>格式:</b> {image_format}\n<b>耗时:</b> {elapsed}{?revised_prompt}\n<b>修订提示词:</b> {revised_prompt}{/?}",
  image_size: "1024x1024",
  aspect_ratio: "1:1",
  image_format: "png",
  delete_command_message: true,
  show_revised_prompt: true,
  reasoning_effort: "low",
  custom_instructions: "",
};

// 支持的主模型列表
const MAIN_MODEL_OPTIONS = [
  { value: "gpt-5.5", label: "gpt-5.5（推荐）" },
  { value: "gpt-5.4-mini", label: "gpt-5.4-mini" },
  { value: "gpt-5.4-nano", label: "gpt-5.4-nano" },
  { value: "gpt-5.2", label: "gpt-5.2" },
  { value: "gpt-5", label: "gpt-5" },
  { value: "gpt-5-nano", label: "gpt-5-nano" },
  { value: "gpt-4.1", label: "gpt-4.1" },
  { value: "gpt-4.1-mini", label: "gpt-4.1-mini" },
  { value: "gpt-4.1-nano", label: "gpt-4.1-nano" },
  { value: "gpt-4o", label: "gpt-4o" },
  { value: "gpt-4o-mini", label: "gpt-4o-mini" },
  { value: "o3", label: "o3" },
];
const MAIN_MODEL_VALUES = new Set(MAIN_MODEL_OPTIONS.map((item) => item.value));

function normalizeMainModel(value: unknown): string {
  const modelId = String(value || "").trim();
  if (modelId === "gpt-5.4") return DEFAULT_CONFIG.model;
  return MAIN_MODEL_VALUES.has(modelId) ? modelId : DEFAULT_CONFIG.model;
}

// 支持的底层图片模型
const IMAGE_MODEL_OPTIONS = [
  { value: "auto", label: "自动选择" },
  { value: "gpt-image-2", label: "gpt-image-2（最新）" },
  { value: "gpt-image-1.5", label: "gpt-image-1.5" },
  { value: "gpt-image-1", label: "gpt-image-1" },
  { value: "gpt-image-1-mini", label: "gpt-image-1-mini" },
];

const TEMPLATE_PLACEHOLDERS = [
  { key: "{status}", label: "状态" },
  { key: "{prompt}", label: "提示词" },
  { key: "{elapsed}", label: "耗时" },
  { key: "{model}", label: "主模型" },
  { key: "{image_model}", label: "图片模型" },
  { key: "{command}", label: "指令" },
  { key: "{image_size}", label: "分辨率" },
  { key: "{aspect_ratio}", label: "比例" },
  { key: "{image_format}", label: "格式" },
  { key: "{has_reference}", label: "参考图" },
  { key: "{revised_prompt}", label: "修订提示词" },
];

function renderTemplate(template: string, values: Record<string, string>): string {
  let out = template || DEFAULT_CONFIG.message_template;
  out = out.replace(/\{\?([a-zA-Z0-9_]+)\}([\s\S]*?)\{\/\?\}/g, (_, key: string, inner: string) =>
    values[key] ? inner : "",
  );
  out = out.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, key: string) => values[key] ?? "");
  return out;
}

export function CodexImageConfigPage() {
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

  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const cmdPrefix = settingsQ.data?.command_prefix || ",";

  const feature = featuresQ.data?.find(
    (f) => f.feature_key === "codex_image"
  );
  const currentConfig = (feature?.config ?? {}) as Partial<CodexImageConfig>;

  const [command, setCommand] = useState(DEFAULT_CONFIG.command);
  const [accessToken, setAccessToken] = useState(DEFAULT_CONFIG.access_token);
  const [model, setModel] = useState(DEFAULT_CONFIG.model);
  const [imageModel, setImageModel] = useState(DEFAULT_CONFIG.image_model);
  const [maxWaitInput, setMaxWaitInput] = useState(
    String(DEFAULT_CONFIG.max_wait_seconds),
  );
  const [statusIntervalInput, setStatusIntervalInput] = useState(
    String(DEFAULT_CONFIG.status_interval_seconds),
  );
  const [messageTemplate, setMessageTemplate] = useState(DEFAULT_CONFIG.message_template);
  const [imageSize, setImageSize] = useState(DEFAULT_CONFIG.image_size);
  const [aspectRatio, setAspectRatio] = useState(DEFAULT_CONFIG.aspect_ratio);
  const [imageFormat, setImageFormat] = useState(DEFAULT_CONFIG.image_format);
  const [deleteCommandMessage, setDeleteCommandMessage] = useState(DEFAULT_CONFIG.delete_command_message);
  const [showRevisedPrompt, setShowRevisedPrompt] = useState(DEFAULT_CONFIG.show_revised_prompt);
  const [reasoningEffort, setReasoningEffort] = useState(DEFAULT_CONFIG.reasoning_effort);
  const [customInstructions, setCustomInstructions] = useState(DEFAULT_CONFIG.custom_instructions);
  const [dirty, setDirty] = useState(false);
  const [remoteConfigChanged, setRemoteConfigChanged] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [hasToken, setHasToken] = useState(false);

  useEffect(() => {
    if (dirty) {
      setRemoteConfigChanged(true);
      return;
    }
    setCommand(currentConfig.command ?? DEFAULT_CONFIG.command);
    setAccessToken("");
    setHasToken(Boolean(currentConfig.access_token));
    setModel(normalizeMainModel(currentConfig.model));
    setImageModel(currentConfig.image_model ?? DEFAULT_CONFIG.image_model);
    setMaxWaitInput(String(currentConfig.max_wait_seconds ?? DEFAULT_CONFIG.max_wait_seconds));
    setStatusIntervalInput(
      String(
        currentConfig.status_interval_seconds ??
          DEFAULT_CONFIG.status_interval_seconds,
      ),
    );
    setMessageTemplate(currentConfig.message_template ?? DEFAULT_CONFIG.message_template);
    setImageSize(currentConfig.image_size ?? DEFAULT_CONFIG.image_size);
    setAspectRatio(currentConfig.aspect_ratio ?? DEFAULT_CONFIG.aspect_ratio);
    setImageFormat(currentConfig.image_format ?? DEFAULT_CONFIG.image_format);
    setDeleteCommandMessage(currentConfig.delete_command_message ?? DEFAULT_CONFIG.delete_command_message);
    setShowRevisedPrompt(currentConfig.show_revised_prompt ?? DEFAULT_CONFIG.show_revised_prompt);
    setReasoningEffort(currentConfig.reasoning_effort ?? DEFAULT_CONFIG.reasoning_effort);
    setCustomInstructions(currentConfig.custom_instructions ?? DEFAULT_CONFIG.custom_instructions);
    setDirty(false);
    setRemoteConfigChanged(false);
  }, [feature?.config]);
  useUnsavedChanges(dirty);

  const saveMut = useMutation({
    mutationFn: async (config: CodexImageConfig) => {
      const { api } = await import("@/lib/api");
      await api.patch(`/api/accounts/${aid}/features/codex_image`, {
        enabled: true,
        config,
      });
    },
    onSuccess: () => {
      toast.success("配置已保存（worker 热加载）");
      setHasToken(Boolean(accessToken.trim()) || hasToken);
      setAccessToken("");
      setShowToken(false);
      setDirty(false);
      setRemoteConfigChanged(false);
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["matrix"] });
      qc.invalidateQueries({ queryKey: ["message-templates", "catalog", aid] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const toggleMut = useMutation({
    mutationFn: (enabled: boolean) => toggleAccountFeature(aid, "codex_image", enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  function handleSave() {
    const maxWaitSeconds = parseClampedInt(maxWaitInput, 60, 1800);
    if (maxWaitSeconds === null) {
      toast.error("最大等待时间不能为空");
      return;
    }
    const statusIntervalSeconds = parseClampedInt(statusIntervalInput, 10, 300);
    if (statusIntervalSeconds === null) {
      toast.error("状态刷新间隔不能为空");
      return;
    }
    saveMut.mutate({
      command: command.trim() || DEFAULT_CONFIG.command,
      access_token: accessToken,
      model,
      image_model: imageModel,
      max_wait_seconds: maxWaitSeconds,
      status_interval_seconds: statusIntervalSeconds,
      message_template: messageTemplate || DEFAULT_CONFIG.message_template,
      image_size: imageSize,
      aspect_ratio: aspectRatio,
      image_format: imageFormat,
      delete_command_message: deleteCommandMessage,
      show_revised_prompt: showRevisedPrompt,
      reasoning_effort: reasoningEffort,
      custom_instructions: customInstructions,
    });
  }

  function resetForm() {
    setCommand(currentConfig.command ?? DEFAULT_CONFIG.command);
    setAccessToken("");
    setHasToken(Boolean(currentConfig.access_token));
    setShowToken(false);
    setModel(normalizeMainModel(currentConfig.model));
    setImageModel(currentConfig.image_model ?? DEFAULT_CONFIG.image_model);
    setMaxWaitInput(String(currentConfig.max_wait_seconds ?? DEFAULT_CONFIG.max_wait_seconds));
    setStatusIntervalInput(
      String(
        currentConfig.status_interval_seconds ??
          DEFAULT_CONFIG.status_interval_seconds,
      ),
    );
    setMessageTemplate(currentConfig.message_template ?? DEFAULT_CONFIG.message_template);
    setImageSize(currentConfig.image_size ?? DEFAULT_CONFIG.image_size);
    setAspectRatio(currentConfig.aspect_ratio ?? DEFAULT_CONFIG.aspect_ratio);
    setImageFormat(currentConfig.image_format ?? DEFAULT_CONFIG.image_format);
    setDeleteCommandMessage(currentConfig.delete_command_message ?? DEFAULT_CONFIG.delete_command_message);
    setShowRevisedPrompt(currentConfig.show_revised_prompt ?? DEFAULT_CONFIG.show_revised_prompt);
    setReasoningEffort(currentConfig.reasoning_effort ?? DEFAULT_CONFIG.reasoning_effort);
    setCustomInstructions(currentConfig.custom_instructions ?? DEFAULT_CONFIG.custom_instructions);
    setDirty(false);
    setRemoteConfigChanged(false);
  }

  const effectiveCommand = command || DEFAULT_CONFIG.command;
  const displayImageModel = imageModel === "auto" ? "自动选择" : imageModel;
  const previewValues = {
    status: "正在生成图片",
    prompt: "云海里的未来城市，电影感光影",
    elapsed: "20秒",
    model,
    image_model: displayImageModel,
    command: effectiveCommand,
    image_size: imageSize,
    aspect_ratio: aspectRatio,
    image_format: imageFormat,
    has_reference: "是",
    revised_prompt: "A cinematic futuristic city above a sea of clouds.",
  };

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
        <Button
          variant="default"
          size="sm"
          className="gap-1.5 shadow-sm"
          onClick={() => confirmDiscardChanges(dirty) && nav(backTarget.backHref)}
        >
          <ArrowLeft className="h-4 w-4" /> {backTarget.backLabel}
        </Button>
        <h1 className="text-2xl font-semibold tracking-tight">
          Codex 图片生成
        </h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">使用说明</CardTitle>
          <CardDescription>用当前账号通过 Codex 生成图片，支持文本和参考图。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-start gap-2 rounded-md border px-3 py-2 text-xs alert-warning">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <div className="font-medium">实验性能力</div>
              <div className="mt-0.5 text-muted-foreground">
                依赖非公开 API，后续可能随上游变化而迁移、降级或失效。
              </div>
            </div>
          </div>
          <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
            <ul className="mt-1.5 list-inside list-disc space-y-1 break-words">
              <li>
                发送 <CommandBadge>{cmdPrefix}{effectiveCommand} 提示词</CommandBadge> 纯文本生成图片
              </li>
              <li>
                回复图片后发送{" "}
                <CommandBadge>{cmdPrefix}{effectiveCommand} 提示词</CommandBadge> 进行参考图生成
              </li>
              <li>
                临时指定比例/尺寸/格式：{" "}
                <CommandBadge>{cmdPrefix}{effectiveCommand} --比例 4:3 --size 1536x1024 --format jpeg 云海里的城市</CommandBadge>
              </li>
              <li>
                也可通过指令{" "}
                <CommandBadge>
                  {cmdPrefix}{effectiveCommand} token 你的access_token
                </CommandBadge>{" "}
                直接设置 Token
              </li>
              <li>
                触发指令名支持中文，例如设置为 <CommandBadge>画图</CommandBadge> 后发送 <CommandBadge>{cmdPrefix}画图 云海里的城市</CommandBadge>
              </li>
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
                关闭后 Codex 图片生成不会响应当前账号的触发指令。
                {` · 运行状态：${featureRuntimeText(feature)}`}
                {feature?.last_error ? ` · 最近错误：${feature.last_error}` : ""}
              </CardDescription>
            </div>
            <Switch
              checked={Boolean(feature?.enabled)}
              disabled={toggleMut.isPending || !feature}
              onCheckedChange={(enabled) => toggleMut.mutate(enabled)}
            />
          </div>
        </CardHeader>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">插件配置</CardTitle>
          <CardDescription>
            配置 Codex API 的鉴权 Token、模型和超时时间。修改后 worker 会自动热加载，无需重启。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6 pb-0">
          <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
          <div className="space-y-1.5">
            <Label htmlFor="command">触发指令名</Label>
            <p className="text-xs text-muted-foreground">
              在系统指令前缀后输入此指令触发图片生成。默认
              <CommandBadge className="mx-1">cximg</CommandBadge>
              ，支持中文，如 <CommandBadge>画图</CommandBadge>。
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

          {/* Access Token */}
          <div className="space-y-1.5">
            <Label htmlFor="access-token">Codex Access Token</Label>
            <p className="text-xs text-muted-foreground">
              从{" "}
              <code className="mx-0.5">.codex/auth.json</code>{" "}
              中获取的 access token，用于鉴权 Codex API。出于安全原因，已保存 token 不会回显。
            </p>
            <div className="flex gap-2">
              <Input
                id="access-token"
                className="font-mono flex-1"
                type={showToken ? "text" : "password"}
                placeholder={hasToken ? MASKED_SECRET_PLACEHOLDER : "eyJhbGciOi..."}
                value={accessToken}
                onChange={(e) => {
                  setAccessToken(e.target.value);
                  setDirty(true);
                }}
              />
              <Button
                variant="outline"
                size="icon"
                onClick={() => setShowToken(!showToken)}
                title={showToken ? "隐藏 Token" : "显示 Token"}
              >
                {showToken ? (
                  <EyeOff className="h-4 w-4" />
                ) : (
                  <Eye className="h-4 w-4" />
                )}
              </Button>
            </div>
            {hasToken && !accessToken && (
              <p className="text-xs text-muted-foreground">
                当前：已配置；留空保存会保留现有 Token，输入新值才会覆盖。
              </p>
            )}
            {accessToken && (
              <p className="text-xs text-muted-foreground">
                当前：将使用新输入的 Token 覆盖
              </p>
            )}
          </div>

          {/* 主模型 */}
          <div className="space-y-1.5">
            <Label htmlFor="model">主模型</Label>
            <p className="text-xs text-muted-foreground">
              处理请求的主模型，支持 <code className="mx-0.5">image_generation</code> 工具。
            </p>
            <Select
              id="model"
              value={model}
              onChange={(e) => {
                setModel(e.target.value);
                setDirty(true);
              }}
            >
              {MAIN_MODEL_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </Select>
          </div>

          {/* 底层图片模型 */}
          <div className="space-y-1.5">
            <Label htmlFor="image-model">底层图片模型</Label>
            <p className="text-xs text-muted-foreground">
              实际生成图片的模型。<code className="mx-0.5">auto</code> 表示由 OpenAI 自动选择。
            </p>
            <Select
              id="image-model"
              value={imageModel}
              onChange={(e) => {
                setImageModel(e.target.value);
                setDirty(true);
              }}
            >
              {IMAGE_MODEL_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </Select>
          </div>

          {/* Max Wait */}
          <div className="space-y-1.5">
            <Label htmlFor="max-wait">最大等待时间（秒）</Label>
            <p className="text-xs text-muted-foreground">
              图片生成最大等待时间，超时后自动停止。默认 600（10 分钟）。
            </p>
            <Input
              id="max-wait"
              inputMode="numeric"
              className="w-full"
              value={maxWaitInput}
              onChange={(e) => {
                setMaxWaitInput(e.target.value.replace(/[^0-9]/g, ""));
                setDirty(true);
              }}
            />
          </div>

          {/* Status interval */}
          <div className="space-y-1.5">
            <Label htmlFor="status-interval">状态刷新间隔（秒）</Label>
            <p className="text-xs text-muted-foreground">
              生成耗时较长时编辑状态消息的间隔。默认 20 秒。
            </p>
            <Input
              id="status-interval"
              inputMode="numeric"
              className="w-full"
              value={statusIntervalInput}
              onChange={(e) => {
                setStatusIntervalInput(e.target.value.replace(/[^0-9]/g, ""));
                setDirty(true);
              }}
            />
          </div>

          </div>

          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="image-size">默认分辨率</Label>
              <Select
                id="image-size"
                value={imageSize}
                onChange={(e) => {
                  setImageSize(e.target.value);
                  setDirty(true);
                }}
              >
                <option value="auto">auto（自动）</option>
                <option value="1024x1024">1024x1024（方形）</option>
                <option value="1536x1024">1536x1024（横图）</option>
                <option value="1024x1536">1024x1536（竖图）</option>
                <option value="from_reference">from_reference（参考图尺寸）</option>
              </Select>
              <p className="text-xs text-muted-foreground">
                <code className="mx-0.5">from_reference</code> 使用参考图尺寸；指令可用 --size 覆盖。
              </p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="aspect-ratio">默认画面比例</Label>
              <Select
                id="aspect-ratio"
                value={aspectRatio}
                onChange={(e) => {
                  setAspectRatio(e.target.value);
                  setDirty(true);
                }}
              >
                <option value="auto">auto（自动）</option>
                <option value="1:1">1:1（方形）</option>
                <option value="3:2">3:2（横图）</option>
                <option value="2:3">2:3（竖图）</option>
                <option value="4:3">4:3</option>
                <option value="3:4">3:4</option>
                <option value="16:9">16:9（宽屏）</option>
                <option value="9:16">9:16（竖屏）</option>
                <option value="from_reference">from_reference（参考图比例）</option>
              </Select>
              <p className="text-xs text-muted-foreground">
                <code className="mx-0.5">from_reference</code> 使用参考图比例；指令可用 --比例 覆盖。
              </p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="image-format">默认图片格式</Label>
              <Select
                id="image-format"
                value={imageFormat}
                onChange={(e) => {
                  setImageFormat(e.target.value);
                  setDirty(true);
                }}
              >
                <option value="png">png</option>
                <option value="jpeg">jpeg</option>
                <option value="webp">webp</option>
              </Select>
              <p className="text-xs text-muted-foreground">指令可用 --format 或 --格式 覆盖。</p>
            </div>
          </div>

          <div className="space-y-2 rounded-md border bg-muted/30 p-3">
            <div>
              <Label htmlFor="message-template">消息模板</Label>
              <p className="text-xs text-muted-foreground">
                同一个模板用于生成中的状态编辑和最终图片 caption。状态刷新间隔建议 10 秒以上，减少频繁编辑。
              </p>
            </div>
            <div className="flex flex-wrap gap-1">
              {TEMPLATE_PLACEHOLDERS.map((p) => (
                <button
                  key={p.key}
                  type="button"
                  className="rounded border px-1.5 py-0.5 text-[11px] font-mono hover:bg-background"
                  title={p.key}
                  onClick={() => {
                    setMessageTemplate((v) => `${v}${p.key}`);
                    setDirty(true);
                  }}
                >
                  {p.label}
                </button>
              ))}
              <button
                type="button"
                className="rounded border px-1.5 py-0.5 text-[11px] font-mono hover:bg-background"
                onClick={() => {
                  setMessageTemplate((v) => `${v}{?revised_prompt}\n{revised_prompt}{/?}`);
                  setDirty(true);
                }}
              >
                条件:修订提示词
              </button>
            </div>
            <Textarea
              id="message-template"
              rows={8}
              maxLength={1000}
              className="font-mono text-xs"
              value={messageTemplate}
              onChange={(e) => {
                setMessageTemplate(e.target.value);
                setDirty(true);
              }}
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm">
              <input
                type="checkbox"
                checked={deleteCommandMessage}
                onChange={(e) => {
                  setDeleteCommandMessage(e.target.checked);
                  setDirty(true);
                }}
              />
              完成后删除指令消息
            </label>
            <label className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm">
              <input
                type="checkbox"
                checked={showRevisedPrompt}
                onChange={(e) => {
                  setShowRevisedPrompt(e.target.checked);
                  setDirty(true);
                }}
              />
              显示修订提示词
            </label>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="reasoning-effort">推理强度</Label>
            <select
              id="reasoning-effort"
              className="h-9 w-36 rounded-md border bg-background px-3 text-sm"
              value={reasoningEffort}
              onChange={(e) => {
                setReasoningEffort(e.target.value);
                setDirty(true);
              }}
            >
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="custom-instructions">自定义系统指令</Label>
            <p className="text-xs text-muted-foreground">
              留空使用默认指令；可指定图片风格、构图偏好或安全边界。
            </p>
            <textarea
              id="custom-instructions"
              className="min-h-24 w-full rounded-md border bg-background px-3 py-2 text-sm"
              value={customInstructions}
              onChange={(e) => {
                setCustomInstructions(e.target.value);
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
              <Button disabled={!dirty} loading={saveMut.isPending} onClick={handleSave}>
                {!saveMut.isPending ? (
                  <Save className="mr-2 h-4 w-4" />
                ) : null}
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
          <CardDescription>使用模拟上下文渲染当前消息模板，不会触发真实发送。</CardDescription>
        </CardHeader>
        <CardContent>
          <TelegramHtmlPreview value={renderTemplate(messageTemplate, previewValues)} />
        </CardContent>
      </Card>
    </div>
  );
}
