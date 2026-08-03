"""内置 telepilot-gateway 子进程的按需生命周期管理。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from ..crypto import decrypt_str
from ..db.base import AsyncSessionLocal
from ..db.models.command import LLM_API_FORMAT_RESPONSES, LLMProvider
from .llm_dto import LLMProviderDTO
from .llm_request_headers import REQUEST_SCOPE_INFERENCE, request_headers_for_scope
from .redactor import redact_text

log = logging.getLogger(__name__)

GATEWAY_PROTOCOL_VERSION = "1"
DEFAULT_GATEWAY_BINARY = "/usr/local/bin/telepilot-gateway"
DEFAULT_GATEWAY_SOCKET = "/run/telepilot/gateway.sock"


@dataclass(frozen=True, slots=True)
class GatewayRuntimeStatus:
    state: str
    required: bool
    revision: int
    provider_count: int
    version: str | None = None
    error: str | None = None


class GatewayRuntimeManager:
    """仅在存在 codex_gateway Provider 时运行一个独立 Gateway 进程。"""

    def __init__(self, *, binary: str | None = None, socket_path: str | None = None) -> None:
        self.binary = binary or os.getenv("TELEPILOT_GATEWAY_BIN", DEFAULT_GATEWAY_BINARY)
        self.socket_path = socket_path or os.getenv("TELEPILOT_GATEWAY_SOCKET", DEFAULT_GATEWAY_SOCKET)
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._log_task: asyncio.Task[None] | None = None
        self._desired: list[LLMProviderDTO] = []
        self._revision = 0
        self._snapshot_fingerprint = ""
        self._state = "not_required"
        self._version: str | None = None
        self._error: str | None = None
        self._closing = False

    async def reconcile(self, providers: list[LLMProviderDTO]) -> GatewayRuntimeStatus:
        desired = [provider for provider in providers if provider.execution_backend == "codex_gateway"]
        async with self._lock:
            self._desired = desired
            if not desired:
                await self._stop_locked()
                self._state = "not_required"
                self._error = None
                return self.status()
            if self._process is None or self._process.returncode is not None:
                if not await self._start_locked():
                    return self.status()
            try:
                await self._sync_locked(desired)
            except Exception as exc:  # noqa: BLE001
                self._state = "degraded"
                self._error = redact_text(f"{type(exc).__name__}: {exc}")[:300]
                log.warning("Gateway 配置同步失败：%s", self._error)
            return self.status()

    async def reconcile_from_db(self) -> GatewayRuntimeStatus:
        async with AsyncSessionLocal() as db:
            rows = list((await db.execute(select(LLMProvider).order_by(LLMProvider.id))).scalars().all())
        return await self.reconcile([LLMProviderDTO.from_orm_row(row) for row in rows])

    async def shutdown(self) -> None:
        self._closing = True
        async with self._lock:
            self._desired = []
            await self._stop_locked()
            self._state = "not_required"

    def status(self) -> GatewayRuntimeStatus:
        return GatewayRuntimeStatus(
            state=self._state,
            required=bool(self._desired),
            revision=self._revision,
            provider_count=len(self._desired),
            version=self._version,
            error=self._error,
        )

    async def _start_locked(self) -> bool:
        binary_path = Path(self.binary)
        if not binary_path.is_file() or not os.access(binary_path, os.X_OK):
            self._state = "degraded"
            self._error = f"Gateway 二进制不存在或不可执行：{binary_path}"
            return False
        self._state = "starting"
        self._error = None
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(binary_path),
                "-socket",
                self.socket_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._log_task = asyncio.create_task(self._consume_logs(self._process), name="gateway-log-reader")
            await self._wait_health()
            version = await self._request_json("GET", "/version")
            if str(version.get("gateway_protocol_version") or "") != GATEWAY_PROTOCOL_VERSION:
                raise RuntimeError("Gateway 协议版本不兼容")
            self._version = str(version.get("version") or "") or None
            self._watch_task = asyncio.create_task(self._watch_process(self._process), name="gateway-process-watch")
            return True
        except Exception as exc:  # noqa: BLE001
            self._state = "degraded"
            self._error = redact_text(f"Gateway 启动失败：{type(exc).__name__}: {exc}")[:300]
            await self._terminate_process()
            return False

    async def _sync_locked(self, providers: list[LLMProviderDTO]) -> None:
        payload = self._build_snapshot(providers)
        fingerprint = hashlib.sha256(
            json.dumps(payload["providers"], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if fingerprint == self._snapshot_fingerprint and self._state == "ready":
            return
        self._revision += 1
        payload["revision"] = self._revision
        result = await self._request_json("PUT", "/internal/v1/config", json_body=payload)
        if int(result.get("revision") or 0) != self._revision:
            raise RuntimeError("Gateway 返回了不一致的配置 revision")
        self._snapshot_fingerprint = fingerprint
        self._state = "ready"
        self._error = None

    def _build_snapshot(self, providers: list[LLMProviderDTO]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for provider in providers:
            if provider.api_format != LLM_API_FORMAT_RESPONSES:
                raise ValueError(f"Provider {provider.id} 的 Gateway 仅支持 Responses")
            if not provider.base_url or not provider.api_key_enc:
                raise ValueError(f"Provider {provider.id} 缺少 Base URL 或 API Key")
            models = provider.enabled_model_ids() or ([provider.default_model] if provider.default_model else [])
            items.append(
                {
                    "id": provider.id,
                    "base_url": provider.base_url,
                    "api_key": decrypt_str(provider.api_key_enc),
                    "models": models,
                    "proxy_url": provider.proxy_url or "",
                    "timeout_seconds": 90,
                    "compatibility_headers": request_headers_for_scope(
                        provider.request_headers_enc,
                        REQUEST_SCOPE_INFERENCE,
                    ),
                    "max_concurrency": 8,
                }
            )
        return {
            "schema_version": 1,
            "gateway_protocol_version": GATEWAY_PROTOCOL_VERSION,
            "revision": 0,
            "providers": items,
        }

    async def _wait_health(self) -> None:
        deadline = asyncio.get_running_loop().time() + 10.0
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            if self._process is None or self._process.returncode is not None:
                raise RuntimeError("Gateway 在健康检查前退出")
            try:
                await self._request_json("GET", "/healthz")
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                await asyncio.sleep(0.1)
        raise TimeoutError(f"Gateway 健康检查超时：{last_error}")

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway", timeout=10.0) as client:
            response = await client.request(method, path, json=json_body)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Gateway 返回的 JSON 不是对象")
        return payload

    async def _watch_process(self, process: asyncio.subprocess.Process) -> None:
        return_code = await process.wait()
        if self._closing:
            return
        self._state = "degraded"
        self._error = f"Gateway 意外退出（code={return_code}）"
        delay = 0.5
        while self._desired and not self._closing:
            await asyncio.sleep(delay)
            async with self._lock:
                if await self._start_locked():
                    try:
                        self._snapshot_fingerprint = ""
                        await self._sync_locked(self._desired)
                        return
                    except Exception as exc:  # noqa: BLE001
                        self._error = redact_text(str(exc))[:300]
            delay = min(delay * 2, 30.0)

    async def _consume_logs(self, process: asyncio.subprocess.Process) -> None:
        if process.stdout is None:
            return
        while line := await process.stdout.readline():
            safe_line = redact_text(line.decode("utf-8", errors="replace").strip())[:500]
            if safe_line:
                log.info("[gateway] %s", safe_line)

    async def _stop_locked(self) -> None:
        current = asyncio.current_task()
        for task in (self._watch_task, self._log_task):
            if task is not None and task is not current:
                task.cancel()
        await self._terminate_process()
        self._watch_task = None
        self._log_task = None
        self._version = None
        self._snapshot_fingerprint = ""

    async def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except TimeoutError:
            process.kill()
            await process.wait()


gateway_runtime_manager = GatewayRuntimeManager()


async def reconcile_gateway_runtime() -> GatewayRuntimeStatus:
    return await gateway_runtime_manager.reconcile_from_db()
