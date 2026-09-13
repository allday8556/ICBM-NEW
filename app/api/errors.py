"""Maps exceptions onto the canonical error envelope."""

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.correlation import get_correlation_id
from app.core.errors import HTTP_STATUS, AppError, ErrorClass, error_envelope

logger = logging.getLogger("icbm.http")

_HTTP_CLASS = {
    401: ErrorClass.AUTH,
    403: ErrorClass.POLICY_BLOCKED,
    404: ErrorClass.NOT_FOUND,
    429: ErrorClass.RATE_LIMITED,
}


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    level = logging.ERROR if exc.error_class is ErrorClass.UNKNOWN else logging.WARNING
    logger.log(
        level,
        "http.app_error",
        extra={"error_class": exc.error_class, "error_code": exc.code, "path": request.url.path},
    )
    return JSONResponse(
        exc.envelope(get_correlation_id()), status_code=HTTP_STATUS[exc.error_class]
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    body = error_envelope(
        ErrorClass.VALIDATION,
        "REQUEST_INVALID",
        "request failed validation",
        get_correlation_id(),
        details={"errors": jsonable_encoder(exc.errors())},
    )
    return JSONResponse(body, status_code=HTTP_STATUS[ErrorClass.VALIDATION])


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = exc.status_code
    default = ErrorClass.UNKNOWN if status >= 500 else ErrorClass.VALIDATION
    body = error_envelope(
        _HTTP_CLASS.get(status, default), f"HTTP_{status}", str(exc.detail), get_correlation_id()
    )
    return JSONResponse(body, status_code=status, headers=getattr(exc, "headers", None))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
