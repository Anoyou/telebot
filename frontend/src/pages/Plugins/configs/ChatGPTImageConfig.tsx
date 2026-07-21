import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  ClipboardPaste,
    Plus,
  Save,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import { listAccountFeatures, toggleAccountFeature } from "@/api/accounts";
import { getEffectiveConfig, updateAccountFeatureConfig } from "@/api/features";
import { getSystemSettings } from "@/api/system";
import { CommandBadge } from "@/components/CommandBadge";
import { TelegramHtmlPreview } from "@/components/TelegramHtmlPreview";
import { Badge } from "@/components/ui/badge";
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
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { getErrMsg } from "@/lib/api";
import { confirmDiscardChanges, useUnsavedChanges } from "@/lib/unsavedChanges";
import { featureConfigBackTarget } from "@/pages/Plugins/_shared/featureConfig";
import { featureRuntimeText, featureSwitchText } from "./_shared/featureStatus";

type TokenEntry = {
  token: string;
  note: string;
  token_id?: string;
};

type ChatGPTImageConfig = {
  command: string;
  edit_command: string;
  admin_command: string;
  token: string;
  tokens: TokenEntry[];
  default_model: string;
  available_models: string;
  default_count: number;
  max_count: number;
  default_size: string;
  image_format: string;
  output_mode: string;
  message_template: string;
  style_templates: string;
  default_style: string;
  timeout: number;
  poll_timeout: number;
  poll_interval: number;
  remember_last_image: boolean;
  reference_image_limit: number;
  skip_failed_seconds: number;
  auto_disable_invalid_tokens: boolean;
  health_check_enabled: boolean;
  health_check_interval: number;
  sub2api_base_url: string;
  sub2api_email: string;
  sub2api_password: string;
  sub2api_api_key: string;
  sub2api_group_id: string;
  cpa_base_url: string;
  cpa_secret_key: string;
  cpa_file_names: string;
  log_prompt_preview: boolean;
};

const DEFAULT_STYLE_TEMPLATES = `写实=请以高质量写实摄影风格生成：{prompt}
海报=请以精致商业海报风格生成，构图清晰，文字区域干净：{prompt}
头像=请生成适合作为头像的正方形主体构图，背景简洁：{prompt}
二次元=请以精致二次元插画风格生成：{prompt}
Logo=请生成简洁现代的品牌 Logo 草案，适合矢量化：{prompt}`;

const DEFAULT_MODELS = [
  "gpt-image-2",
  "codex-gpt-image-2",
  "auto",
  "gpt-5",
  "gpt-5-1",
  "gpt-5-2",
  "gpt-5-3",
  "gpt-5-3-mini",
  "gpt-5-mini",
].join("\n");

const DEFAULT_MESSAGE_TEMPLATE =
  "<b>ChatGPT2API</b>\n<b>状态:</b> {status}\n<b>提示词:</b> {prompt}\n<b>模型:</b> {model} · <b>数量:</b> {count}\n<b>画幅:</b> {size} · <b>格式:</b> {image_format}\n<b>耗时:</b> {elapsed}";

const TEMPLATE_PLACEHOLDERS = [
  { key: "{status}", label: "状态" },
  { key: "{prompt}", label: "提示词" },
  { key: "{model}", label: "模型" },
  { key: "{count}", label: "请求张数" },
  { key: "{result_count}", label: "结果张数" },
  { key: "{size}", label: "画幅" },
  { key: "{style}", label: "风格" },
  { key: "{image_format}", label: "格式" },
  { key: "{output_mode}", label: "发送方式" },
  { key: "{elapsed}", label: "耗时" },
  { key: "{command}", label: "文生图指令" },
  { key: "{edit_command}", label: "编辑指令" },
  { key: "{admin_command}", label: "管理指令" },
  { key: "{has_reference}", label: "参考图" },
  { key: "{reference_count}", label: "参考图数" },
  { key: "{proxy}", label: "代理" },
];

