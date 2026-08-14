"""平台看树只读接口。"""

from fastapi import APIRouter

from ..deps import CurrentUser, DBSession
from ..schemas.platform_tree import PlatformTreeOut
from ..services.platform_tree_service import build_platform_tree

router = APIRouter(prefix="/api/platform", tags=["platform-tree"])


@router.get("/tree", response_model=PlatformTreeOut)
async def get_platform_tree(db: DBSession, _user: CurrentUser) -> PlatformTreeOut:
    return PlatformTreeOut.model_validate(await build_platform_tree(db))


__all__ = ["router"]
