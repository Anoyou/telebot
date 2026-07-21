import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  Braces,
  ChevronDown,
  CheckCircle2,
  Code2,
  Eye,
  FlaskConical,
  Layers3,
  Plus,
  RefreshCw,
  Send,
  Trash2,
  UserRound,
} from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import { listAccountBotUsers } from "@/api/accountBots";
import { getFeatureMatrix } from "@/api/features";
import {
  getMessageTemplateCatalog,
  renderMessageTemplate,
  testSendMessageTemplate,
  type MessageTemplateCatalogItem,
  type MessageTemplateCatalogResponse,
  type MessageTemplateEntity,
  type MessageTemplateMode,
  type MessageTemplateVariableDescriptor,
} from "@/api/messageTemplates";
import { TelegramHtmlPreview } from "@/components/TelegramHtmlPreview";
import { PageShell } from "@/components/layout/PageScaffold";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/misc";
import { SectionHeader, SignalPill, StatusSummaryPanel } from "@/components/ui/status";
import { Textarea } from "@/components/ui/textarea";
import { getErrMsg } from "@/lib/api";
import { goBackOr } from "@/lib/navigation";
import { cn } from "@/lib/utils";

type VariableRowKind = "string" | "number" | "boolean" | "json";

type VariableRow = {
  id: string;
  key: string;
  kind: VariableRowKind;
  value: string;
};

type VariableHint = {
  key: string;
  label: string;
  description?: string;
  required?: boolean;
};

type LabTemplate = {
  uid: string;
  key: string;
  title: string;
  description?: string | null;
  sourceId: string;
  sourceTitle: string;
  groupId: string;
  groupTitle: string;
  parseMode: string | null;
  mode: MessageTemplateMode;
  content: string;
  variables: Record<string, unknown>;
  variableHints: VariableHint[];
  raw: MessageTemplateCatalogItem;
};

type TemplateGroup = {
  uid: string;
  sourceId: string;
  sourceTitle: string;
  groupId: string;
  groupTitle: string;
  templates: LabTemplate[];
};

type NormalizedCatalog = {
  groups: TemplateGroup[];
  templates: LabTemplate[];
  sourceCount: number;
};

const EMPTY_CATALOG: NormalizedCatalog = {
  groups: [],
  templates: [],
  sourceCount: 0,
};

const SAMPLE_TEMPLATE = `<b>{title}</b>

{body}

<blockquote>{summary}</blockquote>`;

const SAMPLE_VARIABLES = {
  title: "模板实验室",
  body: "这里会显示后端 render 后的 Telegram HTML。",
  summary: "选择左侧 catalog 模板后可直接预览实体解析结果。",
};

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function readString(record: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return undefined;
}

function readRawString(record: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string") return value;
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return undefined;
}

function readArray(record: Record<string, unknown>, key: string): unknown[] {
  const value = record[key];
  return Array.isArray(value) ? value : [];
}

function isRecordMap(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function stableStringify(value: unknown, space = 2): string {
  try {
    return JSON.stringify(value, null, space);
  } catch {
    return "{}";
  }
}

function normalizeMode(value?: string | null): MessageTemplateMode {
  const lower = (value || "").toLowerCase();
  if (lower.includes("markdown")) return "markdown";
  if (lower === "plain" || lower === "text") return "plain";
  return "html";
}

function variablesFromDescriptors(
  descriptors: MessageTemplateVariableDescriptor[],
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const descriptor of descriptors) {
    const key = descriptor.key || descriptor.name;
    if (!key) continue;
    result[key] =
      descriptor.value ?? descriptor.example ?? descriptor.default ?? "";
  }
  return result;
}

function readTemplateVariables(record: Record<string, unknown>): Record<string, unknown> {
  const candidates = [
    record.sample_data,
    record.sample_variables,
    record.example_variables,
    record.defaults,
    record.variables,
  ];

  for (const candidate of candidates) {
    if (Array.isArray(candidate)) {
      return variablesFromDescriptors(candidate as MessageTemplateVariableDescriptor[]);
    }
    if (isRecordMap(candidate)) {
      return { ...candidate };
    }
  }

  return {};
}

function readVariableHints(
  record: Record<string, unknown>,
  variables: Record<string, unknown>,
): VariableHint[] {
  const descriptors = Array.isArray(record.variables)
    ? (record.variables as MessageTemplateVariableDescriptor[])
    : [];

  if (descriptors.length > 0) {
    return descriptors
      .map((descriptor) => {
        const key = descriptor.key || descriptor.name || "";
        return {
          key,
          label: descriptor.label || descriptor.title || key,
          description: descriptor.description,
          required: descriptor.required,
        };
      })
      .filter((hint) => hint.key);
  }

  return Object.keys(variables).map((key) => ({
    key,
    label: key,
  }));
}

