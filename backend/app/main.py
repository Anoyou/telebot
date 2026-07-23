"""FastAPI 入口：注册 router、CORS、全局异常 handler、lifespan。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .api import account_bots as account_bots_api
from .api import accounts as accounts_api
from .api import alias as alias_api
from .api import auth as auth_api
from .api import config_bundle as config_bundle_api
from .api import device_profiles as device_profiles_api
from .api import dispatch_debug as dispatch_debug_api
from .api import ledger as ledger_api
from .api import llm_usage as llm_usage_api
from .api import logs as logs_api
from .api import message_templates as message_templates_api
from .api import network as network_api
from .api import notify_bots as notify_bots_api
from .api import platform_capabilities as platform_capabilities_api
from .api import proxies as proxies_api
from .api import rate_limit as rate_limit_api
from .api import sudo as sudo_api
from .api import system_agent as system_agent_api
from .api import webhooks as webhooks_api
from .logging_redaction import configure_dependency_log_levels, install_sensitive_log_filter
from .services import (
    account_bot_runtime,
    event_trace,
    interaction_bot_runtime,
    notify_service,
    platform_capabilities,
    plugin_config_action_jobs,
    remote_plugin_service,
)
from .services.login_service import cleanup_expired_loop
from .settings import settings

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
install_sensitive_log_filter()
configure_dependency_log_levels()

# Postgres advisory lock key（固定值，避免不同进程 key 漂移）
_MIGRATION_ADVISORY_LOCK_KEY = 730140129
_CSRF_HEADER_NAME = "X-Requested-With"
_CSRF_HEADER_VALUES = {"telepilot-ui", "telebot-ui"}
_CSRF_TOKEN_COOKIE = "csrf_token"
_CSRF_TOKEN_HEADER = "X-CSRF-Token"
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
_RUNTIME_COMPONENTS: dict[str, bool | None] = {
    "worker_supervisor": None,
    "account_bot_manager": None,
    "interaction_bot_manager": None,
}
_RUNTIME_COMPONENT_ERRORS: dict[str, str] = {}


async def _start_runtime_component(
    name: str,
    starter: Callable[[], Awaitable[object]],
) -> bool:
    _RUNTIME_COMPONENTS[name] = False
    try:
        await starter()
    except Exception as exc:  # noqa: BLE001
        _RUNTIME_COMPONENT_ERRORS[name] = f"{type(exc).__name__}: {exc}"[:300]
        logging.exception("启动关键组件 %s 失败", name)
        return False
    _RUNTIME_COMPONENTS[name] = True
    _RUNTIME_COMPONENT_ERRORS.pop(name, None)
    return True


async def _retry_runtime_component(
    name: str,
    starter: Callable[[], Awaitable[object]],
) -> None:
    delay = 2.0
    while not _RUNTIME_COMPONENTS.get(name):
        await asyncio.sleep(delay)
        if await _start_runtime_component(name, starter):
            logging.info("关键组件 %s 已自动恢复", name)
            return
        delay = min(delay * 2, 30.0)


async def _start_interaction_bot_component() -> object:
    """缓存就绪后才允许启动 Interaction Bot；DB 恢复后可由重试器收敛。"""

    snapshot = platform_capabilities.get_snapshot()
    if not snapshot.cache_ready:
        await platform_capabilities.bootstrap_from_db()
        snapshot = platform_capabilities.get_snapshot()
    if not snapshot.cache_ready:
        raise RuntimeError("平台能力缓存未就绪")
    if not platform_capabilities.is_module_enabled_cached(
        "interaction_bot",
        fail_closed=True,
    ):
        await platform_capabilities.mark_runtime_ready_if_starting("interaction_bot")
        return 0
    result = await interaction_bot_runtime.start_interaction_bot_manager()
    await platform_capabilities.mark_runtime_ready_if_starting("interaction_bot")
    return result


def _is_container_env() -> bool:
    """粗粒度判断当前是否运行在容器环境。"""
    return Path("/.dockerenv").exists()


def _warn_if_forwarded_for_misconfigured() -> None:
    """检测 TRUST_FORWARDED_FOR 在容器部署下的常见错配并给出启动告警。"""
    if _is_container_env() and not settings.trust_forwarded_for:
        logging.warning(
            "检测到容器部署且 TRUST_FORWARDED_FOR=false："
            "这会让后端忽略反向代理传入的真实客户端 IP。"
            "若前置 nginx/traefik，请将 TRUST_FORWARDED_FOR=true（仅限可信反代场景）。"
        )


def _run_alembic_upgrade() -> None:
    """同步调 ``alembic upgrade head``。

    在 lifespan 启动钩子里以 ``asyncio.to_thread`` 调，避免阻塞 event loop。
    alembic 用的是同步 driver（settings.database_url_sync），跟 alembic CLI 走同一条路径
    （env.py），所以在 process 内调和命令行调结果一致。

    任何失败只 log，不抛——上面注释里有"失败不阻止启动"的设计理由。
    """
    engine = None
    lock_connection = None
    lock_acquired = False
    try:
        # 局部 import：alembic 是 dev 路径常驻依赖，但 import 时会扫脚本目录，放函数内更轻。
        from alembic.config import Config
        from sqlalchemy import create_engine, text

        from alembic import command

        # PostgreSQL advisory lock 是 session 级锁。持锁连接必须活到 upgrade 返回，
        # 否则 with connect() 退出时锁会提前释放，两个实例仍可并发迁移。
        if settings.database_url_sync.startswith("postgresql"):
            engine = create_engine(settings.database_url_sync, future=True)
            lock_connection = engine.connect()
            result = lock_connection.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _MIGRATION_ADVISORY_LOCK_KEY},
            )
            lock_acquired = bool(result.scalar())
            if not lock_acquired:
                logging.warning("另一个实例正在执行迁移，本实例跳过启动期自动迁移")
                return

        # alembic.ini 在 backend/ 根目录；以本文件所在目录的上一级定位，避免 cwd 漂移
        ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
        if not ini_path.exists():
            logging.warning("alembic.ini 不存在：%s；跳过启动期自动迁移", ini_path)
            return
        cfg = Config(str(ini_path))
        # alembic env.py 自己会读 settings.database_url_sync，不在这里传 -x url
        command.upgrade(cfg, "head")
        logging.info("alembic upgrade head 完成（启动期自动迁移）")
    except Exception:  # noqa: BLE001
        # 不打 exc_info=True 时也带 traceback；这里需要明显 → 用 ERROR
        logging.exception(
            "alembic 启动期自动迁移失败；服务仍会继续启动，请尽快手动 `make migrate` 排查"
        )
    finally:
        if lock_connection is not None:
            if lock_acquired:
                try:
                    lock_connection.execute(
                        text("SELECT pg_advisory_unlock(:k)"),
                        {"k": _MIGRATION_ADVISORY_LOCK_KEY},
                    )
                except Exception:  # noqa: BLE001
                    logging.exception("释放迁移 advisory lock 失败；连接关闭后将自动释放")
            lock_connection.close()
        if engine is not None:
            engine.dispose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动 supervisor + login 清理任务，退出时优雅关停。"""
    _warn_if_forwarded_for_misconfigured()
    # 0) 启动期自动 alembic upgrade head
    #    解决"代码加了新字段、DB 还没跑迁移 → 前端列表 500"那类问题。
    #    失败不阻止启动（用户激进策略：让 service 启起来好排查 + /api/system/health-overview
    #    能看到 alembic.in_sync=False 的明确信号）；只在日志里 ERROR 醒目提示。
    if settings.auto_migrate_on_startup:
        await asyncio.to_thread(_run_alembic_upgrade)
    try:
        await event_trace.refresh_trace_settings()
    except Exception:  # noqa: BLE001
        logging.exception("刷新 Trace 写入配置失败，使用默认配置继续启动")

    # 启动期预加载平台能力缓存。Webhook 等公开入口只读缓存，失败按 fail-closed。
    try:
        await platform_capabilities.bootstrap_from_db()
    except Exception:  # noqa: BLE001
        logging.exception("预加载平台能力缓存失败；公开入口将 fail-closed 直至缓存就绪")

    # 启动期加载客户端身份 UA 版本覆盖（system_setting）。失败不阻塞启动。
    try:
        from .services import llm_identity

        await llm_identity.load_version_overrides_from_db()
    except Exception:  # noqa: BLE001
        logging.exception("加载客户端身份 UA 版本覆盖失败，使用默认版本继续启动")

    try:
        interrupted_jobs = await plugin_config_action_jobs.startup_plugin_config_action_jobs()
        if interrupted_jobs:
            logging.warning("已收敛 %d 个上次进程遗留的插件配置任务", interrupted_jobs)
    except Exception:  # noqa: BLE001
        logging.exception("收敛遗留插件配置任务失败")

    # 1) 启动登录会话清理后台任务（每 60s 扫一次）
    cleanup_task = asyncio.create_task(cleanup_expired_loop())
    remote_plugin_update_task = asyncio.create_task(remote_plugin_service.auto_update_check_loop())

    retry_tasks: list[asyncio.Task[None]] = []
    for component in _RUNTIME_COMPONENTS:
        _RUNTIME_COMPONENTS[component] = False
    _RUNTIME_COMPONENT_ERRORS.clear()

    # 2) 拉起 worker supervisor；失败时 readiness 保持失败并后台重试。
    stop_all_workers = None
    try:
        from .worker.supervisor import start_supervisor
        from .worker.supervisor import stop_all_workers as _stop_all
    except ImportError:
        logging.warning("worker.supervisor 导入失败，本进程不会拉起 worker 子进程")
        _RUNTIME_COMPONENT_ERRORS["worker_supervisor"] = "ImportError: worker.supervisor 导入失败"
    else:
        stop_all_workers = _stop_all
        if not await _start_runtime_component("worker_supervisor", start_supervisor):
            retry_tasks.append(
                asyncio.create_task(
                    _retry_runtime_component("worker_supervisor", start_supervisor),
                    name="retry-worker-supervisor",
                )
            )

    # 2-D: 项目启动通知（若未配置 NotifyBot，send 会返回 False 并静默）
    try:
        await notify_service.send(None, f"📦 telepilot v{__version__} started")
    except Exception:  # noqa: BLE001
        logging.exception("发送启动通知失败")

    # 2-E: 账号绑定普通 Bot polling runtime（每账号独立 Bot）。
    if not await _start_runtime_component(
        "account_bot_manager",
        account_bot_runtime.start_account_bot_manager,
    ):
        retry_tasks.append(
            asyncio.create_task(
                _retry_runtime_component(
                    "account_bot_manager",
                    account_bot_runtime.start_account_bot_manager,
                ),
                name="retry-account-bot-manager",
            )
        )

    # 2-F: 高频群互动使用独立交互 Bot runtime，避免和管理 Bot 生命周期混在一起。
    # Interaction Bot 模块关闭时不启动 manager（管理 Bot 仍由 account_bot_manager 负责）。
    if not await _start_runtime_component(
        "interaction_bot_manager",
        _start_interaction_bot_component,
    ):
        retry_tasks.append(
            asyncio.create_task(
                _retry_runtime_component(
                    "interaction_bot_manager",
                    _start_interaction_bot_component,
                ),
                name="retry-interaction-bot-manager",
            )
        )

    try:
        await platform_capabilities.reconcile_runtime_after_startup()
    except Exception:  # noqa: BLE001
        logging.exception("平台能力 runtime 启动收敛失败")

    try:
        yield
    finally:
        # 3) 退出：取消清理任务 + 关停所有 worker
        for task in retry_tasks:
            task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        try:
            await plugin_config_action_jobs.shutdown_plugin_config_action_jobs()
        except Exception:  # noqa: BLE001
            logging.exception("停止插件配置后台任务失败")
        try:
            await interaction_bot_runtime.stop_interaction_bot_manager()
        except Exception:  # noqa: BLE001
            logging.exception("停止 interaction bot manager 失败")
        try:
            await account_bot_runtime.stop_account_bot_manager()
        except Exception:  # noqa: BLE001
            logging.exception("停止 account bot manager 失败")
        cleanup_task.cancel()
        remote_plugin_update_task.cancel()
        try:
            await cleanup_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await remote_plugin_update_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if stop_all_workers is not None:
            try:
                await stop_all_workers()
            except Exception:  # noqa: BLE001
                logging.exception("stop_all_workers 失败")
        try:
            await event_trace.stop_trace_writer()
        except Exception:  # noqa: BLE001
            logging.exception("停止 Trace 后台写入器失败")
        for component in _RUNTIME_COMPONENTS:
            _RUNTIME_COMPONENTS[component] = None
        _RUNTIME_COMPONENT_ERRORS.clear()


