export type AgentExecutionBackend = "provider" | "direct" | "codex_gateway";

export type AgentClientIdentity =
  | "auto"
  | "minimal"
  | "openai_sdk"
  | "codex_tui"
  | "codex_desktop"
  | "claude_code"
  | "claude_desktop"
  | "grok_cli";