function normalizeCatalog(raw: MessageTemplateCatalogResponse | undefined): NormalizedCatalog {
  if (!raw) return EMPTY_CATALOG;

  const groupMap = new Map<string, TemplateGroup>();
  const seenTemplates = new Set<string>();
  const sourceIds = new Set<string>();

  const pushTemplate = (
    item: unknown,
    context: {
      sourceId?: string;
      sourceTitle?: string;
      groupId?: string;
      groupTitle?: string;
    },
  ) => {
    const record = asRecord(item);
    if (!record) return;

    const sourceId = readString(record, ["source", "source_key"]) || context.sourceId || "catalog";
    const sourceTitle =
      readString(record, ["source_title", "source_label"]) ||
      context.sourceTitle ||
      sourceId;
    const groupId = readString(record, ["group", "group_key"]) || context.groupId || "default";
    const groupTitle =
      readString(record, ["group_title", "category", "group_label"]) ||
      readString(record, ["group"]) ||
      context.groupTitle ||
      "默认分组";
    const key =
      readString(record, ["key", "id", "template_key", "name"]) ||
      `${sourceId}:${groupId}:${seenTemplates.size + 1}`;
    const uid = `${sourceId}::${groupId}::${key}`;
    if (seenTemplates.has(uid)) return;
    seenTemplates.add(uid);
    sourceIds.add(sourceId);

    const title =
      readString(record, ["title", "label", "display_name", "name"]) ||
      key;
    const content =
      readRawString(record, ["content", "template", "html", "text", "body"]) ||
      "";
    const parseMode = readString(record, ["parse_mode"]);
    const variables = readTemplateVariables(record);
    const mode = normalizeMode(readString(record, ["format", "parse_mode"]));
    const template: LabTemplate = {
      uid,
      key,
      title,
      description: readString(record, ["description", "hint"]) || null,
      sourceId,
      sourceTitle,
      groupId,
      groupTitle,
      parseMode,
      mode,
      content,
      variables,
      variableHints: readVariableHints(record, variables),
      raw: record as MessageTemplateCatalogItem,
    };

    const groupUid = `${sourceId}::${groupId}`;
    const group =
      groupMap.get(groupUid) ||
      {
        uid: groupUid,
        sourceId,
        sourceTitle,
        groupId,
        groupTitle,
        templates: [],
      };
    group.templates.push(template);
    groupMap.set(groupUid, group);
  };

  const rawValue = raw as unknown;
  if (Array.isArray(rawValue)) {
    rawValue.forEach((item) => pushTemplate(item, {}));
  }

  const root = asRecord(rawValue);
  if (root) {
    for (const source of readArray(root, "sources")) {
      const sourceRecord = asRecord(source);
      if (!sourceRecord) continue;
      const sourceId = readString(sourceRecord, ["key", "id", "name"]) || "catalog";
      const sourceTitle =
        readString(sourceRecord, ["title", "label", "display_name", "name"]) ||
        sourceId;
      sourceIds.add(sourceId);

      for (const group of readArray(sourceRecord, "groups")) {
        const groupRecord = asRecord(group);
        if (!groupRecord) continue;
        const groupId = readString(groupRecord, ["key", "id", "name", "group", "group_key"]) || "default";
        const groupTitle =
          readString(groupRecord, ["title", "label", "display_name", "name", "group", "group_label"]) ||
          groupId;
        const groupTemplates = [
          ...readArray(groupRecord, "templates"),
          ...readArray(groupRecord, "items"),
        ];
        groupTemplates.forEach((item) =>
          pushTemplate(item, { sourceId, sourceTitle, groupId, groupTitle }),
        );
      }

      readArray(sourceRecord, "templates").forEach((item) =>
        pushTemplate(item, { sourceId, sourceTitle }),
      );
    }

    for (const group of readArray(root, "groups")) {
      const groupRecord = asRecord(group);
      if (!groupRecord) continue;
      const groupId = readString(groupRecord, ["key", "id", "name", "group", "group_key"]) || "default";
      const groupTitle =
        readString(groupRecord, ["title", "label", "display_name", "name", "group", "group_label"]) ||
        groupId;
      const sourceId = readString(groupRecord, ["source", "source_key"]) || "catalog";
      const sourceTitle =
        readString(groupRecord, ["source_title", "source_label"]) ||
        sourceId;
      const groupTemplates = [
        ...readArray(groupRecord, "templates"),
        ...readArray(groupRecord, "items"),
      ];
      groupTemplates.forEach((item) =>
        pushTemplate(item, { sourceId, sourceTitle, groupId, groupTitle }),
      );
    }

    readArray(root, "templates").forEach((item) => pushTemplate(item, {}));
    readArray(root, "items").forEach((item) => pushTemplate(item, {}));
  }

  const groups = Array.from(groupMap.values()).sort((a, b) =>
    `${a.sourceTitle}/${a.groupTitle}`.localeCompare(`${b.sourceTitle}/${b.groupTitle}`, "zh-CN"),
  );
  return {
    groups,
    templates: groups.flatMap((group) => group.templates),
    sourceCount: sourceIds.size,
  };
}

function rowKindFromValue(value: unknown): VariableRowKind {
  if (typeof value === "number") return "number";
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "string") return "string";
  return "json";
}

function rowValueFromValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return stableStringify(value);
}

function rowsFromVariables(variables: Record<string, unknown>): VariableRow[] {
  return Object.entries(variables).map(([key, value], index) => ({
    id: `${key}-${index}-${Date.now()}`,
    key,
    kind: rowKindFromValue(value),
    value: rowValueFromValue(value),
  }));
}

function parseVariablesJson(value: string): {
  value: Record<string, unknown>;
  error?: string;
} {
  try {
    const parsed = value.trim() ? JSON.parse(value) : {};
    if (!isRecordMap(parsed)) {
      return { value: {}, error: "变量 JSON 必须是对象" };
    }
    return { value: parsed };
  } catch (err) {
    return {
      value: {},
      error: err instanceof Error ? err.message : "变量 JSON 格式错误",
    };
  }
}

