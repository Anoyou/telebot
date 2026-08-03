import { MonitorCog } from "lucide-react";

import { Select } from "@/components/ui/select";
import type {
  AgentClientIdentity,
  AgentExecutionBackend,
} from "@/lib/assistantClientSelection";
import { cn } from "@/lib/utils";

export type ClientPickerValue = {
  executionBackend: AgentExecutionBackend;
  clientIdentityProfile?: AgentClientIdentity;
};

const OPTIONS: Array<{
  value: string;
  label: string;
  executionBackend: AgentExecutionBackend;
  identity?: AgentClientIdentity;
}> = [
  { value: "provider", label: "跟随 Provider", executionBackend: "provider" },
  { value: "direct:auto", label: "Provider 直连", executionBackend: "direct", identity: "auto" },
  { value: "direct:minimal", label: "最小请求", executionBackend: "direct", identity: "minimal" },
  { value: "direct:openai_sdk", label: "OpenAI SDK", executionBackend: "direct", identity: "openai_sdk" },
  { value: "direct:codex_tui", label: "Codex 兼容", executionBackend: "direct", identity: "codex_tui" },
  { value: "direct:codex_desktop", label: "Codex Desktop", executionBackend: "direct", identity: "codex_desktop" },
  { value: "direct:claude_code", label: "Claude Code", executionBackend: "direct", identity: "claude_code" },
  { value: "direct:claude_desktop", label: "Claude Desktop", executionBackend: "direct", identity: "claude_desktop" },
  { value: "direct:grok_cli", label: "Grok CLI", executionBackend: "direct", identity: "grok_cli" },
  { value: "codex_gateway", label: "Codex Gateway", executionBackend: "codex_gateway" },
];

export function ClientPicker({
  value,
  onChange,
  disabled,
  gatewayAvailable = true,
  className,
}: {
  value: ClientPickerValue;
  onChange: (next: ClientPickerValue) => void;
  disabled?: boolean;
  gatewayAvailable?: boolean;
  className?: string;
}) {
  const selected = value.executionBackend === "direct"
    ? `direct:${value.clientIdentityProfile || "auto"}`
    : value.executionBackend;
  return (
    <div className={cn("relative min-w-0", className)}>
      <MonitorCog
        aria-hidden="true"
        className="pointer-events-none absolute left-2 top-1/2 z-10 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
      />
      <Select
        aria-label="本轮调用客户端"
        title="选择本会话下一轮请求使用的调用客户端"
        value={selected}
        disabled={disabled}
        className="h-8 w-[6.75rem] min-w-0 truncate pl-7 pr-5 text-xs sm:w-[9.5rem]"
        onChange={(event) => {
          const option = OPTIONS.find((item) => item.value === event.target.value) || OPTIONS[0];
          onChange({
            executionBackend: option.executionBackend,
            clientIdentityProfile: option.identity,
          });
        }}
      >
        {OPTIONS.map((option) => (
          <option
            key={option.value}
            value={option.value}
            disabled={option.executionBackend === "codex_gateway" && !gatewayAvailable}
          >
            {option.label}
            {option.executionBackend === "codex_gateway" && !gatewayAvailable ? "（未配置）" : ""}
          </option>
        ))}
      </Select>
    </div>
  );
}
