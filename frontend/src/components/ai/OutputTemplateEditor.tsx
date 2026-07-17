import React from "react";
import { useQuery } from "@tanstack/react-query";

import { TelegramHtmlPreview } from "@/components/TelegramHtmlPreview";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { getSystemSettings } from "@/api/system";
import { CommandBadge } from "@/components/CommandBadge";

export type OutputFormat = "html" | "markdown" | "plain";

export interface OutputTemplatePlaceholder {
  insert: string;
  label: string;
  desc: string;
  group: string;
}

export interface OutputTemplateConditionalBlock {
  snippet: string;
  label: string;
  desc: string;
  group: string;
}

// 消息格式预设（与后端 services/llm_format.py 的 PRESETS 同源）
//
// 这些字符串必须**逐字**与后端 PRESET_SIMPLE / PRESET_QUOTE / PRESET_MINIMAL /
// PRESET_TRANSLATE 一致。改后端要同步改这里；改这里要同步改后端。
//
// 注意：output_format 默认 'html'（Telethon 1.36 不接受 'markdownv2' 字符串）；
// 这些预设里的 <b> <blockquote expandable> 等是字面 HTML，渲染时只对占位符值做
// HTML 转义，模板自身的标签保留。
export const PRESET_SIMPLE_TEMPLATE =
  "{answer}\n\n— {model} · in {in_tokens} / out {out_tokens}{?routing_note}  ·  {routing_note}{/?}";

// 占位符按钮元数据；与后端 PLACEHOLDER_META 同源
export const OUTPUT_TEMPLATE_PLACEHOLDERS: OutputTemplatePlaceholder[] = [
  { insert: "{answer}", label: "回答", desc: "AI 的回答正文", group: "内容" },
  { insert: "{answer_first_2}", label: "回答前2行", desc: "回答的前 2 行（折叠用）", group: "内容" },
  { insert: "{answer_rest}", label: "回答剩余", desc: "回答从第 3 行起（配 <blockquote expandable> 折叠）", group: "内容" },
  { insert: "{display_input}", label: "输入", desc: "用户的输入：被回复消息正文（优先）/ 没有则用问题", group: "上下文" },
  { insert: "{display_input_first_2}", label: "输入前2行", desc: "输入的前 2 行（折叠用）", group: "上下文" },
  { insert: "{display_input_rest}", label: "输入剩余", desc: "输入从第 3 行起（配 <blockquote expandable> 折叠）", group: "上下文" },
  { insert: "{question}", label: "问题", desc: "用户在指令后跟的问题", group: "上下文" },
  { insert: "{quoted}", label: "被引用", desc: "被回复消息的正文（无被回复时为空）", group: "上下文" },
  { insert: "{model}", label: "模型", desc: "模型展示名（优先使用 Provider 模型标签）", group: "模型" },
  { insert: "{model_id}", label: "模型ID", desc: "API 实际返回的原始模型 ID", group: "模型" },
  { insert: "{provider}", label: "提供商", desc: "提供商名称（如 Any GPT）", group: "模型" },
  { insert: "{provider_kind}", label: "厂商", desc: "openai / anthropic / ollama", group: "模型" },
  { insert: "{command}", label: "指令", desc: "当前指令模板名称，如 ai / search / image", group: "运行" },
  { insert: "{mode}", label: "AI模式", desc: "当前指令模式：chat / search / image / video 等", group: "运行" },
  { insert: "{api_format}", label: "实际协议", desc: "本次实际调用协议：chat_completions / responses / anthropic_messages", group: "协议" },
  { insert: "{api_protocol}", label: "API协议", desc: "实际协议的别名，等同于 {api_format}", group: "协议" },
  { insert: "{configured_api_format}", label: "配置协议", desc: "Provider 默认 API 协议", group: "协议" },
  { insert: "{web_search_api_format}", label: "联网协议", desc: "联网搜索时的协议覆盖配置；auto 表示按需切换", group: "协议" },
  { insert: "{endpoint}", label: "接口路径", desc: "本次实际接口路径，如 /chat/completions 或 /responses", group: "协议" },
  { insert: "{web_search}", label: "联网搜索", desc: "本次是否启用联网搜索；未启用时为空", group: "协议" },
  { insert: "{in_tokens}", label: "输入tokens", desc: "输入 token 数", group: "统计" },
  { insert: "{out_tokens}", label: "输出tokens", desc: "输出 token 数", group: "统计" },
  { insert: "{total_tokens}", label: "总tokens", desc: "输入 + 输出", group: "统计" },
  { insert: "{routing_note}", label: "路由说明", desc: "auto 模式的决策原因（fixed 模式空）", group: "运行" },
  { insert: "{sources}", label: "来源", desc: "联网搜索返回的来源列表（无来源时为空）", group: "运行" },
  { insert: "{time}", label: "时间", desc: "当前时间 HH:MM", group: "运行" },
];

