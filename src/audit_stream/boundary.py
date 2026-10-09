"""Small, fail-closed HTTP ingress boundary for the local audit sink."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from secrets import compare_digest
from typing import Literal

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .models import SOURCE_PATTERN

MAX_EVENT_BODY_BYTES = 80 * 1024
MAX_CHECKPOINT_BODY_BYTES = 1024
DEFAULT_EVENTS_PER_MINUTE = 120
_TOKEN_RE = re.compile(r"[!-~]{32,256}\Z")
_SOURCE_RE = re.compile(SOURCE_PATTERN)


@dataclass(frozen=True)
class AccessConfig:
    mode: Literal["legacy", "scoped"]
    reader_token: str = field(repr=False)
    producer_tokens: dict[str, str] = field(repr=False)
    events_per_minute: int


@dataclass(frozen=True)
class Principal:
    role: Literal["reader", "producer"]
    source: str | None


def _valid_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate producer source")
        result[key] = value
    return result


def access_config() -> AccessConfig | None:
    """Read only explicit valid auth modes; never silently downgrade to legacy."""
    mode = os.environ.get("AUDIT_STREAM_AUTH_MODE", "").strip()
    raw_limit = os.environ.get("AUDIT_STREAM_MAX_EVENTS_PER_MINUTE", "").strip()
    try:
        limit = int(raw_limit) if raw_limit else DEFAULT_EVENTS_PER_MINUTE
    except ValueError:
        return None
    if limit < 1 or limit > 10_000:
        return None

    legacy = os.environ.get("AUDIT_STREAM_TOKEN", "")
    reader = os.environ.get("AUDIT_STREAM_READER_TOKEN", "")
    raw_producers = os.environ.get("AUDIT_STREAM_PRODUCER_TOKENS", "")
    if mode == "legacy":
        if not _valid_token(legacy) or reader or raw_producers:
            return None
        return AccessConfig("legacy", legacy, {}, limit)
    if mode != "scoped" or legacy or not _valid_token(reader):
        return None
    try:
        parsed = json.loads(raw_producers, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not 1 <= len(parsed) <= 32:
        return None
    if any(
        not isinstance(source, str)
        or len(source) > 128
        or _SOURCE_RE.fullmatch(source) is None
        or not _valid_token(token)
        for source, token in parsed.items()
    ):
        return None
    tokens: dict[str, str] = parsed
    if reader in tokens.values() or len(set(tokens.values())) != len(tokens):
        return None
    return AccessConfig("scoped", reader, tokens, limit)


def authorize(scope: Scope, role: Literal["reader", "producer"]) -> tuple[Principal, AccessConfig]:
    config = access_config()
    if config is None:
        raise HTTPException(status_code=503, detail="audit access is not configured")
    headers = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
    if len(headers) != 1 or not headers[0].startswith(b"Bearer "):
        raise HTTPException(
            status_code=401, detail="bearer token required", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        provided = headers[0][len(b"Bearer ") :].decode("ascii")
    except UnicodeDecodeError:
        provided = ""
    if role == "reader" and compare_digest(provided, config.reader_token):
        return Principal("reader", None), config
    if role == "producer":
        if config.mode == "legacy" and compare_digest(provided, config.reader_token):
            return Principal("producer", None), config
        for source, token in config.producer_tokens.items():
            if compare_digest(provided, token):
                return Principal("producer", source), config
    raise HTTPException(
        status_code=401, detail="invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
    )


class LocalRateLimiter:
    """Per-source, per-process sliding window. It is not a hosted abuse control."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()

    def allow(self, source: str, limit: int) -> bool:
        now = time.monotonic()
        with self._lock:
            window = self._windows.setdefault(source, deque())
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True


class RequestBoundary:
    """Authenticate and cap POST bytes before FastAPI parses them."""

    def __init__(self, app: ASGIApp, limiter: LocalRateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path == "/events":
            role: Literal["reader", "producer"] = "producer"
            body_limit = MAX_EVENT_BODY_BYTES
        elif path == "/verify/checkpoint":
            role = "reader"
            body_limit = MAX_CHECKPOINT_BODY_BYTES
        else:
            await self.app(scope, receive, send)
            return

        try:
            principal, config = authorize(scope, role)
        except HTTPException as error:
            await JSONResponse(
                {"detail": error.detail}, status_code=error.status_code, headers=error.headers
            )(scope, receive, send)
            return
        if role == "producer" and not self.limiter.allow(
            principal.source or "legacy", config.events_per_minute
        ):
            await JSONResponse(
                {"detail": "event rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"}
            )(scope, receive, send)
            return

        lengths = [value for name, value in scope.get("headers", []) if name.lower() == b"content-length"]
        if len(lengths) > 1:
            await JSONResponse({"detail": "invalid length"}, status_code=400)(scope, receive, send)
            return
        if lengths and (not lengths[0].isdigit() or len(lengths[0]) > 10):
            await JSONResponse({"detail": "invalid length"}, status_code=400)(scope, receive, send)
            return
        if lengths and int(lengths[0]) > body_limit:
            await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > body_limit:
                await JSONResponse({"detail": "request body too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break

        scope.setdefault("state", {})["audit_principal"] = principal
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