app = FastAPI(title="TelePilot", version=__version__, lifespan=lifespan)


# ── CORS ──────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _csrf_required(method: str) -> bool:
    """仅对非安全方法要求自定义头，防止 cookie-based CSRF。"""
    return method.upper() not in {"GET", "HEAD", "OPTIONS"}


def _is_public_webhook_delivery(request: Request) -> bool:
    """Only external webhook deliveries are token-authenticated and CSRF-exempt."""
    if request.method.upper() != "POST":
        return False
    parts = request.url.path.strip("/").split("/")
    return (
        len(parts) == 4
        and parts[0] == "api"
        and parts[1] == "webhooks"
        and parts[2].isdigit()
        and bool(parts[3])
    )


def _request_uses_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return settings.cookie_secure or request.url.scheme == "https" or forwarded_proto == "https"


def _set_csrf_cookie(response: JSONResponse, request: Request) -> JSONResponse:
    """下发 double-submit CSRF token：JS 可读 cookie + 写请求 header 回传。"""
    response.set_cookie(
        key=_CSRF_TOKEN_COOKIE,
        value=secrets.token_urlsafe(32),
        max_age=12 * 3600,
        httponly=False,
        samesite="lax",
        secure=_request_uses_https(request),
    )
    return response


def _with_security_headers(response):
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if settings.cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/api/auth/csrf")
async def csrf_token(request: Request) -> JSONResponse:
    return _set_csrf_cookie(JSONResponse(content={"ok": True}), request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    return _with_security_headers(response)


@app.middleware("http")
async def csrf_header_middleware(request: Request, call_next):
    # 公开 webhook 投递端点由账号级 token 鉴权，外部系统无法携带 UI CSRF 头。
    # 同 router 下的 cookie 鉴权管理端点仍必须走 CSRF 检查。
    if _is_public_webhook_delivery(request):
        return await call_next(request)
    if _csrf_required(request.method):
        header_val = request.headers.get(_CSRF_HEADER_NAME, "")
        if header_val not in _CSRF_HEADER_VALUES:
            return _with_security_headers(JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "CSRF_HEADER_REQUIRED",
                        "message": f"缺少或非法请求头 {_CSRF_HEADER_NAME}",
                    }
                },
            ))
        csrf_cookie = request.cookies.get(_CSRF_TOKEN_COOKIE, "")
        csrf_header = request.headers.get(_CSRF_TOKEN_HEADER, "")
        if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
            return _with_security_headers(JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "CSRF_TOKEN_REQUIRED",
                        "message": f"缺少或非法请求头 {_CSRF_TOKEN_HEADER}",
                    }
                },
            ))
    return await call_next(request)


