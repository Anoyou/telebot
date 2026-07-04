"""核心 builtin 兼容包索引。

不要在包入口导入所有插件实现；worker 会按账号启用项懒加载。
普通插件已经迁出 Core，由插件库分发；这里只保留平台能力兼容壳。
"""

__all__ = [
    "forward",
    "scheduler",
]
