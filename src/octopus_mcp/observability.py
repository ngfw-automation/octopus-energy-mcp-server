"""Structured logging: one JSON line per upstream request and tool call.

A server that anything on the network can reach kept no record at all of who
called what, and no upstream error detail beyond the message a tool returned.
Everything here writes to stderr so container logs pick it up, and the API key
never reaches a log line: it is redacted by value, and meter identifiers are
masked because a log is not the place for them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any, Optional

LOGGER_NAME = "octopus_mcp"
log = logging.getLogger(LOGGER_NAME)

_LONG_DIGITS = re.compile(r"\b\d{9,15}\b")
_METER_PATH = re.compile(r"/meters/[^/?]+")
_ACCOUNT_PATH = re.compile(r"/accounts/[^/?]+")


def mask_identifiers(text: str) -> str:
    """Blank out account numbers, MPANs, MPRNs and meter serials.

    A log line is not the place for the identifiers that address someone's
    home, and these appear in the URL path rather than in a header.
    """
    out = _ACCOUNT_PATH.sub("/accounts/<account>", text)
    out = _METER_PATH.sub("/meters/<serial>", out)
    return _LONG_DIGITS.sub("<meter-id>", out)


class RedactingFilter(logging.Filter):
    """Replace secret values anywhere in a record, whatever built the message."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def add_secret(self, value: Optional[str]) -> None:
        if value and len(value) >= 8 and value not in self._secrets:
            self._secrets.append(value)

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "***redacted***")
            return value
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(v) for v in value]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.msg)
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            # An Authorization header must never survive into a log line.
            record.fields = {
                k: self._scrub(v)
                for k, v in fields.items()
                if k.lower() not in ("authorization", "api_key", "token", "password")
            }
        return True


redactor = RedactingFilter()


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, event, and event fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(api_key: Optional[str] = None) -> None:
    """Send JSON lines to stderr at ``LOG_LEVEL`` (default INFO).

    Safe to call more than once; the handler is attached only once.
    """
    redactor.add_secret(api_key)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(redactor)
        log.addHandler(handler)
        log.propagate = False
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log.setLevel(getattr(logging, level, logging.INFO))
    # httpx logs every request as unstructured text that includes the account
    # number in the URL; our own upstream.request event covers it, masked.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # The SDK warns here when it rejects a Host or Origin header, naming the
    # value it saw -- which is exactly what you need to fix a remote-access
    # setup. Route it through the same handler so it lands in the same stream.
    sdk = logging.getLogger("mcp")
    if not sdk.handlers:
        for handler in log.handlers:
            sdk.addHandler(handler)
        sdk.propagate = False  # otherwise it prints again, unformatted
    sdk.setLevel(logging.WARNING)


def event(name: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log a structured event. Field values are redacted before they land."""
    log.log(level, name, extra={"fields": fields})


def _outcome(result: Any) -> str:
    """Tools report upstream problems as a normal result carrying an "error"
    key, so unwrap that rather than calling every completed call a success.

    The handler result reaches middleware either as a model or as the plain
    dict that goes on the wire, so both spellings are checked.
    """
    payloads = []
    for attr in ("structured_content", "structuredContent"):
        payloads.append(getattr(result, attr, None))
        if isinstance(result, dict):
            payloads.append(result.get(attr))
    for payload in payloads:
        if isinstance(payload, dict) and payload.get("error"):
            return "error:" + str(payload["error"])
    return "ok"


class LoggingMiddleware:
    """Log every inbound MCP request: method, tool, duration, outcome.

    Registered on the server's middleware chain, so it covers tool calls,
    listings and the handshake without each tool having to remember.
    """

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        params = ctx.params if isinstance(getattr(ctx, "params", None), dict) else {}
        tool = params.get("name") if ctx.method == "tools/call" else None
        started = time.monotonic()
        try:
            result = await call_next(ctx)
        except Exception as exc:
            event(
                "mcp.request",
                level=logging.WARNING,
                method=ctx.method,
                tool=tool,
                ms=round((time.monotonic() - started) * 1000),
                outcome="exception",
                error=type(exc).__name__,
            )
            raise
        event(
            "mcp.request",
            method=ctx.method,
            tool=tool,
            ms=round((time.monotonic() - started) * 1000),
            outcome=_outcome(result),
        )
        return result
