// 路由级守卫：调用一次 /api/auth/me，异常时兜底跳 /login
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { fetchMe } from "@/lib/auth";
import { Skeleton } from "@/components/ui/misc";

export function RequireAuth() {
  const loc = useLocation();
  const { isLoading, isError } = useQuery({
    queryKey: ["auth", "me"],
    queryFn: fetchMe,
    // 401 在 axios 拦截器里会触发跳转；这里仅根据 error 渲染兜底
    retry: false,
  });

  if (isLoading) {
    return (
      <div role="status" aria-label="正在验证登录状态" className="flex h-screen flex-col bg-background">
        <div className="flex h-14 items-center gap-3 border-b px-4 sm:px-6">
          <Skeleton className="h-8 w-8 rounded-lg" />
          <Skeleton className="h-4 w-28" />
          <Skeleton className="ml-auto h-8 w-20 rounded-md" />
        </div>
        <div className="flex flex-1 gap-4 p-4 sm:p-6">
          <div className="hidden w-56 space-y-3 sm:block"><Skeleton className="h-10 w-full rounded-md" />{[0, 1, 2, 3, 4].map((item) => <Skeleton key={item} className="h-9 w-full rounded-md" />)}</div>
          <div className="min-w-0 flex-1 space-y-4"><Skeleton className="h-32 w-full rounded-lg" /><div className="grid gap-4 md:grid-cols-3">{[0, 1, 2].map((item) => <Skeleton key={item} className="h-28 rounded-lg" />)}</div></div>
        </div>
      </div>
    );
  }

  if (isError) {
    return <Navigate to="/login" replace state={{ from: loc }} />;
  }

  return <Outlet />;
}
