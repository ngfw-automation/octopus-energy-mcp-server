"""The API key must never reach a log line, and neither should the
identifiers that address someone's home."""

import asyncio
import json
import logging

from octopus_mcp import observability


def _capture(records: list):
    handler = logging.Handler()
    handler.emit = records.append
    return handler


def test_api_key_is_redacted_wherever_it_appears():
    observability.configure_logging("sk-secret-key-12345")
    formatter = observability.JsonFormatter()
    records: list = []
    handler = _capture(records)
    handler.addFilter(observability.redactor)
    observability.log.addHandler(handler)
    try:
        observability.event("test", url="https://x/?key=sk-secret-key-12345")
    finally:
        observability.log.removeHandler(handler)

    line = json.loads(formatter.format(records[-1]))
    assert "sk-secret-key-12345" not in json.dumps(line)
    assert "***redacted***" in line["url"]


def test_authorization_fields_are_dropped_entirely():
    records: list = []
    handler = _capture(records)
    handler.addFilter(observability.redactor)
    observability.log.addHandler(handler)
    try:
        observability.event("test", authorization="Basic abc", status=200)
    finally:
        observability.log.removeHandler(handler)
    fields = records[-1].fields
    assert "authorization" not in fields
    assert fields["status"] == 200


def test_meter_and_account_identifiers_are_masked():
    masked = observability.mask_identifiers(
        "https://api.octopus.energy/v1/electricity-meter-points/1234567890123"
        "/meters/Z1A0000001/consumption/"
    )
    assert "1234567890123" not in masked
    assert "Z1A0000001" not in masked
    assert observability.mask_identifiers("/v1/accounts/A-AAAA1111/") == "/v1/accounts/<account>/"


def test_middleware_reports_tool_errors_as_errors():
    """A tool that answers {"error": "no_meter"} is not a successful call."""
    calls: list = []

    class Ctx:
        method = "tools/call"
        params = {"name": "get_export_consumption"}

    async def call_next(_ctx):
        return {"structuredContent": {"error": "no_meter"}}

    handler = _capture(calls)
    observability.log.addHandler(handler)
    try:
        asyncio.run(observability.LoggingMiddleware()(Ctx(), call_next))
    finally:
        observability.log.removeHandler(handler)

    fields = calls[-1].fields
    assert fields["tool"] == "get_export_consumption"
    assert fields["outcome"] == "error:no_meter"


def test_middleware_records_an_exception_and_reraises():
    calls: list = []

    class Ctx:
        method = "tools/call"
        params = {"name": "boom"}

    async def call_next(_ctx):
        raise RuntimeError("upstream exploded")

    handler = _capture(calls)
    observability.log.addHandler(handler)
    try:
        asyncio.run(observability.LoggingMiddleware()(Ctx(), call_next))
        raise AssertionError("expected the error to propagate")
    except RuntimeError:
        pass
    finally:
        observability.log.removeHandler(handler)

    assert calls[-1].fields["outcome"] == "exception"
