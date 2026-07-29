from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.main import app, http_exc_handler
from app.openapi_contract import (
    PUBLIC_OPERATIONS,
    iter_api_operations,
    operation_security,
    route_requires_auth,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = REPO_ROOT / "openapi" / "telepilot.openapi.json"


def _operation(schema: dict, path: str, method: str) -> dict:
    return schema["paths"][path][method.lower()]


def test_operation_ids_are_present_and_unique() -> None:
    operation_ids: list[str] = []
    schema = app.openapi()
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method.lower() not in {"get", "put", "post", "delete", "patch", "head", "options"}:
                continue
            operation_ids.append(operation.get("operationId", ""))

    assert all(operation_ids)
    assert len(operation_ids) == len(set(operation_ids))


def test_success_responses_have_schemas_except_204() -> None:
    schema = app.openapi()
    missing: list[str] = []
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method.lower() not in {"get", "put", "post", "delete", "patch"}:
                continue
            success = [
                (status, response)
                for status, response in operation.get("responses", {}).items()
                if str(status).startswith("2")
            ]
            if not success:
                missing.append(f"{method.upper()} {path}: no 2xx")
                continue
            for status, response in success:
                if str(status) == "204":
                    continue
                content = response.get("content", {}).get("application/json", {})
                if "schema" not in content:
                    missing.append(f"{method.upper()} {path}: {status} no schema")
    assert missing == []


def test_security_schemes_and_operation_matrix_match_runtime_dependencies() -> None:
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert set(schemes) == {
        "AuthCookie",
        "CsrfCookie",
        "CsrfHeader",
        "RequestedWith",
        "WebhookToken",
    }

    for route, method in iter_api_operations(app):
        operation = _operation(schema, route.path_format, method)
        expected = operation_security(route, method)
        assert operation["security"] == expected, f"{method} {route.path_format}"
        security_names = set(expected[0]) if expected else set()
        responses = operation["responses"]
        if "AuthCookie" in security_names or "WebhookToken" in security_names:
            assert "401" in responses
        if method not in {"GET", "HEAD", "OPTIONS"} and "WebhookToken" not in security_names:
            assert "403" in responses


def test_unauthenticated_operation_allowlist_is_explicit() -> None:
    actual = {
        (method, route.path_format)
        for route, method in iter_api_operations(app)
        if not route_requires_auth(route)
    }
    assert actual == PUBLIC_OPERATIONS


def test_error_contract_keeps_business_and_validation_shapes() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert "ErrorEnvelope" in schemas
    assert "ErrorDetail" in schemas
    assert "HTTPValidationError" in schemas


@pytest.mark.asyncio
async def test_http_exception_headers_are_preserved() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    response = await http_exc_handler(
        request,
        HTTPException(
            status_code=429,
            detail={"code": "RATE_LIMIT", "message": "请稍后重试"},
            headers={"Retry-After": "3"},
        ),
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "3"
    assert json.loads(response.body)["error"]["code"] == "RATE_LIMIT"


def test_committed_openapi_snapshot_matches_runtime() -> None:
    assert SNAPSHOT.is_file(), "请运行 make codegen 生成 OpenAPI 快照"
    assert json.loads(SNAPSHOT.read_text(encoding="utf-8")) == app.openapi()
