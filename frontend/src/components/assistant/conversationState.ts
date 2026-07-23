import type { SystemAgentMessage } from "@/api/systemAgent";

/** 流式中未闭合的 ``` 围栏临时补闭合，避免后续正文被吞进代码块。 */
export function stabilizeStreamingMarkdown(text: string): string {
  const fenceCount = (text.match(/^```/gm) || []).length;
  if (fenceCount % 2 === 1) return `${text}\n\`\`\``;
  return text;
}

/** Tool messages are runtime evidence, not a second conversational reply. */
export function visibleConversationMessages(
  messages: SystemAgentMessage[],
): SystemAgentMessage[] {
  return messages.filter((message) => message.role !== "tool");
}
