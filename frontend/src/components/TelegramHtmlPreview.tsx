function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function restoreTelegramEntities(value: string): string {
  return value.replace(
    /&amp;(#x[\da-f]+|#\d+|lt|gt|amp|quot|apos|nbsp|hellip|mdash|ndash|lsquo|rsquo|ldquo|rdquo);/gi,
    "&$1;",
  );
}

function decodeHtmlAttribute(value: string): string {
  return value.replace(
    /&(#x[\da-f]+|#\d+|quot|apos|amp|lt|gt);/gi,
    (_match, entity: string) => {
      const normalized = entity.toLowerCase();
      if (normalized === "quot") return '"';
      if (normalized === "apos") return "'";
      if (normalized === "amp") return "&";
      if (normalized === "lt") return "<";
      if (normalized === "gt") return ">";
      const codePoint = normalized.startsWith("#x")
        ? Number.parseInt(normalized.slice(2), 16)
        : Number.parseInt(normalized.slice(1), 10);
      return Number.isSafeInteger(codePoint) && codePoint >= 0 && codePoint <= 0x10ffff
        ? String.fromCodePoint(codePoint)
        : "";
    },
  );
}

function readEscapedAttribute(rawAttrs: string, name: string): string | null {
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = rawAttrs.match(
    new RegExp(
      `(?:^|\\s)${escapedName}\\s*=\\s*(?:&quot;([\\s\\S]*?)&quot;|&#39;([\\s\\S]*?)&#39;|([^\\s]+))`,
      "i",
    ),
  );
  const rawValue = match?.[1] ?? match?.[2] ?? match?.[3];
  return rawValue == null ? null : decodeHtmlAttribute(rawValue);
}

function hasEscapedAttribute(rawAttrs: string, name: string): boolean {
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?:^|\\s)${escapedName}(?:\\s|=|/|$)`, "i").test(rawAttrs);
}

function safeLanguage(rawAttrs: string): string | null {
  const language = readEscapedAttribute(rawAttrs, "class")?.match(/^language-([a-z0-9_+.\-]{1,40})$/i);
  return language ? language[1] : null;
}

function safeHref(rawAttrs: string): string | null {
  const href = readEscapedAttribute(rawAttrs, "href")?.trim();
  if (!href || !/^(?:https?:|mailto:|tel:|tg:|#)/i.test(href)) return null;
  return href;
}

function safeNumberAttribute(rawAttrs: string, name: string, maxDigits = 4): string | null {
  const value = readEscapedAttribute(rawAttrs, name)?.trim();
  return value && new RegExp(`^\\d{1,${maxDigits}}$`).test(value) ? value : null;
}

function safeListType(rawAttrs: string): string | null {
  const value = readEscapedAttribute(rawAttrs, "type")?.trim();
  return value && /^(?:a|A|i|I|1)$/.test(value) ? value : null;
}

function safeAlign(rawAttrs: string, name: "align" | "valign"): string | null {
  const value = readEscapedAttribute(rawAttrs, name)?.trim().toLowerCase();
  const values = name === "align" ? ["left", "center", "right"] : ["top", "middle", "bottom"];
  return value && values.includes(value) ? value : null;
}

function richTag(tag: string, rawAttrs: string, closing: boolean): string {
  if (closing) {
    switch (tag) {
      case "b":
      case "strong":
        return "</strong>";
      case "i":
      case "em":
        return "</em>";
      case "u":
      case "ins":
        return "</u>";
      case "s":
      case "strike":
      case "del":
        return "</del>";
      case "a":
      case "tg-emoji":
      case "tg-time":
      case "tg-reference":
      case "tg-math":
      case "tg-thinking":
        return "</span>";
      case "tg-spoiler":
        return "</button>";
      case "tg-math-block":
        return "</pre>";
      case "img":
      case "video":
      case "audio":
      case "tg-map":
      case "input":
        return "";
      case "tg-collage":
      case "tg-slideshow":
        return "</div>";
      case "table":
        return "</table></div>";
      case "blockquote":
        return hasEscapedAttribute(rawAttrs, "expandable")
          ? "</blockquote></details>"
          : "</blockquote>";
      default:
        return [
          "code", "mark", "sub", "sup", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "p", "footer", "ul",
          "ol", "li", "aside", "cite", "figure", "figcaption", "caption", "thead", "tbody", "tr", "th", "td",
          "details", "summary",
        ].includes(tag)
          ? `</${tag}>`
          : "";
    }
  }

  switch (tag) {
    case "b":
    case "strong":
      return "<strong>";
    case "i":
    case "em":
      return "<em>";
    case "u":
    case "ins":
      return "<u>";
    case "s":
    case "strike":
    case "del":
      return "<del>";
    case "code": {
      const language = safeLanguage(rawAttrs);
      return language
        ? `<span class="telegram-code-language">${escapeHtml(language)}</span><code class="telegram-code-language-${language}">`
        : "<code>";
    }
    case "pre":
      return "<pre class=\"telegram-pre\">";
    case "mark":
      return "<mark>";
    case "sub":
      return "<sub>";
    case "sup":
      return "<sup>";
    case "tg-spoiler":
      return '<button type="button" class="telegram-spoiler" aria-label="显示隐藏文本">';
    case "a": {
      const href = safeHref(rawAttrs);
      const name = readEscapedAttribute(rawAttrs, "name");
      const title = href || (name ? `#${name}` : null);
      return title
        ? `<span class="telegram-link" title="${escapeHtml(title)}">`
        : '<span class="telegram-link">';
    }
    case "tg-emoji": {
      const emojiId = readEscapedAttribute(rawAttrs, "emoji-id");
      return emojiId
        ? `<span class="telegram-custom-emoji" title="自定义 Emoji ${escapeHtml(emojiId)}">`
        : '<span class="telegram-custom-emoji">';
    }
    case "tg-time": {
      const unix = safeNumberAttribute(rawAttrs, "unix", 12);
      const format = readEscapedAttribute(rawAttrs, "format");
      const title = unix ? `Telegram 时间 ${unix}${format ? ` · ${format}` : ""}` : "Telegram 时间";
      return `<span class="telegram-time" title="${escapeHtml(title)}">`;
    }
    case "tg-reference":
      return '<span class="telegram-reference">';
    case "tg-math":
      return '<span class="telegram-math">';
    case "tg-math-block":
      return '<pre class="telegram-math-block">';
    case "tg-thinking":
      return '<span class="telegram-thinking">';
    case "blockquote":
      return hasEscapedAttribute(rawAttrs, "expandable")
        ? '<details class="telegram-expandable-quote"><summary>展开引用</summary><blockquote class="telegram-quote">'
        : '<blockquote class="telegram-quote">';
    case "aside":
      return '<aside class="telegram-pullquote">';
    case "details":
      return `<details class="telegram-details"${hasEscapedAttribute(rawAttrs, "open") ? " open" : ""}>`;
    case "summary":
      return '<summary class="telegram-details-summary">';
    case "table": {
      const variant = [
        hasEscapedAttribute(rawAttrs, "bordered") ? "telegram-table--bordered" : "",
        hasEscapedAttribute(rawAttrs, "striped") ? "telegram-table--striped" : "",
      ].filter(Boolean).join(" ");
      return `<div class="telegram-table-scroll"><table class="telegram-table${variant ? ` ${variant}` : ""}">`;
    }
    case "th":
    case "td": {
      const colspan = safeNumberAttribute(rawAttrs, "colspan");
      const rowspan = safeNumberAttribute(rawAttrs, "rowspan");
      const align = safeAlign(rawAttrs, "align");
      const valign = safeAlign(rawAttrs, "valign");
      const attrs = [
        colspan ? ` colspan="${colspan}"` : "",
        rowspan ? ` rowspan="${rowspan}"` : "",
        align ? ` data-align="${align}"` : "",
        valign ? ` data-valign="${valign}"` : "",
      ].join("");
      return `<${tag}${attrs}>`;
    }
    case "ol": {
      const start = safeNumberAttribute(rawAttrs, "start");
      const type = safeListType(rawAttrs);
      const reversed = hasEscapedAttribute(rawAttrs, "reversed");
      return `<ol${start ? ` start="${start}"` : ""}${type ? ` type="${type}"` : ""}${reversed ? " reversed" : ""}>`;
    }
    case "input": {
      if ((readEscapedAttribute(rawAttrs, "type") || "").toLowerCase() !== "checkbox") return "";
      const checked = hasEscapedAttribute(rawAttrs, "checked");
      return `<span class="telegram-checkbox${checked ? " is-checked" : ""}" role="checkbox" aria-checked="${checked}">${checked ? "✓" : ""}</span>`;
    }
    case "img":
    case "video":
    case "audio": {
      const source = readEscapedAttribute(rawAttrs, "src");
      const alt = readEscapedAttribute(rawAttrs, "alt");
      if (tag === "img" && (source?.startsWith("tg://emoji") || readEscapedAttribute(rawAttrs, "class") === "emoji")) {
        return `<span class="telegram-inline-emoji">${escapeHtml(alt || "🙂")}</span>`;
      }
      const kind = tag === "img" ? "图片" : tag === "video" ? "视频" : "音频";
      const fallback = alt || (source?.startsWith("tg://emoji") ? "🙂" : kind);
      return `<span class="telegram-media-placeholder telegram-media-${tag}" title="富文本${kind}占位">${escapeHtml(fallback)}</span>`;
    }
    case "tg-map": {
      const lat = readEscapedAttribute(rawAttrs, "lat");
      const long = readEscapedAttribute(rawAttrs, "long");
      const label = lat && long ? `${lat}, ${long}` : "位置";
      return `<span class="telegram-map-placeholder" title="富文本位置占位">${escapeHtml(label)}</span>`;
    }
    case "tg-collage":
      return '<div class="telegram-media-grid">';
    case "tg-slideshow":
      return '<div class="telegram-media-grid telegram-media-slideshow">';
    case "hr":
      return '<hr class="telegram-divider" />';
    case "br":
      return "<br />";
    default:
      return [
        "h1", "h2", "h3", "h4", "h5", "h6", "p", "footer", "ul", "ol", "li", "figure", "figcaption", "caption",
        "cite", "thead", "tbody", "tr",
      ].includes(tag)
        ? `<${tag}>`
        : "";
  }
}

