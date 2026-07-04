# TelePilot 远程插件开发与安装指南

> 兼容入口已保留。正式正文请阅读 [TelePilot 远程插件](./PLUGIN-REMOTE.md) 和 [TelePilot 插件开发指南（索引）](./PLUGIN-DEV-GUIDE.md)。

远程插件、插件库维护插件和核心平台能力共用同一套 `Plugin` / `Manifest` / `PluginContext` 规范。远程插件额外需要 `plugin.json` 作为安装阶段静态元数据，并由 Git 仓库安装到 `plugins/installed/{name}/`；插件库维护插件同样通过 Web 安装到 `plugins/installed/{name}/` 后运行。

建议直接阅读：

- [TelePilot 远程插件](./PLUGIN-REMOTE.md)
- [TelePilot 插件 API 参考](./PLUGIN-API-REFERENCE.md)
- [TelePilot 插件安全边界](./PLUGIN-SAFETY.md)
- [TelePilot 插件概览](./PLUGIN-OVERVIEW.md)

保留这个文件只是为了兼容旧链接；后续内容维护请更新新的拆分文档。
