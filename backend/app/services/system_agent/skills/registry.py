"""领域技能选择、提示渲染和工具收窄。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ..registry import ToolSpec
from ..tool_routing import ToolRoute, tool_domain
from .spec import SkillSpec

MAX_SKILLS_PER_TURN = 2
MAX_TOOLS_PER_TURN = 8


class SkillRegistry:
    """按 ToolRoute 渐进披露领域流程。"""

    def __init__(self, skills: Iterable[SkillSpec] = ()) -> None:
        self._skills = tuple(skills)
        names = [skill.name for skill in self._skills]
        if len(names) != len(set(names)):
            raise ValueError("duplicate skill name")

    def list_all(self) -> tuple[SkillSpec, ...]:
        return self._skills

    def select(
        self,
        route: ToolRoute,
        *,
        limit: int = MAX_SKILLS_PER_TURN,
    ) -> tuple[SkillSpec, ...]:
        """按路由领域顺序、重合度与目录顺序稳定选择技能。"""

        if not route.domains or limit <= 0:
            return ()
        route_order = {domain: index for index, domain in enumerate(route.domains)}
        candidates: list[tuple[int, int, int, SkillSpec]] = []
        for catalog_index, skill in enumerate(self._skills):
            matching = set(skill.domains).intersection(route_order)
            if not matching:
                continue
            first_domain = min(route_order[domain] for domain in matching)
            candidates.append((first_domain, -len(matching), catalog_index, skill))
        candidates.sort(key=lambda item: item[:3])
        return tuple(item[3] for item in candidates[: min(limit, MAX_SKILLS_PER_TURN)])

    def narrow_tools(
        self,
        routed_specs: Sequence[ToolSpec],
        selected: Sequence[SkillSpec],
        *,
        limit: int = MAX_TOOLS_PER_TURN,
    ) -> list[ToolSpec]:
        """只从路由已允许的工具中筛选，绝不扩大权限。"""

        if limit <= 0:
            return []
        capped_limit = min(limit, MAX_TOOLS_PER_TURN)
        if not selected:
            return list(routed_specs[:capped_limit])

        routed_by_name = {spec.name: spec for spec in routed_specs}
        selected_domains = {domain for skill in selected for domain in skill.domains}
        catalog_domains = {domain for skill in self._skills for domain in skill.domains}

        buckets: list[list[ToolSpec]] = []
        for skill in selected:
            bucket = [
                routed_by_name[name]
                for name in skill.allowed_tools
                if name in routed_by_name
                and tool_domain(routed_by_name[name]) in selected_domains
            ]
            if bucket:
                buckets.append(bucket)

        # 尚未配置技能的旧领域保持兼容；已配置但本轮未选中的第三个技能领域不披露。
        uncovered = [
            spec for spec in routed_specs if tool_domain(spec) not in catalog_domains
        ]
        if uncovered:
            buckets.append(uncovered)

        narrowed: list[ToolSpec] = []
        seen: set[str] = set()
        while buckets and len(narrowed) < capped_limit:
            next_buckets: list[list[ToolSpec]] = []
            for bucket in buckets:
                while bucket and bucket[0].name in seen:
                    bucket.pop(0)
                if bucket:
                    spec = bucket.pop(0)
                    seen.add(spec.name)
                    narrowed.append(spec)
                if bucket:
                    next_buckets.append(bucket)
                if len(narrowed) >= capped_limit:
                    break
            buckets = next_buckets
        return narrowed

    def render_prompt(self, selected: Sequence[SkillSpec]) -> str:
        if not selected:
            return ""
        body = "\n\n".join(skill.render_prompt() for skill in selected)
        return (
            "## 当前按需领域技能\n"
            "仅使用下列技能处理当前请求；工具参数与业务校验以工具本身为准。\n\n"
            f"{body}"
        )

    def understanding_summary(self, selected: Sequence[SkillSpec]) -> str:
        descriptions = [skill.description.rstrip("。") for skill in selected]
        return "；".join(descriptions)


__all__ = ["MAX_SKILLS_PER_TURN", "MAX_TOOLS_PER_TURN", "SkillRegistry"]
