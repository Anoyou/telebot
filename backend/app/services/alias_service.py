"""命令别名管理服务。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.command import CommandAlias
from ..schemas.alias import CommandAliasCreate, CommandAliasUpdate


async def get_aliases(db: AsyncSession, account_id: int | None = None) -> list[CommandAlias]:
    """获取命令别名列表。"""
    stmt = select(CommandAlias)
    if account_id:
        stmt = stmt.where(
            (CommandAlias.account_id == account_id) | (CommandAlias.account_id.is_(None))
        )
    stmt = stmt.order_by(CommandAlias.id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_alias(db: AsyncSession, alias_id: int) -> CommandAlias | None:
    """获取单个命令别名。"""
    stmt = select(CommandAlias).where(CommandAlias.id == alias_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def create_alias(
    db: AsyncSession,
    data: CommandAliasCreate,
    *,
    commit: bool = True,
) -> CommandAlias:
    """创建命令别名。"""
    alias = CommandAlias(
        alias=data.alias,
        target=data.target,
        account_id=data.account_id,
    )
    db.add(alias)
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(alias)
    return alias


async def update_alias(
    db: AsyncSession,
    alias_id: int,
    data: CommandAliasUpdate,
    *,
    commit: bool = True,
) -> CommandAlias | None:
    """更新命令别名。"""
    alias = await get_alias(db, alias_id)
    if not alias:
        return None

    alias.target = data.target
    if data.account_id is not None:
        alias.account_id = data.account_id

    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(alias)
    return alias


async def delete_alias(
    db: AsyncSession,
    alias_id: int,
    *,
    commit: bool = True,
) -> bool:
    """删除命令别名。"""
    alias = await get_alias(db, alias_id)
    if not alias:
        return False

    await db.delete(alias)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return True
