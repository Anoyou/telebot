"""TelePilot 内部 Web/PWA API 的 OpenAPI 契约补充。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic import BaseModel

from .deps import get_current_user

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
OPENAPI_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
PUBLIC_OPERATIONS = {
    ("GET", "/api/auth/csrf"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/webhooks/{account_id}/{hook_key}"),
    ("GET", "/api/system/version"),
    ("GET", "/healthz"),
    ("GET", "/readyz"),
}


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


def route_requires_auth(route: APIRoute) -> bool:
    pending = [route.dependant]
    seen: set[int] = set()
    while pending:
        dependant = pending.pop()
        marker = id(dependant)
        if marker in seen:
            continue
        seen.add(marker)
        if dependant.call is get_current_user:
            return True
        pending.extend(dependant.dependencies)
    return False


def operation_security(route: APIRoute, method: str) -> list[dict[str, list[str]]]:
    method = method.upper()
    if method == "POST" and route.path == "/api/webhooks/{account_id}/{hook_key}":
        return [{"WebhookToken": []}]

    requirement: dict[str, list[str]] = {}
    if route_requires_auth(route):
        requirement["AuthCookie"] = []
    if method not in SAFE_METHODS:
        requirement.update(
            {
                "CsrfCookie": [],
                "CsrfHeader": [],
                "RequestedWith": [],
            }
        )
    return [requirement] if requirement else []


def _error_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
            }
        },
    }


def build_openapi_schema(app: FastAPI) -> dict[str, Any]:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["ErrorDetail"] = ErrorDetail.model_json_schema()
    schemas["ErrorEnvelope"] = {
        "properties": {
            "error": {"$ref": "#/components/schemas/ErrorDetail"},
        },
        "required": ["error"],
        "title": "ErrorEnvelope",
        "type": "object",
    }
    components["securitySchemes"] = {
        "AuthCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "auth_token",
            "description": "Web 登录后由服务端写入的 HttpOnly Cookie。",
        },
        "CsrfCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "csrf_token",
            "description": "写请求 double-submit CSRF Cookie。",
        },
        "CsrfHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-CSRF-Token",
            "description": "必须与 csrf_token Cookie 相同。",
        },
        "RequestedWith": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Requested-With",
            "description": "Web/PWA 写请求固定为 telepilot-ui。",
        },
        "WebhookToken": {
            "type": "apiKey",
            "in": "header",
            "name": "X-TelePilot-Webhook-Token",
            "description": "外部 Webhook 投递使用的账号级 Token。",
        },
    }

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path_item = schema.get("paths", {}).get(route.path_format, {})
        for method in route.methods or set():
            operation = path_item.get(method.lower())
            if not isinstance(operation, dict):
                continue
            security = operation_security(route, method)
            operation["security"] = security
            responses = operation.setdefault("responses", {})
            security_names = set(security[0]) if security else set()
            if "AuthCookie" in security_names or "WebhookToken" in security_names:
                responses.setdefault("401", _error_response("认证失败"))
            if method.upper() not in SAFE_METHODS and "WebhookToken" not in security_names:
                responses.setdefault("403", _error_response("CSRF 校验失败或权限不足"))
            responses.setdefault("500", _error_response("服务器内部错误"))
    return schema


def install_openapi_contract(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi_schema(app)
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def iter_api_operations(app: FastAPI) -> list[tuple[APIRoute, str]]:
    operations: list[tuple[APIRoute, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods or set()):
            if method.lower() in OPENAPI_METHODS:
                operations.append((route, method))
    return operations


__all__ = [
    "ErrorDetail",
    "ErrorEnvelope",
    "PUBLIC_OPERATIONS",
    "build_openapi_schema",
    "install_openapi_contract",
    "iter_api_operations",
    "operation_security",
    "route_requires_auth",
]