/** Restore only Telegram's supported rich-message tags after escaping input. */
export function sanitizeTelegramHtml(value: string): string {
  const html = restoreTelegramEntities(escapeHtml(value));
  const expandableBlockquotes: boolean[] = [];
  const spoilerSpans: boolean[] = [];
  const sanitized = html.replace(
    /&lt;(\/?)\s*([a-z][a-z0-9-]*)([\s\S]*?)&gt;/gi,
    (_match, slash: string, rawTag: string, rawAttrs: string) => {
      const tag = rawTag.toLowerCase();
      const closing = slash === "/";
      let attrs = rawAttrs.replace(/\/\s*$/, "");
      if (tag === "span") {
        if (closing) {
          return spoilerSpans.pop() ? richTag("tg-spoiler", "", true) : "";
        }
        const isSpoiler = readEscapedAttribute(attrs, "class")?.toLowerCase() === "tg-spoiler";
        spoilerSpans.push(isSpoiler);
        return isSpoiler ? richTag("tg-spoiler", attrs, false) : "";
      }
      if (tag === "blockquote") {
        if (closing) {
          attrs = expandableBlockquotes.pop() ? " expandable" : "";
        } else {
          expandableBlockquotes.push(hasEscapedAttribute(attrs, "expandable"));
        }
      }
      return richTag(tag, attrs, closing);
    },
  );
  const structuralTag = "(?:h[1-6]|p|ul|ol|li|pre|blockquote|aside|details|summary|table|thead|tbody|tr|th|td|footer|figure|figcaption|caption|div|hr)";
  return sanitized.replace(
    new RegExp(`>\\s+(?=<\\/?${structuralTag}(?:\\s|>))`, "gi"),
    ">",
  );
}

