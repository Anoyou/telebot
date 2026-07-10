import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { toast } from "sonner";

import { listAccounts } from "@/api/accounts";
import { getEffectiveConfig, updateAccountFeatureConfig } from "@/api/features";
import type { SchedulerFeatureConfig } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Spinner } from "@/components/ui/misc";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { getErrMsg } from "@/lib/api";
import { PageShell } from "@/components/layout/PageScaffold";
import { PluginWorkspaceHeader } from "./WorkspaceHeader";

export function PluginsAutoCommandWhitelistPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const aidFromQuery = Number(searchParams.get("aid"));
  const aid = Number.isFinite(aidFromQuery) && aidFromQuery > 0 ? aidFromQuery : 0;
  const [whitelistText, setWhitelistText] = useState("");
  const [dirty, setDirty] = useState(false);

  const accountsQ = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  const schedulerCfgQ = useQuery({
    queryKey: ["features", aid, "scheduler", "config"],
    queryFn: () =>
      getEffectiveConfig(aid, "scheduler") as Promise<SchedulerFeatureConfig>,
    enabled: aid > 0,
  });

  const schedulerWhitelist = (
    schedulerCfgQ.data?.allowed_command_whitelist || []
  ).join("\n");

  useEffect(() => {
    if (!dirty) {
      setWhitelistText(schedulerWhitelist);
    }
  }, [schedulerWhitelist, dirty]);

  const saveMut = useMutation({
    mutationFn: async () => {
      const whitelist = whitelistText
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      await updateAccountFeatureConfig(aid, "scheduler", {
        allowed_command_whitelist: whitelist,
      });
    },
    onSuccess: async () => {
      setDirty(false);
      await schedulerCfgQ.refetch();
      toast.success("指令白名单已保存");
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  return (
    <PageShell>
      <PluginWorkspaceHeader activeTab="whitelist" selectedAid={aid || null} />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">选择账号</CardTitle>
          <CardDescription>
            白名单是账号级安全边界；只填写指令 key，不带系统指令前缀。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {accountsQ.isLoading ? (
            <div className="flex h-20 items-center justify-center">
              <Spinner className="text-primary" />
            </div>
          ) : accountsQ.data && accountsQ.data.length > 0 ? (
            <Select
              value={aid ? String(aid) : ""}
              onChange={(e) => setSearchParams({ aid: e.target.value })}
              className="w-full sm:w-80"
            >
              <option value="" disabled>请选择账号</option>
              {accountsQ.data.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.display_name || a.phone || `账号 #${a.id}`}
                </option>
              ))}
            </Select>
          ) : (
            <p className="text-sm text-muted-foreground">暂无可用账号，请先绑定账号。</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">允许自动触发的指令</CardTitle>
          <CardDescription>
            每行一个指令 key，例如 <code>help</code> 或模板指令名。未写入白名单的指令不会被自动动作执行。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Textarea
            value={whitelistText}
            onChange={(e) => {
              setWhitelistText(e.target.value);
              setDirty(true);
            }}
            placeholder={"测试\nhelp"}
            rows={8}
            disabled={!aid || schedulerCfgQ.isLoading}
          />
          <div className="flex justify-end">
            <Button
              onClick={() => saveMut.mutate()}
              disabled={!aid || !dirty || saveMut.isPending || schedulerCfgQ.isLoading}
            >
              {saveMut.isPending ? (
                <Spinner className="mr-2" />
              ) : (
                <Save className="mr-2 h-4 w-4" />
              )}
              保存白名单
            </Button>
          </div>
        </CardContent>
      </Card>
    </PageShell>
  );
}