function parseRowValue(row: VariableRow): unknown {
  if (row.kind === "number") {
    const value = Number(row.value);
    if (!Number.isFinite(value)) throw new Error(`${row.key || "变量"} 必须是数字`);
    return value;
  }
  if (row.kind === "boolean") {
    return row.value === "true" || row.value === "1" || row.value === "是";
  }
  if (row.kind === "json") {
    return row.value.trim() ? JSON.parse(row.value) : null;
  }
  return row.value;
}

function variablesFromRows(rows: VariableRow[]): {
  value: Record<string, unknown>;
  error?: string;
} {
  const result: Record<string, unknown> = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (!key) continue;
    if (Object.prototype.hasOwnProperty.call(result, key)) {
      return { value: result, error: `变量 ${key} 重复` };
    }
    try {
      result[key] = parseRowValue(row);
    } catch (err) {
      return {
        value: result,
        error: err instanceof Error ? err.message : `变量 ${key} 解析失败`,
      };
    }
  }
  return { value: result };
}

function renderedTextFromResult(result?: {
  rendered_text?: string;
  text?: string;
  html?: string;
} | null): string {
  return result?.rendered_text || result?.text || result?.html || "";
}

function entityListFromResult(result?: {
  entity_summary?: MessageTemplateEntity[];
  entities?: MessageTemplateEntity[];
} | null): MessageTemplateEntity[] {
  return result?.entity_summary?.length ? result.entity_summary : result?.entities ?? [];
}

const ENTITY_TYPE_LABELS: Record<string, string> = {
  bold: "加粗",
  italic: "斜体",
  underline: "下划线",
  strike: "删除线",
  code: "行内代码",
  pre: "代码块",
  blockquote: "引用块",
  text_url: "文字链接",
  url: "链接",
  mention: "用户提及",
  spoiler: "隐藏文本",
  custom_emoji: "自定义表情",
};

function entityDisplayName(entity: MessageTemplateEntity): string {
  return ENTITY_TYPE_LABELS[entity.type] || entity.type || "特殊格式";
}

function entitySnippet(entity: MessageTemplateEntity, plainText: string): string {
  if (entity.text) return entity.text;
  if (
    typeof entity.offset !== "number" ||
    typeof entity.length !== "number" ||
    entity.length <= 0 ||
    !plainText
  ) {
    return "未返回具体内容";
  }
  const snippet = plainText.slice(entity.offset, entity.offset + entity.length);
  return snippet.replace(/\n/g, "\\n") || "空白内容";
}

function entityPositionLabel(entity: MessageTemplateEntity): string {
  if (typeof entity.offset !== "number" || typeof entity.length !== "number") {
    return "位置未返回";
  }
  return `第 ${entity.offset + 1} 个字符起，长度 ${entity.length}`;
}

function entityReadableDetails(entity: MessageTemplateEntity): string[] {
  const details: string[] = [];
  if (entity.language) details.push(`代码块标识：language-${entity.language}`);
  if (entity.collapsed !== null && entity.collapsed !== undefined) {
    details.push(entity.collapsed ? "折叠引用：是" : "折叠引用：否");
  }
  if (entity.url) details.push(`链接：${entity.url}`);
  if (entity.custom_emoji_id) details.push(`表情 ID：${entity.custom_emoji_id}`);
  return details;
}

function parsePrivateChatId(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number(trimmed);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) return null;
  return parsed;
}

