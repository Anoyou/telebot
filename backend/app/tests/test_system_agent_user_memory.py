from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models.system_agent import SystemAgentUserMemory
from app.services.system_agent.user_memory import (
    MAX_ITEMS_PER_SCOPE,
    create_memory,
    delete_memory,
    list_memories,
    prompt_block_for_scope,
    update_memory,
)


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: SystemAgentUserMemory.__table__.create(sync_conn, checkfirst=True)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_user_memory_crud_and_limit(db: AsyncSession) -> None:
    row = await create_memory(
        db, scope_type="web_user", scope_id=1, content="优先用简洁中文回复"
    )
    assert row.id is not None
    items = await list_memories(db, scope_type="web_user", scope_id=1)
    assert len(items) == 1
    await update_memory(
        db, memory_id=row.id, scope_type="web_user", scope_id=1, content="更简洁"
    )
    await db.refresh(row)
    assert row.content == "更简洁"
    block = await prompt_block_for_scope(db, scope_type="web_user", scope_id=1)
    assert "用户长期偏好" in block
    assert "更简洁" in block
    await delete_memory(db, memory_id=row.id, scope_type="web_user", scope_id=1)
    assert await list_memories(db, scope_type="web_user", scope_id=1) == []


@pytest.mark.asyncio
async def test_user_memory_rejects_secrets_and_enforces_cap(db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="密钥"):
        await create_memory(
            db,
            scope_type="web_user",
            scope_id=2,
            content="我的 key 是 sk-abcdefghijklmnopqrstuvwxyz123456",
        )
    for i in range(MAX_ITEMS_PER_SCOPE):
        await create_memory(db, scope_type="web_user", scope_id=2, content=f"偏好 {i}")
    with pytest.raises(ValueError, match="最多"):
        await create_memory(db, scope_type="web_user", scope_id=2, content="溢出")
