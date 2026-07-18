import type { SystemAgentMessage } from "@/api/systemAgent";

/** Tool messages are runtime evidence, not a second conversational reply. */
export function visibleConversationMessages(
  messages: SystemAgentMessage[],
): SystemAgentMessage[] {
  return messages.filter((message) => message.role !== "tool");
}
