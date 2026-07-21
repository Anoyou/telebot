import { ReactNode } from "react";
import { Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import { Field } from "./RulePageShell";

/**
 * 编辑规则 Dialog 的外壳：
 *   - 公共字段：name / enabled / priority
 *   - 中间 children slot：每个 feature 自己的 config 字段
 *   - 底部统一保存 / 取消按钮
 *
 * 用法：父组件管 `open` / `editing` / `form` state，
 * 把 form 的 name/enabled/priority 字段绑到这里，其余字段在 children 里渲染。
 */
export interface RuleEditDialogShellProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** null = 新建；非空 = 编辑 */
  editing: { id: number } | null;
  description?: string;
  /** 公共字段值 */
  name: string;
  enabled: boolean;
  priority: number;
  onNameChange: (v: string) => void;
  onEnabledChange: (v: boolean) => void;
  onPriorityChange: (v: number) => void;
  /** 提交按钮（保存）回调 */
  onSave: () => void;
  saving: boolean;
  /** config 字段（type-specific），渲染在公共字段下方 */
  children: ReactNode;
  /** dialog 宽度，默认 max-w-xl */
  maxWidthClass?: string;
}

export function RuleEditDialogShell({
  open,
  onOpenChange,
  editing,
  description,
  name,
  enabled,
  priority,
  onNameChange,
  onEnabledChange,
  onPriorityChange,
  onSave,
  saving,
  children,
  maxWidthClass = "max-w-xl",
}: RuleEditDialogShellProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={`${maxWidthClass} max-h-[90vh] overflow-y-auto`}>
        <DialogHeader>
          <DialogTitle>{editing ? "编辑规则" : "新建规则"}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <Field label="名称">
            <Input value={name} onChange={(e) => onNameChange(e.target.value)} />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="启用">
              <div className="flex h-10 items-center">
                <Switch checked={enabled} onCheckedChange={onEnabledChange} />
              </div>
            </Field>
            <Field label="优先级（数字越大越优先）">
              <Input
                inputMode="numeric"
                value={priority.toString()}
                onChange={(e) =>
                  onPriorityChange(
                    Number(e.target.value.replace(/[^0-9]/g, "") || 0),
                  )
                }
              />
            </Field>
          </div>

          {children}
        </div>

        <DialogFooter className="!flex !flex-row gap-2 sm:space-x-0 [&>*]:min-w-0 [&>*]:flex-1 sm:[&>*]:flex-none">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button onClick={onSave} loading={saving}>
            {!saving ? <Save className="mr-2 h-4 w-4" /> : null}
            {saving ? "保存中…" : "保存"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
