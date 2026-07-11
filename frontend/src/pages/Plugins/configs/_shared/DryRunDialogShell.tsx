import { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DryRunDetail } from "@/components/DryRunDetail";

import type { RuleDryRunResponse, RuleOut } from "@/api/types";

/**
 * Dry-run Dialog 的外壳：
 *   - 标题展示当前规则名
 *   - 中间 children = 各 feature 自己的 sample 输入字段（消息、chat_id 等）
 *   - 底部统一"运行"按钮
 *   - 自动渲染结果区（matched + output + DryRunDetail）
 *
 * 父组件需要管：open / rule / result 三个 state，以及 onRun。
 */
export interface DryRunDialogShellProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  rule: RuleOut | null;
  /** 中间表单（sample_message / sample_chat_id 等） */
  children: ReactNode;
  /** 运行按钮触发 */
  onRun: () => void;
  /** 运行按钮 disabled 条件（除了 pending） */
  runDisabled?: boolean;
  pending: boolean;
  /** 上次运行结果；null = 还没运行过 */
  result: RuleDryRunResponse | null;
  description?: string;
  /** dialog 宽度 */
  maxWidthClass?: string;
}

export function DryRunDialogShell({
  open,
  onOpenChange,
  rule,
  children,
  onRun,
  runDisabled,
  pending,
  result,
  description,
  maxWidthClass = "max-w-md",
}: DryRunDialogShellProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={maxWidthClass}>
        <DialogHeader>
          <DialogTitle>试运行 · {rule?.name}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>
        <div className="space-y-3 text-sm">
          {children}

          {result ? (
            <>
              <div className="rounded-md border bg-muted/40 p-3 text-xs">
                <div className="mb-1">
                  命中：
                  <Badge variant={result.matched ? "success" : "secondary"}>
                    {result.matched ? "是" : "否"}
                  </Badge>
                </div>
                {result.output != null && (
                  <pre className="whitespace-pre-wrap">{result.output}</pre>
                )}
              </div>
              <DryRunDetail detail={result.detail} />
            </>
          ) : null}
        </div>
        <DialogFooter className="!flex !flex-row gap-2 sm:space-x-0 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
          <Button disabled={!!runDisabled || pending} onClick={onRun}>
            {pending ? "运行中…" : "运行"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
