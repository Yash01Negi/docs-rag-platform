"""Request body size limit for uploads.

Starlette parses a multipart body completely (spooling files to disk) before the endpoint
runs, so the upload size check in the endpoint alone would not stop a client from streaming
gigabytes. This ASGI middleware rejects oversized bodies early: from ``Content-Length`` when
it is present, and by counting bytes as they arrive when it is not.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import ApiError, error_response

# Room for multipart boundaries and part headers on top of the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, path: str, max_body_bytes: int) -> None:
        self.app = app
        self.path = path
        self.max_body_bytes = max_body_bytes

    def _error(self) -> ApiError:
        limit_mb = (self.max_body_bytes - MULTIPART_OVERHEAD_BYTES) // (1024 * 1024)
        return ApiError(413, "file_too_large", f"The file is larger than the {limit_mb} MB limit.")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != self.path:
            await self.app(scope, receive, send)
            return

        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_body_bytes:
                    error = self._error()
                    response = error_response(413, error.code, str(error.detail))
                    await response(scope, receive, send)
                    return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    # FastAPI re-raises HTTPExceptions from body parsing unchanged, so this
                    # becomes a normal 413 error envelope.
                    raise self._error()
            return message

        await self.app(scope, limited_receive, send)