export const OUTPUT_TEMPLATE_CONDITIONAL_BLOCKS: OutputTemplateConditionalBlock[] = [
  {
    snippet: "{?quoted}\n\n{/?}",
    label: "被引用",
    desc: "仅当被回复消息非空才渲染括号内",
    group: "上下文",
  },
  {
    snippet: "{?routing_note}\n\n{/?}",
    label: "路由",
    desc: "仅 auto 模式才渲染括号内",
    group: "运行",
  },
  {
    snippet: "{?sources}\n\n<b>来源</b>\n{sources}{/?}",
    label: "来源",
    desc: "仅联网搜索返回来源时渲染",
    group: "运行",
  },
  {
    snippet: "{?web_search}\n\n{/?}",
    label: "联网",
    desc: "仅本次启用联网搜索时渲染",
    group: "协议",
  },
  {
    snippet: "{?answer_rest}\n<blockquote expandable>{answer_rest}</blockquote>{/?}",
    label: "回答有剩余",
    desc: "仅当回答超过 2 行才渲染（配折叠块用）",
    group: "内容",
  },
  {
    snippet: "{?display_input_rest}\n<blockquote expandable>{display_input_rest}</blockquote>{/?}",
    label: "输入有剩余",
    desc: "仅当输入超过 2 行才渲染（配折叠块用）",
    group: "上下文",
  },
];

export function renderOutputTemplatePreview(template: string, values: Record<string, string>): string {
  let out = template || PRESET_SIMPLE_TEMPLATE;
  out = out.replace(/\{\?([a-zA-Z0-9_]+)\}([\s\S]*?)\{\/\?\}/g, (_, key: string, inner: string) =>
    values[key] ? inner : "",
  );
  out = out.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, key: string) => values[key] ?? "");
  return out;
}

function groupBy<T>(items: T[], getKey: (item: T) => string): Array<[string, T[]]> {
  const grouped = new Map<string, T[]>();
  for (const item of items) {
    const key = getKey(item);
    grouped.set(key, [...(grouped.get(key) ?? []), item]);
  }
  return [...grouped.entries()];
}

export interface OutputTemplateEditorProps {
  outputFormat: OutputFormat;
  onOutputFormatChange: (v: OutputFormat) => void;
  template: string;
  onTemplateChange: (v: string) => void;
  escapeValues: boolean;
  onEscapeValuesChange: (v: boolean) => void;
}

