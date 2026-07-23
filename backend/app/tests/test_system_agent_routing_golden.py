"""System Agent 本地路由 golden set（纯离线，不调 LLM）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.system_agent.tool_routing import DOMAIN_CATALOG, route_locally
from app.services.system_agent.turn_context import is_retry_reference

_GOLDEN = Path(__file__).resolve().parent / "data" / "golden_routes.json"
_AVAILABLE = set(DOMAIN_CATALOG.keys())


def _classify(text: str) -> str:
    if is_retry_reference(text):
        return "reference_or_retry"
    route = route_locally(text, available=_AVAILABLE, memory_state={"last_domains": ["logs"]})
    if route is None:
        return "model_route"
    if route.reason == "general_help":
        return "general_help"
    if route.reason == "no_live_system_data_needed":
        return "no_tools"
    if route.source == "memory":
        return "reference_or_domain"
    if route.domains:
        # 取首域匹配 expect domain
        return route.domains[0]
    return "no_tools"


def test_golden_routes_cover_at_least_40_cases() -> None:
    cases = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert len(cases) >= 40


def test_golden_routes_match_expectations() -> None:
    cases = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    failures: list[str] = []
    for case in cases:
        text = case["text"]
        expect = case["expect"]
        got = _classify(text)
        domains = {
            "ledger", "logs", "accounts", "interaction", "system_ops", "providers",
            "plugins", "plugin_repos", "scheduler", "features", "commands", "routing",
            "rules", "system", "memory",
        }
        if expect in domains:
            if got != expect:
                failures.append(f"{text!r}: expect domain {expect}, got {got}")
        elif expect == "any_domain":
            if got not in domains and got not in {"reference_or_domain"}:
                failures.append(f"{text!r}: expect some domain, got {got}")
        elif expect == "general_help":
            if got != "general_help":
                failures.append(f"{text!r}: expect general_help, got {got}")
        elif expect == "no_tools":
            if got != "no_tools":
                failures.append(f"{text!r}: expect no_tools, got {got}")
        elif expect == "model_route":
            if got != "model_route":
                failures.append(f"{text!r}: expect model_route, got {got}")
        elif expect == "reference_or_retry":
            if got not in {"reference_or_retry", "reference_or_domain"}:
                failures.append(f"{text!r}: expect retry/ref, got {got}")
        elif expect == "reference_or_domain":
            if got not in {"reference_or_domain", "logs"}:
                failures.append(f"{text!r}: expect reference domain, got {got}")
        else:
            failures.append(f"unknown expect {expect} for {text!r}")
    assert not failures, "\n".join(failures)
