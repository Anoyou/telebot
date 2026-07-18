"""System Agent 按需领域技能。"""

from __future__ import annotations

from .catalog import BUILTIN_SKILLS
from .registry import MAX_SKILLS_PER_TURN, MAX_TOOLS_PER_TURN, SkillRegistry
from .spec import SkillSpec

_SKILL_REGISTRY: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry:
    global _SKILL_REGISTRY
    if _SKILL_REGISTRY is None:
        _SKILL_REGISTRY = SkillRegistry(BUILTIN_SKILLS)
    return _SKILL_REGISTRY


def reset_skill_registry_for_tests() -> None:
    global _SKILL_REGISTRY
    _SKILL_REGISTRY = None


__all__ = [
    "BUILTIN_SKILLS",
    "MAX_SKILLS_PER_TURN",
    "MAX_TOOLS_PER_TURN",
    "SkillRegistry",
    "SkillSpec",
    "get_skill_registry",
    "reset_skill_registry_for_tests",
]