# ── 全局异常 handler：把 HTTPException 的结构化 detail 转成 {"error":...} ──
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        return JSONResponse(status_code=exc.status_code, content={"error": detail})
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "HTTP", "message": str(detail)}},
    )


@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception):
    logging.exception("未处理异常: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL", "message": "服务器内部错误"}},
    )


# ── Router ────────────────────────────────────────────────────────
app.include_router(auth_api.router)
app.include_router(accounts_api.router)
app.include_router(account_bots_api.router)
app.include_router(rate_limit_api.router)   # C Agent：风控 + 拟人化 + 全局总闸
app.include_router(logs_api.router)         # 主会话补：审计日志 + 运行日志
app.include_router(proxies_api.router)      # 主会话补：代理 CRUD + 连通性测试
app.include_router(device_profiles_api.router)  # 设备伪装库：device_model / app_version / lang_code
app.include_router(network_api.router)      # 主会话补：当前网络环境探测
app.include_router(notify_bots_api.router)  # Sprint4 #2D：多 Telegram Bot 通知
app.include_router(sudo_api.router)        # Sprint5：Sudo 用户管理
app.include_router(alias_api.router)      # Sprint5：命令别名管理
app.include_router(config_bundle_api.router)  # B1：Config Bundle export / dry-run
app.include_router(llm_usage_api.router)  # AI 中心：最近 LLM 调用记录
app.include_router(message_templates_api.router)  # 消息模板实验室
app.include_router(dispatch_debug_api.router)  # WP4：命中调试器接口空桩
app.include_router(ledger_api.router)  # WP5：资金台账接口空桩
app.include_router(webhooks_api.router)  # WP7：入站 Webhook 接口空桩
app.include_router(system_agent_api.router)  # System Agent：自然语言系统助手
app.include_router(platform_capabilities_api.router)  # 平台能力热插拔


