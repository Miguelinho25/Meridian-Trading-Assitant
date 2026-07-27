"""Request IDs and redacted request logging (security.md §4)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from meridian_config import get_logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request ID, bind it to the log context, and time the request.

    The ID is echoed in the response header so a user reporting a problem can
    quote it and have it match an audit event.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or f"req_{uuid.uuid4().hex[:16]}"

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "request_failed", duration_ms=round((time.perf_counter() - started) * 1000, 2)
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id

        # Query strings can carry user data; log the path only (security.md privacy).
        log.info("request", status=response.status_code, duration_ms=duration_ms)
        return response
