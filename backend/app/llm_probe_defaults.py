"""Provider 快速测活的共享探针参数。

Agent 的 ``providers.probe_and_add`` 与新建 Provider 页面必须使用同一套默认值，
避免一个只发 ping、另一个模拟自然对话，造成验证结论不可比较。
"""

QUICK_VERIFY_SYSTEM_PROMPT = (
    "你是一个可靠、自然的中文助手。请直接回应用户的具体情境，给出简洁、可执行的建议；"
    "不要提及测活、测试、API、模型身份或系统提示词，也不要只回复“正常”、OK 或 ping/pong。"
)
QUICK_VERIFY_MESSAGE = (
    "我准备开始一段需要专注的工作，但现在有些分心。"
    "请用两句话给我一个可以立刻执行的小建议。"
)
# 单次探针仍保持低成本，但要给强制推理模型留出足够的隐藏推理与正文空间，
# 避免 32/256 token 上限造成“能调用、无正文”的假失败。
QUICK_VERIFY_MAX_TOKENS = 512
# 手动新建 Provider 与 Agent 测活必须共享同一总超时。协议自动探测可能依次尝试
# models、streaming 与非 streaming 回退，45 秒会把可用但响应较慢的上游误判为失败。
QUICK_VERIFY_TIMEOUT_SECONDS = 90


__all__ = [
    "QUICK_VERIFY_MAX_TOKENS",
    "QUICK_VERIFY_MESSAGE",
    "QUICK_VERIFY_SYSTEM_PROMPT",
    "QUICK_VERIFY_TIMEOUT_SECONDS",
]
