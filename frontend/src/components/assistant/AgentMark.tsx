import { cn } from "@/lib/utils";

/** System Agent 专属徽记：机器人终端脸 + 在线信号节点。 */
export function AgentMark({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      className={cn("h-5 w-5", className)}
    >
      <circle cx="12" cy="3.1" r="1" fill="currentColor" />
      <path d="M12 4.2v2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <path
        d="M7.4 6.6h9.2a3 3 0 0 1 3 3v5.1a3 3 0 0 1-3 3H7.4a3 3 0 0 1-3-3V9.6a3 3 0 0 1 3-3Z"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <path d="M4.3 10.1H2.8v3.3h1.5M19.7 10.1h1.5v3.3h-1.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="9" cy="11.4" r="1.05" fill="currentColor" />
      <circle cx="15" cy="11.4" r="1.05" fill="currentColor" />
      <path d="m8.8 14.3 1.2 1-1.2 1M12.3 16.2h2.7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M8.2 20.2c1.1-.7 2.4-1 3.8-1s2.7.3 3.8 1" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" opacity=".72" />
      <path d="M19.8 4.3v2.2M18.7 5.4h2.2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
