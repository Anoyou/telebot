from app.worker.plugins.base import Plugin, PluginContext, register


@register
class ContextEchoPlugin(Plugin):
    key = "context_echo"
    display_name = "Context Echo"
    message_channels = {"incoming"}
    owner_only = True

    async def on_startup(self, ctx: PluginContext) -> None:
        # 配置与账号范围：都从 PluginContext 读取，不跨账号查询
        self._command = str(ctx.config.get("command") or "cecho").strip() or "cecho"

        async def _handler(client, event, args: list[str], account_id: int, runtime_ctx: PluginContext) -> None:
            # 运行时访问：只用公开上下文对象；第三方模块应把 engine/redis 视为可选能力
            await runtime_ctx.log(
                "info",
                "context_echo.triggered",
                account_id=account_id,
                feature=runtime_ctx.feature_key,
            )
            payload = " ".join(args) if args else "ok"
            # 旧命令 hook 只用于兼容触发和日志演示；新插件发送消息请参考
            # examples/plugins/event_bus_demo，通过 ctx.messages 或标准 action 交给平台执行。
            await runtime_ctx.log(
                "info",
                "context_echo.payload",
                account_id=account_id,
                payload=payload,
            )

        self.commands = {self._command: _handler}
