import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

export type ResponsiveListColumn<T> = {
  key: string;
  header: ReactNode;
  priority: 0 | 1 | 2;
  render: (row: T) => ReactNode;
  className?: string;
};

export type ResponsiveListProps<T> = {
  data: T[];
  columns: ResponsiveListColumn<T>[];
  rowKey: (row: T) => string | number;
  onRowClick?: (row: T) => void;
  expandRender?: (row: T) => ReactNode;
  loading?: boolean;
  empty?: ReactNode;
  className?: string;
};

/** 桌面复用原生 table，窄屏把次要列收入可展开卡片，避免固定宽度横滚。 */
export function ResponsiveList<T>({
  data,
  columns,
  rowKey,
  onRowClick,
  expandRender,
  loading = false,
  empty,
  className,
}: ResponsiveListProps<T>) {
  const primary = columns.filter((column) => column.priority === 0);
  const secondary = columns.filter((column) => column.priority === 1);
  const tertiary = columns.filter((column) => column.priority === 2);

  if (loading) return <div className={cn("min-h-36", className)} />;
  if (data.length === 0) return <>{empty}</>;

  return (
    <div className={className}>
      <div className="hidden overflow-x-auto md:block">
        <Table>
          <TableHeader>
            <TableRow>
              {columns.map((column) => <TableHead key={column.key} className={column.className}>{column.header}</TableHead>)}
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.map((row) => (
              <TableRow key={rowKey(row)} className={onRowClick ? "cursor-pointer" : undefined} onClick={() => onRowClick?.(row)}>
                {columns.map((column) => <TableCell key={column.key} className={column.className}>{column.render(row)}</TableCell>)}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <div className="space-y-2 md:hidden">
        {data.map((row) => <ResponsiveCard key={rowKey(row)} row={row} primary={primary} secondary={secondary} tertiary={tertiary} expandRender={expandRender} onRowClick={onRowClick} />)}
      </div>
    </div>
  );
}

function ResponsiveCard<T>({
  row,
  primary,
  secondary,
  tertiary,
  expandRender,
  onRowClick,
}: {
  row: T;
  primary: ResponsiveListColumn<T>[];
  secondary: ResponsiveListColumn<T>[];
  tertiary: ResponsiveListColumn<T>[];
  expandRender?: (row: T) => ReactNode;
  onRowClick?: (row: T) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails = Boolean(expandRender || tertiary.length);
  return (
    <article className="rounded-lg border border-border/70 bg-card p-3 shadow-sm">
      <div className="flex items-start justify-between gap-3" onClick={() => onRowClick?.(row)}>
        <div className="min-w-0 flex-1 space-y-1">
          {primary.map((column) => <div key={column.key} className={cn("min-w-0", column.className)}>{column.render(row)}</div>)}
        </div>
        {hasDetails ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-8 w-8 shrink-0"
            aria-label={expanded ? "收起详情" : "展开详情"}
            onClick={(event) => {
              event.stopPropagation();
              setExpanded((value) => !value);
            }}
          >
            {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </Button>
        ) : null}
      </div>
      {secondary.length ? <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 border-t border-border/60 pt-3">{secondary.map((column) => <div key={column.key} className={cn("min-w-0", column.className)}><div className="mb-0.5 text-[11px] text-muted-foreground">{column.header}</div>{column.render(row)}</div>)}</div> : null}
      {expanded ? <div className="mt-3 space-y-2 border-t border-border/60 pt-3">{tertiary.map((column) => <div key={column.key} className={cn("min-w-0", column.className)}><div className="mb-0.5 text-[11px] text-muted-foreground">{column.header}</div>{column.render(row)}</div>)}{expandRender?.(row)}</div> : null}
    </article>
  );
}