const DEFAULT_CONFIG: ChatGPTImageConfig = {
  command: "draw",
  edit_command: "edit",
  admin_command: "gptimg",
  token: "",
  tokens: [],
  default_model: "gpt-image-2",
  available_models: DEFAULT_MODELS,
  default_count: 1,
  max_count: 4,
  default_size: "1:1",
  image_format: "png",
  output_mode: "auto",
  message_template: DEFAULT_MESSAGE_TEMPLATE,
  style_templates: DEFAULT_STYLE_TEMPLATES,
  default_style: "",
  timeout: 300,
  poll_timeout: 180,
  poll_interval: 10,
  remember_last_image: true,
  reference_image_limit: 6,
  skip_failed_seconds: 600,
  auto_disable_invalid_tokens: true,
  health_check_enabled: false,
  health_check_interval: 3600,
  sub2api_base_url: "",
  sub2api_email: "",
  sub2api_password: "",
  sub2api_api_key: "",
  sub2api_group_id: "",
  cpa_base_url: "",
  cpa_secret_key: "",
  cpa_file_names: "",
  log_prompt_preview: true,
};

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function secretText(value: unknown): string {
  const raw = text(value).trim();
  return raw === "***" ? "" : raw;
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function num(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clampedInt(value: string, fallback: number, min: number, max: number): number {
  const parsed = Number.parseInt(value.replace(/[^0-9]/g, ""), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}

function normalizeTokens(value: unknown): TokenEntry[] {
  if (!Array.isArray(value)) return [];
  const out: TokenEntry[] = [];
  for (const item of value) {
    if (typeof item === "string") {
      const token = item.trim();
      if (token) out.push({ token, note: "" });
      continue;
    }
    if (!item || typeof item !== "object") continue;
    const raw = item as Record<string, unknown>;
    const token = text(raw.token).trim();
    if (!token) continue;
    out.push({
      token,
      note: text(raw.note || raw.remark || raw.source).trim(),
      token_id: text(raw.token_id).trim() || undefined,
    });
  }
  return out;
}

function extractAccessToken(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  try {
    const payload = JSON.parse(trimmed);
    const visit = (value: unknown): string => {
      if (!value || typeof value !== "object") return "";
      const dict = value as Record<string, unknown>;
      const direct = text(dict.accessToken || dict.access_token).trim();
      if (direct) return direct;
      for (const child of Object.values(dict)) {
        const found = visit(child);
        if (found) return found;
      }
      return "";
    };
    return visit(payload);
  } catch {
    return "";
  }
}

function parseTokenInput(raw: string): string[] {
  const sessionToken = extractAccessToken(raw);
  if (sessionToken) return [sessionToken];
  return Array.from(
    new Set(
      raw
        .replace(/,/g, "\n")
        .split(/\s+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function renderTemplate(template: string, values: Record<string, string>): string {
  let out = template || DEFAULT_CONFIG.message_template;
  out = out.replace(/\{\?([a-zA-Z0-9_]+)\}([\s\S]*?)\{\/\?\}/g, (_, key: string, inner: string) =>
    values[key] ? inner : "",
  );
  out = out.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, key: string) => values[key] ?? "");
  return out;
}

function CollapsibleSection({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <details className="rounded-md border bg-background">
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
        {title}
        <span className="ml-2 text-xs font-normal text-muted-foreground">
          {description}
        </span>
      </summary>
      <div className="space-y-4 border-t px-4 py-4">{children}</div>
    </details>
  );
}

export function ChatGPTImageConfigPage() {
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
  const configQ = useQuery({
    queryKey: ["account", aid, "features", "chatgpt_image", "config"],
    queryFn: () => getEffectiveConfig(aid, "chatgpt_image"),
    enabled: !!aid,
  });
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });

  const feature = featuresQ.data?.find((item) => item.feature_key === "chatgpt_image");
  const cmdPrefix = settingsQ.data?.command_prefix || ",";

  const [command, setCommand] = useState(DEFAULT_CONFIG.command);
  const [editCommand, setEditCommand] = useState(DEFAULT_CONFIG.edit_command);
  const [adminCommand, setAdminCommand] = useState(DEFAULT_CONFIG.admin_command);
  const [tokens, setTokens] = useState<TokenEntry[]>([]);
  const [newToken, setNewToken] = useState("");
  const [newTokenNote, setNewTokenNote] = useState("");
  const [defaultModel, setDefaultModel] = useState(DEFAULT_CONFIG.default_model);
  const [availableModels, setAvailableModels] = useState(DEFAULT_CONFIG.available_models);
  const [defaultCount, setDefaultCount] = useState(String(DEFAULT_CONFIG.default_count));
  const [maxCount, setMaxCount] = useState(String(DEFAULT_CONFIG.max_count));
  const [defaultSize, setDefaultSize] = useState(DEFAULT_CONFIG.default_size);
  const [imageFormat, setImageFormat] = useState(DEFAULT_CONFIG.image_format);
  const [outputMode, setOutputMode] = useState(DEFAULT_CONFIG.output_mode);
  const [messageTemplate, setMessageTemplate] = useState(DEFAULT_CONFIG.message_template);
  const [styleTemplates, setStyleTemplates] = useState(DEFAULT_CONFIG.style_templates);
  const [defaultStyle, setDefaultStyle] = useState(DEFAULT_CONFIG.default_style);
  const [timeout, setTimeoutInput] = useState(String(DEFAULT_CONFIG.timeout));
  const [pollTimeout, setPollTimeout] = useState(String(DEFAULT_CONFIG.poll_timeout));
  const [pollInterval, setPollInterval] = useState(String(DEFAULT_CONFIG.poll_interval));
  const [rememberLastImage, setRememberLastImage] = useState(DEFAULT_CONFIG.remember_last_image);
  const [referenceImageLimit, setReferenceImageLimit] = useState(String(DEFAULT_CONFIG.reference_image_limit));
  const [skipFailedSeconds, setSkipFailedSeconds] = useState(String(DEFAULT_CONFIG.skip_failed_seconds));
  const [autoDisableInvalidTokens, setAutoDisableInvalidTokens] = useState(DEFAULT_CONFIG.auto_disable_invalid_tokens);
  const [healthCheckEnabled, setHealthCheckEnabled] = useState(DEFAULT_CONFIG.health_check_enabled);
  const [healthCheckInterval, setHealthCheckInterval] = useState(String(DEFAULT_CONFIG.health_check_interval));
  const [sub2apiBaseUrl, setSub2apiBaseUrl] = useState(DEFAULT_CONFIG.sub2api_base_url);
  const [sub2apiEmail, setSub2apiEmail] = useState(DEFAULT_CONFIG.sub2api_email);
  const [sub2apiPassword, setSub2apiPassword] = useState(DEFAULT_CONFIG.sub2api_password);
  const [sub2apiApiKey, setSub2apiApiKey] = useState(DEFAULT_CONFIG.sub2api_api_key);
  const [sub2apiGroupId, setSub2apiGroupId] = useState(DEFAULT_CONFIG.sub2api_group_id);
  const [cpaBaseUrl, setCpaBaseUrl] = useState(DEFAULT_CONFIG.cpa_base_url);
  const [cpaSecretKey, setCpaSecretKey] = useState(DEFAULT_CONFIG.cpa_secret_key);
  const [cpaFileNames, setCpaFileNames] = useState(DEFAULT_CONFIG.cpa_file_names);
  const [logPromptPreview, setLogPromptPreview] = useState(DEFAULT_CONFIG.log_prompt_preview);
  const [dirty, setDirty] = useState(false);
  const [remoteConfigChanged, setRemoteConfigChanged] = useState(false);

  useEffect(() => {
    if (dirty) {
      setRemoteConfigChanged(true);
      return;
    }
    const cfg = (configQ.data ?? {}) as Record<string, unknown>;
    setCommand(text(cfg.command, DEFAULT_CONFIG.command));
    setEditCommand(text(cfg.edit_command, DEFAULT_CONFIG.edit_command));
    setAdminCommand(text(cfg.admin_command, DEFAULT_CONFIG.admin_command));
    setTokens(normalizeTokens(cfg.tokens));
    setDefaultModel(text(cfg.default_model, DEFAULT_CONFIG.default_model));
    setAvailableModels(text(cfg.available_models, DEFAULT_CONFIG.available_models));
    setDefaultCount(String(num(cfg.default_count, DEFAULT_CONFIG.default_count)));
    setMaxCount(String(num(cfg.max_count, DEFAULT_CONFIG.max_count)));
    setDefaultSize(text(cfg.default_size, DEFAULT_CONFIG.default_size));
    setImageFormat(text(cfg.image_format, DEFAULT_CONFIG.image_format));
    setOutputMode(text(cfg.output_mode, DEFAULT_CONFIG.output_mode));
    setMessageTemplate(text(cfg.message_template, DEFAULT_CONFIG.message_template));
    setStyleTemplates(text(cfg.style_templates, DEFAULT_CONFIG.style_templates));
    setDefaultStyle(text(cfg.default_style, DEFAULT_CONFIG.default_style));
    setTimeoutInput(String(num(cfg.timeout, DEFAULT_CONFIG.timeout)));
    setPollTimeout(String(num(cfg.poll_timeout, DEFAULT_CONFIG.poll_timeout)));
    setPollInterval(String(num(cfg.poll_interval, DEFAULT_CONFIG.poll_interval)));
    setRememberLastImage(bool(cfg.remember_last_image, DEFAULT_CONFIG.remember_last_image));
    setReferenceImageLimit(String(num(cfg.reference_image_limit, DEFAULT_CONFIG.reference_image_limit)));
    setSkipFailedSeconds(String(num(cfg.skip_failed_seconds, DEFAULT_CONFIG.skip_failed_seconds)));
    setAutoDisableInvalidTokens(bool(cfg.auto_disable_invalid_tokens, DEFAULT_CONFIG.auto_disable_invalid_tokens));
    setHealthCheckEnabled(bool(cfg.health_check_enabled, DEFAULT_CONFIG.health_check_enabled));
    setHealthCheckInterval(String(num(cfg.health_check_interval, DEFAULT_CONFIG.health_check_interval)));
    setSub2apiBaseUrl(text(cfg.sub2api_base_url, DEFAULT_CONFIG.sub2api_base_url));
    setSub2apiEmail(text(cfg.sub2api_email, DEFAULT_CONFIG.sub2api_email));
    setSub2apiPassword("");
    setSub2apiApiKey(secretText(cfg.sub2api_api_key));
    setSub2apiGroupId(text(cfg.sub2api_group_id, DEFAULT_CONFIG.sub2api_group_id));
    setCpaBaseUrl(text(cfg.cpa_base_url, DEFAULT_CONFIG.cpa_base_url));
    setCpaSecretKey(secretText(cfg.cpa_secret_key));
    setCpaFileNames(text(cfg.cpa_file_names, DEFAULT_CONFIG.cpa_file_names));
    setLogPromptPreview(bool(cfg.log_prompt_preview, DEFAULT_CONFIG.log_prompt_preview));
    setDirty(false);
    setRemoteConfigChanged(false);
  }, [configQ.data]);
  useUnsavedChanges(dirty);

  const modelOptions = useMemo(
    () => availableModels.split(/\n+/).map((item) => item.trim()).filter(Boolean),
    [availableModels],
  );

  const saveMut = useMutation({
    mutationFn: (config: ChatGPTImageConfig) =>
      updateAccountFeatureConfig(aid, "chatgpt_image", config as unknown as Record<string, unknown>),
    onSuccess: () => {
      toast.success("配置已保存，worker 会自动热加载");
      setDirty(false);
      setRemoteConfigChanged(false);
      setNewToken("");
      setNewTokenNote("");
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["account", aid, "features", "chatgpt_image", "config"] });
      qc.invalidateQueries({ queryKey: ["matrix"] });
      qc.invalidateQueries({ queryKey: ["message-templates", "catalog", aid] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const toggleMut = useMutation({
    mutationFn: (enabled: boolean) => toggleAccountFeature(aid, "chatgpt_image", enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account", aid, "features"] });
      qc.invalidateQueries({ queryKey: ["matrix"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  function markDirty() {
    setDirty(true);
  }

  function addTokens() {
    const parsed = parseTokenInput(newToken);
    if (parsed.length === 0) {
      toast.error("没有识别到 token 或 accessToken");
      return;
    }
    setTokens((prev) => {
      const seen = new Set(prev.map((item) => item.token));
      const next = [...prev];
      for (const token of parsed) {
        if (seen.has(token)) continue;
        seen.add(token);
        next.push({ token, note: newTokenNote.trim() });
      }
      return next;
    });
    setNewToken("");
    setNewTokenNote("");
    markDirty();
    toast.success(`已加入 ${parsed.length} 条，保存后生效`);
  }

  function updateTokenNote(index: number, note: string) {
    setTokens((prev) =>
      prev.map((item, itemIndex) => (itemIndex === index ? { ...item, note } : item)),
    );
    markDirty();
  }

  function removeToken(index: number) {
    setTokens((prev) => prev.filter((_, itemIndex) => itemIndex !== index));
    markDirty();
  }

  function handleSave() {
    const max = clampedInt(maxCount, DEFAULT_CONFIG.max_count, 1, 4);
    const defCount = clampedInt(defaultCount, DEFAULT_CONFIG.default_count, 1, max);
    saveMut.mutate({
      command: command.trim() || DEFAULT_CONFIG.command,
      edit_command: editCommand.trim() || DEFAULT_CONFIG.edit_command,
      admin_command: adminCommand.trim() || DEFAULT_CONFIG.admin_command,
      token: "",
      tokens: tokens.map((item) => ({
        token: item.token,
        note: item.note.trim(),
        token_id: item.token_id,
      })),
      default_model: defaultModel,
      available_models: availableModels,
      default_count: defCount,
      max_count: max,
      default_size: defaultSize.trim() || DEFAULT_CONFIG.default_size,
      image_format: imageFormat,
      output_mode: outputMode,
      message_template: messageTemplate || DEFAULT_CONFIG.message_template,
      style_templates: styleTemplates,
      default_style: defaultStyle.trim(),
      timeout: clampedInt(timeout, DEFAULT_CONFIG.timeout, 30, 900),
      poll_timeout: clampedInt(pollTimeout, DEFAULT_CONFIG.poll_timeout, 30, 900),
      poll_interval: clampedInt(pollInterval, DEFAULT_CONFIG.poll_interval, 3, 60),
      remember_last_image: rememberLastImage,
      reference_image_limit: clampedInt(referenceImageLimit, DEFAULT_CONFIG.reference_image_limit, 1, 10),
      skip_failed_seconds: clampedInt(skipFailedSeconds, DEFAULT_CONFIG.skip_failed_seconds, 0, 86400),
      auto_disable_invalid_tokens: autoDisableInvalidTokens,
      health_check_enabled: healthCheckEnabled,
      health_check_interval: clampedInt(healthCheckInterval, DEFAULT_CONFIG.health_check_interval, 300, 86400),
      sub2api_base_url: sub2apiBaseUrl.trim(),
      sub2api_email: sub2apiEmail.trim(),
      sub2api_password: sub2apiPassword,
      sub2api_api_key: sub2apiApiKey.trim(),
      sub2api_group_id: sub2apiGroupId.trim(),
      cpa_base_url: cpaBaseUrl.trim(),
      cpa_secret_key: cpaSecretKey.trim(),
      cpa_file_names: cpaFileNames,
      log_prompt_preview: logPromptPreview,
    });
  }

  const effectiveCommand = command.trim() || DEFAULT_CONFIG.command;
  const effectiveEditCommand = editCommand.trim() || DEFAULT_CONFIG.edit_command;
  const effectiveAdminCommand = adminCommand.trim() || DEFAULT_CONFIG.admin_command;
  const previewValues = {
    status: "已完成",
    prompt: "精致二次元插画：云海里的未来城市，电影感光影",
    model: defaultModel,
    count: defaultCount || "1",
    result_count: defaultCount || "1",
    size: defaultSize,
    style: defaultStyle || "二次元",
    image_format: imageFormat,
    output_mode: outputMode,
    elapsed: "42秒",
    command: effectiveCommand,
    edit_command: effectiveEditCommand,
    admin_command: effectiveAdminCommand,
    has_reference: "是",
    reference_count: "1",
    proxy: "跟随账号代理",
  };

  if (!aid) return <p>账号 ID 不合法</p>;
  if (featuresQ.isLoading || configQ.isLoading) {
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
        <div className="flex items-center gap-3">
          <Button
            variant="default"
            size="sm"
            className="gap-1.5 shadow-sm"
            onClick={() => confirmDiscardChanges(dirty) && nav(backTarget.backHref)}
          >
            <ArrowLeft className="h-4 w-4" /> {backTarget.backLabel}
          </Button>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">ChatGPT2API</h1>
            <p className="text-sm text-muted-foreground">
              实验性 · 开关：{featureSwitchText(feature)} · 运行状态：{featureRuntimeText(feature)}
            </p>
          </div>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">使用说明</CardTitle>
          <CardDescription>通过 ChatGPT2API 生成和编辑图片，命令名不需要填写系统前缀。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-start gap-2 rounded-md border px-3 py-2 text-xs alert-warning">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <div className="font-medium">实验性能力</div>
              <div className="mt-0.5 text-muted-foreground">
                依赖 ChatGPT Web 非公开接口，可能随上游变化出现排队、风控、降级或失效。
              </div>
            </div>
          </div>

          <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
            <ul className="mt-1.5 list-inside list-disc space-y-1 break-words">
              <li>文生图：<CommandBadge>{cmdPrefix}{effectiveCommand} 提示词</CommandBadge></li>
              <li>
                指定模型/数量/风格：{" "}
                <CommandBadge>{cmdPrefix}{effectiveCommand} -m gpt-image-2 -n 1 -s 二次元 云海里的未来城市</CommandBadge>
              </li>
              <li>指定画幅：<CommandBadge>{cmdPrefix}{effectiveCommand} --size 16:9 赛博朋克街景</CommandBadge></li>
              <li>回复图片编辑：<CommandBadge>{cmdPrefix}{effectiveEditCommand} 给这张图加上雨夜霓虹</CommandBadge></li>
              <li>续改最近图片：<CommandBadge>{cmdPrefix}{effectiveEditCommand} last 改成二次元头像</CommandBadge></li>
              <li>
                管理指令：{" "}
                <CommandBadge>{cmdPrefix}{effectiveAdminCommand} ping</CommandBadge>{" "}
                <CommandBadge>{cmdPrefix}{effectiveAdminCommand} status</CommandBadge>{" "}
                <CommandBadge>{cmdPrefix}{effectiveAdminCommand} token list</CommandBadge>
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
              <CardDescription>关闭后 ChatGPT2API 图片生成不会响应当前账号的触发指令。</CardDescription>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant={feature?.enabled ? "default" : "outline"}>
                  {featureSwitchText(feature)}
                </Badge>
                <span>运行状态：{featureRuntimeText(feature)}</span>
                {feature?.last_error ? (
                  <span className="text-destructive">最近错误：{feature.last_error}</span>
                ) : null}
              </div>
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
            指令、模型、Token、网络和高级参数。所有指令名都不需要填写系统前缀。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6 pb-0">
          <section className="space-y-5 rounded-md border bg-muted/20 p-4">
            <div>
              <div className="text-sm font-semibold">基础配置</div>
              <p className="text-xs text-muted-foreground">指令、模型、输出格式和 ChatGPT 访问参数。</p>
            </div>
          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label>文生图指令</Label>
              <Input value={command} onChange={(e) => { setCommand(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">例如 draw，实际使用为 ,draw 提示词。</p>
            </div>
            <div className="space-y-1.5">
              <Label>图片编辑指令</Label>
              <Input value={editCommand} onChange={(e) => { setEditCommand(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">回复图片后使用，也支持 edit last 续改最近图片。</p>
            </div>
            <div className="space-y-1.5">
              <Label>管理指令</Label>
              <Input value={adminCommand} onChange={(e) => { setAdminCommand(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">用于 ping、models、status、refresh、token、import、proxy。</p>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label>默认图片模型</Label>
              <Select value={defaultModel} onChange={(e) => { setDefaultModel(e.target.value); markDirty(); }}>
                {modelOptions.map((model) => (
                  <option key={model} value={model}>{model}</option>
                ))}
              </Select>
              <p className="text-xs text-muted-foreground">指令没有 -m 参数时使用此模型。</p>
            </div>
            <div className="space-y-1.5">
              <Label>默认生成张数</Label>
              <Input value={defaultCount} onChange={(e) => { setDefaultCount(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">未写 -n 时默认生成几张。</p>
            </div>
            <div className="space-y-1.5">
              <Label>单次最多张数</Label>
              <Input value={maxCount} onChange={(e) => { setMaxCount(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">限制单次指令最多图片数，范围 1-4。</p>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-4">
            <div className="space-y-1.5">
              <Label>默认画幅提示</Label>
              <Input value={defaultSize} onChange={(e) => { setDefaultSize(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">如 1:1、16:9、9:16，也可写自定义说明。</p>
            </div>
            <div className="space-y-1.5">
              <Label>发送图片格式</Label>
              <Select value={imageFormat} onChange={(e) => { setImageFormat(e.target.value); markDirty(); }}>
                <option value="png">PNG</option>
                <option value="jpeg">JPEG</option>
                <option value="webp">WebP</option>
              </Select>
              <p className="text-xs text-muted-foreground">用于 Telegram 文件名后缀。</p>
            </div>
            <div className="space-y-1.5">
              <Label>结果发送方式</Label>
              <Select value={outputMode} onChange={(e) => { setOutputMode(e.target.value); markDirty(); }}>
                <option value="auto">自动</option>
                <option value="image">图片</option>
                <option value="file">文件</option>
              </Select>
              <p className="text-xs text-muted-foreground">自动模式会在图片发送失败后改发文件。</p>
            </div>
            <div className="space-y-1.5">
              <Label>最多参考图数量</Label>
              <Input value={referenceImageLimit} onChange={(e) => { setReferenceImageLimit(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">图片编辑时最多读取的参考图片数。</p>
            </div>
          </div>

          <div className="space-y-2 rounded-md border bg-muted/30 p-3">
            <div>
              <Label>消息模板</Label>
              <p className="text-xs text-muted-foreground">
                用于最终图片 caption，支持 HTML 标签和占位符。点击下方按钮会把占位符追加到模板末尾。
              </p>
            </div>
            <div className="flex flex-wrap gap-1">
              {TEMPLATE_PLACEHOLDERS.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className="rounded border px-1.5 py-0.5 text-[11px] font-mono hover:bg-background"
                  title={item.key}
                  onClick={() => {
                    setMessageTemplate((value) => `${value}${item.key}`);
                    markDirty();
                  }}
                >
                  {item.label}
                </button>
              ))}
              <button
                type="button"
                className="rounded border px-1.5 py-0.5 text-[11px] font-mono hover:bg-background"
                onClick={() => {
                  setMessageTemplate((value) => `${value}{?has_reference}\n参考图：{reference_count} 张{/?}`);
                  markDirty();
                }}
              >
                条件:参考图
              </button>
            </div>
            <Textarea
              rows={8}
              maxLength={1000}
              className="font-mono text-xs"
              value={messageTemplate}
              onChange={(e) => { setMessageTemplate(e.target.value); markDirty(); }}
            />
          </div>

          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-1.5 md:col-span-2">
              <Label>可选模型列表</Label>
              <Textarea rows={6} value={availableModels} onChange={(e) => { setAvailableModels(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">每行一个模型名；指令 -m 会按这里校验。</p>
            </div>
            <div className="space-y-3 rounded-md border bg-muted/20 p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <Label>记住最近图片</Label>
                  <p className="text-xs text-muted-foreground">开启后可用 edit last 续改最近生成或编辑的图片。</p>
                </div>
                <Switch checked={rememberLastImage} onCheckedChange={(v) => { setRememberLastImage(v); markDirty(); }} />
              </div>
              <div className="flex items-center justify-between gap-3">
                <div>
                  <Label>记录提示词摘要</Label>
                  <p className="text-xs text-muted-foreground">插件日志只记录截断后的提示词摘要。</p>
                </div>
                <Switch checked={logPromptPreview} onCheckedChange={(v) => { setLogPromptPreview(v); markDirty(); }} />
              </div>
            </div>
          </div>
          </section>

          <section className="space-y-5 rounded-md border bg-muted/20 p-4">
            <div>
              <div className="text-sm font-semibold">网络与 Token 池</div>
              <p className="text-xs text-muted-foreground">插件网络出口跟随当前账号代理；这里逐条管理 ChatGPT access token。</p>
            </div>
          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label>请求超时</Label>
              <Input value={timeout} onChange={(e) => { setTimeoutInput(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">范围 30-900 秒。</p>
            </div>
            <div className="space-y-1.5">
              <Label>轮询超时</Label>
              <Input value={pollTimeout} onChange={(e) => { setPollTimeout(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">等待图片结果的最长时间。</p>
            </div>
            <div className="space-y-1.5">
              <Label>轮询间隔</Label>
              <Input value={pollInterval} onChange={(e) => { setPollInterval(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">建议 5-15 秒。</p>
            </div>
          </div>

          <div className="space-y-3">
            <div>
              <Label>Token 池</Label>
              <p className="text-xs text-muted-foreground">
                已保存 token 会显示首尾各 10 字符，中间用圆点隐藏；备注用于标记来源。
              </p>
            </div>
            <div className="space-y-2">
              {tokens.length === 0 ? (
                <div className="rounded-md border border-dashed px-4 py-6 text-sm text-muted-foreground">
                  暂未配置 token。
                </div>
              ) : (
                tokens.map((entry, index) => (
                  <div key={`${entry.token_id || entry.token}-${index}`} className="grid gap-2 rounded-md border p-3 md:grid-cols-[minmax(0,1fr)_minmax(180px,280px)_auto]">
                    <Input value={entry.token} readOnly aria-label="已保存 token" />
                    <Input
                      value={entry.note}
                      placeholder="备注，例如账号邮箱、sub2api 分组、CPA 文件名"
                      onChange={(e) => updateTokenNote(index, e.target.value)}
                    />
                    <Button variant="ghost" size="icon" onClick={() => removeToken(index)} aria-label="删除 token">
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                ))
              )}
            </div>

            <div className="grid gap-3 rounded-md border bg-muted/20 p-4 md:grid-cols-[minmax(0,1fr)_minmax(180px,260px)_auto]">
              <div className="space-y-1.5">
                <Label>新增 token 或 session JSON</Label>
                <Textarea
                  rows={4}
                  value={newToken}
                  onChange={(e) => setNewToken(e.target.value)}
                  placeholder="粘贴 access token；也可粘贴 https://chatgpt.com/api/auth/session 返回的完整 JSON"
                />
              </div>
              <div className="space-y-1.5">
                <Label>备注</Label>
                <Input
                  value={newTokenNote}
                  onChange={(e) => setNewTokenNote(e.target.value)}
                  placeholder="这条 token 来自哪里"
                />
              </div>
              <div className="flex items-end gap-2">
                <Button type="button" onClick={addTokens}>
                  {extractAccessToken(newToken) ? <ClipboardPaste className="mr-2 h-4 w-4" /> : <Plus className="mr-2 h-4 w-4" />}
                  加入
                </Button>
              </div>
            </div>
          </div>
          </section>

          <section className="space-y-4 rounded-md border bg-muted/20 p-4">
            <div>
              <div className="text-sm font-semibold">高级配置</div>
              <p className="text-xs text-muted-foreground">风格模板、导入来源和健康检测。外部来源配置默认折叠，避免常用项被淹没。</p>
            </div>
          <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_220px]">
            <div className="space-y-1.5">
              <Label>风格模板</Label>
              <Textarea rows={7} value={styleTemplates} onChange={(e) => { setStyleTemplates(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">每行一个，格式为 名称=模板内容；模板内用 {"{prompt}"} 代表原始提示词。</p>
            </div>
            <div className="space-y-1.5">
              <Label>默认风格</Label>
              <Input value={defaultStyle} onChange={(e) => { setDefaultStyle(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">留空表示默认不套模板；指令可用 -s 覆盖。</p>
            </div>
          </div>

          <CollapsibleSection title="CPA 导入配置" description="用于从 CLIProxyAPI auth 文件导入 token">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label>CPA 地址</Label>
                <Input value={cpaBaseUrl} onChange={(e) => { setCpaBaseUrl(e.target.value); markDirty(); }} />
                <p className="text-xs text-muted-foreground">CLIProxyAPI 管理端地址。</p>
              </div>
              <div className="space-y-1.5">
                <Label>CPA Secret Key</Label>
                <Input value={cpaSecretKey} onChange={(e) => { setCpaSecretKey(e.target.value); markDirty(); }} />
                <p className="text-xs text-muted-foreground">只用于请求远程 auth 文件，不写入日志。</p>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>CPA 文件名</Label>
              <Textarea rows={4} value={cpaFileNames} onChange={(e) => { setCpaFileNames(e.target.value); markDirty(); }} />
              <p className="text-xs text-muted-foreground">每行一个文件名；留空时 import cpa 只列出远程文件。</p>
            </div>
          </CollapsibleSection>

          <CollapsibleSection title="sub2api 导入配置" description="用于从 sub2api 管理端导入 OpenAI OAuth 账号">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label>sub2api 地址</Label>
                <Input value={sub2apiBaseUrl} onChange={(e) => { setSub2apiBaseUrl(e.target.value); markDirty(); }} />
                <p className="text-xs text-muted-foreground">sub2api 管理端地址。</p>
              </div>
              <div className="space-y-1.5">
                <Label>sub2api 分组 ID</Label>
                <Input value={sub2apiGroupId} onChange={(e) => { setSub2apiGroupId(e.target.value); markDirty(); }} />
                <p className="text-xs text-muted-foreground">留空表示读取所有 OpenAI OAuth 账号。</p>
              </div>
              <div className="space-y-1.5">
                <Label>sub2api 邮箱</Label>
                <Input value={sub2apiEmail} onChange={(e) => { setSub2apiEmail(e.target.value); markDirty(); }} />
                <p className="text-xs text-muted-foreground">未填写 API Key 时用于登录。</p>
              </div>
              <div className="space-y-1.5">
                <Label>sub2api 密码</Label>
                <Input type="password" value={sub2apiPassword} onChange={(e) => { setSub2apiPassword(e.target.value); markDirty(); }} />
                <p className="text-xs text-muted-foreground">留空时会保留已保存的密码。</p>
              </div>
              <div className="space-y-1.5 md:col-span-2">
                <Label>sub2api API Key</Label>
                <Input value={sub2apiApiKey} onChange={(e) => { setSub2apiApiKey(e.target.value); markDirty(); }} />
                <p className="text-xs text-muted-foreground">填写后优先使用 API Key，无需邮箱密码。</p>
              </div>
            </div>
          </CollapsibleSection>

          <CollapsibleSection title="监控与失效处理" description="定时刷新额度并在内存中跳过异常 token">
            <div className="grid gap-4 md:grid-cols-3">
              <div className="flex items-center justify-between gap-3 rounded-md border p-3">
                <div>
                  <Label>启用健康检测</Label>
                  <p className="text-xs text-muted-foreground">定时刷新 token 额度与代理可用性，只写插件日志。</p>
                </div>
                <Switch checked={healthCheckEnabled} onCheckedChange={(v) => { setHealthCheckEnabled(v); markDirty(); }} />
              </div>
              <div className="flex items-center justify-between gap-3 rounded-md border p-3">
                <div>
                  <Label>自动禁用失效 token</Label>
                  <p className="text-xs text-muted-foreground">检测到 token 失效后在内存中跳过，不删除配置。</p>
                </div>
                <Switch checked={autoDisableInvalidTokens} onCheckedChange={(v) => { setAutoDisableInvalidTokens(v); markDirty(); }} />
              </div>
              <div className="space-y-1.5">
                <Label>失败 token 跳过秒数</Label>
                <Input value={skipFailedSeconds} onChange={(e) => { setSkipFailedSeconds(e.target.value); markDirty(); }} />
                <p className="text-xs text-muted-foreground">鉴权、额度或网络错误后临时跳过多久。</p>
              </div>
              <div className="space-y-1.5">
                <Label>健康检测间隔</Label>
                <Input value={healthCheckInterval} onChange={(e) => { setHealthCheckInterval(e.target.value); markDirty(); }} />
                <p className="text-xs text-muted-foreground">仅启用健康检测后生效，范围 300-86400 秒。</p>
              </div>
            </div>
          </CollapsibleSection>
          </section>
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
            <Button onClick={handleSave} loading={saveMut.isPending} disabled={!dirty}>
              {!saveMut.isPending ? <Save className="mr-2 h-4 w-4" /> : null}
              保存配置
            </Button>
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