# ── 健康检查 ─────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    """liveness：进程是否还在跑（不查依赖）。"""
    return {"ok": True}


@app.get("/readyz")
async def readyz() -> dict:
    """readiness：依赖是否健康（DB + Redis 实际 ping）。

    任一依赖不健康都返回 503，便于反代/编排系统据此把流量摘走。
    DB 与 Redis ping **并行执行**，各自 2s 超时；最坏耗时 ~2s 而非串行的 4s
    （后者会踩 docker compose healthcheck timeout: 5s 的边缘）。
    """
    import asyncio as _asyncio

    from sqlalchemy import text as _text

    from .db.base import AsyncSessionLocal
    from .redis_client import get_redis

    async def _db_ping() -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(_text("SELECT 1"))

    async def _redis_ping() -> None:
        r = get_redis()
        pong = await r.ping()
        if not pong:
            raise RuntimeError("redis PING returned falsy")

    # 并行：两个探测同时跑，各自带 2s 超时
    db_task = _asyncio.wait_for(_db_ping(), timeout=2.0)
    redis_task = _asyncio.wait_for(_redis_ping(), timeout=2.0)
    db_res, redis_res = await _asyncio.gather(db_task, redis_task, return_exceptions=True)

    checks: dict[str, dict] = {}
    overall_ok = True

    if isinstance(db_res, BaseException):
        checks["db"] = {"ok": False, "error": str(db_res)[:200]}
        overall_ok = False
    else:
        checks["db"] = {"ok": True}

    if isinstance(redis_res, BaseException):
        checks["redis"] = {"ok": False, "error": str(redis_res)[:200]}
        overall_ok = False
    else:
        checks["redis"] = {"ok": True}

    for name, component_ok in _RUNTIME_COMPONENTS.items():
        if component_ok is None:
            continue
        if component_ok:
            checks[name] = {"ok": True}
        else:
            checks[name] = {
                "ok": False,
                "error": _RUNTIME_COMPONENT_ERRORS.get(name, "组件尚未启动"),
            }
            overall_ok = False

    body = {"ok": overall_ok, "checks": checks}
    if not overall_ok:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content=body)
    return body


