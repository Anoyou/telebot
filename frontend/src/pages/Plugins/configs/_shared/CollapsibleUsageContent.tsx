import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function CollapsibleUsageContent({
  children,
  resetKey,
}: {
  children: ReactNode;
  resetKey: string;
}) {
  const contentRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [canExpand, setCanExpand] = useState(false);

  useLayoutEffect(() => {
    setExpanded(false);
    setCanExpand(false);
  }, [resetKey]);

  useLayoutEffect(() => {
    if (expanded) return;
    const content = contentRef.current;
    if (!content) return;

    const measure = () => {
      setCanExpand(content.scrollHeight > content.clientHeight + 1);
    };
    measure();

    const observer = new ResizeObserver(measure);
    observer.observe(content);
    return () => observer.disconnect();
  }, [expanded, resetKey]);

  return (
    <div>
      <div
        ref={contentRef}
        className={cn(!expanded && "max-h-[4.5rem] overflow-hidden")}
      >
        {children}
      </div>
      {canExpand ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="mt-1 h-7 px-1.5 text-muted-foreground"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起说明" : "展开说明"}
          <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", expanded && "rotate-180")} />
        </Button>
      ) : null}
    </div>
  );
}
