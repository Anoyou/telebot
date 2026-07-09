"""with_ai_components 示例模块 manifest。"""

from __future__ import annotations

from app.worker.plugins.manifest import Manifest

MANIFEST = Manifest(
    key="with_ai_components",
    display_name="AI 玩法组件示例",
    version="0.1.0",
    author="examples",
    description="演示用 QuizMaker + AnswerJudge 组件跑一个最小问答局，AI 不可用时自动降级。",
    usage="账号命令 quiz_new [主题] 出题、quiz_answer 答案 判题；无可用 AI 时走内置题库与规则判定。",
    category="interactive",
    permissions=["ai_text", "edit_message"],
    config_schema={
        "type": "object",
        "x-ui-mode": "single",
        "additionalProperties": False,
        "properties": {
            "provider_tag": {
                "type": "string",
                "title": "Provider 标签",
                "default": "chat",
                "description": "可选：出题/判题时优先选择带该 tag 的可用 provider。",
            },
            "default_topic": {
                "type": "string",
                "title": "默认主题",
                "default": "常识",
                "description": "quiz_new 不带参数时使用的默认出题主题。",
            },
        },
    },
)

__all__ = ["MANIFEST"]
