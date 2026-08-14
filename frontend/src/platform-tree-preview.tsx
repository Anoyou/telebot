import { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";

import type { PlatformTree } from "@/api/types";
import { PlatformTreeView } from "@/pages/Settings/PlatformTreeView";
import "@/index.css";

// 免登录设计预览：镜像一台真实部署的看树数据，用于视觉迭代。
// 与 assistant-pet-states-preview 同一模式，仅 dev server 可访问。

function buildFixture(options: { safeWatch: boolean; killSwitch: boolean; empty: boolean }): PlatformTree {
  const leaves: PlatformTree["leaves"] = options.empty
    ? []
    : [
        { key: "ai-chat", attachment: "命令", enabled: false, requires: ["ai"], warnings: [], source_missing: false },
        {
          key: "ai_redpacket",
          attachment: "交互",
          enabled: true,
          requires: ["ai", "interaction_bot", "ledger"],
          warnings: [],
          source_missing: false,
        },
        { key: "bot_mute_guard", attachment: "命令", enabled: true, requires: [], warnings: [], source_missing: false },
        {
          key: "codex_image",
          attachment: "命令",
          enabled: false,
          requires: [],
          warnings: ["本地未找到插件源码，仅存安装记录"],
          source_missing: true,
        },
        {
          key: "dice_grid_hunt",
          attachment: "交互",
          enabled: true,
          requires: ["interaction_bot", "ledger"],
          warnings: [],
          source_missing: false,
        },
        { key: "forward", attachment: "命令", enabled: false, requires: [], warnings: [], source_missing: false },
        {
          key: "game24",
          attachment: "交互",
          enabled: true,
          requires: ["interaction_bot", "ledger"],
          warnings: [],
          source_missing: false,
        },
        {
          key: "legacy_demo",
          attachment: "命令",
          enabled: true,
          requires: [],
          warnings: ["存量插件未声明 requires_platform_capabilities"],
          source_missing: false,
        },
        { key: "lucky_redpack", attachment: "命令", enabled: true, requires: ["ledger"], warnings: [], source_missing: false },
        {
          key: "math10",
          attachment: "交互",
          enabled: true,
          requires: ["interaction_bot", "ledger"],
          warnings: [],
          source_missing: false,
        },
        { key: "pt_promote", attachment: "交互", enabled: true, requires: ["interaction_bot"], warnings: [], source_missing: false },
        { key: "random_benefit", attachment: "直通", enabled: false, requires: [], warnings: [], source_missing: false },
        {
          key: "redpack-byRBQ",
          attachment: "交互",
          enabled: false,
          requires: ["interaction_bot", "ledger"],
          warnings: [],
          source_missing: false,
        },
        { key: "scheduler", attachment: "命令", enabled: true, requires: [], warnings: [], source_missing: false },
        { key: "sum", attachment: "命令", enabled: true, requires: ["ai"], warnings: [], source_missing: false },
        {
          key: "ten_half",
          attachment: "交互",
          enabled: true,
          requires: ["interaction_bot", "ledger"],
          warnings: [],
          source_missing: false,
        },
      ];

  const demanded = (key: string) =>
    leaves.filter((leaf) => leaf.requires.includes(key as never)).map((leaf) => leaf.key);

  const branch = (key: string, desired: boolean, forcedOff = false) => ({
    state: (desired ? "ready" : "stopped") as PlatformTree["branches"]["ai"]["state"],
    desired,
    forced_off: forcedOff,
    demanded_by: demanded(key),
    can_turn_off: demanded(key).length === 0,
  });

  const watching = options.safeWatch;
  return {
    trunk: {
      userbot: {
        workers: [
          { account_id: 1, pid: 101, alive: true, desired: "running", fail_count: 0, queued: false, starting: false },
          { account_id: 2, pid: 102, alive: true, desired: "running", fail_count: 0, queued: false, starting: false },
        ],
        total: 2,
        alive: 2,
      },
      kill_switch: options.killSwitch,
      current_profile: watching ? "safe_watch" : "custom",
    },
    branches: {
      ai: branch("ai", !watching),
      interaction_bot: branch("interaction_bot", !watching, watching),
      webhooks: branch("webhooks", false),
      ledger: branch("ledger", true),
      dispatch_debug: branch("dispatch_debug", false),
    } as PlatformTree["branches"],
    leaves,
  };
}

function PreviewApp() {
  const [dark, setDark] = useState(true);
  const [safeWatch, setSafeWatch] = useState(false);
  const [killSwitch, setKillSwitch] = useState(false);
  const [empty, setEmpty] = useState(false);
  const [narrow, setNarrow] = useState(false);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  const tree = buildFixture({ safeWatch, killSwitch, empty });

  const toggle = (label: string, value: boolean, onChange: (next: boolean) => void) => (
    <button
      type="button"
      onClick={() => onChange(!value)}
      className={
        "rounded-md border px-2.5 py-1 text-xs transition-colors " +
        (value
          ? "border-success/50 bg-success/10 text-success"
          : "border-border bg-muted/20 text-muted-foreground hover:text-foreground")
      }
    >
      {label} {value ? "开" : "关"}
    </button>
  );

  return (
    <div className="min-h-screen bg-background p-6 text-foreground">
      <div className="mx-auto max-w-4xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="mr-auto text-sm font-semibold">看树视图 · 设计预览</h1>
          {toggle("值守", safeWatch, setSafeWatch)}
          {toggle("总闸拉闸", killSwitch, setKillSwitch)}
          {toggle("空叶态", empty, setEmpty)}
          {toggle("窄屏 375px", narrow, setNarrow)}
          {toggle("暗色", dark, setDark)}
        </div>
        <div
          className="rounded-lg border bg-card p-4 text-card-foreground shadow-sm"
          style={narrow ? { width: 375 - 48 } : undefined}
        >
          <PlatformTreeView tree={tree} />
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<PreviewApp />);
