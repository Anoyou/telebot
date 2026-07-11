"""功能矩阵与账号-功能开关 REST API（PRD §9.2）。

Endpoint：
  - GET  /api/feature-matrix                      → 一次返回 N×M 矩阵
  - GET  /api/accounts/{aid}/features            → 该账号所有 feature 开关
  - PATCH /api/accounts/{aid}/features/{key}     → 启停或调整 config
  - GET  /api/plugins/{key}/config                → 获取 global config
  - PUT  /api/plugins/{key}/config               → 设置 global config
  - GET  /api/accounts/{aid}/features/{key}/config → 获取最终生效配置
  - POST /api/plugins/{key}/config/validate       → 验证配置是否符合 schema
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Query

from ..db.models.account import Account
from ..db.models.feature import AccountFeature, Feature
from ..db.models.plugin import InstalledPlugin
from ..deps import CurrentUser, DBSession
from ..schemas.feature import (
    AccountFeatureConfigUpdate,
    AccountFeatureItem,
    AccountFeatureToggle,
    ConfigValidationResponse,
    FeatureMatrixResponse,
    PluginConfigActionJobResponse,
    PluginConfigActionRequest,
    PluginConfigActionResponse,
    PluginGlobalConfigResponse,
    PluginGlobalConfigUpdate,
)
from ..services import audit, feature_service
from ..services.plugin_config_action_jobs import (
    create_plugin_config_action_job,
    get_plugin_config_action_job,
    job_response,
    list_plugin_config_action_jobs,
)
from ..services.plugin_config_actions import (
    PluginConfigActionError,
    PluginConfigActionNotFound,
    PluginConfigActionUnavailable,
    run_plugin_config_action,
)
from ..services.redactor import is_sensitive_key, redact_value
from ..worker.plugins.ai_facade import AIQuotaError, AIUnavailableError
from ..worker.plugins.http_facade import PluginHTTPError

router = APIRouter(tags=["features"])


def _bad(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _declares_direct_passthrough(manifest: object) -> bool:
    if not isinstance(manifest, dict):
        return False
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    raw = capabilities.get("telegram_direct_passthrough")
    if raw is True:
        return True
    return isinstance(raw, dict) and raw.get("enabled") is True


def _allow_account_direct_passthrough_config(
    schema: dict[str, object],
    manifest: object,
) -> dict[str, object]:
    """Expose the platform-owned account opt-in only to declaring plugins."""

    if not _declares_direct_passthrough(manifest):
        return schema
    properties = schema.setdefault("properties", {})
    if isinstance(properties, dict):
        properties.setdefault(
            "direct_passthrough",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "enabled": {"type": "boolean", "default": False},
                },
            },
        )
    return schema


def _account_config_schema(
    manifest: object,
    config: dict[str, object],
) -> dict[str, object] | None:
    """Build the account schema and protect the platform-owned direct opt-in."""

    declares_direct = _declares_direct_passthrough(manifest)
    if "direct_passthrough" in config and not declares_direct:
        raise _bad(
            "CONFIG_VALIDATION_ERROR",
            "配置验证失败: direct_passthrough: 仅声明 telegram_direct_passthrough 的插件可使用此字段",
        )

    raw_schema = manifest.get("config_schema") if isinstance(manifest, dict) else None
    if isinstance(raw_schema, dict):
        scoped_schema = feature_service.config_schema_for_scope(raw_schema, "account")
    elif declares_direct:
        # A direct-only plugin still needs a schema for the platform-owned opt-in.
        scoped_schema = {"type": "object", "properties": {}}
    else:
        return None
    return _allow_account_direct_passthrough_config(scoped_schema, manifest)


def _chatgpt_token_id(token: str) -> str:
    value = str(token or "").strip()
    if not value:
        return "token:empty"
    return f"token:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:10]}"


def _chatgpt_mask_token(token: str) -> str:
    value = str(token or "").strip()
    if not value:
        return ""
    if len(value) <= 24:
        return f"{value[:4]}···{value[-4:]}"
    return f"{value[:10]}···{value[-10:]}"


def _chatgpt_token_entries(config: dict[str, object]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(token: object, note: object = "") -> None:
        value = str(token or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        entries.append({"token": value, "note": str(note or "").strip()})

    raw_tokens = config.get("tokens")
    if isinstance(raw_tokens, list):
        for item in raw_tokens:
            if isinstance(item, dict):
                add(
                    item.get("token") or item.get("accessToken") or item.get("access_token"),
                    item.get("note") or item.get("remark") or item.get("source") or "",
                )
            else:
                add(item)
    legacy = str(config.get("token") or "")
    for line in legacy.replace(",", "\n").splitlines():
        add(line)
    return entries


def _sanitize_chatgpt_image_config(config: dict[str, object]) -> dict[str, object]:
    entries = _chatgpt_token_entries(config)
    rest = dict(config)
    rest["token"] = ""
    rest["tokens"] = []
    sanitized = redact_value(rest)
    sanitized["tokens"] = [
        {
            "token": _chatgpt_mask_token(entry["token"]),
            "note": entry["note"],
            "token_id": _chatgpt_token_id(entry["token"]),
        }
        for entry in entries
    ]
    return sanitized


def _sanitize_config(config: dict[str, object], key: str | None = None) -> dict[str, object]:
    if key == "chatgpt_image":
        return _sanitize_chatgpt_image_config(config)
    return redact_value(config)


def _preserve_existing_sensitive_values(
    existing: dict[str, object] | None,
    incoming: dict[str, object],
    key: str | None = None,
) -> dict[str, object]:
    merged = dict(incoming)
    existing_dict = dict(existing or {})
    if key == "chatgpt_image":
        merged = _preserve_chatgpt_image_tokens(existing_dict, merged)
    for item_key, value in existing_dict.items():
        if key == "chatgpt_image" and item_key == "token" and "tokens" in merged:
            continue
        if not is_sensitive_key(str(item_key)):
            continue
        if item_key not in merged or merged.get(item_key) in ("", None, "***"):
            merged[item_key] = value
    return merged


def _preserve_chatgpt_image_tokens(
    existing: dict[str, object],
    incoming: dict[str, object],
) -> dict[str, object]:
    if "tokens" not in incoming or not isinstance(incoming.get("tokens"), list):
        return incoming
    existing_entries = _chatgpt_token_entries(existing)
    by_id = {_chatgpt_token_id(entry["token"]): entry["token"] for entry in existing_entries}
    by_mask = {_chatgpt_mask_token(entry["token"]): entry["token"] for entry in existing_entries}
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for idx, item in enumerate(incoming["tokens"]):
        if not isinstance(item, dict):
            token_value = str(item or "").strip()
            note = ""
            token_ref = ""
        else:
            token_value = str(item.get("token") or "").strip()
            note = str(item.get("note") or item.get("remark") or item.get("source") or "").strip()
            token_ref = str(item.get("token_id") or "").strip()
        token = by_id.get(token_ref) or by_mask.get(token_value) or token_value
        if token in {"", "***"} and idx < len(existing_entries):
            token = existing_entries[idx]["token"]
        if not token or token in {"***"} or token in seen:
            continue
        seen.add(token)
        normalized.append({"token": token, "note": note})
    merged = dict(incoming)
    merged["tokens"] = normalized
    merged["token"] = ""
    return merged


def _normalize_feature_config(key: str, config: dict[str, object]) -> dict[str, object]:
    normalized = dict(config)
    if key == "codex_image" and normalized.get("model") == "gpt-5.4":
        normalized["model"] = "gpt-5.5"
    return normalized


# ─────────────────────────────────────────────────────
# 矩阵
# ─────────────────────────────────────────────────────
@router.get("/api/feature-matrix", response_model=FeatureMatrixResponse)
async def get_feature_matrix(db: DBSession, _user: CurrentUser) -> FeatureMatrixResponse:
    """返回 N(账号) × M(功能) 矩阵。"""
    data = await feature_service.feature_matrix(db)
    return FeatureMatrixResponse(**data)


# ─────────────────────────────────────────────────────
# 单账号 feature 列表
# ─────────────────────────────────────────────────────
@router.get(
    "/api/accounts/{aid}/features",
    response_model=list[AccountFeatureItem],
)
async def list_account_features(
    aid: int, db: DBSession, _user: CurrentUser
) -> list[AccountFeatureItem]:
    """列出该账号所有 ``account_feature`` 行。"""
    if await db.get(Account, aid) is None:
        raise _bad("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    rows = await feature_service.get_account_features(db, aid)
    return [
        AccountFeatureItem(
            feature_key=r.feature_key,
            enabled=r.enabled,
            state=r.state,
            last_error=r.last_error,
            config=_sanitize_config(dict(r.config or {}), r.feature_key),
        )
        for r in rows
    ]


# ─────────────────────────────────────────────────────
# 启停 / 改 config
# ─────────────────────────────────────────────────────
@router.patch(
    "/api/accounts/{aid}/features/{key}",
    response_model=AccountFeatureItem,
)
async def patch_account_feature(
    aid: int,
    key: str,
    payload: AccountFeatureToggle,
    db: DBSession,
    user: CurrentUser,
) -> AccountFeatureItem:
    """启用 / 禁用某 feature，或更新它的 config。

    若 feature key 在 ``feature`` 表里没有登记，会拒绝（避免误开未知插件）。
    若提供了 config，会验证 JSON Schema 并保存。
    """
    if await db.get(Account, aid) is None:
        raise _bad("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    # 校验 feature 存在（首次调用矩阵或 list 时已 seed 过；这里也 seed 一次以幂等）
    await feature_service.seed_builtin_features(db)
    feature = await db.get(Feature, key)
    if feature is None:
        raise _bad("FEATURE_NOT_FOUND", f"未注册的 feature: {key}", 404)

    # 如果提供了 config，验证 JSON Schema
    if payload.config is not None:
        existing = await db.get(AccountFeature, (aid, key))
        payload.config = _preserve_existing_sensitive_values(
            dict(existing.config or {}) if existing is not None else None,
            dict(payload.config),
            key,
        )
        payload.config = _normalize_feature_config(key, payload.config)
        scoped_schema = _account_config_schema(feature.manifest, payload.config)
        if scoped_schema is not None:
            payload.config = feature_service.apply_required_config_defaults(
                payload.config,
                scoped_schema,
            )
            validation = feature_service.validate_config_against_schema(
                payload.config,
                scoped_schema,
            )
            if not validation.valid:
                raise _bad(
                    "CONFIG_VALIDATION_ERROR",
                    f"配置验证失败: {'; '.join(f'{e.field}: {e.message}' for e in validation.errors)}",
                )

    af = await feature_service.set_account_feature(
        db, aid, key, enabled=payload.enabled, config=payload.config
    )
    await audit.write(
        db,
        user.id,
        "feature.toggle",
        target=f"account:{aid}/feature:{key}",
        detail={"enabled": payload.enabled},
    )
    await db.commit()
    return AccountFeatureItem(
        feature_key=af.feature_key,
        enabled=af.enabled,
        state=af.state,
        last_error=af.last_error,
        config=_sanitize_config(dict(af.config or {}), key),
    )


# ─────────────────────────────────────────────────────
# 账号级配置更新（仅更新 config，不改变 enabled）
# ─────────────────────────────────────────────────────
@router.patch(
    "/api/accounts/{aid}/features/{key}/config",
    response_model=AccountFeatureItem,
)
async def update_account_feature_config(
    aid: int,
    key: str,
    payload: AccountFeatureConfigUpdate,
    db: DBSession,
    user: CurrentUser,
) -> AccountFeatureItem:
    """仅更新账号级配置，不改变 enabled 状态。

    会验证 JSON Schema 并保存。
    """
    if await db.get(Account, aid) is None:
        raise _bad("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    await feature_service.seed_builtin_features(db)
    feature = await db.get(Feature, key)
    if feature is None:
        raise _bad("FEATURE_NOT_FOUND", f"未注册的 feature: {key}", 404)

    # 验证 JSON Schema
    existing = await db.get(AccountFeature, (aid, key))
    payload.config = _preserve_existing_sensitive_values(
        dict(existing.config or {}) if existing is not None else None,
        dict(payload.config),
        key,
    )
    payload.config = _normalize_feature_config(key, payload.config)
    scoped_schema = _account_config_schema(feature.manifest, payload.config)
    if scoped_schema is not None:
        payload.config = feature_service.apply_required_config_defaults(
            payload.config,
            scoped_schema,
        )
        validation = feature_service.validate_config_against_schema(
            payload.config,
            scoped_schema,
        )
        if not validation.valid:
            raise _bad(
                "CONFIG_VALIDATION_ERROR",
                f"配置验证失败: {'; '.join(f'{e.field}: {e.message}' for e in validation.errors)}",
            )

    af = await feature_service.set_account_feature(
        db,
        aid,
        key,
        enabled=bool(existing.enabled) if existing is not None else False,
        config=payload.config,
    )
    await audit.write(
        db,
        user.id,
        "feature.config.update",
        target=f"account:{aid}/feature:{key}",
        detail={"config_keys": sorted(payload.config.keys())},
    )
    await db.commit()
    return AccountFeatureItem(
        feature_key=af.feature_key,
        enabled=af.enabled,
        state=af.state,
        last_error=af.last_error,
        config=_sanitize_config(dict(af.config or {}), key),
    )


@router.post(
    "/api/accounts/{aid}/features/{key}/config/actions/{action_key}",
    response_model=PluginConfigActionResponse,
)
async def run_account_feature_config_action(
    aid: int,
    key: str,
    action_key: str,
    payload: PluginConfigActionRequest,
    db: DBSession,
    user: CurrentUser,
) -> PluginConfigActionResponse:
    """运行插件声明的配置页动作。"""

    account = await db.get(Account, aid)
    if account is None:
        raise _bad("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    await feature_service.seed_builtin_features(db)
    feature = await db.get(Feature, key)
    if feature is None:
        raise _bad("FEATURE_NOT_FOUND", f"未注册的 feature: {key}", 404)
    installed_plugin = await db.get(InstalledPlugin, key)

    effective_config = await feature_service.get_effective_plugin_config(db, aid, key)
    try:
        result = await run_plugin_config_action(
            db,
            account=account,
            feature=feature,
            action_key=action_key,
            effective_config=effective_config,
            current_config=payload.config,
            action_input=payload.input,
            installed_plugin=installed_plugin,
        )
    except PluginConfigActionNotFound as exc:
        raise _bad("CONFIG_ACTION_NOT_FOUND", str(exc), 404) from exc
    except PluginConfigActionUnavailable as exc:
        raise _bad("CONFIG_ACTION_UNAVAILABLE", str(exc), 400) from exc
    except PluginHTTPError as exc:
        raise _bad("CONFIG_ACTION_HTTP_REJECTED", str(exc), 400) from exc
    except AIQuotaError as exc:
        raise _bad("CONFIG_ACTION_AI_QUOTA", str(exc), 429) from exc
    except AIUnavailableError as exc:
        raise _bad("CONFIG_ACTION_AI_UNAVAILABLE", str(exc), 503) from exc
    except PluginConfigActionError as exc:
        raise _bad("CONFIG_ACTION_FAILED", str(exc), 400) from exc
    except Exception as exc:
        raise _bad("CONFIG_ACTION_FAILED", str(exc), 400) from exc

    await audit.write(
        db,
        user.id,
        "feature.config.action",
        target=f"account:{aid}/feature:{key}",
        detail={
            "action_key": action_key,
            "config_patch_keys": sorted((result.get("config_patch") or {}).keys()),
        },
    )
    await db.commit()
    return PluginConfigActionResponse(**result)


@router.post(
    "/api/accounts/{aid}/features/{key}/config/actions/{action_key}/jobs",
    response_model=PluginConfigActionJobResponse,
)
async def start_account_feature_config_action_job(
    aid: int,
    key: str,
    action_key: str,
    payload: PluginConfigActionRequest,
    db: DBSession,
    user: CurrentUser,
) -> PluginConfigActionJobResponse:
    """启动插件声明的配置页后台动作。"""

    account = await db.get(Account, aid)
    if account is None:
        raise _bad("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    await feature_service.seed_builtin_features(db)
    feature = await db.get(Feature, key)
    if feature is None:
        raise _bad("FEATURE_NOT_FOUND", f"未注册的 feature: {key}", 404)
    installed_plugin = await db.get(InstalledPlugin, key)

    effective_config = await feature_service.get_effective_plugin_config(db, aid, key)
    try:
        job = await create_plugin_config_action_job(
            db,
            account=account,
            feature=feature,
            action_key=action_key,
            effective_config=effective_config,
            current_config=payload.config,
            action_input=payload.input,
            installed_plugin=installed_plugin,
        )
    except PluginConfigActionNotFound as exc:
        raise _bad("CONFIG_ACTION_NOT_FOUND", str(exc), 404) from exc

    await audit.write(
        db,
        user.id,
        "feature.config.action.job.start",
        target=f"account:{aid}/feature:{key}",
        detail={"action_key": action_key, "job_id": job.job_id},
    )
    await db.commit()
    return job_response(job, logs=[])


@router.get(
    "/api/plugin-config-action-jobs/{job_id}",
    response_model=PluginConfigActionJobResponse,
)
async def get_config_action_job_status(
    job_id: str,
    db: DBSession,
    _user: CurrentUser,
) -> PluginConfigActionJobResponse:
    """查询配置动作后台任务状态与过程日志。"""

    response = await get_plugin_config_action_job(db, job_id)
    if response is None:
        raise _bad("CONFIG_ACTION_JOB_NOT_FOUND", "配置动作任务不存在", 404)
    return response


@router.get(
    "/api/accounts/{aid}/features/{key}/config/action-jobs",
    response_model=list[PluginConfigActionJobResponse],
)
async def list_account_feature_config_action_jobs(
    aid: int,
    key: str,
    db: DBSession,
    _user: CurrentUser,
    limit: int = Query(10, ge=1, le=50),
) -> list[PluginConfigActionJobResponse]:
    """查询某账号某插件最近的配置动作任务，用于恢复后台窗口。"""

    if await db.get(Account, aid) is None:
        raise _bad("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    await feature_service.seed_builtin_features(db)
    if await db.get(Feature, key) is None:
        raise _bad("FEATURE_NOT_FOUND", f"未注册的 feature: {key}", 404)
    return await list_plugin_config_action_jobs(
        db,
        account_id=aid,
        plugin_key=key,
        limit=limit,
    )


# ─────────────────────────────────────────────────────
# Global Config API
# ─────────────────────────────────────────────────────
@router.get(
    "/api/plugins/{key}/config",
    response_model=PluginGlobalConfigResponse,
)
async def get_plugin_global_config(
    key: str,
    db: DBSession,
    _user: CurrentUser,
) -> PluginGlobalConfigResponse:
    """获取插件的 global config。"""
    await feature_service.seed_builtin_features(db)
    feature = await db.get(Feature, key)
    if feature is None:
        raise _bad("FEATURE_NOT_FOUND", f"未注册的插件: {key}", 404)

    global_config = await feature_service.get_plugin_global_config(db, key)
    return PluginGlobalConfigResponse(
        plugin_key=key,
        config=_sanitize_config(global_config, key),
        global_config=_sanitize_config(global_config, key),
    )


@router.put(
    "/api/plugins/{key}/config",
    response_model=PluginGlobalConfigResponse,
)
async def set_plugin_global_config(
    key: str,
    payload: PluginGlobalConfigUpdate,
    db: DBSession,
    user: CurrentUser,
) -> PluginGlobalConfigResponse:
    """设置插件的 global config。

    - 验证配置是否符合 config_schema。
    - 仅更新标记为 level="global" 的字段。
    - 通知所有使用该插件的账号的 worker reload。
    """
    existing_global_config = await feature_service.get_plugin_global_config(db, key)
    payload.config = _preserve_existing_sensitive_values(
        existing_global_config,
        dict(payload.config),
        key,
    )
    try:
        global_config = await feature_service.set_plugin_global_config(
            db, key, payload.config
        )
    except ValueError as e:
        raise _bad("CONFIG_VALIDATION_ERROR", str(e)) from e

    await audit.write(
        db,
        user.id,
        "feature.global_config.update",
        target=f"plugin:{key}",
        detail={"global_config_keys": sorted(global_config.keys())},
    )
    await db.commit()

    return PluginGlobalConfigResponse(
        plugin_key=key,
        config=_sanitize_config(global_config, key),
        global_config=_sanitize_config(global_config, key),
    )


# ─────────────────────────────────────────────────────
# 获取账号级最终生效配置（合并后）
# ─────────────────────────────────────────────────────
@router.get(
    "/api/accounts/{aid}/features/{key}/config",
    response_model=dict[str, object],
)
async def get_effective_config(
    aid: int,
    key: str,
    db: DBSession,
    _user: CurrentUser,
) -> dict[str, object]:
    """获取某账号某插件的最终生效配置。

    合并顺序：schema defaults < global config < account config
    """
    if await db.get(Account, aid) is None:
        raise _bad("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    await feature_service.seed_builtin_features(db)
    feature = await db.get(Feature, key)
    if feature is None:
        raise _bad("FEATURE_NOT_FOUND", f"未注册的 feature: {key}", 404)

    effective_config = await feature_service.get_effective_plugin_config(db, aid, key)
    return _sanitize_config(effective_config, key)


# ─────────────────────────────────────────────────────
# 配置验证
# ─────────────────────────────────────────────────────
@router.post(
    "/api/plugins/{key}/config/validate",
    response_model=ConfigValidationResponse,
)
async def validate_plugin_config(
    key: str,
    payload: PluginGlobalConfigUpdate,
    db: DBSession,
    _user: CurrentUser,
) -> ConfigValidationResponse:
    """验证配置是否符合插件的 config_schema。"""
    await feature_service.seed_builtin_features(db)
    feature = await db.get(Feature, key)
    if feature is None:
        raise _bad("FEATURE_NOT_FOUND", f"未注册的插件: {key}", 404)

    config_schema = (feature.manifest or {}).get("config_schema")
    if not config_schema:
        return {"valid": True, "errors": []}

    return feature_service.validate_config_against_schema(payload.config, config_schema)


__all__ = ["router"]
