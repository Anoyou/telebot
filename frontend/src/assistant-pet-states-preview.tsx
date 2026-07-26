import React, { useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import ReactDOM from "react-dom/client";
import {
  ArrowLeft,
  ArrowRight,
  Bot,
  CircleCheck,
  CircleX,
  Eye,
  Menu,
  Moon,
  PanelRightClose,
  Pause,
  Play,
  Send,
  Smartphone,
  Sun,
} from "lucide-react";

import { AssistantPetSprite } from "@/components/assistant/AssistantPet";
import { assistantPetLookPhase } from "@/components/assistant/assistantPetAnimation";
import "@/index.css";

type PreviewState =
  | "docked"
  | "idle"
  | "look"
  | "running-right"
  | "running-left"
  | "working"
  | "complete"
  | "failed"
  | "pwa";

const PREVIEW_STATES = [
  { id: "docked", label: "贴边", detail: "上半身招手", icon: PanelRightClose },
  { id: "idle", label: "待机", detail: "完整静止", icon: Pause },
  { id: "look", label: "注视", detail: "跟随鼠标", icon: Eye },
  { id: "running-right", label: "右拖", detail: "8 帧右跑", icon: ArrowRight },
  { id: "running-left", label: "左拖", detail: "镜像左跑", icon: ArrowLeft },
  { id: "working", label: "工作中", detail: "专注动作", icon: Play },
  { id: "complete", label: "已完成", detail: "完整跳跃", icon: CircleCheck },
  { id: "failed", label: "失败", detail: "异常反馈", icon: CircleX },
  { id: "pwa", label: "PWA", detail: "圆形入口", icon: Smartphone },
] satisfies Array<{
  id: PreviewState;
  label: string;
  detail: string;
  icon: typeof Pause;
}>;

const WORKSPACE_ITEMS = [
  { icon: Bot, label: "系统助手", active: true },
  { icon: CircleCheck, label: "任务记录", active: false },
  { icon: Eye, label: "运行日志", active: false },
] as const;

function DesktopPet({
  state,
  lookDirection,
  shellRef,
}: {
  state: Exclude<PreviewState, "pwa">;
  lookDirection: number | null;
  shellRef: React.RefObject<HTMLDivElement>;
}) {
  const docked = state === "docked";
  const complete = state === "complete";
  const failed = state === "failed";
  const runningRight = state === "running-right";
  const runningLeft = state === "running-left";
  const leftSide = runningLeft;

  return (
    <div
      ref={shellRef}
      data-preview-production-pet
      data-preview-state={state}
      data-side={leftSide ? "left" : "right"}
      className={[
        "assistant-pet absolute z-20 h-[114px] w-[102px] select-none",
        docked ? "right-[-28px] top-[44%]" : leftSide ? "left-[14%] top-[46%]" : "right-8 top-[46%]",
        complete ? "assistant-pet-complete" : "",
        failed ? "assistant-pet-failed" : "",
      ].join(" ")}
    >
      {complete || failed ? (
        <span
          aria-hidden="true"
          data-assistant-pet-notice={failed ? "failed" : "complete"}
          className="assistant-pet-notice"
        >
          {failed ? "出错了" : "任务完成啦"}
        </span>
      ) : null}
      <AssistantPetSprite
        active={docked}
        celebrating={complete}
        failed={failed}
        peeking={docked}
        streaming={state === "working"}
        dragDirection={runningLeft ? "left" : runningRight ? "right" : null}
        lookDirection={state === "look" ? lookDirection : null}
      />
    </div>
  );
}

function PwaPet() {
  return (
    <div className="absolute inset-x-0 bottom-5 z-20 flex justify-center px-3">
      <div className="flex w-full max-w-[23rem] items-center justify-between rounded-full bg-card/95 p-2 shadow-lg ring-1 ring-border">
        <button type="button" className="grid h-10 w-10 place-items-center rounded-full text-muted-foreground" aria-label="菜单">
          <Menu className="h-5 w-5" />
        </button>
        <span className="h-8 w-px bg-border" aria-hidden="true" />
        <button
          type="button"
          data-assistant-mobile-button
          aria-label="打开系统助手"
          aria-expanded="false"
          className="assistant-nav-orb liquid-bottom-nav relative grid h-[3.75rem] w-[3.75rem] shrink-0 content-center place-items-center rounded-full text-primary active:scale-95 motion-reduce:transform-none"
        >
          <span className="relative grid place-items-center">
            <AssistantPetSprite compact active />
          </span>
        </button>
        <span className="h-8 w-px bg-border" aria-hidden="true" />
        <button type="button" className="grid h-10 w-10 place-items-center rounded-full text-muted-foreground" aria-label="发送">
          <Send className="h-5 w-5" />
        </button>
      </div>
    </div>
  );
}

function PreviewApp() {
  const [state, setState] = useState<PreviewState>("complete");
  const [lookDirection, setLookDirection] = useState<number | null>(4);
  const [dark, setDark] = useState(false);
  const petShellRef = useRef<HTMLDivElement>(null);
  const selected = useMemo(() => PREVIEW_STATES.find((item) => item.id === state) ?? PREVIEW_STATES[0], [state]);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
  }, [dark]);

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (state !== "look" || !petShellRef.current) return;
    const rect = petShellRef.current.getBoundingClientRect();
    setLookDirection(assistantPetLookPhase(event.clientX, event.clientY, rect));
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-background text-foreground">
      <header className="flex h-16 shrink-0 items-center justify-between border-b bg-card px-4 shadow-sm sm:px-5">
        <div className="flex min-w-0 items-center gap-3">
          <img src="/pwa-192x192.png" alt="" className="h-9 w-9 rounded-md" width="36" height="36" />
          <div className="min-w-0">
            <div className="truncate text-[17px] font-semibold">TelePilot</div>
            <div className="truncate text-xs text-muted-foreground">Agent 实装动作</div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="hidden items-center gap-1.5 text-xs text-muted-foreground sm:inline-flex">
            <span className="h-2 w-2 rounded-full bg-emerald-500" />
            生产组件
          </span>
          <button
            type="button"
            onClick={() => setDark((value) => !value)}
            className="grid h-10 w-10 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground active:scale-95"
            aria-label={dark ? "切换亮色" : "切换深色"}
            title={dark ? "切换亮色" : "切换深色"}
          >
            {dark ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-56 shrink-0 flex-col border-r bg-card px-3 py-4 md:flex">
          <div className="mb-2 px-3 text-[11px] font-medium text-muted-foreground">工作台</div>
          {WORKSPACE_ITEMS.map(({ icon: Icon, label, active }) => (
            <div
              key={label}
              className={`flex h-10 items-center gap-3 rounded-md px-3 text-sm ${active ? "bg-muted font-medium text-foreground" : "text-muted-foreground"}`}
            >
              <Icon className="h-[18px] w-[18px]" />
              <span>{label}</span>
            </div>
          ))}
          <div className="mt-auto border-t px-3 pt-4 text-xs text-muted-foreground">
            {selected.label} · {selected.detail}
          </div>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col p-2.5 sm:p-4">
          <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg bg-card shadow-md ring-1 ring-border">
            <div className="shrink-0 border-b px-3 py-3 sm:px-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                  <h1 className="text-base font-semibold">系统助手</h1>
                  <p className="text-xs text-muted-foreground">{selected.detail}</p>
                </div>
                <span className="rounded-md bg-muted px-2 py-1 font-mono text-[10px] text-muted-foreground">
                  {selected.id}
                </span>
              </div>
              <div role="tablist" aria-label="Agent 动作状态" className="horizontal-scroll-touch flex gap-1 overflow-x-auto pb-1">
                {PREVIEW_STATES.map((item) => {
                  const Icon = item.icon;
                  const active = item.id === state;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      role="tab"
                      aria-selected={active}
                      onClick={() => setState(item.id)}
                      className={`flex h-9 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-colors active:scale-95 ${
                        active ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      <Icon className="h-4 w-4" />
                      {item.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div
              data-preview-stage
              onPointerMove={handlePointerMove}
              className="relative min-h-0 flex-1 overflow-hidden bg-background"
            >
              <div className="absolute inset-0 flex min-h-0">
                <div className="hidden w-52 shrink-0 border-r bg-card/70 p-3 sm:block">
                  <div className="mb-3 h-8 rounded-md bg-muted" />
                  <div className="space-y-2">
                    <div className="h-12 rounded-md bg-primary/10 ring-1 ring-primary/20" />
                    <div className="h-12 rounded-md bg-muted/70" />
                    <div className="h-12 rounded-md bg-muted/70" />
                  </div>
                </div>
                <div className="relative min-w-0 flex-1">
                  <div className="absolute inset-x-0 top-0 flex h-12 items-center border-b bg-card/75 px-4 text-xs text-muted-foreground">
                    新建会话
                  </div>
                  <div className="absolute inset-x-4 bottom-4 h-12 rounded-lg bg-card shadow-sm ring-1 ring-border sm:left-8 sm:right-8" />
                  <div className="absolute left-1/2 top-[42%] -translate-x-1/2 text-center text-muted-foreground/70">
                    <Bot className="mx-auto mb-2 h-8 w-8" />
                    <div className="text-sm">Agent 会话</div>
                  </div>
                </div>
              </div>

              {state === "pwa" ? (
                <PwaPet />
              ) : (
                <DesktopPet
                  state={state}
                  lookDirection={lookDirection}
                  shellRef={petShellRef}
                />
              )}
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}

type PreviewRootHost = HTMLElement & {
  __assistantPetPreviewRoot?: ReturnType<typeof ReactDOM.createRoot>;
};

const previewRootHost = document.getElementById("root") as PreviewRootHost;
const previewRoot = previewRootHost.__assistantPetPreviewRoot ?? ReactDOM.createRoot(previewRootHost);
previewRootHost.__assistantPetPreviewRoot = previewRoot;

previewRoot.render(
  <React.StrictMode>
    <PreviewApp />
  </React.StrictMode>,
);