export function OutputTemplateEditor({
  outputFormat,
  onOutputFormatChange,
  template,
  onTemplateChange,
  escapeValues,
  onEscapeValuesChange,
}: OutputTemplateEditorProps) {
  const settingsQ = useQuery({
    queryKey: ["system", "settings"],
    queryFn: getSystemSettings,
  });
  const cmdPrefix = settingsQ.data?.command_prefix || ",";
  const textareaRef = React.useRef<HTMLTextAreaElement | null>(null);
  const previewText = renderOutputTemplatePreview(template, {
    answer: "这是 AI 回答示例，已按当前消息模板渲染。",
    answer_first_2: "这是 AI 回答示例，已按当前消息模板渲染。",
    answer_rest: "这里是从第三行开始的回答内容。",
    display_input: "被回复消息或用户问题示例",
    display_input_first_2: "被回复消息或用户问题示例",
    display_input_rest: "这里是输入内容的剩余部分。",
    question: "请总结这段内容",
    quoted: "这是一段被回复的原文。",
    model: "GPT-5.4",
    model_id: "gpt-5.4",
    provider: "OpenAI",
    provider_kind: "openai",
    command: "ai",
    mode: "search",
    api_format: "responses",
    configured_api_format: "chat_completions",
    web_search_api_format: "auto",
    api_protocol: "responses",
    endpoint: "/responses",
    web_search: "true",
    in_tokens: "128",
    out_tokens: "64",
    total_tokens: "192",
    routing_note: "auto: chat",
    sources: "1. OpenAI 文档\nhttps://platform.openai.com/docs\n2. 示例来源\nhttps://example.com",
    time: "12:30",
  });

  // 在光标位置插入文本，光标停在插入末尾
  const insertAtCursor = (text: string) => {
    const ta = textareaRef.current;
    if (!ta) {
      onTemplateChange((template || "") + text);
      return;
    }
    const start = ta.selectionStart ?? template.length;
    const end = ta.selectionEnd ?? template.length;
    const next = template.slice(0, start) + text + template.slice(end);
    onTemplateChange(next);
    // 在 React 下次 render 后把光标停到插入末尾
    queueMicrotask(() => {
      ta.focus();
      const pos = start + text.length;
      ta.setSelectionRange(pos, pos);
    });
  };

  const placeholderGroups = groupBy(OUTPUT_TEMPLATE_PLACEHOLDERS, (item) => item.group);
  const conditionalGroups = groupBy(OUTPUT_TEMPLATE_CONDITIONAL_BLOCKS, (item) => item.group);

  return (
    <div className="rounded-md border bg-muted/30 p-3 space-y-3">
      <div>
        <Label className="text-sm font-semibold">消息格式</Label>
        <p className="text-xs text-muted-foreground">
          决定 <CommandBadge>{cmdPrefix}ai</CommandBadge> 调用后编辑回 TG 的消息长什么样。留空使用系统默认简洁格式。
          可从下方选择占位符或条件块，插入到当前光标位置。
        </p>
      </div>

      {/* 解析模式 */}
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label className="text-xs">解析模式（parse_mode）</Label>
          <Select
            value={outputFormat}
            onChange={(e) => onOutputFormatChange(e.target.value as OutputFormat)}
          >
            <option value="html">HTML（推荐；支持 &lt;b&gt; &lt;blockquote expandable&gt; 折叠引用）</option>
            <option value="markdown">Markdown v1（**bold** / `code` / [link](url)；不支持折叠）</option>
            <option value="plain">纯文本（不解析任何格式）</option>
          </Select>
          <p className="text-[11px] text-muted-foreground">
            注：Telethon 1.36 不识别 MarkdownV2；要折叠引用块请用 HTML 模式 +
            <code>&lt;blockquote expandable&gt;</code>
          </p>
        </div>
        <div className="flex items-center gap-2 self-end pb-2">
          <Switch
            checked={escapeValues}
            onCheckedChange={onEscapeValuesChange}
            id="escapeValues"
          />
          <Label htmlFor="escapeValues" className="cursor-pointer text-xs">
            自动转义占位符值
          </Label>
        </div>
      </div>
      {!escapeValues && (
        <p className="rounded-md border px-3 py-1.5 text-xs alert-warning">
          ⚠ 关闭自动转义后，{"{answer}"} 里的 markdown 字符会被 TG 解析为格式（高级用法）；
          解析失败时本条指令会回落为纯文本展示
        </p>
      )}

      {/* 紧凑插入器：避免所有占位符和条件块同时铺满表单。 */}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label className="text-xs" htmlFor="output-placeholder-picker">插入占位符</Label>
          <Select
            id="output-placeholder-picker"
            value=""
            onChange={(event) => {
              const item = OUTPUT_TEMPLATE_PLACEHOLDERS.find(
                (placeholder) => placeholder.insert === event.target.value,
              );
              if (item) insertAtCursor(item.insert);
            }}
          >
            <option value="">选择内容、模型、协议或统计字段</option>
            {placeholderGroups.map(([group, items]) => (
              <optgroup key={group} label={group}>
                {items.map((item) => (
                  <option key={item.insert} value={item.insert}>
                    {item.label} · {item.insert} · {item.desc}
                  </option>
                ))}
              </optgroup>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label className="text-xs" htmlFor="output-condition-picker">插入条件块</Label>
          <Select
            id="output-condition-picker"
            value=""
            onChange={(event) => {
              const item = OUTPUT_TEMPLATE_CONDITIONAL_BLOCKS.find(
                (block) => block.snippet === event.target.value,
              );
              if (item) insertAtCursor(item.snippet);
            }}
          >
            <option value="">仅在存在内容时显示一段模板</option>
            {conditionalGroups.map(([group, items]) => (
              <optgroup key={group} label={group}>
                {items.map((item) => (
                  <option key={`${group}-${item.label}`} value={item.snippet}>
                    {item.label} · {item.desc}
                  </option>
                ))}
              </optgroup>
            ))}
          </Select>
        </div>
        <p className="text-xs leading-5 text-muted-foreground sm:col-span-2">
          选择后会插入到模板光标位置；字段说明保留在选项内，不再长期占用整块页面。
        </p>
      </div>

      {/* 模板 textarea */}
      <div className="space-y-1.5">
        <Label className="text-xs">模板（≤ 4000 字符）</Label>
        <Textarea
          ref={textareaRef}
          value={template}
          rows={10}
          maxLength={4000}
          onChange={(e) => onTemplateChange(e.target.value)}
          placeholder={"留空 = 使用系统默认简洁格式。\n也可以从上方选择占位符或条件块后继续编辑。"}
          className="font-mono text-xs"
        />
        <p className="text-xs text-muted-foreground">
          剩余 {4000 - (template || "").length} 字符。{template.length === 0 ? "（已留空，会用默认）" : ""}
        </p>
      </div>

      <div className="rounded-md border bg-background p-3 text-xs">
        <div className="mb-1 font-medium">预览</div>
        <TelegramHtmlPreview value={previewText} mode={outputFormat} />
      </div>
    </div>
  );
}
