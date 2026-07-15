const BLOCKQUOTE_STYLE =
  "margin:0.35rem 0;padding:0.35rem 0.65rem;border-left:3px solid rgba(255,255,255,.65);background:rgba(255,255,255,.14);border-radius:0.45rem;";

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function sanitizeTelegramHtml(value: string): string {
  let html = escapeHtml(value);

  html = html.replace(
    /&lt;code\s+class=(?:&quot;|&#39;)(language-[^&<>\s]+)(?:&quot;|&#39;)&gt;/gi,
    (_match, className: string) => `<code class="${className}">`,
  );

  html = html.replace(
    /&lt;(\/?)(b|strong|i|em|u|s|del|strike|code|pre)&gt;/gi,
    "<$1$2>",
  );
  html = html.replace(
    /&lt;blockquote(?:\s+expandable)?&gt;/gi,
    `<blockquote style="${BLOCKQUOTE_STYLE}">`,
  );
  html = html.replace(/&lt;\/blockquote&gt;/gi, "</blockquote>");
  html = html.replace(
    /<pre><code class="language-([^"]+)">/gi,
    (_match, language: string) =>
      `<pre><span class="telegram-code-language">${language}</span><code class="language-${language}">`,
  );

  return html;
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
      <pre className="whitespace-pre-wrap break-words font-sans text-white">
        {content}
      </pre>
    ) : (
      <div
        className="whitespace-pre-wrap break-words text-white [&_.telegram-code-language]:mb-1 [&_.telegram-code-language]:block [&_.telegram-code-language]:border-b [&_.telegram-code-language]:border-white/20 [&_.telegram-code-language]:pb-1 [&_.telegram-code-language]:text-[10px] [&_.telegram-code-language]:font-semibold [&_.telegram-code-language]:text-white/70 [&_a]:text-white [&_code]:rounded [&_code]:bg-white/15 [&_code]:px-1 [&_code]:py-0.5 [&_pre_code]:block [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre]:rounded [&_pre]:bg-white/15 [&_pre]:p-2"
        dangerouslySetInnerHTML={{ __html: sanitizeTelegramHtml(content) }}
      />
    );

  const modeLabel = mode === "markdown" ? "Markdown" : mode === "plain" ? "Plain" : "HTML";

  return (
    <div className="rounded-2xl border bg-gradient-to-b from-sky-50 to-emerald-50 p-4 text-xs dark:from-sky-950/30 dark:to-emerald-950/20">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="rounded-full border bg-background/80 px-2 py-0.5 font-medium text-muted-foreground">
          解析：{modeLabel}
        </span>
        {caption ? <span className="text-muted-foreground">{caption}</span> : null}
      </div>
      <div className="space-y-2.5">
        <div className="w-fit max-w-[78%] rounded-2xl rounded-bl-lg border bg-card px-3.5 py-2.5 text-foreground shadow-sm sm:max-w-[66%]">
          <div className="font-medium text-[11px] text-muted-foreground">示例用户</div>
          <div className="mt-1">请根据下面内容回复。</div>
        </div>

        <div className="ml-auto w-fit max-w-[88%] rounded-2xl rounded-br-lg bg-sky-500 px-3.5 py-2.5 text-white shadow-sm sm:max-w-[76%]">
          <div className="mb-1.5 text-[11px] font-semibold text-white/85">{title}</div>
          {rendered}
          <div className="mt-1.5 text-right text-[10px] leading-none text-white/75">
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
    <div className="rounded-2xl border bg-gradient-to-b from-sky-50 to-emerald-50 p-4 text-xs dark:from-sky-950/30 dark:to-emerald-950/20">
      <div className="space-y-2.5">
        <div className="w-fit max-w-[78%] rounded-2xl rounded-bl-lg border bg-card px-3.5 py-2.5 text-foreground shadow-sm sm:max-w-[66%]">
          <div className="font-medium text-[11px] text-muted-foreground">示例用户</div>
          <div className="mt-1">发送指令并参与竞猜。</div>
        </div>

        {renderedMessages.map((message, index) => (
          <div
            key={`${message.title ?? "preview"}-${index}`}
            className="ml-auto w-fit max-w-[88%] rounded-2xl rounded-br-lg bg-sky-500 px-3.5 py-2.5 text-white shadow-sm sm:max-w-[76%]"
          >
            <div className="mb-1.5 text-[11px] font-semibold text-white/85">
              {message.title || "TelePilot"}
            </div>
            {renderTelegramPreviewContent(message.value, message.mode)}
            <div className="mt-1.5 text-right text-[10px] leading-none text-white/75">
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
      <pre className="whitespace-pre-wrap break-words font-sans text-white">
        {content}
      </pre>
    );
  }

  return (
    <div
      className="whitespace-pre-wrap break-words text-white [&_.telegram-code-language]:mb-1 [&_.telegram-code-language]:block [&_.telegram-code-language]:border-b [&_.telegram-code-language]:border-white/20 [&_.telegram-code-language]:pb-1 [&_.telegram-code-language]:text-[10px] [&_.telegram-code-language]:font-semibold [&_.telegram-code-language]:text-white/70 [&_a]:text-white [&_code]:rounded [&_code]:bg-white/15 [&_code]:px-1 [&_code]:py-0.5 [&_pre_code]:block [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre]:rounded [&_pre]:bg-white/15 [&_pre]:p-2"
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
      className="whitespace-pre-wrap break-words text-muted-foreground [&_.telegram-code-language]:mb-1 [&_.telegram-code-language]:block [&_.telegram-code-language]:border-b [&_.telegram-code-language]:border-border [&_.telegram-code-language]:pb-1 [&_.telegram-code-language]:text-[10px] [&_.telegram-code-language]:font-semibold [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_pre_code]:block [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre]:rounded [&_pre]:bg-muted [&_pre]:p-2"
      dangerouslySetInnerHTML={{ __html: sanitizeTelegramHtml(value) }}
    />
  );
}
