"""Errors as application/problem+json with a stable `code`."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

CODES = {
    "invalid_request",
    "unauthorized",
    "not_found",
    "rate_limited",
    "queue_full",
    "conflict",
    "engine_timeout",
    "engine_failed",
    "schema_mismatch",
    "canceled",
    "internal_error",
}
_TITLES = {
    400: "Bad request",
    401: "Unauthorized",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    422: "Invalid request",
    429: "Too many requests",
    500: "Internal error",
    503: "Service unavailable",
}
_CODE_FOR_STATUS = {
    400: "invalid_request",
    401: "unauthorized",
    404: "not_found",
    405: "invalid_request",
    409: "conflict",
    422: "invalid_request",
    429: "rate_limited",
    503: "queue_full",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str, headers: dict[str, str] | None = None) -> None:
        assert code in CODES, code
        super().__init__(detail)
        self.status, self.code, self.detail, self.headers = status, code, detail, headers or {}

    def problem(self) -> dict[str, Any]:
        return problem(self.status, self.code, self.detail)


def problem(status: int, code: str, detail: str) -> dict[str, Any]:
    return {
        "type": f"urn:last30days:error:{code}",
        "title": _TITLES.get(status, "Error"),
        "status": status,
        "code": code,
        "detail": detail,
    }


def _respond(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return _respond(exc.status, exc.problem(), exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODE_FOR_STATUS.get(exc.status_code, "internal_error")
        hdrs = dict(exc.headers) if exc.headers else None
        return _respond(exc.status_code, problem(exc.status_code, code, str(exc.detail)), hdrs)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        detail = f"{where}: {first.get('msg', 'invalid')}" if where else "invalid request"
        return _respond(422, problem(422, "invalid_request", detail))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        return _respond(500, problem(500, "internal_error", "unexpected server error"))
