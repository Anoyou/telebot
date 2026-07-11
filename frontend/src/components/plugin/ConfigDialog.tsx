/**
 * 插件配置弹窗组件。
 *
 * 根据插件的 config_schema 自动渲染表单：
 * - level: "global" 的字段 → 全局配置区
 * - level: "account" 的字段 → 账号配置区
 * - 无 level → 默认 account
 *
 * 配置合并顺序（前端用于展示）：
 * schema defaults < globalConfig < accountConfig
 */
import { useState, useEffect, useCallback, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  Copy,
  GripVertical,
  Loader2,
  Pencil,
  Plus,
  Save,
  Trash2,
  Wand,
} from "lucide-react";
import { toast } from "sonner";
import { TelegramHtmlPreview, TelegramHtmlPreviewThread } from "@/components/TelegramHtmlPreview";
import { listLLMProviders } from "@/api/commands";
import { listIgnoredPeers } from "@/api/ignored_peers";
import { getSystemSettings } from "@/api/system";
import type { IgnoredPeer, LLMProviderOut } from "@/api/types";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

export const MASKED_SECRET_PLACEHOLDER = "••••••••••••••••";
const EMPTY_CONFIG: Record<string, unknown> = {};

export interface ConfigField {
  key: string;
  title?: string;
  type: string;
  format?: string;
  "x-ui-widget"?: string;
  "x-ui-hidden"?: boolean;
  "x-ui-section"?: string;
  "x-ui-order"?: number;
  "x-ui-columns"?: 1 | 2 | 3 | number;
  "x-ui-provider-field"?: string;
  "x-ui-model-modality"?: string;
  "x-ui-summary"?: string;
  "x-ui-title-field"?: string;
  "x-ui-description-field"?: string;
  "x-ui-enabled-field"?: string;
  "x-ui-reorderable"?: boolean;
  "x-ui-add-label"?: string;
  enum?: Array<string | number | boolean>;
  enumNames?: string[];
  enumDescriptions?: string[];
  items?: ConfigField;
  properties?: Record<string, ConfigField>;
  default?: unknown;
  description?: string;
  minimum?: number;
  maximum?: number;
  minItems?: number;
  maxItems?: number;
  level?: "global" | "account";
  readOnly?: boolean;
}

export interface ConfigAction {
  key: string;
  title?: string;
  description?: string;
  placement?: string;
  input_schema?: ConfigSchema;
  submit_label?: string;
}

export interface ConfigSchema {
  type: string;
  properties: Record<string, ConfigField>;
  required?: string[];
  "x-config-actions"?: ConfigAction[];
  "x-usage-guide"?: unknown;
  "x-usage-instructions"?: unknown;
  "x-usage-steps"?: unknown;
  "x-help"?: unknown;
}

interface ConfigDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pluginKey: string;
  pluginName: string;
  schema: ConfigSchema | Record<string, unknown> | null;
  accountName?: string;
  accountId?: number;
  globalConfig?: Record<string, unknown>;
  accountConfig?: Record<string, unknown>;
  /** 保存回调，返回更新后的 effective config */
  onSave?: (globalVals: Record<string, unknown>, accountVals: Record<string, unknown>) => Promise<void>;
}

