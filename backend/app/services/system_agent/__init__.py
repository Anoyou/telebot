"""System Agent：平台级自然语言助手。

阶段 1 提供只读查询；阶段 2 起引入 Action 与写操作。
"""

from .service import SystemAgentService, get_system_agent_service

__all__ = [
    "SystemAgentService",
    "get_system_agent_service",
]
