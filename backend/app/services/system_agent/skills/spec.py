"""System Agent 领域技能定义。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillSpec:
    """只描述领域流程，不复制工具参数或业务校验。"""

    name: str
    description: str
    domains: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    instructions: tuple[str, ...]
    examples: tuple[str, ...] = ()
    required_context: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("skill name required")
        if not self.domains:
            raise ValueError(f"skill {self.name} requires at least one domain")
        if not self.allowed_tools:
            raise ValueError(f"skill {self.name} requires at least one allowed tool")

    def render_prompt(self) -> str:
        """渲染当前轮需要的最小技能上下文。"""

        lines = [f"### {self.name}", self.description]
        if self.required_context:
            lines.append(
                "仅在完成当前请求确实需要时补齐上下文："
                f"{'、'.join(self.required_context)}。"
            )
        lines.append("处理要求：")
        lines.extend(f"- {instruction}" for instruction in self.instructions)
        if self.examples:
            lines.append(f"典型请求：{'；'.join(self.examples)}。")
        return "\n".join(lines)


__all__ = ["SkillSpec"]