export function ConfigDialog({
  open, onOpenChange, pluginKey, pluginName, schema, accountName,
  accountId, globalConfig, accountConfig, onSave,
}: ConfigDialogProps) {
  const effectiveGlobalConfig = globalConfig ?? EMPTY_CONFIG;
  const effectiveAccountConfig = accountConfig ?? EMPTY_CONFIG;
  const [globalVals, setGlobalVals] = useState<Record<string, unknown>>({});
  const [accountVals, setAccountVals] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
    enabled: open,
  });
  const hasLLMSelect = schemaHasLLMSelect(schema);
  const llmProvidersQ = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLLMProviders,
    enabled: open && hasLLMSelect,
  });
  const commandPrefix = settingsQ.data?.command_prefix || ",";

  const handleSave = useCallback(async () => {
    if (!onSave) return;
    const properties = ((schema as ConfigSchema | null)?.properties ?? {}) as Record<string, ConfigField>;
    const editableGlobalVals = withoutReadOnlyValues(globalVals, properties, effectiveGlobalConfig);
    const editableAccountVals = withoutReadOnlyValues(accountVals, properties, effectiveAccountConfig);
    setSaving(true);
    try {
      await onSave(editableGlobalVals, editableAccountVals);
      toast.success("配置已保存");
      onOpenChange(false);
    } catch (err) {
      toast.error(getErrMsg(err));
    } finally {
      setSaving(false);
    }
  }, [onSave, schema, globalVals, accountVals, effectiveGlobalConfig, effectiveAccountConfig, onOpenChange]);

  // 初始化配置值
  useEffect(() => {
    if (open && schema && typeof schema === "object" && "properties" in schema) {
      const s = schema as ConfigSchema;
      const { globalVals: gv, accountVals: av } = buildScopedConfigValues(s, effectiveGlobalConfig, effectiveAccountConfig);
      setGlobalVals(gv);
      setAccountVals(av);
    }
  }, [open, schema, effectiveGlobalConfig, effectiveAccountConfig]);

  const s = schema as ConfigSchema | null;
  if (!s?.properties || Object.keys(s.properties).length === 0) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{pluginName} — 配置</DialogTitle>
            <DialogDescription>该插件没有可配置的选项。</DialogDescription>
          </DialogHeader>
          <DialogFooter><Button onClick={() => onOpenChange(false)}>关闭</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  const globalFields = Object.entries(s.properties).filter(([, f]) => f.level === "global");
  const accountFields = Object.entries(s.properties).filter(([, f]) => f.level !== "global");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{pluginName} — 配置</DialogTitle>
          <DialogDescription>
            插件: <code className="text-xs">{pluginKey}</code>
            {accountName && <> · 账号: {accountName}</>}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          {globalFields.length > 0 && (
            <ConfigScopeSection
              title="全局配置"
              description="所有账号共享"
              fields={globalFields}
              values={globalVals}
              accountId={accountId}
              commandPrefix={commandPrefix}
              llmProviders={llmProvidersQ.data}
              llmProvidersLoading={llmProvidersQ.isLoading}
              onChange={(key, value) => setGlobalVals((p) => ({ ...p, [key]: value }))}
            />
          )}
          {accountFields.length > 0 && (
            <ConfigScopeSection
              title="账号配置"
              description={accountName ? accountName + " 专属" : "按账号隔离"}
              fields={accountFields}
              values={accountVals}
              accountId={accountId}
              commandPrefix={commandPrefix}
              llmProviders={llmProvidersQ.data}
              llmProvidersLoading={llmProvidersQ.isLoading}
              onChange={(key, value) => setAccountVals((p) => ({ ...p, [key]: value }))}
            />
          )}
        </div>
        <DialogFooter className="!flex !flex-row gap-2 sm:space-x-0 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
          <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
          <Button onClick={handleSave} disabled={saving}>
            {saving ? <Spinner className="mr-2 h-4 w-4" /> : <Save className="mr-2 h-4 w-4" />}
            {saving ? "保存中…" : "保存"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export type FieldEntry = [string, ConfigField];

interface ConfigScopeSectionProps {
  title: string;
  description: string;
  fields: FieldEntry[];
  values: Record<string, unknown>;
  accountId?: number;
  commandPrefix: string;
  llmProviders?: LLMProviderOut[];
  llmProvidersLoading?: boolean;
  showPreviews?: boolean;
  configActions?: ConfigAction[];
  onConfigAction?: (action: ConfigAction, input: Record<string, unknown>) => Promise<void>;
  onChange: (key: string, value: unknown) => void;
}

export function ConfigScopeSection({
  title,
  description,
  fields,
  values,
  accountId,
  commandPrefix,
  llmProviders,
  llmProvidersLoading = false,
  showPreviews = true,
  configActions = [],
  onConfigAction,
  onChange,
}: ConfigScopeSectionProps) {
  const [openTemplates, setOpenTemplates] = useState<Record<string, boolean>>({});
  const groups = groupConfigFields(fields);

  return (
    <section className="space-y-4 rounded-md border bg-muted/30 p-3">
      <div>
        <div className="text-sm font-semibold">{title}</div>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>

      {groups.sections.length > 0 && (
        <div className="space-y-3">
          {groups.sections.map((section) => (
            <div key={section.key} className="space-y-3 rounded-md border bg-background p-3">
              <div className="text-sm font-semibold">{section.title}</div>
              <div className={configGridClass(section.columns)}>
                {section.fields.map(([key, field]) => (
                  <ConfigFieldWithActions
                    key={key}
                    fk={key}
                    field={field}
                    value={values[key]}
                    values={values}
                    accountId={accountId}
                    llmProviders={llmProviders}
                    llmProvidersLoading={llmProvidersLoading}
                    fieldActions={actionsForField(configActions, key)}
                    onConfigAction={onConfigAction}
                    onChange={(value) => onChange(key, value)}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {groups.basic.length > 0 && (
        <div className="space-y-4">
          {groups.basic.map(([key, field]) => (
            <ConfigFieldWithActions
              key={key}
              fk={key}
              field={field}
              value={values[key]}
              values={values}
              accountId={accountId}
              llmProviders={llmProviders}
              llmProvidersLoading={llmProvidersLoading}
              fieldActions={actionsForField(configActions, key)}
              onConfigAction={onConfigAction}
              onChange={(value) => onChange(key, value)}
            />
          ))}
        </div>
      )}

      {(groups.templates.length > 0 || groups.placeholders.length > 0) && (
        <div className="space-y-3 rounded-md border bg-background p-3">
          <div>
            <div className="text-sm font-semibold">消息模板</div>
            <p className="text-xs text-muted-foreground">点击展开后编辑对应消息。</p>
          </div>
          {groups.placeholders.map(([key, field]) => (
            <ConfigFieldWithActions
              key={key}
              fk={key}
              field={field}
              value={values[key]}
              values={values}
              llmProviders={llmProviders}
              llmProvidersLoading={llmProvidersLoading}
              fieldActions={actionsForField(configActions, key)}
              onConfigAction={onConfigAction}
              onChange={(value) => onChange(key, value)}
            />
          ))}
          <div className="space-y-2">
            {groups.templates.map(([key, field]) => {
              const label = field.title || key;
              const open = Boolean(openTemplates[key]);
              return (
                <div key={key} className="rounded-md border bg-muted/20">
                  <button
                    type="button"
                    className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm font-medium"
                    onClick={() => setOpenTemplates((p) => ({ ...p, [key]: !p[key] }))}
                  >
                    <span>{label}</span>
                    {open ? (
                      <ChevronDown className="h-4 w-4 text-muted-foreground" />
                    ) : (
                      <ChevronRight className="h-4 w-4 text-muted-foreground" />
                    )}
                  </button>
                  {open && (
                    <div className="border-t px-3 py-3">
                      <ConfigFieldWithActions
                        fk={key}
                        field={field}
                        value={values[key]}
                        values={values}
                        accountId={accountId}
                        llmProviders={llmProviders}
                        llmProvidersLoading={llmProvidersLoading}
                        fieldActions={actionsForField(configActions, key)}
                        onConfigAction={onConfigAction}
                        onChange={(value) => onChange(key, value)}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {showPreviews && groups.previews.length > 0 && (
        <div className="space-y-3 rounded-md border bg-background p-3">
          <div>
            <div className="text-sm font-semibold">预览结果</div>
            <p className="text-xs text-muted-foreground">按模板顺序展示一组 Telegram 消息气泡。</p>
          </div>
          <TelegramHtmlPreviewThread
            messages={groups.previews.map(([key, field]) => ({
              title: field.title || key,
              value: renderPreviewValue(key, field, fields, values, commandPrefix),
              mode: "html",
            }))}
          />
        </div>
      )}
    </section>
  );
}

function ConfigFieldWithActions({
  fk,
  field,
  value,
  values,
  accountId,
  llmProviders,
  llmProvidersLoading,
  fieldActions,
  onConfigAction,
  onChange,
}: {
  fk: string;
  field: ConfigField;
  value: unknown;
  values: Record<string, unknown>;
  accountId?: number;
  llmProviders?: LLMProviderOut[];
  llmProvidersLoading?: boolean;
  fieldActions: ConfigAction[];
  onConfigAction?: (action: ConfigAction, input: Record<string, unknown>) => Promise<void>;
  onChange: (value: unknown) => void;
}) {
  if (field["x-ui-hidden"]) return null;
  return (
    <div className="space-y-2">
      <FieldInput
        fk={fk}
        field={field}
        value={value}
        values={values}
        accountId={accountId}
        llmProviders={llmProviders}
        llmProvidersLoading={llmProvidersLoading}
        configActions={fieldActions}
        onConfigAction={onConfigAction}
        onChange={onChange}
      />
      {field["x-ui-widget"] !== "config-list" ? (
        <ConfigActionButtons actions={fieldActions} onRun={onConfigAction} />
      ) : null}
    </div>
  );
}

export function ConfigPreviewSection({
  fields,
  values,
  commandPrefix,
}: {
  fields: FieldEntry[];
  values: Record<string, unknown>;
  commandPrefix: string;
}) {
  const groups = groupConfigFields(fields);
  if (groups.previews.length === 0) return null;
  return (
    <div className="space-y-3">
      <TelegramHtmlPreviewThread
        messages={groups.previews.map(([key, field]) => ({
          title: field.title || key,
          value: renderPreviewValue(key, field, fields, values, commandPrefix),
          mode: "html",
        }))}
      />
    </div>
  );
}

export function groupConfigFields(fields: FieldEntry[]): {
  basic: FieldEntry[];
  placeholders: FieldEntry[];
  templates: FieldEntry[];
  previews: FieldEntry[];
  sections: Array<{ key: string; title: string; fields: FieldEntry[]; columns: number }>;
} {
  const basic: FieldEntry[] = [];
  const placeholders: FieldEntry[] = [];
  const templates: FieldEntry[] = [];
  const previews: FieldEntry[] = [];
  const sectionsByKey = new Map<string, { key: string; title: string; fields: FieldEntry[]; columns: number }>();

  const sortedFields = [...fields].sort((a, b) => fieldOrder(a[1]) - fieldOrder(b[1]));

  for (const entry of sortedFields) {
    const [key, field] = entry;
    if (isPreviewField(key)) {
      previews.push(entry);
    } else if (isPlaceholderField(key)) {
      placeholders.push(entry);
    } else if (isTemplateField(key)) {
      templates.push(entry);
    } else {
      basic.push(entry);
      const sectionName = typeof field["x-ui-section"] === "string" ? field["x-ui-section"].trim() : "";
      if (sectionName) {
        const sectionKey = sectionName.toLowerCase();
        const current = sectionsByKey.get(sectionKey) ?? {
          key: sectionKey,
          title: sectionName,
          fields: [],
          columns: clampColumns(field["x-ui-columns"]),
        };
        current.fields.push(entry);
        current.columns = Math.max(current.columns, clampColumns(field["x-ui-columns"]));
        sectionsByKey.set(sectionKey, current);
      }
    }
  }

  const sectionFieldKeys = new Set(
    Array.from(sectionsByKey.values()).flatMap((section) => section.fields.map(([key]) => key)),
  );
  const unsectionedBasic = basic.filter(([key]) => !sectionFieldKeys.has(key));

  return {
    basic: unsectionedBasic,
    placeholders,
    templates,
    previews,
    sections: Array.from(sectionsByKey.values()),
  };
}

function fieldOrder(field: ConfigField): number {
  return Number.isFinite(field["x-ui-order"]) ? Number(field["x-ui-order"]) : 0;
}

function clampColumns(value: unknown): number {
  const raw = Number(value);
  if (!Number.isFinite(raw)) return 2;
  return Math.min(3, Math.max(1, Math.floor(raw)));
}

function configGridClass(columns: number): string {
  if (columns >= 3) return "grid gap-4 md:grid-cols-2 xl:grid-cols-3";
  if (columns === 2) return "grid gap-4 md:grid-cols-2";
  return "space-y-4";
}

interface FieldInputProps {
  fk: string;
  field: ConfigField;
  value: unknown;
  values?: Record<string, unknown>;
  accountId?: number;
  llmProviders?: LLMProviderOut[];
  llmProvidersLoading?: boolean;
  configActions?: ConfigAction[];
  onConfigAction?: (action: ConfigAction, input: Record<string, unknown>) => Promise<void>;
  previewValue?: string;
  onChange: (v: unknown) => void;
}

function FieldInput({
  fk,
  field,
  value,
  values = EMPTY_CONFIG,
  accountId,
  llmProviders,
  llmProvidersLoading = false,
  configActions = [],
  onConfigAction,
  previewValue,
  onChange,
}: FieldInputProps) {
  const label = field.title || fk;
  const description = field.description;
  const inputId = `plugin-config-${fk}`;
  const textValue = formatConfigValue(value);
  const defaultValue = field.default != null ? formatConfigValue(field.default) : "";
  const isPreview = isPreviewField(fk);
  const isPlaceholders = isPlaceholderField(fk);
  const isTemplate = isTemplateField(fk);
  const isReadOnly = isReadOnlyField(fk, field);
  const isSensitive = isSensitiveConfigKey(fk);

  if (field["x-ui-hidden"]) {
    return null;
  }

  if (isPreview) {
    return (
      <ReadOnlyField label={label} description={description}>
        <TelegramHtmlPreview value={previewValue ?? (textValue || defaultValue)} mode="html" title={label} />
      </ReadOnlyField>
    );
  }

  if (isPlaceholders) {
    return (
      <ReadOnlyField label={label} description={description}>
        <pre className="whitespace-pre-wrap break-words font-sans text-sm text-muted-foreground">
          {textValue || defaultValue || "暂无占位符说明"}
        </pre>
      </ReadOnlyField>
    );
  }

  if (isReadOnly) {
    return (
      <ReadOnlyField label={label} description={description}>
        <pre className="whitespace-pre-wrap break-words font-sans text-sm text-muted-foreground">
          {textValue || defaultValue || "未设置"}
        </pre>
      </ReadOnlyField>
    );
  }

  if (field["x-ui-widget"] === "llm-provider-select") {
    return (
      <LLMProviderSelectField
        inputId={inputId}
        label={label}
        description={description}
        value={value}
        providers={llmProviders}
        loading={llmProvidersLoading}
        onChange={onChange}
      />
    );
  }

  if (field["x-ui-widget"] === "llm-model-select") {
    const providerField = field["x-ui-provider-field"];
    const providerValue = providerField ? values[providerField] : undefined;
    return (
      <LLMModelSelectField
        inputId={inputId}
        label={label}
        description={description}
        value={value}
        providerValue={providerValue}
        providers={llmProviders}
        loading={llmProvidersLoading}
        modelModality={field["x-ui-model-modality"]}
        onChange={onChange}
      />
    );
  }

  if (field.type === "array" && field["x-ui-widget"] === "config-list") {
    return (
      <ConfigListField
        fk={fk}
        field={field}
        value={value}
        actions={configActions}
        onConfigAction={onConfigAction}
        llmProviders={llmProviders}
        llmProvidersLoading={llmProvidersLoading}
        onChange={onChange}
      />
    );
  }

  if (field.type === "array" && field["x-ui-widget"] === "allowed-peer-multi-select") {
    return (
      <AllowedPeerMultiSelectField
        accountId={accountId}
        label={label}
        description={description}
        value={value}
        onChange={onChange}
      />
    );
  }

  if (field.type === "array" && (field["x-ui-widget"] === "multi-select" || field.items?.enum || field.enum)) {
    return (
      <MultiSelectField
        inputId={inputId}
        label={label}
        description={description}
        field={field}
        value={value}
        onChange={onChange}
      />
    );
  }

  if (field.enum && field.enum.length > 0) {
    if (field["x-ui-widget"] === "list-select") {
      return (
        <ListSelectField
          inputId={inputId}
          label={label}
          description={description}
          field={field}
          value={value}
          onChange={onChange}
        />
      );
    }
    const enumLabels = field.enumNames ?? [];
    return (
      <div className="space-y-1.5">
        <Label htmlFor={inputId}>{label}</Label>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
        <Select
          id={inputId}
          value={value != null ? String(value) : ""}
          onChange={(e) => onChange(e.target.value)}
        >
          {!field.enum.some((v) => String(v) === "") && <option value="">未设置</option>}
          {field.enum.map((opt, index) => (
            <option key={String(opt)} value={String(opt)}>
              {enumLabels[index] || String(opt)}
            </option>
          ))}
        </Select>
        {field.enumDescriptions && field.enumDescriptions.length > 0 && (
          <ul className="space-y-1 text-xs text-muted-foreground">
            {field.enum.map((opt, index) => (
              field.enumDescriptions?.[index] ? (
                <li key={String(opt)}>
                  <span className="font-medium">{enumLabels[index] || String(opt)}</span>
                  ：{field.enumDescriptions[index]}
                </li>
              ) : null
            ))}
          </ul>
        )}
      </div>
    );
  }

  if (field.type === "boolean") {
    return (
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1.5">
          <Label htmlFor={inputId}>{label}</Label>
          {description && <p className="text-xs text-muted-foreground">{description}</p>}
        </div>
        <Switch
          id={inputId}
          checked={Boolean(value)}
          onCheckedChange={onChange}
        />
      </div>
    );
  }

  if (field.type === "integer" || field.type === "number") {
    return (
      <div className="space-y-1.5">
        <Label htmlFor={inputId}>{label}</Label>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
        <Input
          id={inputId}
          type="number"
          value={value != null ? String(value) : ""}
          min={field.minimum as number}
          max={field.maximum as number}
          onChange={(e) => {
            const v = e.target.value;
            onChange(v === "" ? null : Number(v));
          }}
        />
        {field.minimum !== undefined && field.maximum !== undefined && (
          <p className="text-xs text-muted-foreground">范围: {field.minimum} — {field.maximum}</p>
        )}
      </div>
    );
  }

  if (field.type === "object") {
    const properties = field.properties ?? {};
    const objectValue = normalizeConfigObject(value);
    if (Object.keys(properties).length > 0) {
      return (
        <div className="space-y-2">
          <div className="space-y-1.5">
            <Label>{label}</Label>
            {description && <p className="text-xs text-muted-foreground">{description}</p>}
          </div>
          <div className="rounded-md border bg-background p-3">
            <ConfigObjectEditor
              prefix={fk}
              properties={properties}
              values={objectValue}
              accountId={accountId}
              llmProviders={llmProviders}
              llmProvidersLoading={llmProvidersLoading}
              onChange={(key, nextValue) => onChange({ ...objectValue, [key]: nextValue })}
            />
          </div>
        </div>
      );
    }
    return (
      <JsonObjectField
        inputId={inputId}
        label={label}
        description={description}
        value={value}
        onChange={onChange}
      />
    );
  }

  if (field.type === "array") {
    if (field.items?.type === "object" || arrayHasObject(value)) {
      return (
        <JsonArrayField
          inputId={inputId}
          label={label}
          description={description}
          value={value}
          onChange={onChange}
        />
      );
    }
    return (
      <div className="space-y-1.5">
        <Label htmlFor={inputId}>{label}</Label>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
        <Input
          id={inputId}
          type="text"
          value={textValue}
          onChange={(e) => {
            const parts = e.target.value
              .split(",")
              .map((part) => part.trim())
              .filter(Boolean);
            if (field.items?.type === "integer" || field.items?.type === "number") {
              onChange(parts.map((part) => Number(part)).filter((part) => Number.isFinite(part)));
            } else {
              onChange(parts);
            }
          }}
          placeholder="用逗号分隔多个值"
        />
      </div>
    );
  }

  // 默认：string 类型
  const multiline =
    field.format === "textarea" ||
    field["x-ui-widget"] === "textarea" ||
    isTemplate ||
    textValue.includes("\n") ||
    defaultValue.includes("\n") ||
    /message|text|prompt|content/i.test(fk);

  if (multiline) {
    return (
      <div className="space-y-1.5">
        <Label htmlFor={inputId}>{label}</Label>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
        <Textarea
          id={inputId}
          value={textValue}
          rows={4}
          onChange={(e) => onChange(e.target.value)}
          placeholder={defaultValue}
        />
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <Label htmlFor={inputId}>{label}</Label>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
      <Input
        id={inputId}
        type={isSensitive ? "password" : "text"}
        value={textValue}
        onChange={(e) => onChange(e.target.value)}
        placeholder={isSensitive && !textValue ? MASKED_SECRET_PLACEHOLDER : defaultValue}
      />
    </div>
  );
}

function actionsForField(actions: ConfigAction[], fieldKey: string): ConfigAction[] {
  return actions.filter((action) => {
    const placement = String(action.placement || "").trim();
    return placement === `field:${fieldKey}` || placement === `field.${fieldKey}`;
  });
}

function ConfigListField({
  fk,
  field,
  value,
  actions,
  onConfigAction,
  llmProviders,
  llmProvidersLoading = false,
  onChange,
}: {
  fk: string;
  field: ConfigField;
  value: unknown;
  actions: ConfigAction[];
  onConfigAction?: (action: ConfigAction, input: Record<string, unknown>) => Promise<void>;
  llmProviders?: LLMProviderOut[];
  llmProvidersLoading?: boolean;
  onChange: (v: unknown) => void;
}) {
  const label = field.title || fk;
  const items = normalizeObjectArray(value);
  const properties = field.items?.properties ?? {};
  const enabledField = String(field["x-ui-enabled-field"] || (properties.enabled ? "enabled" : "")).trim();
  const reorderable = field["x-ui-reorderable"] !== false;
  const minItems = Number.isFinite(field.minItems) ? Number(field.minItems) : 0;
  const maxItems = Number.isFinite(field.maxItems) ? Number(field.maxItems) : Infinity;
  const [editingIndex, setEditingIndex] = useState<number | "new" | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [dragIndex, setDragIndex] = useState<number | null>(null);

  const updateItems = (next: Record<string, unknown>[]) => onChange(next);
  const openEditor = (index: number | "new") => {
    setEditingIndex(index);
    if (index === "new") {
      setDraft(buildDefaultObject(properties));
    } else {
      setDraft({ ...items[index] });
    }
  };
  const closeEditor = () => {
    setEditingIndex(null);
    setDraft({});
  };
  const saveDraft = () => {
    if (editingIndex === "new") {
      updateItems([...items, draft]);
    } else if (editingIndex != null) {
      updateItems(items.map((item, index) => (index === editingIndex ? draft : item)));
    }
    closeEditor();
  };
  const moveItem = (from: number, to: number) => {
    if (from === to || to < 0 || to >= items.length) return;
    updateItems(moveArrayItem(items, from, to));
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Label>{label}</Label>
          {field.description && <p className="mt-1 text-xs text-muted-foreground">{field.description}</p>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ConfigActionButtons actions={actions} onRun={onConfigAction} />
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={items.length >= maxItems}
            onClick={() => openEditor("new")}
          >
            <Plus className="mr-1 h-4 w-4" />
            {field["x-ui-add-label"] || "添加一组"}
          </Button>
        </div>
      </div>

      {items.length === 0 ? (
        <div className="rounded-md border border-dashed bg-muted/20 px-3 py-5 text-sm text-muted-foreground">
          暂无配置组。
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((item, index) => {
            const enabled = enabledField ? item[enabledField] !== false : true;
            const title = configListItemTitle(field, item, index);
            const description = configListItemDescription(field, item);
            const summary = configListItemSummary(field, item);
            const canDelete = items.length > minItems;
            return (
              <div
                key={configListItemKey(item, index)}
                draggable={reorderable && items.length > 1}
                onDragStart={(event) => {
                  setDragIndex(index);
                  event.dataTransfer.effectAllowed = "move";
                }}
                onDragOver={(event) => {
                  if (reorderable) event.preventDefault();
                }}
                onDrop={(event) => {
                  event.preventDefault();
                  if (dragIndex != null) moveItem(dragIndex, index);
                  setDragIndex(null);
                }}
                onDragEnd={() => setDragIndex(null)}
                className="flex flex-col gap-3 rounded-md border bg-background p-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex min-w-0 flex-1 items-start gap-3">
                  <div
                    className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border bg-muted/30 text-muted-foreground"
                    aria-hidden="true"
                  >
                    {reorderable ? <GripVertical className="h-4 w-4" /> : index + 1}
                  </div>
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <div className="min-w-0 truncate text-sm font-medium">{title}</div>
                      {enabledField ? (
                        <Badge variant={enabled ? "success" : "secondary"}>{enabled ? "启用" : "停用"}</Badge>
                      ) : null}
                    </div>
                    {description ? (
                      <div className="truncate text-xs text-muted-foreground">{description}</div>
                    ) : null}
                    {summary ? (
                      <div className="break-words text-xs text-muted-foreground">{summary}</div>
                    ) : null}
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-1.5 sm:justify-end">
                  {enabledField ? (
                    <Switch
                      checked={enabled}
                      onCheckedChange={(checked) => {
                        updateItems(items.map((row, rowIndex) => (
                          rowIndex === index ? { ...row, [enabledField]: checked } : row
                        )));
                      }}
                    />
                  ) : null}
                  <Button type="button" variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEditor(index)} aria-label="编辑">
                    <Pencil className="h-4 w-4" />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8"
                    disabled={items.length >= maxItems}
                    onClick={() => updateItems([...items.slice(0, index + 1), cloneConfigObject(item), ...items.slice(index + 1)])}
                    aria-label="复制"
                  >
                    <Copy className="h-4 w-4" />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8"
                    disabled={!reorderable || index === 0}
                    onClick={() => moveItem(index, index - 1)}
                    aria-label="上移"
                  >
                    <ArrowUp className="h-4 w-4" />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8"
                    disabled={!reorderable || index === items.length - 1}
                    onClick={() => moveItem(index, index + 1)}
                    aria-label="下移"
                  >
                    <ArrowDown className="h-4 w-4" />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-destructive hover:text-destructive"
                    disabled={!canDelete}
                    onClick={() => updateItems(items.filter((_, rowIndex) => rowIndex !== index))}
                    aria-label="删除"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <Dialog open={editingIndex !== null} onOpenChange={(open) => { if (!open) closeEditor(); }}>
        <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editingIndex === "new" ? "添加配置组" : "编辑配置组"}</DialogTitle>
            <DialogDescription>配置组会按当前页面顺序保存，拖动或使用上下箭头可调整优先级。</DialogDescription>
          </DialogHeader>
          <ConfigObjectEditor
            prefix={`${fk}.${editingIndex ?? "closed"}`}
            properties={properties}
            values={draft}
            llmProviders={llmProviders}
            llmProvidersLoading={llmProvidersLoading}
            onChange={(key, nextValue) => setDraft((prev) => ({ ...prev, [key]: nextValue }))}
          />
          <DialogFooter className="!flex !flex-row gap-2 sm:space-x-0 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
            <Button type="button" variant="outline" onClick={closeEditor}>取消</Button>
            <Button type="button" onClick={saveDraft}>保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ConfigObjectEditor({
  prefix,
  properties,
  values,
  accountId,
  llmProviders,
  llmProvidersLoading = false,
  onChange,
}: {
  prefix: string;
  properties: Record<string, ConfigField>;
  values: Record<string, unknown>;
  accountId?: number;
  llmProviders?: LLMProviderOut[];
  llmProvidersLoading?: boolean;
  onChange: (key: string, value: unknown) => void;
}) {
  const entries = Object.entries(properties)
    .filter(([, field]) => !field["x-ui-hidden"])
    .sort((a, b) => fieldOrder(a[1]) - fieldOrder(b[1]));

  if (entries.length === 0) {
    return <div className="rounded-md border border-dashed bg-muted/20 px-3 py-4 text-sm text-muted-foreground">该配置组没有可编辑字段。</div>;
  }

  return (
    <div className="space-y-4">
      {entries.map(([key, field]) => (
        <FieldInput
          key={key}
          fk={`${prefix}.${key}`}
          field={{ ...field, key }}
          value={values[key]}
          values={values}
          accountId={accountId}
          llmProviders={llmProviders}
          llmProvidersLoading={llmProvidersLoading}
          onChange={(value) => onChange(key, value)}
        />
      ))}
    </div>
  );
}

const PEER_KIND_LABEL: Record<string, string> = {
  private: "私聊",
  group: "普通群",
  supergroup: "超级群",
  channel: "频道",
};

function peerKindLabel(kind: string): string {
  return PEER_KIND_LABEL[kind] || kind || "会话";
}

function allowedPeerDisplayName(peer: IgnoredPeer): string {
  return peer.peer_label?.trim() || `${peerKindLabel(String(peer.peer_kind))} ${peer.peer_id}`;
}

function normalizeAllowedPeerIds(value: unknown): number[] {
  const rawItems = Array.isArray(value)
    ? value
    : String(value ?? "")
      .split(/[\n,，\s]+/)
      .filter(Boolean);
  const seen = new Set<number>();
  const ids: number[] = [];
  for (const item of rawItems) {
    const parsed = Number(item);
    if (!Number.isFinite(parsed)) continue;
    const id = Math.trunc(parsed);
    if (seen.has(id)) continue;
    seen.add(id);
    ids.push(id);
  }
  return ids;
}

function AllowedPeerMultiSelectField({
  accountId,
  label,
  description,
  value,
  onChange,
}: {
  accountId?: number;
  label: string;
  description?: string;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const selectedIds = normalizeAllowedPeerIds(value);
  const selected = new Set(selectedIds.map(String));
  const peersQ = useQuery({
    queryKey: ["ignored-peers", accountId],
    queryFn: () => listIgnoredPeers(Number(accountId)),
    enabled: Number.isFinite(Number(accountId)) && Number(accountId) > 0,
  });
  const peers = peersQ.data ?? [];
  const knownPeerIds = new Set(peers.map((peer) => String(peer.peer_id)));
  const unknownSelected = selectedIds.filter((id) => !knownPeerIds.has(String(id)));

  const updateIds = (ids: number[]) => onChange(ids);
  const removeSelectedId = (id: number) => {
    updateIds(selectedIds.filter((item) => item !== id));
  };
  const togglePeer = (peer: IgnoredPeer) => {
    const id = Number(peer.peer_id);
    if (!Number.isFinite(id)) return;
    if (selected.has(String(id))) {
      updateIds(selectedIds.filter((item) => item !== id));
    } else {
      updateIds([...selectedIds, id]);
    }
  };

  if (!accountId) {
    return (
      <div className="space-y-1.5">
        <Label>{label}</Label>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
        <div className="rounded-md border border-dashed bg-background px-3 py-2 text-xs text-muted-foreground">
          该字段需要在账号配置页中选择允许会话。
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
      {peersQ.isLoading || peersQ.isFetching ? (
        <div className="flex h-10 items-center rounded-md border bg-background px-2 text-xs text-muted-foreground">
          <Spinner className="mr-2 h-3.5 w-3.5 text-primary" />
          正在读取已允许会话
        </div>
      ) : peers.length === 0 ? (
        <div className="rounded-md border border-dashed bg-background px-3 py-2 text-xs leading-5 text-muted-foreground">
          暂无已允许会话。没有找到想选择的会话时，请先去{" "}
          <Link to={`/accounts/${accountId}?tab=ignored`} className="font-medium text-primary hover:underline">
            账号详情页的允许会话
          </Link>{" "}
          添加。
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>从已允许会话选择</span>
            <span>{selected.size} 个已选</span>
          </div>
          <div className="flex max-h-28 flex-wrap gap-1.5 overflow-y-auto rounded-md border bg-background p-1.5">
            {peers.map((peer) => {
              const id = String(peer.peer_id);
              const active = selected.has(id);
              return (
                <button
                  key={peer.id}
                  type="button"
                  className={cn(
                    "min-w-0 max-w-full rounded-md border px-2 py-1.5 text-left text-xs transition-colors",
                    active
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border bg-muted/30 text-muted-foreground [@media(hover:hover)]:hover:border-primary/40 [@media(hover:hover)]:hover:text-foreground",
                  )}
                  title={`${allowedPeerDisplayName(peer)} · ${id}`}
                  onClick={() => togglePeer(peer)}
                >
                  <span className="block max-w-[210px] truncate font-medium">
                    {allowedPeerDisplayName(peer)}
                  </span>
                  <span className="mt-0.5 block font-mono text-[11px] opacity-75">
                    {peerKindLabel(String(peer.peer_kind))} · {id}
                  </span>
                </button>
              );
            })}
          </div>
        </>
      )}
      {unknownSelected.length > 0 ? (
        <div className="flex flex-wrap gap-1.5 rounded-md border border-warning/30 bg-warning/10 px-2 py-1.5 text-xs text-warning">
          {unknownSelected.map((id) => (
            <button
              key={id}
              type="button"
              className="rounded border border-warning/40 px-1.5 py-0.5 font-mono hover:bg-warning/15"
              title="从本插件配置移除这个未在允许会话中的 Chat ID"
              onClick={() => removeSelectedId(id)}
            >
              {id} x
            </button>
          ))}
        </div>
      ) : null}
      <div className="text-xs leading-5 text-muted-foreground">
        没有找到想选择的会话？去{" "}
        <Link to={`/accounts/${accountId}?tab=ignored`} className="font-medium text-primary hover:underline">
          账号详情页的允许会话
        </Link>{" "}
        添加后再回来选择。留空表示不限制插件监听群聊。
      </div>
    </div>
  );
}

function MultiSelectField({
  inputId,
  label,
  description,
  field,
  value,
  onChange,
}: {
  inputId: string;
  label: string;
  description?: string;
  field: ConfigField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const itemField = field.items ?? field;
  const options = itemField.enum ?? field.enum ?? [];
  const labels = itemField.enumNames ?? field.enumNames ?? [];
  const descriptions = itemField.enumDescriptions ?? field.enumDescriptions ?? [];
  const selected = new Set(Array.isArray(value) ? value.map((item) => String(item)) : []);

  return (
    <div className="space-y-1.5">
      <Label htmlFor={inputId}>{label}</Label>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
      <div id={inputId} className="grid gap-2 sm:grid-cols-2">
        {options.map((option, index) => {
          const key = String(option);
          const checked = selected.has(key);
          return (
            <label
              key={key}
              className="flex min-h-10 cursor-pointer items-start gap-2 rounded-md border bg-background px-3 py-2 text-sm"
            >
              <input
                type="checkbox"
                className="mt-1 h-4 w-4"
                checked={checked}
                onChange={(event) => {
                  const next = new Set(selected);
                  if (event.target.checked) next.add(key);
                  else next.delete(key);
                  onChange(options.filter((candidate) => next.has(String(candidate))));
                }}
              />
              <span className="min-w-0">
                <span className="block break-words font-medium">{labels[index] || key}</span>
                {descriptions[index] ? (
                  <span className="mt-0.5 block break-words text-xs text-muted-foreground">{descriptions[index]}</span>
                ) : null}
              </span>
            </label>
          );
        })}
      </div>
    </div>
  );
}

function ListSelectField({
  inputId,
  label,
  description,
  field,
  value,
  onChange,
}: {
  inputId: string;
  label: string;
  description?: string;
  field: ConfigField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const selected = value != null ? String(value) : "";
  const labels = field.enumNames ?? [];
  const descriptions = field.enumDescriptions ?? [];

  return (
    <div className="space-y-1.5">
      <Label htmlFor={inputId}>{label}</Label>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
      <div id={inputId} className="grid gap-2 sm:grid-cols-2">
        {(field.enum ?? []).map((option, index) => {
          const key = String(option);
          const active = selected === key;
          return (
            <button
              key={key}
              type="button"
              className={`min-h-10 rounded-md border px-3 py-2 text-left text-sm transition-colors ${
                active ? "border-primary bg-primary/10 text-foreground" : "bg-background hover:bg-muted/40"
              }`}
              onClick={() => onChange(option)}
            >
              <span className="block break-words font-medium">{labels[index] || key}</span>
              {descriptions[index] ? (
                <span className="mt-0.5 block break-words text-xs text-muted-foreground">{descriptions[index]}</span>
              ) : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function JsonArrayField({
  inputId,
  label,
  description,
  value,
  onChange,
}: {
  inputId: string;
  label: string;
  description?: string;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(Array.isArray(value) ? value : [], null, 2));
  const [error, setError] = useState("");

  useEffect(() => {
    setText(JSON.stringify(Array.isArray(value) ? value : [], null, 2));
    setError("");
  }, [value]);

  return (
    <div className="space-y-1.5">
      <Label htmlFor={inputId}>{label}</Label>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
      <Textarea
        id={inputId}
        value={text}
        rows={6}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          try {
            const parsed = JSON.parse(next);
            if (!Array.isArray(parsed)) {
              setError("请输入 JSON 数组。");
              return;
            }
            setError("");
            onChange(parsed);
          } catch {
            setError("JSON 尚未解析成功，保存前请修正。");
          }
        }}
      />
      {error ? <p className="text-xs text-destructive">{error}</p> : null}
    </div>
  );
}

function JsonObjectField({
  inputId,
  label,
  description,
  value,
  onChange,
}: {
  inputId: string;
  label: string;
  description?: string;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const serializedValue = stringifyConfigObject(value);
  const [text, setText] = useState(serializedValue);
  const [error, setError] = useState("");

  useEffect(() => {
    setText((prev) => (prev === serializedValue ? prev : serializedValue));
    setError("");
  }, [serializedValue]);

  return (
    <div className="space-y-1.5">
      <Label htmlFor={inputId}>{label}</Label>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
      <Textarea
        id={inputId}
        value={text}
        rows={6}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          try {
            const parsed = JSON.parse(next);
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
              setError("请输入 JSON 对象。");
              return;
            }
            setError("");
            onChange(parsed);
          } catch {
            setError("JSON 尚未解析成功，保存前请修正。");
          }
        }}
      />
      {error ? <p className="text-xs text-destructive">{error}</p> : null}
    </div>
  );
}

function ConfigActionButtons({
  actions,
  onRun,
}: {
  actions: ConfigAction[];
  onRun?: (action: ConfigAction, input: Record<string, unknown>) => Promise<void>;
}) {
  if (actions.length === 0 || !onRun) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {actions.map((action) => (
        <ConfigActionButton key={action.key} action={action} onRun={onRun} />
      ))}
    </div>
  );
}

function ConfigActionButton({
  action,
  onRun,
}: {
  action: ConfigAction;
  onRun: (action: ConfigAction, input: Record<string, unknown>) => Promise<void>;
}) {
  const inputSchema = configActionInputSchema(action);
  const inputFields = Object.entries(inputSchema?.properties ?? {}) as FieldEntry[];
  const [open, setOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [inputValues, setInputValues] = useState<Record<string, unknown>>(() => (
    inputSchema ? buildDefaultObject(inputSchema.properties) : {}
  ));
  const buttonLabel = action.title || "执行动作";
  const submitLabel = action.submit_label || buttonLabel;

  useEffect(() => {
    if (!open || !inputSchema) return;
    setInputValues(buildDefaultObject(inputSchema.properties));
  }, [open, inputSchema]);

  const run = async (input: Record<string, unknown>) => {
    setRunning(true);
    try {
      await onRun(action, input);
      setOpen(false);
    } catch (err) {
      toast.error(getErrMsg(err));
    } finally {
      setRunning(false);
    }
  };

  if (inputFields.length === 0) {
    return (
      <Button type="button" variant="outline" size="sm" disabled={running} onClick={() => run({})}>
        {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Wand className="mr-1 h-4 w-4" />}
        {buttonLabel}
      </Button>
    );
  }

  return (
    <>
      <Button type="button" variant="outline" size="sm" disabled={running} onClick={() => setOpen(true)}>
        {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Wand className="mr-1 h-4 w-4" />}
        {buttonLabel}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85vh] max-w-xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{buttonLabel}</DialogTitle>
            {action.description ? <DialogDescription>{action.description}</DialogDescription> : null}
          </DialogHeader>
          <div className="space-y-4">
            {inputFields.map(([key, field]) => (
              <FieldInput
                key={key}
                fk={`action.${action.key}.${key}`}
                field={{ ...field, key }}
                value={inputValues[key]}
                values={inputValues}
                onChange={(value) => setInputValues((prev) => ({ ...prev, [key]: value }))}
              />
            ))}
          </div>
          <DialogFooter className="!flex !flex-row gap-2 sm:space-x-0 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
            <Button type="button" variant="outline" disabled={running} onClick={() => setOpen(false)}>取消</Button>
            <Button type="button" disabled={running} onClick={() => run(inputValues)}>
              {running ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Wand className="mr-2 h-4 w-4" />}
              {running ? "处理中…" : submitLabel}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function configActionInputSchema(action: ConfigAction): ConfigSchema | null {
  const schema = action.input_schema as ConfigSchema | undefined;
  if (!schema || schema.type !== "object" || !schema.properties || typeof schema.properties !== "object") {
    return null;
  }
  return schema;
}

function normalizeConfigObject(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return { ...(value as Record<string, unknown>) };
  }
  if (typeof value === "string" && value.trim()) {
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return {};
    }
  }
  return {};
}

function stringifyConfigObject(value: unknown): string {
  return JSON.stringify(normalizeConfigObject(value), null, 2);
}

function normalizeObjectArray(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    .map((item) => ({ ...item }));
}

function arrayHasObject(value: unknown): boolean {
  return Array.isArray(value) && value.some((item) => Boolean(item && typeof item === "object"));
}

function cloneConfigObject(value: Record<string, unknown>): Record<string, unknown> {
  try {
    return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
  } catch {
    return { ...value };
  }
}

function moveArrayItem<T>(items: T[], from: number, to: number): T[] {
  const next = [...items];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

function buildDefaultObject(properties: Record<string, ConfigField> | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, field] of Object.entries(properties ?? {})) {
    out[key] = defaultValueForField(field);
  }
  return out;
}

function defaultValueForField(field: ConfigField): unknown {
  if (field.default !== undefined) return cloneDefaultValue(field.default);
  if (field.type === "boolean") return false;
  if (field.type === "array") return [];
  if (field.type === "object") return buildDefaultObject(field.properties);
  if (field.type === "integer" || field.type === "number") return null;
  return "";
}

function cloneDefaultValue(value: unknown): unknown {
  if (value == null || typeof value !== "object") return value;
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return value;
  }
}

function configListItemKey(item: Record<string, unknown>, index: number): string {
  return `${String(item.id || item.kb_id || item.key || "row")}:${index}`;
}

function configListItemTitle(field: ConfigField, item: Record<string, unknown>, index: number): string {
  const titleField = String(field["x-ui-title-field"] || "").trim();
  const candidates = [
    titleField ? item[titleField] : "",
    item.remark,
    item.name,
    item.title,
    item.label,
    item.key,
    item.id,
    item.kb_id,
  ];
  return candidates.map(formatInlineValue).find(Boolean) || `第 ${index + 1} 组配置`;
}

function configListItemDescription(field: ConfigField, item: Record<string, unknown>): string {
  const descriptionField = String(field["x-ui-description-field"] || "").trim();
  const candidates = [
    descriptionField ? item[descriptionField] : "",
    item.url,
    item.description,
    item.summary,
  ];
  return candidates.map(formatInlineValue).find(Boolean) || "";
}

function configListItemSummary(field: ConfigField, item: Record<string, unknown>): string {
  const template = String(field["x-ui-summary"] || "").trim();
  if (!template) return "";
  return template.replace(/\{([^}]+)\}/g, (_match, path: string) => formatInlineValue(valueAtPath(item, path)));
}

function valueAtPath(item: Record<string, unknown>, path: string): unknown {
  const parts = String(path || "").split(".").map((part) => part.trim()).filter(Boolean);
  let current: unknown = item;
  for (const part of parts) {
    if (part === "length") {
      return Array.isArray(current) || typeof current === "string" ? current.length : 0;
    }
    if (!current || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function formatInlineValue(value: unknown): string {
  if (value == null) return "";
  if (Array.isArray(value)) return value.length ? `${value.length}` : "";
  if (typeof value === "object") return "";
  return String(value).trim();
}

function LLMProviderSelectField({
  inputId,
  label,
  description,
  value,
  providers,
  loading,
  onChange,
}: {
  inputId: string;
  label: string;
  description?: string;
  value: unknown;
  providers?: LLMProviderOut[];
  loading: boolean;
  onChange: (v: unknown) => void;
}) {
  const selected = formatConfigValue(value);
  const hasSelectedProvider = Boolean(selected) && Boolean(findLLMProviderBySelector(providers, selected));

  return (
    <div className="space-y-1.5">
      <Label htmlFor={inputId}>{label}</Label>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
      <Select
        id={inputId}
        value={selected}
        disabled={loading}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">{loading ? "正在加载 TelePilot Provider..." : "自动路由"}</option>
        {providers?.map((provider) => (
          <option key={provider.id} value={String(provider.id)}>
            {formatLLMProviderOptionLabel(provider)}
          </option>
        ))}
        {selected && !hasSelectedProvider && (
          <option value={selected}>当前值：{selected}</option>
        )}
      </Select>
      {!loading && (!providers || providers.length === 0) && (
        <p className="text-xs text-muted-foreground">
          尚未配置 TelePilot AI Provider；请先到 AI 模型提供商中添加。
        </p>
      )}
    </div>
  );
}

function LLMModelSelectField({
  inputId,
  label,
  description,
  value,
  providerValue,
  providers,
  loading,
  modelModality,
  onChange,
}: {
  inputId: string;
  label: string;
  description?: string;
  value: unknown;
  providerValue: unknown;
  providers?: LLMProviderOut[];
  loading: boolean;
  modelModality?: string;
  onChange: (v: unknown) => void;
}) {
  const selected = formatConfigValue(value);
  const provider = findLLMProviderBySelector(providers, formatConfigValue(providerValue));
  const rows = provider ? buildLLMModelOptions(provider, modelModality) : [];
  const hasSelectedModel = Boolean(selected) && rows.some((row) => row.value === selected);

  return (
    <div className="space-y-1.5">
      <Label htmlFor={inputId}>{label}</Label>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
      <Select
        id={inputId}
        value={selected}
        disabled={loading || !provider}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">
          {provider ? "使用 Provider 默认模型" : loading ? "正在加载 TelePilot Provider..." : "跟随自动路由"}
        </option>
        {rows.map((row) => (
          <option key={row.value} value={row.value}>
            {row.label}
          </option>
        ))}
        {selected && !hasSelectedModel && (
          <option value={selected}>当前值：{selected}</option>
        )}
      </Select>
      {!loading && !provider && (
        <p className="text-xs text-muted-foreground">
          先选择固定 TelePilot Provider 后，才能选择该 Provider 下已启用的模型。
        </p>
      )}
    </div>
  );
}

export function schemaHasLLMSelect(schema: ConfigSchema | Record<string, unknown> | null): boolean {
  if (!schema || typeof schema !== "object" || !("properties" in schema)) return false;
  const properties = (schema as ConfigSchema).properties ?? {};
  return Object.values(properties).some(fieldHasLLMSelect);
}

function fieldHasLLMSelect(field: ConfigField | undefined): boolean {
  if (!field) return false;
  const widget = field["x-ui-widget"];
  if (widget === "llm-provider-select" || widget === "llm-model-select") return true;
  if (field.items && fieldHasLLMSelect(field.items)) return true;
  if (field.properties && Object.values(field.properties).some(fieldHasLLMSelect)) return true;
  return false;
}

function findLLMProviderBySelector(providers: LLMProviderOut[] | undefined, selector: string): LLMProviderOut | null {
  const raw = selector.trim();
  if (!raw) return null;
  const lowered = raw.toLowerCase();
  return providers?.find((provider) => (
    String(provider.id) === raw ||
    provider.name.toLowerCase() === lowered ||
    String(provider.provider).toLowerCase() === lowered
  )) ?? null;
}

function formatLLMProviderOptionLabel(provider: LLMProviderOut): string {
  const tags = provider.tags && provider.tags.length > 0 ? ` · ${provider.tags.join(",")}` : "";
  const keyState = provider.has_api_key || provider.provider === "ollama" ? "" : " · 未配置 API Key";
  return `${provider.name}（${provider.provider} · ${provider.default_model}${tags}${keyState}）`;
}

function buildLLMModelOptions(provider: LLMProviderOut, modelModality?: string): Array<{ value: string; label: string }> {
  const enabled = (provider.models ?? []).filter((model) => (
    model.enabled && modelMatchesModality(model.id, modelModality)
  ));
  const seen = new Set<string>();
  const rows: Array<{ value: string; label: string }> = [];
  for (const model of enabled) {
    const value = model.id.trim();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    const label = model.label && model.label !== model.id ? `${model.label}（${model.id}）` : model.id;
    rows.push({ value, label });
  }
  return rows;
}

function modelMatchesModality(modelId: string, modelModality?: string): boolean {
  if ((modelModality || "").trim().toLowerCase() !== "text") return true;
  const normalized = modelId.trim().toLowerCase();
  return !(
    normalized.startsWith("gpt-image-") ||
    normalized.startsWith("dall-e-") ||
    normalized.includes("image")
  );
}

function isTemplateField(key: string): boolean {
  return key === "message_template" || /_template$/i.test(key);
}

function isPreviewField(key: string): boolean {
  return key === "template_preview" || /_preview$/i.test(key);
}

function isPlaceholderField(key: string): boolean {
  return key === "template_placeholders";
}

function isReadOnlyField(key: string, field: ConfigField): boolean {
  return Boolean(field.readOnly) || isPreviewField(key) || isPlaceholderField(key);
}

function renderPreviewValue(
  previewKey: string,
  previewField: ConfigField,
  fields: FieldEntry[],
  values: Record<string, unknown>,
  commandPrefix: string,
): string {
  const templateKey = findTemplateKeyForPreview(previewKey, fields);
  const templateField = templateKey ? fields.find(([key]) => key === templateKey)?.[1] : undefined;
  const templateValue = templateKey ? values[templateKey] : undefined;
  const template = formatConfigValue(templateValue ?? templateField?.default ?? previewField.default);
  return renderTemplateSample(normalizePreviewEscapes(template), values, commandPrefix);
}

function findTemplateKeyForPreview(previewKey: string, fields: FieldEntry[]): string | null {
  const keys = new Set(fields.map(([key]) => key));
  if (previewKey === "template_preview") {
    if (keys.has("round_message_template")) return "round_message_template";
    if (keys.has("message_template")) return "message_template";
  }

  const base = previewKey.replace(/_preview$/i, "");
  const candidates = [
    `${base}_message_template`,
    `${base}_template`,
    base,
  ];
  return candidates.find((key) => keys.has(key)) ?? null;
}

function renderTemplateSample(
  template: string,
  values: Record<string, unknown>,
  commandPrefix: string,
): string {
  const sample: Record<string, string> = {
    version: "1.0.0",
    prefix: commandPrefix || ",",
    command: formatConfigValue(values.command) || "dicegrid",
    force_stop_command: formatConfigValue(values.force_stop_command) || "stop",
    target_sum: "17",
    answer_index: "6",
    prize: "100",
    timeout: formatConfigValue(values.timeout) || "90",
    guess_cooldown: formatConfigValue(values.guess_cooldown) || "2.0",
    winner: "小明",
    elapsed: "8.2",
    example: "100",
    round: "12",
    number: "3",
    count: "5",
    cost: "50003",
    pool: "188888",
    winners: "2",
    payout: "53888",
    history_limit: formatConfigValue(values.history_show_limit) || "5",
    draw_numbers: formatConfigValue(values.draw_numbers) || "1,2,3,4,5,6",
    draw_time: `${padClockValue(values.draw_hour, "21")}:${padClockValue(values.draw_minute, "00")}`,
    close_minutes: formatConfigValue(values.close_minutes_before_draw) || "1",
    interval: formatConfigValue(values.auto_draw_interval_sec) || "86400",
    chat_id: "-1001234567890",
    chat_display: "示例工作群",
    summary: "1) 讨论了版本回滚原因\n2) 确认改为平台 AI 路由\n3) 约定今天内回归验证",
    time: "2026-05-26 14:30",
    message_count: formatConfigValue(values.default_count) || "100",
  };
  sample.title = "九宫格竞猜";
  sample.target_line = `目标点数：<b>${sample.target_sum}</b>（9 格里唯一）`;
  sample.guide_line = "回复 <code>1-9</code> 选择你认为答案所在的格子。";
  sample.reward_line = `首个答对者奖励：<b>+${sample.prize}</b> · 超时 ${sample.timeout} 秒`;

  return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, key: string) => sample[key] ?? match);
}

function normalizePreviewEscapes(value: string): string {
  return value
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .replace(/\\r/g, "\n");
}

function padClockValue(value: unknown, fallback: string): string {
  const raw = formatConfigValue(value) || fallback;
  const num = Number(raw);
  if (!Number.isFinite(num)) return fallback;
  return String(Math.max(0, Math.floor(num))).padStart(2, "0");
}

function formatConfigValue(value: unknown): string {
  if (value == null) return "";
  if (Array.isArray(value)) {
    if (value.some((item) => item && typeof item === "object")) {
      return JSON.stringify(value, null, 2);
    }
    return value.map((item) => String(item)).join(", ");
  }
  if (typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

export function buildScopedConfigValues(
  schema: ConfigSchema,
  globalConfig: Record<string, unknown>,
  accountConfig: Record<string, unknown>,
): { globalVals: Record<string, unknown>; accountVals: Record<string, unknown> } {
  const globalVals: Record<string, unknown> = {};
  const accountVals: Record<string, unknown> = {};
  for (const [key, field] of Object.entries(schema.properties)) {
    const isGlobalField = field.level === "global";
    const effectiveVal = isGlobalField
      ? globalConfig[key] ?? accountConfig[key] ?? field.default
      : accountConfig[key] ?? globalConfig[key] ?? field.default;
    const displayVal = isSensitiveConfigKey(key) && isRedactedSecretValue(effectiveVal) ? "" : effectiveVal;
    if (isGlobalField) {
      globalVals[key] = displayVal;
    } else {
      accountVals[key] = displayVal;
    }
  }
  return { globalVals, accountVals };
}

export function withoutReadOnlyValues(
  values: Record<string, unknown>,
  properties: Record<string, ConfigField>,
  originalValues: Record<string, unknown> = {},
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(values)) {
    const field = properties[key];
    if (field && isReadOnlyField(key, field)) continue;
    if (isSensitiveConfigKey(key) && isRedactedSecretValue(originalValues[key]) && value === "") continue;
    if (isSensitiveConfigKey(key) && isRedactedSecretValue(value)) continue;
    out[key] = value;
  }
  return out;
}

function isSensitiveConfigKey(key: string): boolean {
  return /(^|_)(api_key|access_token|auth_token|bot_token|token|tokens|secret|password|passwd|pwd)$/i.test(key);
}

function isRedactedSecretValue(value: unknown): boolean {
  return typeof value === "string" && /^(\*{3,}|•{3,})$/.test(value.trim());
}

function ReadOnlyField({
  label,
  description,
  children,
}: {
  label: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div>
        <Label>{label}</Label>
        {description && <p className="mt-1 text-xs text-muted-foreground">{description}</p>}
      </div>
      <div className="rounded-md border bg-background px-3 py-2">
        {children}
      </div>
    </div>
  );
}
