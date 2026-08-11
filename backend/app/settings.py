"""全局配置：从 .env 加载，所有模块统一通过 settings.* 读取。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
# 本地源码树的配置/插件目录位于仓库根；生产镜像把 backend 直接复制到 /app。
# 不能固定 parents[2]，否则容器中会错误提升为文件系统根目录 `/`。
PROJECT_ROOT = (
    _BACKEND_ROOT.parent
    if (_BACKEND_ROOT.parent / "frontend").is_dir()
    and (_BACKEND_ROOT.parent / "plugins").is_dir()
    else _BACKEND_ROOT
)


class Settings(BaseSettings):
    """应用配置，所有字段都可通过环境变量覆盖（pydantic-settings 自动映射）。"""

    # ── 加密 / 认证 ────────────────────────────────────────────────
    master_key: str = Field(..., description="Fernet 主密钥，加密 session 等敏感字段")
    jwt_secret: str = Field(..., description="JWT HS256 签名密钥")
    jwt_expire_seconds: int = 12 * 3600
    # Cookie 安全：HTTPS 部署应设为 true（反代后由后端直接打 Secure=True，少一层依赖）
    # 默认 false，方便本地 HTTP 调试；生产 .env 显式设 COOKIE_SECURE=true
    cookie_secure: bool = False
    # 登录限速（针对 /api/auth/login 与 /api/auth/register）
    # 0 表示不限速；默认 30 次/分钟，按 IP+用户名两个维度同时计数
    login_rate_limit_per_min: int = 30
    # 兼容旧 Webhook URL 的 query token；生产默认关闭，避免 token 进入访问日志。
    webhook_allow_query_token: bool = False
    # 登录密码连续失败达到阈值后，下一次正确密码需要通知 Bot OTP。
    # 默认关闭；生产建议在 Web 设置页确认恢复码路径和通知 Bot 后再开启。
    login_otp_failed_attempt_threshold: int = 0
    login_otp_fail_window_seconds: int = 15 * 60
    login_otp_ttl_seconds: int = 5 * 60
    login_otp_max_attempts: int = 3
    # 登录向导挂起会话上限（防止批量 start_login 造成进程内存占用过高）
    max_pending_logins: int = 100
    # 是否信任 X-Forwarded-For 取客户端 IP；
    # 仅当部署在可信反代（nginx/traefik）后面时才设为 true，否则攻击者可通过伪造头绕过 IP 限速
    trust_forwarded_for: bool = False

    # ── 数据库 / Redis ─────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://telebot:telebot@localhost:5432/telebot"
    db_pool_size: int = 5
    db_max_overflow: int = 2
    db_pool_timeout: int = 30
    # Worker 子进程默认更紧的连接池：仍需覆盖命令/全局 pubsub、
    # 周期任务、插件 Redis 锁与 IPC 回执，避免长连接把池子占满。
    # worker.entry.worker_entry 会在 import runtime 之前设 ``TELEBOT_WORKER_PROC=1``，
    # db.base / redis_client 据此切到下面的 ``*_worker`` 默认值。
    db_pool_size_worker: int = 1
    # 允许短时突发借 1 条连接；10 账号四协程压测 p99 等待 <1s，且避免击穿 PG 连接上限。
    db_max_overflow_worker: int = 1
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 16
    redis_max_connections_worker: int = 15

    # Worker 周期性配置 reconcile 间隔（秒）；只是 IPC 丢消息兜底，
    # 180s 足够，原 60s 在多账号下产生不必要的 DB 抖动。
    worker_reconcile_seconds: int = 180

    # 是否在每条 incoming TG 消息上都额外写一行可见性 runtime_log。
    # 默认关闭：活跃账号每分钟可能数百条，对小机器是显著开销。
    # 不影响命令派发 / 插件错误 / 业务事件——它们各自独立写日志。
    # 也可在系统设置（``system_setting`` 表 key=``log_incoming_messages``）覆盖。
    log_incoming_messages_default: bool = False

    # ── Web ────────────────────────────────────────────────────────
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    cors_origins: str = "http://localhost:5173"

    # ── userbot ────────────────────────────────────────────────────
    command_prefix: str = ","
    # 群聊抽奖防误触：自己发出的"纯命令"若在前 N 条消息内出现过相同的他人消息，
    # 视为参与抽奖/接龙等回声，不触发命令。0 表示关闭。
    command_echo_guard_previous_messages: int = 8
    # 已废弃且无业务消费者；保留字段只为旧环境变量继续通过 Settings 解析。
    session_dir: str = "./sessions"
    # 头像本地缓存目录；主进程通过 IPC 让 worker 写盘 ``{aid}.jpg``
    # 24h TTL，worker 离线时返 404 → 前端首字母 fallback
    avatars_dir: str = "./data/avatars"

    # ── 第三方插件（阶段 B/C） ────────────────────────────────
    # 已安装第三方插件的根目录；loader.discover_plugins 会扫描这里下的子目录。
    # 对应 worker/plugins/loader.py 中的 _INSTALLED_DIR；二者一定要一致。
    plugins_installed_dir: str = "./plugins/installed"
    # 插件仓库（plugin_repo）本地克隆缓存目录；用于浏览仓库内可装插件而不重复克隆。
    plugin_repos_cache_dir: str = "./data/plugin_repos"
    # 上传 zip 时验签使用的 Ed25519 公钥（PEM）；为空表示不验签，前端给出"未签名"警告。
    # 公钥示例：-----BEGIN PUBLIC KEY-----\nMC...\n-----END PUBLIC KEY-----
    plugin_pubkey: str = ""
    # 兼容旧版本已安装且 signature_ok=NULL 的 zip 插件；新安装流程仍强制验签通过。
    # 设为 false 后，历史未签名插件即使 PluginInstall.enabled=true 也不会被 worker 加载。
    plugin_allow_legacy_unsigned_plugins: bool = True
    # 新上传 ZIP 是否允许免签安装；与历史加载兼容分离，生产默认拒绝。
    plugin_allow_new_unsigned_plugins: bool = False
    # 上传 zip 体积上限（字节），默认 10 MiB。超出直接 413。
    plugin_zip_max_bytes: int = 10 * 1024 * 1024
    # 解压资源上限：必须在执行 manifest.py 前完成检查，防止压缩炸弹耗尽磁盘/inode。
    plugin_zip_max_members: int = 512
    plugin_zip_max_member_bytes: int = 32 * 1024 * 1024
    plugin_zip_max_uncompressed_bytes: int = 100 * 1024 * 1024
    plugin_zip_max_compression_ratio: int = 200

    # 全局默认 Telegram 代理（仅当账号未绑定 Proxy 行时兜底）。
    # 格式：``socks5://[user:pass@]host:port``、``socks4://host:port`` 或 ``http://host:port``
    # SOCKS4 仅供 Telethon 兼容；插件 HTTP、LLM 与 Gateway 不支持。
    # 留空 = 直连（在能直接访问 Telegram 的网络下使用）
    tg_default_proxy: str = ""

    # ── 全局风控 ──────────────────────────────────────────────────
    kill_switch: bool = False

    # ── LLM 成本控制 ───────────────────────────────────────────────
    # 以下限制按账号生效，0 表示关闭该项限制。限制在 worker 调用 LLM 前检查，
    # 防止 scheduler / sudo / 误配置高价模型造成不可追踪的成本飙升。
    llm_per_minute_request_limit_per_account: int = 0
    llm_daily_request_limit_per_account: int = 0
    llm_daily_token_limit_per_account: int = 0
    # cost_tier >= 3 视为高价模型；0 表示不限制。
    llm_premium_daily_request_limit_per_account: int = 0
    # 0 表示不覆盖调用方传入的 max_tokens。
    llm_max_output_tokens: int = 0
    # 第三方插件 ctx.ai 的额外保护上限；仅放宽调用方可请求的上限，
    # 普通插件仍按各自传入的 max_tokens 执行。长时间离线建库任务可主动请求大输出。
    plugin_ai_max_output_tokens: int = 131072
    plugin_ai_timeout_seconds: int = 600
    # 插件 handler 协作式 deadline 与连续失败熔断。不是 CPU/恶意代码沙箱。
    plugin_invoke_timeout_seconds: float = 30.0
    plugin_circuit_failure_threshold: int = 3
    plugin_circuit_cooldown_seconds: float = 60.0

    # ── 启动期自动迁移 ────────────────────────────────────────────
    # True = backend 启动时自动 ``alembic upgrade head``，把 DB schema 升到代码期望的版本
    #        适合单实例或开发环境
    # False = 完全不动 DB；适合多实例部署、由 CI/CD 单独跑迁移的场景，避免并发起服务时 race
    # 默认 false：与 docker-compose 的"启动前迁移"职责分离，避免重复执行迁移
    auto_migrate_on_startup: bool = False

    # ── 日志 ───────────────────────────────────────────────────────
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=(
            # 优先从仓库根目录 .env 加载
            PROJECT_ROOT / ".env",
            ".env",
        ),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """以逗号分隔的 CORS 源解析为 list。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def database_url_sync(self) -> str:
        """Alembic 用的同步 DSN（去掉 +asyncpg 后缀，psycopg2/psycopg 都能识别）。"""
        return self.database_url.replace("+asyncpg", "")

    def resolve_project_path(self, raw: str) -> Path:
        """把相对路径固定解析到仓库根目录，避免主进程/worker cwd 不同导致分裂。"""
        path = Path(raw)
        if path.is_absolute():
            if PROJECT_ROOT == Path("/app"):
                legacy_container_paths = {
                    Path("/plugins/installed"): PROJECT_ROOT / "plugins" / "installed",
                    Path("/data/plugin_repos"): PROJECT_ROOT / "data" / "plugin_repos",
                }
                if path in legacy_container_paths:
                    return legacy_container_paths[path].resolve()
            return path.resolve()
        return (PROJECT_ROOT / path).resolve()

    @property
    def plugins_installed_path(self) -> Path:
        return self.resolve_project_path(self.plugins_installed_dir)


@lru_cache
def get_settings() -> Settings:
    """单例配置访问器。"""
    return Settings()  # type: ignore[call-arg]


# 顶层导出便于 from app.settings import settings
settings = get_settings()
