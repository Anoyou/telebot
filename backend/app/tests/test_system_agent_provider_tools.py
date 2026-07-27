from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.system_agent.context import ToolContext
from app.services.system_agent.registry import ToolRegistry
from app.services.system_agent.tools.providers import list_providers, register


def _empty_result() -> SimpleNamespace:
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected_where_count"),
    (({"id": 0}, 0), ({"id": 0, "name": "muyuan"}, 1)),
)
async def test_list_providers_treats_non_positive_id_as_absent(
    args: dict[str, object], expected_where_count: int
) -> None:
    db = AsyncMock()
    db.execute.return_value = _empty_result()

    await list_providers(ToolContext(db=db, channel="web", role="viewer"), args)

    statement = db.execute.await_args.args[0]
    assert len(statement._where_criteria) == expected_where_count


def test_provider_list_schema_rejects_non_positive_ids() -> None:
    registry = ToolRegistry()
    register(registry)

    spec = next(item for item in registry.list_all() if item.name == "providers.list")
    assert spec.input_schema["properties"]["id"]["minimum"] == 1