export function MessageTemplateLabPage() {
  const nav = useNavigate();
  const [searchParams] = useSearchParams();
  const [selectedAid, setSelectedAid] = useState<number | null>(null);
  const [selectedTemplateUid, setSelectedTemplateUid] = useState<string | null>(null);
  const [draftContent, setDraftContent] = useState(SAMPLE_TEMPLATE);
  const [variablesText, setVariablesText] = useState(() => stableStringify(SAMPLE_VARIABLES));
  const [variableRows, setVariableRows] = useState<VariableRow[]>(() =>
    rowsFromVariables(SAMPLE_VARIABLES),
  );
  const [targetChatId, setTargetChatId] = useState("");
  const [openGroupUids, setOpenGroupUids] = useState<Set<string>>(() => new Set());
  const [renderResult, setRenderResult] = useState<Awaited<ReturnType<typeof renderMessageTemplate>> | null>(null);
  const [testResult, setTestResult] = useState<Awaited<ReturnType<typeof testSendMessageTemplate>> | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const renderRequestSeq = useRef(0);
  const testRequestSeq = useRef(0);
  const invalidatePendingRequests = () => {
    renderRequestSeq.current += 1;
    testRequestSeq.current += 1;
  };

  const matrixQ = useQuery({
    queryKey: ["matrix"],
    queryFn: getFeatureMatrix,
  });

  const accounts = matrixQ.data?.accounts ?? [];

  useEffect(() => {
    if (accounts.length === 0) return;

    const aidParam =
      searchParams.get("aid") ||
      searchParams.get("account_id") ||
      searchParams.get("account");
    const requestedAid = aidParam ? Number(aidParam) : NaN;
    const validRequestedAid =
      Number.isInteger(requestedAid) && accounts.some((account) => account.id === requestedAid);

    if (validRequestedAid) {
      setSelectedAid(requestedAid);
      return;
    }

    setSelectedAid((prev) => {
      if (prev !== null && accounts.some((account) => account.id === prev)) return prev;
      return accounts[0].id;
    });
  }, [accounts, searchParams]);

  const catalogQ = useQuery({
    queryKey: ["message-templates", "catalog", selectedAid],
    queryFn: () => getMessageTemplateCatalog(selectedAid!),
    enabled: selectedAid !== null,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });

  const usersQ = useQuery({
    queryKey: ["account", selectedAid, "bot", "users"],
    queryFn: () => listAccountBotUsers(selectedAid!),
    enabled: selectedAid !== null,
  });

  const catalog = useMemo(() => normalizeCatalog(catalogQ.data), [catalogQ.data]);
  const selectedTemplate =
    catalog.templates.find((template) => template.uid === selectedTemplateUid) ??
    catalog.templates[0] ??
    null;
  const selectedAccount = accounts.find((account) => account.id === selectedAid) ?? null;
  const parsedVariables = useMemo(
    () => parseVariablesJson(variablesText),
    [variablesText],
  );
  const rowsVariables = useMemo(
    () => variablesFromRows(variableRows),
    [variableRows],
  );
  const renderedText = renderedTextFromResult(renderResult);
  const previewValue = renderedText || draftContent || SAMPLE_TEMPLATE;
  const entities = entityListFromResult(renderResult);
  const entityPlainText =
    renderResult?.plain_text ||
    renderResult?.validation?.plain_text ||
    renderedText ||
    "";
  const privateUsers = (usersQ.data ?? []).filter((user) =>
    user.enabled &&
    typeof user.last_chat_id === "number" &&
    user.last_chat_id > 0 &&
    user.last_chat_id === user.tg_user_id,
  );

  const renderMut = useMutation({
    mutationFn: renderMessageTemplate,
    onMutate: () => {
      const requestSeq = ++renderRequestSeq.current;
      return { requestSeq };
    },
    onSuccess: (result, _variables, context) => {
      if (context?.requestSeq !== renderRequestSeq.current) return;
      if (result.validation && !result.validation.ok) {
        setRenderResult(result);
        setTestResult(null);
        toast.error(result.validation.errors?.[0] || "模板校验失败");
        return;
      }
      setRenderResult(result);
      setTestResult(null);
      setTestError(null);
      if (result.validation?.warnings?.length) {
        toast.warning(result.validation.warnings[0]);
      } else {
        toast.success("模板已由后端渲染");
      }
    },
    onError: (err, _variables, context) => {
      if (context?.requestSeq !== renderRequestSeq.current) return;
      toast.error(getErrMsg(err));
    },
  });

  const testMut = useMutation({
    mutationFn: testSendMessageTemplate,
    onMutate: () => {
      setTestError(null);
      const requestSeq = ++testRequestSeq.current;
      return { requestSeq };
    },
    onSuccess: (result, _variables, context) => {
      if (context?.requestSeq !== testRequestSeq.current) return;
      if (result.ok === false) {
        const message = result.message || "测试发送失败";
        setTestError(message);
        toast.error(message);
        return;
      }
      setTestResult(result);
      setTestError(null);
      toast.success(result.message || "私聊测试消息已发送");
    },
    onError: (err, _variables, context) => {
      if (context?.requestSeq !== testRequestSeq.current) return;
      const message = getErrMsg(err);
      setTestError(message);
      toast.error(message);
    },
  });

  useEffect(() => {
    if (catalog.templates.length === 0) {
      setSelectedTemplateUid(null);
      return;
    }

    setSelectedTemplateUid((prev) => {
      if (prev && catalog.templates.some((template) => template.uid === prev)) return prev;
      return catalog.templates[0].uid;
    });
  }, [catalog.templates]);

  useEffect(() => {
    if (catalog.groups.length === 0) {
      setOpenGroupUids(new Set());
      return;
    }

    setOpenGroupUids((prev) => {
      const validGroupUids = new Set(catalog.groups.map((group) => group.uid));
      const retained = Array.from(prev).filter((uid) => validGroupUids.has(uid));
      if (retained.length > 0) return new Set(retained);
      const activeGroup =
        catalog.groups.find((group) =>
          selectedTemplate
            ? group.templates.some((template) => template.uid === selectedTemplate.uid)
            : false,
        ) ?? catalog.groups[0];
      return new Set(activeGroup ? [activeGroup.uid] : []);
    });
  }, [catalog.groups, selectedTemplate]);

  const toggleGroup = (groupUid: string) => {
    setOpenGroupUids((prev) => {
      const next = new Set(prev);
      if (next.has(groupUid)) {
        next.delete(groupUid);
      } else {
        next.add(groupUid);
      }
      return next;
    });
  };

  const expandAllGroups = () => {
    setOpenGroupUids(new Set(catalog.groups.map((group) => group.uid)));
  };

  const collapseAllGroups = () => {
    setOpenGroupUids(new Set());
  };

  useEffect(() => {
    if (!selectedTemplate) return;
    setDraftContent(selectedTemplate.content || SAMPLE_TEMPLATE);
    setVariablesText(stableStringify(
      Object.keys(selectedTemplate.variables).length > 0
        ? selectedTemplate.variables
        : SAMPLE_VARIABLES,
    ));
    setVariableRows(rowsFromVariables(
      Object.keys(selectedTemplate.variables).length > 0
        ? selectedTemplate.variables
        : SAMPLE_VARIABLES,
    ));
    setRenderResult(null);
    setTestResult(null);
    setTestError(null);
  }, [selectedTemplate]);

  const changeAccount = (aid: number) => {
    invalidatePendingRequests();
    setSelectedAid(aid);
    setSelectedTemplateUid(null);
    setRenderResult(null);
    setTestResult(null);
    setTargetChatId("");
    const next = new URLSearchParams(searchParams);
    next.set("aid", String(aid));
    nav(`/plugins/message-template-lab?${next.toString()}`, { replace: true });
  };

  const runRender = (
    template = selectedTemplate,
    content = draftContent,
    variables = parsedVariables.value,
  ) => {
    if (!selectedAid) {
      toast.error("请先选择账号");
      return;
    }
    if (!template) {
      toast.error("请先选择模板");
      return;
    }
    if (parsedVariables.error && variables === parsedVariables.value) {
      toast.error(`变量 JSON 无法解析：${parsedVariables.error}`);
      return;
    }
    if (rowsVariables.error) {
      toast.error(`键值变量无法解析：${rowsVariables.error}`);
      return;
    }
    renderMut.mutate({
      template: content,
      sample_data: variables,
      parse_mode: template.parseMode ?? template.mode,
    });
  };

  const selectTemplate = (template: LabTemplate) => {
    invalidatePendingRequests();
    setSelectedTemplateUid(template.uid);
    const variables = Object.keys(template.variables).length > 0
      ? template.variables
      : SAMPLE_VARIABLES;
    const content = template.content || SAMPLE_TEMPLATE;
    setDraftContent(content);
    setVariablesText(stableStringify(variables));
    setVariableRows(rowsFromVariables(variables));
    setRenderResult(null);
    setTestResult(null);
    if (selectedAid) {
      renderMut.mutate({
        template: content,
        sample_data: variables,
        parse_mode: template.parseMode ?? template.mode,
      });
    }
  };

  const syncRowsToJson = (rows = variableRows) => {
    invalidatePendingRequests();
    const next = variablesFromRows(rows);
    if (next.error) {
      toast.error(next.error);
      return false;
    }
    setVariablesText(stableStringify(next.value));
    return true;
  };

  const syncJsonToRows = () => {
    invalidatePendingRequests();
    const parsed = parseVariablesJson(variablesText);
    if (parsed.error) {
      toast.error(`变量 JSON 无法解析：${parsed.error}`);
      return;
    }
    setVariableRows(rowsFromVariables(parsed.value));
  };

  const updateVariableRow = (
    id: string,
    patch: Partial<Omit<VariableRow, "id">>,
  ) => {
    invalidatePendingRequests();
    setVariableRows((prev) => {
      const next = prev.map((row) => (row.id === id ? { ...row, ...patch } : row));
      const parsed = variablesFromRows(next);
      if (!parsed.error) {
        setVariablesText(stableStringify(parsed.value));
      }
      return next;
    });
  };

  const addVariableRow = () => {
    invalidatePendingRequests();
    const next = [
      ...variableRows,
      {
        id: `var-${Date.now()}`,
        key: "",
        kind: "string" as const,
        value: "",
      },
    ];
    setVariableRows(next);
  };

  const removeVariableRow = (id: string) => {
    invalidatePendingRequests();
    const next = variableRows.filter((row) => row.id !== id);
    setVariableRows(next);
    syncRowsToJson(next);
  };

  const resetVariables = () => {
    invalidatePendingRequests();
    const source = selectedTemplate && Object.keys(selectedTemplate.variables).length > 0
      ? selectedTemplate.variables
      : SAMPLE_VARIABLES;
    setVariablesText(stableStringify(source));
    setVariableRows(rowsFromVariables(source));
  };

  const runTestSend = () => {
    if (!selectedAid) {
      toast.error("请先选择账号");
      return;
    }
    if (!selectedTemplate) {
      toast.error("请先选择模板");
      return;
    }
    const chatId = parsePrivateChatId(targetChatId);
    if (!chatId) {
      const message = "target_chat_id 仅支持授权用户的 Telegram 数字 ID，不能填群 ID、频道 ID、手机号或 username。";
      setTestError(message);
      toast.error(message);
      return;
    }
    const parsed = parseVariablesJson(variablesText);
    if (parsed.error) {
      toast.error(`变量 JSON 无法解析：${parsed.error}`);
      return;
    }
    if (rowsVariables.error) {
      toast.error(`键值变量无法解析：${rowsVariables.error}`);
      return;
    }
    if (!renderedText) {
      toast.error("请先渲染模板，再发送私聊测试");
      return;
    }
    if (renderResult && renderResult.validation && !renderResult.validation.ok) {
      toast.error(renderResult.validation.errors?.[0] || "模板校验失败，不能发送");
      return;
    }
    testMut.mutate({
      account_id: selectedAid,
      target_chat_id: chatId,
      text: renderedText,
      parse_mode: selectedTemplate.parseMode ?? selectedTemplate.mode,
    });
  };

  if (matrixQ.isLoading && accounts.length === 0) {
    return (
      <div className="flex h-[40vh] items-center justify-center">
        <Spinner className="text-primary" />
      </div>
    );
  }

  return (
    <PageShell className="pb-24">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="default" size="sm" className="gap-1.5 shadow-sm" onClick={() => goBackOr(nav, "/plugins")}>
          <ArrowLeft className="h-4 w-4" /> 返回上一页
        </Button>
      </div>

      <StatusSummaryPanel
        icon={FlaskConical}
        title="消息模板实验室"
        description="按账号拉取后端 catalog，编辑变量后由后端 render。测试发送只允许发给当前账号已授权且私聊过 Bot 的用户。"
        signals={(
          <>
            <SignalPill tone="primary" label="模板" value={catalog.templates.length} />
            <SignalPill tone="neutral" label="来源" value={catalog.sourceCount || "-"} />
            <SignalPill tone={renderResult ? "success" : "neutral"} label="渲染" value={renderResult ? "已完成" : "待执行"} />
          </>
        )}
        actions={(
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center">
            <Select
              value={selectedAid?.toString() ?? ""}
              onChange={(event) => changeAccount(Number(event.target.value))}
              className="w-full sm:w-56"
              disabled={accounts.length === 0}
              aria-label="选择账号"
            >
              {accounts.length === 0 ? (
                <option value="">暂无账号</option>
              ) : null}
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}
                </option>
              ))}
            </Select>
              <Button
                type="button"
                onClick={() => runRender()}
                disabled={
                  !selectedTemplate ||
                  renderMut.isPending ||
                  Boolean(parsedVariables.error) ||
                  Boolean(rowsVariables.error)
                }
                className="w-full sm:w-auto"
              >
              {renderMut.isPending ? (
                <Spinner className="mr-1" />
              ) : (
                <RefreshCw className="mr-1 h-4 w-4" />
              )}
              重新渲染
            </Button>
          </div>
        )}
      />

      <div className="grid min-w-0 gap-4 xl:grid-cols-[280px_minmax(0,1fr)_380px]">
        <Card className="h-fit xl:sticky xl:top-4">
          <CardHeader className="pb-3">
            <SectionHeader
              icon={Layers3}
              title="模板目录"
              description={selectedAccount ? `当前账号：${selectedAccount.name}` : "请选择账号"}
              meta={catalogQ.isFetching ? <Spinner className="text-muted-foreground" /> : null}
              actions={catalog.groups.length > 0 ? (
                <div className="flex gap-1.5">
                  <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={expandAllGroups}>
                    展开
                  </Button>
                  <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={collapseAllGroups}>
                    收起
                  </Button>
                </div>
              ) : null}
            />
          </CardHeader>
          <CardContent className="space-y-3">
            {catalogQ.isError ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                catalog 加载失败：{getErrMsg(catalogQ.error)}
              </div>
            ) : null}

            {catalogQ.isLoading ? (
              <div className="flex h-32 items-center justify-center">
                <Spinner className="text-primary" />
              </div>
            ) : null}

            {!catalogQ.isLoading && catalog.groups.length === 0 ? (
              <div className="rounded-md border bg-muted/30 px-3 py-6 text-center text-sm text-muted-foreground">
                暂无可测试模板
              </div>
            ) : null}

            <div className="space-y-2">
              {catalog.groups.map((group) => {
                const open = openGroupUids.has(group.uid);
                const activeInGroup = group.templates.some((template) => template.uid === selectedTemplate?.uid);
                return (
                  <div
                    key={group.uid}
                    className={cn(
                      "overflow-hidden rounded-lg border transition",
                      activeInGroup ? "border-primary/35 bg-primary/5" : "bg-background",
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => toggleGroup(group.uid)}
                      className="flex w-full min-w-0 items-center gap-2 px-3 py-2.5 text-left transition hover:bg-muted/50"
                      aria-expanded={open}
                    >
                      <ChevronDown
                        className={cn(
                          "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
                          open ? "rotate-0" : "-rotate-90",
                        )}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-semibold text-foreground">
                          {group.groupTitle}
                        </span>
                        <span className="mt-0.5 block truncate text-[11px] text-muted-foreground">
                          {group.sourceTitle}
                        </span>
                      </span>
                      <MetaBadge tone={activeInGroup ? "success" : "neutral"} className="shrink-0 text-[10px]">
                        {group.templates.length}
                      </MetaBadge>
                    </button>

                    {open ? (
                      <div className="space-y-1.5 border-t bg-muted/20 p-2">
                        {group.templates.map((template) => {
                          const active = template.uid === selectedTemplate?.uid;
                          return (
                            <button
                              key={template.uid}
                              type="button"
                              onClick={() => selectTemplate(template)}
                              className={cn(
                                "w-full rounded-lg border px-3 py-2 text-left transition",
                                active
                                  ? "border-primary/40 bg-primary/10 shadow-sm"
                                  : "bg-background hover:bg-muted/60",
                              )}
                            >
                              <span className="flex min-w-0 items-center justify-between gap-2">
                                <span className="min-w-0 truncate text-sm font-medium">
                                  {template.title}
                                </span>
                                <MetaBadge tone={active ? "success" : "outline"} className="shrink-0 text-[10px]">
                                  {(template.parseMode ?? template.mode).toUpperCase()}
                                </MetaBadge>
                              </span>
                              <span className="mt-1 block truncate font-mono text-[11px] text-muted-foreground">
                                {template.key}
                              </span>
                              <span className="mt-2 flex flex-wrap gap-1.5">
                                <MetaBadge tone="neutral" className="text-[10px]">
                                  变量 {template.variableHints.length}
                                </MetaBadge>
                                {template.description ? (
                                  <MetaBadge tone="outline" className="max-w-full truncate text-[10px]">
                                    {template.description}
                                  </MetaBadge>
                                ) : null}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>

        <div className="min-w-0 space-y-4">
          <Card>
            <CardHeader className="pb-3">
              <SectionHeader
                icon={Code2}
                title="模板内容"
                description={selectedTemplate ? `${selectedTemplate.sourceTitle} / ${selectedTemplate.groupTitle}` : "从左侧选择 catalog 模板"}
                meta={selectedTemplate ? (
                  <MetaBadge mono tone="outline" className="max-w-[180px] truncate">
                    {selectedTemplate.key}
                  </MetaBadge>
                ) : null}
              />
            </CardHeader>
            <CardContent className="space-y-3">
              <Textarea
                value={draftContent}
                onChange={(event) => {
                  invalidatePendingRequests();
                  setDraftContent(event.target.value);
                  setRenderResult(null);
                  setTestResult(null);
                }}
                className="min-h-[260px] resize-y font-mono text-xs leading-5"
                placeholder="catalog 模板内容为空时，可在这里输入 HTML 模板。"
              />
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <MetaBadge tone="outline">Telegram HTML</MetaBadge>
                <span className="break-words">
                  后端 render 会返回最终文本与 entities，本地编辑只影响本页测试。
                </span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <SectionHeader
                icon={Braces}
                title="变量"
                description="JSON 与键值行会互相同步，复杂对象可保留为 JSON 类型。"
                actions={(
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" variant="outline" onClick={resetVariables}>
                      套用样例
                    </Button>
                    <Button size="sm" variant="outline" onClick={syncJsonToRows}>
                      JSON 转键值
                    </Button>
                  </div>
                )}
              />
            </CardHeader>
            <CardContent className="space-y-4">
              {parsedVariables.error ? (
                <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span className="break-words">变量 JSON 无法解析：{parsedVariables.error}</span>
                </div>
              ) : null}

              <Textarea
                value={variablesText}
                onChange={(event) => {
                  invalidatePendingRequests();
                  setVariablesText(event.target.value);
                  setRenderResult(null);
                  setTestResult(null);
                }}
                className="min-h-[180px] resize-y font-mono text-xs leading-5"
                spellCheck={false}
              />

              {selectedTemplate?.variableHints.length ? (
                <div className="grid gap-2 rounded-lg border bg-muted/20 p-3 sm:grid-cols-2">
                  {selectedTemplate.variableHints.map((hint) => (
                    <div key={hint.key} className="min-w-0 text-xs">
                      <div className="flex min-w-0 items-center gap-2">
                        <code className="truncate rounded bg-background px-1.5 py-0.5 font-mono">
                          {hint.key}
                        </code>
                        {hint.required ? <MetaBadge tone="warn">必填</MetaBadge> : null}
                      </div>
                      {hint.description ? (
                        <div className="mt-1 line-clamp-2 text-muted-foreground">
                          {hint.description}
                        </div>
                      ) : null}
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm font-medium">键值编辑</div>
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" variant="outline" onClick={() => syncRowsToJson()}>
                      同步到 JSON
                    </Button>
                    <Button size="sm" variant="outline" onClick={addVariableRow}>
                      <Plus className="mr-1 h-4 w-4" />
                      添加变量
                    </Button>
                  </div>
                </div>
                <div className="space-y-2">
                  {rowsVariables.error ? (
                    <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                      <span className="break-words">键值变量无法解析：{rowsVariables.error}</span>
                    </div>
                  ) : null}
                  {variableRows.length === 0 ? (
                    <div className="rounded-md border bg-muted/30 px-3 py-3 text-sm text-muted-foreground">
                      当前没有变量。
                    </div>
                  ) : null}
                  {variableRows.map((row) => (
                    <div
                      key={row.id}
                      className="grid min-w-0 gap-2 rounded-lg border bg-background p-2 md:grid-cols-[minmax(120px,0.8fr)_112px_minmax(0,1.4fr)_40px]"
                    >
                      <Input
                        value={row.key}
                        onChange={(event) => updateVariableRow(row.id, { key: event.target.value })}
                        placeholder="key"
                        className="font-mono text-xs"
                      />
                      <Select
                        value={row.kind}
                        onChange={(event) =>
                          updateVariableRow(row.id, { kind: event.target.value as VariableRowKind })
                        }
                        aria-label={`${row.key || "变量"} 类型`}
                      >
                        <option value="string">string</option>
                        <option value="number">number</option>
                        <option value="boolean">boolean</option>
                        <option value="json">json</option>
                      </Select>
                      {row.kind === "boolean" ? (
                        <Select
                          value={row.value || "false"}
                          onChange={(event) => updateVariableRow(row.id, { value: event.target.value })}
                          aria-label={`${row.key || "变量"} 布尔值`}
                        >
                          <option value="true">true</option>
                          <option value="false">false</option>
                        </Select>
                      ) : (
                        <Input
                          value={row.value}
                          onChange={(event) => updateVariableRow(row.id, { value: event.target.value })}
                          placeholder="value"
                          className="font-mono text-xs"
                        />
                      )}
                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        onClick={() => removeVariableRow(row.id)}
                        aria-label="删除变量"
                      >
                        <Trash2 className="h-4 w-4 text-muted-foreground" />
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="min-w-0 space-y-4">
          <Card>
            <CardHeader className="pb-3">
              <SectionHeader
                icon={Eye}
                title="Telegram HTML 预览"
                description={renderResult ? "后端渲染结果" : "等待后端 render"}
                meta={renderMut.isPending ? <Spinner className="text-muted-foreground" /> : null}
              />
            </CardHeader>
            <CardContent className="space-y-3">
              <TelegramHtmlPreview
                value={previewValue}
                mode={selectedTemplate?.mode ?? "html"}
                title={selectedTemplate?.title || "TelePilot"}
                caption={renderResult ? "render" : "draft"}
                hints={[
                  { label: "account", value: selectedAid ? String(selectedAid) : "-" },
                  { label: "template", value: selectedTemplate?.key ?? "-" },
                ]}
              />
              <div className="rounded-lg border bg-muted/20 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="text-sm font-medium">渲染文本</div>
                  {renderResult ? (
                    <MetaBadge tone="success">
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      已返回
                    </MetaBadge>
                  ) : (
                    <MetaBadge tone="neutral">未渲染</MetaBadge>
                  )}
                </div>
                <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-background p-2 font-mono text-xs text-muted-foreground">
                  {renderedText || "尚未调用 render，右上角或选择左侧模板后可触发。"}
                </pre>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <SectionHeader
                icon={Code2}
                title="格式解析结果"
                description="这里显示 Telegram 实际识别出的加粗、链接、代码块、折叠引用等格式片段。"
                meta={<SignalPill tone="neutral" label="识别到" value={entities.length} className="h-8" />}
              />
            </CardHeader>
            <CardContent>
              {entities.length === 0 ? (
                <div className="rounded-md border bg-muted/30 px-3 py-3 text-sm text-muted-foreground">
                  还没有识别到特殊格式。普通纯文本模板这里为空是正常的。
                </div>
              ) : (
                <div className="space-y-2">
                  {entities.map((entity, index) => {
                    const details = entityReadableDetails(entity);
                    return (
                      <div key={`${entity.type}-${index}`} className="space-y-2 rounded-lg border bg-background px-3 py-2.5">
                        <div className="flex min-w-0 flex-wrap items-center gap-2">
                          <MetaBadge tone="outline">{entityDisplayName(entity)}</MetaBadge>
                          {details.slice(0, 2).map((detail) => (
                            <MetaBadge key={detail} tone="neutral" className="max-w-full truncate text-[10px]">
                              {detail}
                            </MetaBadge>
                          ))}
                        </div>
                        <div className="min-w-0 rounded-md bg-muted/40 px-2 py-1.5 text-xs">
                          <span className="text-muted-foreground">识别内容：</span>
                          <span className="break-words font-medium text-foreground">
                            {entitySnippet(entity, entityPlainText)}
                          </span>
                        </div>
                        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
                          <span>{entityPositionLabel(entity)}</span>
                          {details.slice(2).map((detail) => (
                            <span key={detail} className="break-all">
                              {detail}
                            </span>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <SectionHeader
                icon={Send}
                title="测试发送"
                description="测试发送只发当前账号的授权私聊用户。先在管理 Bot 里添加 Telegram 用户 ID，再让该用户私聊 Bot 发送 /start。"
              />
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="space-y-2 rounded-lg border border-primary/20 bg-primary/5 px-3 py-3 text-sm">
                <div className="flex items-start gap-2">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <div className="min-w-0 space-y-1">
                    <div className="font-medium text-foreground">配置 Bot Token 只是具备发送能力，还需要指定安全测试对象。</div>
                    <div className="text-muted-foreground">
                      去当前账号的管理 Bot 页添加并启用目标 Telegram 用户 ID；然后让这个用户私聊该 Bot 发送 /start。系统记录到 last_chat_id 后，这里会出现可选用户。
                    </div>
                  </div>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={!selectedAid}
                  onClick={() => selectedAid && nav(`/accounts/${selectedAid}?tab=bot-management`)}
                >
                  <UserRound className="mr-1 h-4 w-4" /> 去管理 Bot 授权用户
                </Button>
              </div>
              {privateUsers.length > 0 ? (
                <div className="space-y-1.5">
                  <Label htmlFor="bot-user-select">已记录私聊</Label>
                  <Select
                    id="bot-user-select"
                    value={
                      privateUsers.some((user) => String(user.last_chat_id) === targetChatId)
                        ? targetChatId
                        : ""
                    }
                    onChange={(event) => setTargetChatId(event.target.value)}
                  >
                    <option value="">选择授权用户 last_chat_id</option>
                    {privateUsers.map((user) => (
                      <option key={user.id} value={user.last_chat_id ?? ""}>
                        {(user.display_name || user.tg_user_id)} / {user.last_chat_id}
                      </option>
                    ))}
                  </Select>
                </div>
              ) : (
                <div className="flex items-start gap-2 rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                  <UserRound className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>
                    还没有可直接选择的私聊用户。请先去管理 Bot 添加授权用户，并让该用户私聊 Bot 发送 /start；完成后刷新这里即可选择。
                  </span>
                </div>
              )}

              <div className="space-y-1.5">
                <Label htmlFor="target-chat-id">target_chat_id</Label>
                <Input
                  id="target-chat-id"
                  inputMode="numeric"
                  placeholder="例如：123456789"
                  value={targetChatId}
                  onChange={(event) => setTargetChatId(event.target.value.replace(/[^\d]/g, ""))}
                />
                <p className="text-xs leading-5 text-muted-foreground">
                  这里填授权用户的 Telegram 用户 ID。群 ID、频道 ID、手机号、username 都不会通过测试发送校验。
                </p>
              </div>

              <Button
                type="button"
                className="w-full"
                onClick={runTestSend}
                disabled={
                  !selectedTemplate ||
                  testMut.isPending ||
                  Boolean(parsedVariables.error) ||
                  Boolean(rowsVariables.error) ||
                  Boolean(renderResult && renderResult.validation && !renderResult.validation.ok)
                }
              >
                {testMut.isPending ? (
                  <Spinner className="mr-1" />
                ) : (
                  <Send className="mr-1 h-4 w-4" />
                )}
                发送私聊测试
              </Button>

              {testError ? (
                <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span className="break-words">{testError}</span>
                </div>
              ) : null}

              {testResult ? (
                <div className="rounded-md border border-success/30 bg-success/10 px-3 py-2 text-sm text-success">
                  {testResult.message || `已发送 ${testResult.sent ?? 1} 条测试消息`}
                </div>
              ) : null}
            </CardContent>
          </Card>
        </div>
      </div>
    </PageShell>
  );
}
