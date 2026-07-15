import { useNavigate } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

import { Button } from "@/components/ui/button";
import { goBackOr } from "@/lib/navigation";
import { AliasManagement } from "@/pages/Plugins/AliasManagement";

export function PluginsAliasesPage() {
  const nav = useNavigate();

  return (
    <div className="space-y-4">
      <Button variant="default" size="sm" className="gap-1.5 shadow-sm" onClick={() => goBackOr(nav, "/plugins")}>
        <ArrowLeft className="h-4 w-4" /> 返回上一页
      </Button>
      <AliasManagement />
    </div>
  );
}