# === 以下 router 由其他 Agent 追加 ===

# Agent D：功能矩阵 / 规则 / 插件市场
from .api import features as features_api  # noqa: E402
from .api import plugins as plugins_api  # noqa: E402
from .api import plugins_install as plugins_install_api  # noqa: E402
from .api import rules as rules_api  # noqa: E402

app.include_router(features_api.router)
app.include_router(rules_api.router)
app.include_router(plugins_api.router)
# Sprint2 #4：第三方插件 zip 上传 / 启停 / 卸载
app.include_router(plugins_install_api.router)

# Sprint2 #3 Ignored Peers
from .api import ignored_peers as ignored_peers_api  # noqa: E402

app.include_router(ignored_peers_api.router)

# Sprint2 #2 Custom Commands（命令模板 + LLM provider）
from .api import commands as commands_api  # noqa: E402

app.include_router(commands_api.router)

# 系统健康概览（DB / alembic / redis / providers / proxies / workers）
from .api import system_health as system_health_api  # noqa: E402

app.include_router(system_health_api.router)

# 远程插件管理（git clone 安装的第三方插件）
from .api import remote_plugin as remote_plugin_api  # noqa: E402

app.include_router(remote_plugin_api.router)

# 插件仓库管理（可浏览的 Git 仓库列表 + 选择性安装其中插件）
from .api import plugin_repo as plugin_repo_api  # noqa: E402

app.include_router(plugin_repo_api.router)
