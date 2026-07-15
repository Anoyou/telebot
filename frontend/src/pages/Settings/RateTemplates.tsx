// 风控模板：账号可绑定模板作为默认风控阈值预设。
// 原本嵌在 SettingsIndex 里，现归到「通用模板」页统一管理。
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Star, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Card,
  CardContent,
  CardHeader,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import {
  createRateTemplate,
  deleteRateTemplate,
  listRateTemplates,
} from "@/api/system";
import { getErrMsg } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";

export function RateTemplates() {
  const qc = useQueryClient();

  const templatesQ = useQuery({
    queryKey: ["rate-templates"],
    queryFn: listRateTemplates,
  });

  const [newTplName, setNewTplName] = useState("");
  const [newTplDefault, setNewTplDefault] = useState(false);

  const createMut = useMutation({
    mutationFn: () =>
      createRateTemplate({ name: newTplName.trim(), is_default: newTplDefault }),
    onSuccess: () => {
      toast.success("已创建");
      setNewTplName("");
      setNewTplDefault(false);
      qc.invalidateQueries({ queryKey: ["rate-templates"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteRateTemplate(id),
    onSuccess: () => {
      toast.success("已删除");
      qc.invalidateQueries({ queryKey: ["rate-templates"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  return (
    <Card>
      <CardHeader>
        <SectionHeader
          title="风控模板"
          description="一组阈值（每秒 / 每分钟 / 每小时 / 每日 API 调用上限）。被账号绑定后作为默认起点；单条规则的精细调整在账号详情 → 风控基础。"
          meta={
            <SignalPill
              tone={(templatesQ.data?.length ?? 0) > 0 ? "primary" : "neutral"}
              label="模板数"
              value={templatesQ.data?.length ?? 0}
            />
          }
        />
      </CardHeader>
      <CardContent className="space-y-4">
        {/* 新建表单 */}
        <div className="flex flex-wrap items-end gap-2">
          <div className="min-w-[12rem] flex-1 space-y-1.5">
            <Label>模板名称</Label>
            <Input
              placeholder="例如：conservative"
              value={newTplName}
              onChange={(e) => setNewTplName(e.target.value)}
              maxLength={50}
            />
          </div>
          <div className="flex items-center gap-2 pb-2">
            <Switch
              checked={newTplDefault}
              onCheckedChange={setNewTplDefault}
              id="rateNewDefault"
            />
            <Label htmlFor="rateNewDefault" className="cursor-pointer">
              设为默认
            </Label>
          </div>
          <Button
            onClick={() => createMut.mutate()}
            disabled={!newTplName.trim() || createMut.isPending}
          >
            <Plus className="mr-1 h-4 w-4" /> 新建
          </Button>
        </div>

        {/* 列表 */}
        {templatesQ.isLoading ? (
          <div className="flex h-16 items-center justify-center">
            <Spinner className="text-primary" />
          </div>
        ) : templatesQ.data && templatesQ.data.length > 0 ? (
          <ul className="divide-y rounded-md border">
            {templatesQ.data.map((t) => (
              <li
                key={t.id}
                className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="truncate font-medium">{t.name}</span>
                  {t.is_default ? (
                    <span className="inline-flex items-center gap-0.5 rounded bg-warning/15 px-1.5 py-0.5 text-xs text-warning">
                      <Star className="h-3 w-3" /> 默认
                    </span>
                  ) : null}
                  <span className="text-xs text-muted-foreground">
                    创建于 {formatDateTime(t.created_at)}
                  </span>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={deleteMut.isPending}
                  onClick={() => {
                    if (!confirm(`确认删除模板「${t.name}」？`)) return;
                    deleteMut.mutate(t.id);
                  }}
                >
                  <Trash2 className="h-4 w-4 text-destructive" />
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="rounded-md border border-dashed py-6 text-center text-xs text-muted-foreground">
            尚无模板。新建一个后即可在账号详情中绑定
          </p>
        )}
      </CardContent>
    </Card>
  );
}
