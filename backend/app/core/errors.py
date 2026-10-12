"""Consistent JSON error responses.

Every error returned by the API has the same shape::

    {"error": {"code": "not_found", "message": "Not Found", "request_id": "..."}}

Validation errors additionally include a ``details`` list.
"""

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


def _status_code_name(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        return "error"


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, object]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": request_id_var.get(),
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


async def _http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    return error_response(
        exc.status_code, _status_code_name(exc.status_code), message, headers=exc.headers
    )


async def _validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"location": list(err.get("loc", ())), "message": err.get("msg", ""), "type": err["type"]}
        for err in exc.errors()
    ]
    return error_response(422, "validation_error", "Request validation failed", details=details)


def unhandled_exception_response(request: Request) -> JSONResponse:
    """Log the active exception with its traceback and return a generic 500."""
    logger.exception(
        "unhandled_exception", extra={"path": request.url.path, "method": request.method}
    )
    # Never leak exception messages or internals to the client.
    return error_response(500, "internal_server_error", "An unexpected error occurred")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)  # type: ignore[arg-type]
