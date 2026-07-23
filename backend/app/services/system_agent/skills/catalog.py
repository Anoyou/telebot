"""TelePilot 内置领域技能目录。"""

from __future__ import annotations

from .spec import SkillSpec

BUILTIN_SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="interaction",
        description="查询、解释和维护交互 Bot 规则、触发条件与活跃会话。",
        domains=("interaction",),
        allowed_tools=(
            "interaction.list_rules",
            "interaction.get_rule",
            "interaction.list_active_sessions",
            "interaction.save_rule",
            "interaction.set_enabled",
            "interaction.delete_rule",
        ),
        instructions=(
            "先查询规则或会话的当前状态，再解释或提出变更。",
            "区分交互规则与通用 Rule；不要用 rules 工具代替交互规则工具。",
            "规则不明确时先列出候选项；写操作只生成待确认 Action。",
        ),
        examples=("交互里有哪些规则", "暂停这条交互规则", "查看当前活跃会话"),
        required_context=("账号", "规则 ID 或可唯一识别的规则"),
    ),
    SkillSpec(
        name="scheduler",
        description="查询、创建、修改和立即执行定时任务。",
        domains=("scheduler",),
        allowed_tools=(
            "scheduler.list",
            "scheduler.save",
            "scheduler.get",
            "scheduler.set_enabled",
            "scheduler.execute_now",
            "scheduler.delete",
        ),
        instructions=(
            "先读取任务现状；创建或修改时明确时区、调度类型和目标账号。",
            "解释今天、明天或几点时以系统时区为准。",
            "立即执行、启停、删除和保存都属于写操作，只生成待确认 Action。",
        ),
        examples=("今晚有哪些定时任务", "每天九点执行这条任务", "立即运行这个计划"),
        required_context=("账号", "任务 ID 或任务名称", "时间与时区"),
    ),
    SkillSpec(
        name="ai-config",
        description="管理模型 Provider、自定义指令以及 auto/fixed AI 路由。",
        domains=("providers", "commands", "routing"),
        allowed_tools=(
            "providers.list",
            "providers.save",
            "providers.verify",
            "providers.delete",
            "commands.list",
            "commands.save",
            "commands.set_enabled_for_accounts",
            "commands.delete",
            "routing.list_ai_commands",
            "routing.set_command_mode",
            "routing.preview",
        ),
        instructions=(
            "先查询现有 Provider、指令或路由，再决定需要修改的对象。",
            "保存 Provider 前必须验证；任何回复都不得复述 API Key 或掩码。",
            "区分指令模板配置与 auto/fixed 路由模式，写操作只生成待确认 Action。",
        ),
        examples=("有哪些 Provider", "修改这条 AI 指令", "把这个指令切到 fixed"),
        required_context=("Provider 或指令", "目标账号范围", "路由模式"),
    ),
    SkillSpec(
        name="plugins",
        description="查询和维护插件包、插件仓库及账号级插件启停状态。",
        domains=("plugins", "plugin_repos", "features"),
        allowed_tools=(
            "plugins.list_installed",
            "plugins.install",
            "plugins.get",
            "plugins.check_updates",
            "plugins.update",
            "plugins.set_package_enabled",
            "plugins.uninstall",
            "plugin_repos.list",
            "plugin_repos.list_plugins",
            "plugin_repos.list_official",
            "plugin_repos.install_plugin",
            "plugin_repos.create",
            "plugin_repos.delete",
            "features.get_account_status",
            "features.set_enabled",
        ),
        instructions=(
            "先区分全局插件包状态、插件仓库和账号级功能状态。",
            "安装或更新前先查询当前版本与可用更新；账号启停使用 features。",
            "安装、更新、卸载和启停只生成待确认 Action。",
        ),
        examples=("检查插件更新", "从官方仓库安装插件", "给这个账号停用插件"),
        required_context=("插件 key", "仓库", "账号"),
    ),
    SkillSpec(
        name="product",
        description="查询 TelePilot 产品更新日志和界面入口。",
        domains=("product",),
        allowed_tools=("product.get_changelog",),
        instructions=(
            "‘更新日志’、‘版本更新’指产品发布说明，不是运行时日志。",
            "回答时同时给出最近版本变化和桌面端/移动端入口。",
        ),
        examples=("看看更新日志", "这版更新了什么", "移动端在哪里看版本更新"),
        required_context=("版本范围（可省略，默认最近 4 个版本）",),
    ),
    SkillSpec(
        name="diagnostics",
        description="读取日志、错误事件和系统健康信息并定位运行问题。",
        domains=("logs", "system"),
        allowed_tools=(
            "logs.recent",
            "logs.search_errors",
            "logs.get_event_detail",
            "system.get_health",
            "system.get_context",
            "system.check_update",
            "system.apply_update",
            "system.restart",
        ),
        instructions=(
            "先读取最近日志或健康状态，再按错误线索查询事件详情。",
            "区分已观察到的事实与推断，并说明业务是否发生变化。",
            "系统更新或重启属于写操作，只生成待确认 Action。",
        ),
        examples=("看看最近报错", "为什么任务失败", "检查系统健康状态"),
        required_context=("时间范围", "账号或运行对象", "错误或事件线索"),
    ),
)


__all__ = ["BUILTIN_SKILLS"]
