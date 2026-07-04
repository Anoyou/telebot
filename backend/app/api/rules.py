"""规则（Rule）REST API（PRD §9.3）。

统一为 ``[账号 × feature]`` 下的 Rule 提供 CRUD + dry-run + 复制到其它账号。
所有写操作完成后通过 IPC ``CMD_RELOAD_CONFIG`` 通知对应 worker 热加载。

注意：普通插件不再随 TelePilot 本体分发。插件 dry-run 优先调用已安装插件导出的
``_dry_run_match``；未安装或未导出时返回明确提示，不从 builtin 路径兜底。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import CroniterBadCronError, croniter
from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..db.models.account import Account
from ..db.models.feature import (
    BUILTIN_FEATURES,
    FEATURE_AUTO_REPLY,
    FEATURE_AUTOREPEAT,
    FEATURE_CODEX_IMAGE,
    FEATURE_FORWARD,
    FEATURE_SCHEDULER,
    Feature,
)
from ..db.models.plugin import PLUGIN_TRUST_ORPHAN, InstalledPlugin
from ..db.models.rule import Rule
from ..deps import CurrentUser, DBSession
from ..redis_client import get_redis
from ..schemas.rule import (
    RuleCopyRequest,
    RuleCreate,
    RuleDryRunRequest,
    RuleDryRunResponse,
    RuleOut,
    RuleUpdate,
)
from ..services import audit
from ..services.redactor import redact_value
from ..settings import settings
from ..worker.ipc import CMD_RELOAD_CONFIG, cmd_channel, make_cmd, publish_cmd_with_ack

log = logging.getLogger(__name__)
router = APIRouter(tags=["rules"])


# ─────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────
def _bad(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _ensure_account(db, aid: int) -> Account:
    acc = await db.get(Account, aid)
    if acc is None:
        raise _bad("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    return acc


async def _ensure_feature(db, key: str) -> None:
    """feature_key 必须在 feature 表里有登记（包括内置 5 + 第三方同步）。"""
    if key in BUILTIN_FEATURES:
        return
    if await db.get(Feature, key) is None:
        raise _bad("FEATURE_NOT_FOUND", f"未知 feature: {key}", 404)


async def _notify_reload(aid: int) -> None:
    """规则变化后通知对应 worker 热加载。redis 不可用静默。"""
    try:
        redis = get_redis()
        ok = await publish_cmd_with_ack(redis, aid, CMD_RELOAD_CONFIG)
        if not ok:
            log.debug("worker reload_config 未确认 aid=%s，将由周期 reconcile 收敛", aid)
    except Exception:  # noqa: BLE001
        log.debug("通知 worker reload 失败 aid=%s", aid, exc_info=True)


def _to_out(r: Rule) -> RuleOut:
    out = RuleOut.model_validate(r)
    out.config = redact_value(dict(out.config or {}))
    return out


def _forward_dry_run_match(*args):
    from ..worker.plugins.builtin.forward.plugin import _dry_run_match

    return _dry_run_match(*args)


async def _installed_plugin_dry_run_match(db, feature_key: str, *args):
    installed = await db.get(InstalledPlugin, feature_key)
    if installed is None:
        return False, f"{feature_key} 插件库插件未安装，无法执行 dry-run。"
    if not bool(getattr(installed, "enabled", False)):
        return False, f"{feature_key} 已安装但全局开关关闭，无法执行 dry-run。"
    if str(getattr(installed, "trust_tier", "") or "") == PLUGIN_TRUST_ORPHAN:
        return False, f"{feature_key} 安装记录为 orphan，无法执行 dry-run。"
    signature_ok = getattr(installed, "signature_ok", None)
    if signature_ok is False:
        return False, f"{feature_key} 签名校验失败，无法执行 dry-run。"
    if signature_ok is None and not bool(settings.plugin_allow_legacy_unsigned_plugins):
        return False, f"{feature_key} 签名状态未知，当前策略不允许执行 dry-run。"
    install_error = str(getattr(installed, "last_install_error", "") or "").strip()
    if install_error:
        return False, f"{feature_key} 安装失败：{install_error}"

    import sys

    from ..worker.plugins.loader import _installed_module_name, _load_installed_plugin

    loaded = _load_installed_plugin(feature_key)
    if feature_key not in loaded:
        return False, f"{feature_key} 插件库插件未安装或加载失败，无法执行 dry-run。"

    package_name = _installed_module_name(feature_key)
    for module_name in (f"{package_name}.plugin", package_name):
        module = sys.modules.get(module_name)
        dry_run = getattr(module, "_dry_run_match", None)
        if callable(dry_run):
            return dry_run(*args)

    return False, f"{feature_key} 插件未导出 _dry_run_match，无法执行 dry-run。"


def _parse_scheduler_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def _get_system_tz(db) -> ZoneInfo | None:
    """从 system_setting 读取用户配置的时区。"""
    from ..db.models.system import SystemSetting

    row = await db.get(SystemSetting, "timezone")
    if row and isinstance(row.value, dict):
        tz_str = str(row.value.get("value", "")).strip()
        if tz_str:
            return ZoneInfo(tz_str)
    return ZoneInfo("Asia/Shanghai")


def _croniter_next_dryrun(
    expr: str, start_utc: datetime, tz: ZoneInfo | None
) -> datetime | None:
    """与 plugin._croniter_next 逻辑一致：按时区计算后返回 UTC。"""
    try:
        if tz is not None:
            local_now = start_utc.astimezone(tz)
            next_local: datetime = _croniter_for_scheduler(expr, local_now).get_next(datetime)
            return next_local.astimezone(UTC)
        return _croniter_for_scheduler(expr, start_utc).get_next(datetime)
    except (CroniterBadCronError, ValueError):
        return None


def _croniter_for_scheduler(expr: str, start: datetime) -> croniter:
    parts = str(expr or "").split()
    kwargs = {"second_at_beginning": True} if len(parts) in (6, 7) else {}
    return croniter(expr, start, **kwargs)


def _scheduler_dt_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat()


async def _normalize_scheduler_config_for_save(
    db,
    config: dict[str, Any],
) -> dict[str, Any]:
    """保存时刷新调度运行态，避免前端带回旧 next_fire。"""
    cfg = dict(config or {})
    kind = str(cfg.get("kind") or "cron").lower()
    now = datetime.now(UTC)
    tz = await _get_system_tz(db)

    if kind == "once":
        cfg["next_fire"] = _scheduler_dt_iso(_parse_scheduler_dt(cfg.get("fire_at")))
        return cfg

    if kind == "interval":
        try:
            interval = int(cfg.get("interval_sec") or 0)
        except (TypeError, ValueError):
            interval = 0
        last_fire = _parse_scheduler_dt(cfg.get("last_fire"))
        if interval <= 0:
            cfg["next_fire"] = None
        elif last_fire is not None:
            cfg["next_fire"] = _scheduler_dt_iso(last_fire + timedelta(seconds=interval))
        else:
            cfg["next_fire"] = _scheduler_dt_iso(now)
        return cfg

    expr = str(cfg.get("cron") or "").strip()
    cfg["_last_cron"] = expr
    cfg["_cron_seconds_mode"] = len(expr.split()) in (6, 7)
    cfg["_cron_timezone"] = getattr(tz, "key", None) or "UTC"
    cfg.pop("_config_dirty", None)
    cfg["next_fire"] = _scheduler_dt_iso(_croniter_next_dryrun(expr, now, tz)) if expr else None
    return cfg


# ─────────────────────────────────────────────────────
# 列表 / 创建
# ─────────────────────────────────────────────────────
@router.get(
    "/api/accounts/{aid}/features/{key}/rules",
    response_model=list[RuleOut],
)
async def list_rules(
    aid: int, key: str, db: DBSession, _user: CurrentUser
) -> list[RuleOut]:
    """按 priority 倒序返回该 [账号 × feature] 下的所有 rule。"""
    await _ensure_account(db, aid)
    await _ensure_feature(db, key)
    rows = (
        await db.execute(
            select(Rule)
            .where(Rule.account_id == aid, Rule.feature_key == key)
            .order_by(Rule.priority.desc(), Rule.id.asc())
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


@router.post(
    "/api/accounts/{aid}/features/{key}/rules",
    response_model=RuleOut,
    status_code=201,
)
async def create_rule(
    aid: int,
    key: str,
    payload: RuleCreate,
    db: DBSession,
    user: CurrentUser,
) -> RuleOut:
    """新建一条 rule。"""
    await _ensure_account(db, aid)
    await _ensure_feature(db, key)
    config = dict(payload.config or {})
    if key == FEATURE_SCHEDULER:
        config = await _normalize_scheduler_config_for_save(db, config)
    rule = Rule(
        account_id=aid,
        feature_key=key,
        name=payload.name,
        enabled=payload.enabled,
        priority=payload.priority,
        config=config,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    await audit.write(
        db,
        user.id,
        "rule.create",
        target=f"account:{aid}/feature:{key}/rule:{rule.id}",
        detail={"name": payload.name, "priority": payload.priority},
    )
    await db.commit()
    await _notify_reload(aid)
    return _to_out(rule)


# ─────────────────────────────────────────────────────
# 单条 GET / PATCH / DELETE
# ─────────────────────────────────────────────────────
async def _load_rule(db, aid: int, key: str, rid: int) -> Rule:
    rule = await db.get(Rule, rid)
    if rule is None or rule.account_id != aid or rule.feature_key != key:
        raise _bad("RULE_NOT_FOUND", "规则不存在", 404)
    return rule


@router.get(
    "/api/accounts/{aid}/features/{key}/rules/{rid}",
    response_model=RuleOut,
)
async def get_rule(
    aid: int, key: str, rid: int, db: DBSession, _user: CurrentUser
) -> RuleOut:
    await _ensure_account(db, aid)
    await _ensure_feature(db, key)
    rule = await _load_rule(db, aid, key, rid)
    return _to_out(rule)


@router.patch(
    "/api/accounts/{aid}/features/{key}/rules/{rid}",
    response_model=RuleOut,
)
async def patch_rule(
    aid: int,
    key: str,
    rid: int,
    payload: RuleUpdate,
    db: DBSession,
    user: CurrentUser,
) -> RuleOut:
    """更新单条 rule 的部分字段（exclude_unset）。"""
    await _ensure_account(db, aid)
    await _ensure_feature(db, key)
    rule = await _load_rule(db, aid, key, rid)
    data = payload.model_dump(exclude_unset=True)
    if key == FEATURE_SCHEDULER and data.get("config") is not None:
        data["config"] = await _normalize_scheduler_config_for_save(db, dict(data["config"]))
    for k, v in data.items():
        setattr(rule, k, dict(v) if k == "config" and v is not None else v)
    await db.commit()
    await db.refresh(rule)
    await audit.write(
        db,
        user.id,
        "rule.update",
        target=f"account:{aid}/feature:{key}/rule:{rid}",
        detail={"fields": sorted(data.keys())},
    )
    await db.commit()
    await _notify_reload(aid)
    return _to_out(rule)


@router.delete(
    "/api/accounts/{aid}/features/{key}/rules/{rid}",
    status_code=204,
)
async def delete_rule(
    aid: int, key: str, rid: int, db: DBSession, user: CurrentUser
) -> None:
    await _ensure_account(db, aid)
    await _ensure_feature(db, key)
    rule = await _load_rule(db, aid, key, rid)
    await db.delete(rule)
    await db.commit()
    await audit.write(
        db,
        user.id,
        "rule.delete",
        target=f"account:{aid}/feature:{key}/rule:{rid}",
    )
    await db.commit()
    await _notify_reload(aid)


# ─────────────────────────────────────────────────────
# Dry-run
# ─────────────────────────────────────────────────────
@router.post(
    "/api/accounts/{aid}/features/{key}/rules/{rid}/dry-run",
    response_model=RuleDryRunResponse,
)
async def dry_run_rule(
    aid: int,
    key: str,
    rid: int,
    payload: RuleDryRunRequest,
    db: DBSession,
    _user: CurrentUser,
) -> RuleDryRunResponse:
    """试运行：把 sample 消息喂给规则，返回是否命中 + 渲染输出。

    - ``auto_reply``：调用已安装插件的 ``_dry_run_match`` 完成匹配 + 渲染
    - ``forward``：按 ``source_kind`` 判断是否进入转发流水线，输出 "would forward to ..." 描述
    - 其它 feature：当前返回 matched=False（未实现）
    """
    await _ensure_account(db, aid)
    await _ensure_feature(db, key)
    rule = await _load_rule(db, aid, key, rid)
    if key == FEATURE_AUTO_REPLY:
        chat_type = payload.sample_chat_type or "private"
        cfg = rule.config or {}
        matched, output = await _installed_plugin_dry_run_match(
            db,
            key,
            cfg,
            payload.sample_message,
            chat_type,
            payload.sample_chat_id,
        )
        logs: list[dict[str, str]] = [
            {"step": "scope", "msg": f"会话类型：{chat_type}"},
            {"step": "scope", "msg": f"规则 scope：{cfg.get('scope', 'all')}"},
        ]
        if chat_type == "group" and payload.sample_chat_id:
            logs.append({"step": "scope", "msg": f"样本 chat_id：{payload.sample_chat_id}"})
        if cfg.get("scope") in ("group_specific", "groups"):
            gids = cfg.get("group_ids", cfg.get("groups", []))
            logs.append({"step": "scope", "msg": f"规则 group_ids：{gids}"})
        logs.append({"step": "match", "msg": f"匹配模式：{cfg.get('match_mode', 'contains')}"})
        logs.append({"step": "match", "msg": f"关键词：{cfg.get('keyword', '(any)')}"})
        if not matched:
            logs.append({"step": "result", "msg": "未命中：scope 检查或关键词匹配失败"})
        else:
            logs.append({"step": "result", "msg": "命中"})
        return RuleDryRunResponse(
            matched=matched,
            output=output,
            detail={"feature": key, "rule_id": rid, "logs": logs},
        )
    if key == FEATURE_FORWARD:
        cfg = rule.config or {}
        matched, output = _forward_dry_run_match(
            cfg,
            payload.sample_message,
            payload.sample_chat_id,
        )
        logs = [
            {"step": "source", "msg": f"source_kind：{cfg.get('source_kind', 'all')}"},
        ]
        sk = cfg.get("source_kind", "all")
        if sk == "peers":
            logs.append({"step": "source", "msg": f"source_peers：{cfg.get('source_peers', [])}"})
            logs.append({"step": "source", "msg": f"样本 chat_id：{payload.sample_chat_id or '(未填)'}"})
        elif sk == "keyword":
            logs.append({"step": "source", "msg": f"关键词：{cfg.get('keyword', '')}"})
            logs.append({"step": "source", "msg": f"样本文本长度：{len(payload.sample_message)}"})
        elif sk == "duplicate":
            logs.append({"step": "source", "msg": f"duplicate_window：{cfg.get('duplicate_window', 60)}s"})
            logs.append({"step": "source", "msg": f"duplicate_threshold：{cfg.get('duplicate_threshold', 3)}"})
        logs.append({"step": "mode", "msg": f"转发方式：{cfg.get('mode', 'forward_native')}"})
        logs.append({"step": "mode", "msg": f"目标 chat_id：{cfg.get('target_chat_id', '(未设置)')}"})
        if not matched:
            logs.append({"step": "result", "msg": "未命中"})
        else:
            logs.append({"step": "result", "msg": "命中"})
        return RuleDryRunResponse(
            matched=matched,
            output=output,
            detail={"feature": key, "rule_id": rid, "logs": logs},
        )
    if key == FEATURE_SCHEDULER:
        cfg = rule.config or {}
        kind = str(cfg.get("kind") or "cron").lower()
        action = cfg.get("action") if isinstance(cfg.get("action"), dict) else {}
        now = datetime.now(UTC)
        tz = await _get_system_tz(db)
        next_fire: datetime | None = None
        logs: list[dict[str, str]] = [
            {"step": "kind", "msg": f"触发类型：{kind}"},
            {"step": "now", "msg": f"当前 UTC 时间：{now.isoformat()}"},
        ]
        if tz is not None:
            logs.append({"step": "now", "msg": f"cron 解析时区：{tz}"})
            logs.append({"step": "now", "msg": f"当前本地时间：{now.astimezone(tz).isoformat()}"})

        try:
            if kind == "once":
                fire_at = _parse_scheduler_dt(cfg.get("fire_at"))
                logs.append({"step": "once", "msg": f"fire_at：{fire_at.isoformat() if fire_at else '(无效)'}"})
                next_fire = fire_at
                if cfg.get("last_fire"):
                    logs.append({"step": "once", "msg": "已执行过（last_fire 存在），跳过"})
                elif fire_at and fire_at <= now:
                    logs.append({"step": "once", "msg": "已到期，应触发"})
                elif fire_at:
                    logs.append({"step": "once", "msg": f"未到期，还差 {(fire_at - now).total_seconds():.0f}s"})
            elif kind == "interval":
                interval_sec = int(cfg.get("interval_sec") or 0)
                last_fire = _parse_scheduler_dt(cfg.get("last_fire"))
                logs.append({"step": "interval", "msg": f"interval_sec：{interval_sec}"})
                logs.append({"step": "interval", "msg": f"last_fire：{last_fire.isoformat() if last_fire else '(无，将立即触发)'}"})
                if interval_sec > 0:
                    next_fire = (last_fire if last_fire is not None else now).astimezone(UTC)
                    if last_fire is not None:
                        next_fire = next_fire + timedelta(seconds=interval_sec)
                        logs.append({"step": "interval", "msg": f"计算 next_fire：{next_fire.isoformat()}"})
                    else:
                        logs.append({"step": "interval", "msg": "首次运行，将立即触发"})
            else:
                expr = str(cfg.get("cron") or "").strip()
                logs.append({"step": "cron", "msg": f"cron 表达式：{expr or '(空)'}"})
                part_count = len(expr.split()) if expr else 0
                if part_count == 5:
                    logs.append({"step": "cron", "msg": "字段解释：分 时 日 月 周"})
                elif part_count in (6, 7):
                    logs.append({"step": "cron", "msg": "字段解释：秒 分 时 日 月 周 [年]"})
                logs.append({"step": "cron", "msg": f"已存 next_fire：{cfg.get('next_fire') or '(无，首次将只计算不触发)'}"})
                if expr:
                    next_fire = _croniter_next_dryrun(expr, now, tz)
                    if next_fire:
                        logs.append({"step": "cron", "msg": f"计算 next_fire(UTC)：{next_fire.isoformat()}"})
                        if tz is not None:
                            logs.append({"step": "cron", "msg": f"计算 next_fire(本地)：{next_fire.astimezone(tz).isoformat()}"})
                    else:
                        logs.append({"step": "cron", "msg": "cron 表达式无效"})
        except (ValueError, CroniterBadCronError) as exc:
            logs.append({"step": "error", "msg": f"解析失败：{type(exc).__name__}: {exc}"})
            return RuleDryRunResponse(
                matched=False,
                output=None,
                detail={"feature": key, "rule_id": rid, "logs": logs, "error": str(exc)},
            )

        # action 详情
        action_type = str(action.get("type") or "send_message")
        target = action.get("target_chat_id")
        logs.append({"step": "action", "msg": f"动作类型：{action_type}"})
        logs.append({"step": "action", "msg": f"target_chat_id：{target or '(未设置)'}"})
        if action_type == "call_llm":
            logs.append({"step": "action", "msg": f"provider_id：{action.get('provider_id', '(未设置)')}"})
        if action_type in ("send_message", "call_llm"):
            da = action.get("delete_after")
            if da:
                logs.append({"step": "action", "msg": f"delete_after：{da}s"})

        due = bool(next_fire and next_fire <= now)
        output = (
            f"would fire {action_type} to {target}"
            if due
            else f"next fire at {next_fire.isoformat() if next_fire else 'N/A'}"
        )
        logs.append({"step": "result", "msg": f"{'应触发' if due else '未到期'}"})
        # 运行时状态
        logs.append({"step": "state", "msg": f"last_fire：{cfg.get('last_fire') or '(无)'}"})
        logs.append({"step": "state", "msg": f"last_result：{cfg.get('last_result') or '(无)'}"})
        err = cfg.get("last_error")
        if err:
            logs.append({"step": "state", "msg": f"last_error：{err}"})
        logs.append({"step": "state", "msg": f"enabled：{cfg.get('enabled', True)}"})
        return RuleDryRunResponse(
            matched=due,
            output=output,
            detail={
                "feature": key,
                "rule_id": rid,
                "logs": logs,
            },
        )
    # ── autorepeat dry-run ──
    if key == FEATURE_AUTOREPEAT:
        cfg = rule.config or {}
        matched, output = await _installed_plugin_dry_run_match(
            db,
            key,
            cfg,
            payload.sample_message,
            payload.sample_chat_id,
        )
        logs = [
            {"step": "target", "msg": f"目标群组：{cfg.get('target_chat_id', '(未设置)')}"},
            {"step": "config", "msg": f"时间窗口：{cfg.get('time_window', 300)}s"},
            {"step": "config", "msg": f"触发人数：{cfg.get('min_users', 5)}"},
            {"step": "sample", "msg": f"样本 chat_id：{payload.sample_chat_id or '(未填)'}"},
        ]
        if not matched:
            logs.append({"step": "result", "msg": "未命中"})
        else:
            logs.append({"step": "result", "msg": "命中"})
        return RuleDryRunResponse(
            matched=matched,
            output=output,
            detail={"feature": key, "rule_id": rid, "logs": logs},
        )
    # ── codex_image dry-run ──
    if key == FEATURE_CODEX_IMAGE:
        cfg = rule.config or {}
        matched, output = await _installed_plugin_dry_run_match(
            db,
            key,
            cfg,
            payload.sample_message,
            payload.sample_chat_id,
        )
        logs = [
            {"step": "auth", "msg": f"Token：{'已配置' if cfg.get('access_token') else '未配置'}"},
            {"step": "config", "msg": f"模型：{cfg.get('model', 'gpt-5.5')}"},
            {"step": "config", "msg": f"最大等待：{cfg.get('max_wait_seconds', 600)}s"},
            {"step": "sample", "msg": f"提示词长度：{len(payload.sample_message)}"},
        ]
        if not matched:
            logs.append({"step": "result", "msg": output or "未命中（缺 access_token）"})
        else:
            logs.append({"step": "result", "msg": "命中"})
        return RuleDryRunResponse(
            matched=matched,
            output=output,
            detail={"feature": key, "rule_id": rid, "logs": logs},
        )
    return RuleDryRunResponse(
        matched=False,
        output=None,
        detail={"feature": key, "note": "dry-run for this feature is not implemented yet"},
    )


# ─────────────────────────────────────────────────────
# 手动执行规则（仅 scheduler）
# ─────────────────────────────────────────────────────
@router.post(
    "/api/accounts/{aid}/features/{key}/rules/{rid}/execute",
    response_model=dict,
)
async def execute_rule(
    aid: int,
    key: str,
    rid: int,
    db: DBSession,
    _user: CurrentUser,
) -> dict[str, Any]:
    """手动执行一条 scheduler 规则。

    通过 IPC RPC 让 worker 端立即执行 rule 的 action，
    等待结果返回（超时 15s）。
    """
    if key != FEATURE_SCHEDULER:
        raise _bad("unsupported", "仅 scheduler 规则支持手动执行", 400)

    await _ensure_account(db, aid)
    rule = await db.get(Rule, rid)
    if rule is None or rule.account_id != aid or rule.feature_key != key:
        raise _bad("RULE_NOT_FOUND", "规则不存在", 404)

    # 通过 IPC RPC 让 worker 执行
    import asyncio
    import secrets

    from ..worker.ipc import CMD_EXECUTE_RULE, IPCMessage

    reply_channel = f"worker_reply:{aid}:exec_rule:{secrets.token_hex(8)}"
    redis = get_redis()
    if redis is None:
        raise _bad("NO_REDIS", "Redis 不可用，无法连接 worker", 503)

    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(reply_channel)
        await redis.publish(
            cmd_channel(aid),
            make_cmd(CMD_EXECUTE_RULE, rule_id=rid, reply_to=reply_channel),
        )
        deadline = asyncio.get_event_loop().time() + 15.0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return {"ok": False, "error": "worker 响应超时（可能未启动）"}
            msg = await asyncio.wait_for(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                timeout=remaining,
            )
            if msg is None or msg.get("type") != "message":
                continue
            payload = IPCMessage.decode(msg["data"]).payload
            ok = bool(payload.get("ok"))
            error = payload.get("error")
            return {"ok": ok, "error": error}
    finally:
        try:
            await pubsub.unsubscribe(reply_channel)
            await pubsub.close()
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────
# 复制规则到其它账号
# ─────────────────────────────────────────────────────
@router.post(
    "/api/accounts/{aid}/features/{key}/rules/copy",
    response_model=dict,
)
async def copy_rules(
    aid: int,
    key: str,
    payload: RuleCopyRequest,
    db: DBSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """把 ``rule_ids`` 指定的 rule（必须属于 source aid×key）复制到 ``target_account_ids``。

    每条 rule 在每个目标账号下都会插入新行（自增 id），feature_key 保持一致。
    """
    await _ensure_account(db, aid)
    await _ensure_feature(db, key)
    if not payload.rule_ids or not payload.target_account_ids:
        return {"copied": 0}
    if aid in payload.target_account_ids:
        # 防呆：避免误把自己复制成第二份
        targets = [t for t in payload.target_account_ids if t != aid]
    else:
        targets = list(payload.target_account_ids)
    if not targets:
        return {"copied": 0}

    src_rules = (
        await db.execute(
            select(Rule).where(
                Rule.account_id == aid,
                Rule.feature_key == key,
                Rule.id.in_(list(payload.rule_ids)),
            )
        )
    ).scalars().all()
    if not src_rules:
        return {"copied": 0}

    # 校验目标账号都存在
    for tgt in targets:
        if await db.get(Account, tgt) is None:
            raise _bad("ACCOUNT_NOT_FOUND", f"目标账号不存在: {tgt}", 404)

    copied = 0
    for tgt in targets:
        for r in src_rules:
            db.add(
                Rule(
                    account_id=tgt,
                    feature_key=key,
                    name=r.name,
                    enabled=r.enabled,
                    priority=r.priority,
                    config=dict(r.config or {}),
                )
            )
            copied += 1
    await db.commit()

    await audit.write(
        db,
        user.id,
        "rule.copy",
        target=f"account:{aid}/feature:{key}",
        detail={"rule_ids": list(payload.rule_ids), "targets": targets, "copied": copied},
    )
    await db.commit()
    # 每个目标 worker 都通知一遍
    for tgt in targets:
        await _notify_reload(tgt)
    return {"copied": copied, "targets": targets}


__all__ = ["router"]