function usesWideTelegramLayout(value: string, mode?: "html" | "markdown" | "plain"): boolean {
  if (mode && mode !== "html") return /^(?:#{1,6}\s|\|.+\|)/m.test(value);
  return /<(?:h[1-6]|table|details|figure|tg-collage|tg-slideshow|tg-math-block)\b/i.test(value);
}

export function TelegramHtmlPreview({
  value,
  mode,
  title = "TelePilot",
  caption,
  hints,
}: {
  value: string;
  mode?: "html" | "markdown" | "plain";
  title?: string;
  caption?: string;
  hints?: Array<{ label: string; value: string }>;
}) {
  const content = value || "预览内容为空。";
  const rendered =
    mode && mode !== "html" ? (
      <pre className="telegram-rich-plain">
        {content}
      </pre>
    ) : (
      <div
        className="telegram-rich-content telegram-rich-content--bubble"
        dangerouslySetInnerHTML={{ __html: sanitizeTelegramHtml(content) }}
      />
    );

  const modeLabel = mode === "markdown" ? "Markdown" : mode === "plain" ? "Plain" : "HTML";

  return (
    <div className="telegram-chat-preview rounded-2xl border p-4 text-xs">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="rounded-full border bg-background/80 px-2 py-0.5 font-medium text-muted-foreground">
          解析：{modeLabel}
        </span>
        {caption ? <span className="text-muted-foreground">{caption}</span> : null}
      </div>
      <div className="space-y-2.5">
        <div className="telegram-user-bubble ml-auto w-fit max-w-[78%] rounded-2xl rounded-br-md px-3.5 py-2.5 shadow-sm sm:max-w-[66%]">
          <div className="font-medium text-[11px] opacity-70">示例用户</div>
          <div className="mt-1">请根据下面内容回复。</div>
        </div>

        <div
          className={`telegram-bot-bubble max-w-[88%] rounded-2xl rounded-bl-md px-3.5 py-2.5 shadow-sm sm:max-w-[76%] ${
            usesWideTelegramLayout(content, mode) ? "w-full" : "w-fit"
          }`}
        >
          <div className="mb-1.5 text-[11px] font-semibold opacity-80">{title}</div>
          {rendered}
          <div className="mt-1.5 text-right text-[10px] leading-none opacity-60">
            12:30 ✓✓
          </div>
        </div>
      </div>
      {hints && hints.length > 0 ? (
        <div className="mt-3 grid min-w-0 gap-1.5 rounded-xl border border-border/70 bg-background/75 p-2.5 text-[11px]">
          {hints.map((hint) => (
            <div
              key={`${hint.label}-${hint.value}`}
              className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-start gap-x-1.5 gap-y-0.5"
            >
              <span className="shrink-0 text-muted-foreground">{hint.label}</span>
              <code className="min-w-0 whitespace-normal break-all rounded bg-muted/70 px-1 py-0.5 font-mono text-foreground">
                {hint.value}
              </code>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export interface TelegramHtmlPreviewMessage {
  title?: string;
  value: string;
  mode?: "html" | "markdown" | "plain";
}

export function TelegramHtmlPreviewThread({
  messages,
}: {
  messages: TelegramHtmlPreviewMessage[];
}) {
  const renderedMessages = messages.length > 0
    ? messages
    : [{ title: "TelePilot", value: "预览内容为空。", mode: "plain" as const }];

  return (
    <div className="telegram-chat-preview rounded-2xl border p-4 text-xs">
      <div className="space-y-2.5">
        <div className="telegram-user-bubble ml-auto w-fit max-w-[78%] rounded-2xl rounded-br-md px-3.5 py-2.5 shadow-sm sm:max-w-[66%]">
          <div className="font-medium text-[11px] opacity-70">示例用户</div>
          <div className="mt-1">发送指令并参与竞猜。</div>
        </div>

        {renderedMessages.map((message, index) => (
          <div
            key={`${message.title ?? "preview"}-${index}`}
            className={`telegram-bot-bubble max-w-[88%] rounded-2xl rounded-bl-md px-3.5 py-2.5 shadow-sm sm:max-w-[76%] ${
              usesWideTelegramLayout(message.value, message.mode) ? "w-full" : "w-fit"
            }`}
          >
            <div className="mb-1.5 text-[11px] font-semibold opacity-80">
              {message.title || "TelePilot"}
            </div>
            {renderTelegramPreviewContent(message.value, message.mode)}
            <div className="mt-1.5 text-right text-[10px] leading-none opacity-60">
              12:{String(30 + index).padStart(2, "0")} ✓✓
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function renderTelegramPreviewContent(
  value: string,
  mode?: "html" | "markdown" | "plain",
) {
  const content = value || "预览内容为空。";
  if (mode && mode !== "html") {
    return (
      <pre className="telegram-rich-plain">
        {content}
      </pre>
    );
  }

  return (
    <div
      className="telegram-rich-content telegram-rich-content--bubble"
      dangerouslySetInnerHTML={{ __html: sanitizeTelegramHtml(content) }}
    />
  );
}

export function TelegramHtmlContentPreview({
  value,
  mode,
}: {
  value: string;
  mode?: "html" | "markdown" | "plain";
}) {
  if (mode && mode !== "html") {
    return (
      <pre className="whitespace-pre-wrap break-words font-sans text-muted-foreground">
        {value}
      </pre>
    );
  }

  return (
    <div
      className="telegram-rich-content telegram-rich-content--surface"
      dangerouslySetInnerHTML={{ __html: sanitizeTelegramHtml(value) }}
    />
  );
}
