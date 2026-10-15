"""TracingTransport unit tests against httpx2.MockTransport (no network)."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from api.mcp_adapter import TracingTransport
from tests.conftest import Telemetry, ctx

_tracer = trace.get_tracer(__name__)
URL = "http://user:pw@worker:8080/mcp?token=not-for-telemetry"
CALL: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/call",
    "params": {
        "name": "build_availability_report",
        "arguments": {"product_code": "DEMO-LANTERN", "destination_zone": "DEMO-NORTH"},
        "_meta": {
            "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
            "tracestate": "vendor=1",
            "progressToken": 7,
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        },
    },
}


def make_client(scenario: str, seen: list[httpx2.Request], status: int = 200) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json={"jsonrpc": "2.0", "id": 7, "result": {}})

    transport = TracingTransport(scenario, inner=httpx2.MockTransport(handler))
    return httpx2.AsyncClient(transport=transport, headers={"x-default": "kept"})


def http_spans(captured: Telemetry) -> list[ReadableSpan]:
    scope = "api.mcp_adapter"
    return [
        s
        for s in captured.spans.get_finished_spans()
        if s.instrumentation_scope and s.instrumentation_scope.name == scope
    ]


def header_ids(request: httpx2.Request) -> tuple[int, int]:
    _version, trace_id, span_id, _flags = request.headers["traceparent"].split("-")
    return int(trace_id, 16), int(span_id, 16)


def test_injects_its_client_span_into_this_request_only(captured: Telemetry) -> None:
    seen: list[httpx2.Request] = []

    async def go() -> trace.Span:
        async with make_client("baseline", seen) as client:
            with _tracer.start_as_current_span("parent") as parent:
                await client.post(URL, json=CALL)
            assert "traceparent" not in client.headers  # never on the shared client's defaults
            return parent

    parent = asyncio.run(go())
    [span] = http_spans(captured)
    assert span.parent and span.parent.span_id == parent.get_span_context().span_id
    assert header_ids(seen[0]) == (ctx(span).trace_id, ctx(span).span_id)
    assert seen[0].headers["x-default"] == "kept"
    assert span.name == "POST"
    assert dict(span.attributes or {}) == {
        "http.request.method": "POST",
        "url.full": "http://worker:8080/mcp",  # no userinfo, no query
        "server.address": "worker",
        "server.port": 8080,
        "http.response.status_code": 200,
    }
    assert span.status.status_code == StatusCode.UNSET


def test_concurrent_requests_get_their_own_carrier(captured: Telemetry) -> None:
    seen: list[httpx2.Request] = []

    async def go() -> None:
        async with make_client("baseline", seen) as client:

            async def under_parent(name: str) -> None:
                with _tracer.start_as_current_span(name):
                    await client.post(URL, json=CALL)

            await asyncio.gather(under_parent("a"), under_parent("b"))

    asyncio.run(go())
    client_parent = {ctx(s).span_id: s.parent.span_id for s in http_spans(captured) if s.parent}
    test_parents = {ctx(s).span_id for s in captured.spans.get_finished_spans() if s.name in ("a", "b")}
    header_parents = [header_ids(r)[1] for r in seen]
    assert len(set(header_parents)) == 2  # each request names its own CLIENT span
    assert {client_parent[p] for p in header_parents} == test_parents  # ... which hangs off its own caller


class _StreamedBody(httpx2.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data: {"jsonrpc": "2.0", "id": 7, "result": {}}\n\n'


def test_span_covers_the_streamed_response_body(captured: Telemetry) -> None:
    transport = TracingTransport(
        "baseline", httpx2.MockTransport(lambda _: httpx2.Response(200, stream=_StreamedBody()))
    )

    async def go() -> int:
        async with httpx2.AsyncClient(transport=transport) as client, client.stream("POST", URL, json=CALL) as response:
            finished_at_headers = len(http_spans(captured))
            await response.aread()
        return finished_at_headers

    assert asyncio.run(go()) == 0
    assert len(http_spans(captured)) == 1


def test_server_error_and_transport_failure_mark_the_span(captured: Telemetry) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    async def go() -> None:
        async with make_client("baseline", [], status=503) as client:
            await client.post(URL, json=CALL)
        async with httpx2.AsyncClient(transport=TracingTransport("baseline", httpx2.MockTransport(refuse))) as client:
            with pytest.raises(httpx2.ConnectError):
                await client.post(URL, json=CALL)

    asyncio.run(go())
    unavailable, refused = http_spans(captured)
    assert unavailable.status.status_code == StatusCode.ERROR
    assert unavailable.attributes and unavailable.attributes["error.type"] == "503"
    assert refused.status.status_code == StatusCode.ERROR
    assert refused.attributes and refused.attributes["error.type"] == "ConnectError"


@pytest.mark.parametrize(
    ("scenario", "header", "meta"),
    [
        ("baseline", True, True),
        ("headers-removed", False, True),
        ("meta-removed", True, False),
        ("both-removed", False, False),
        ("serial-io", True, True),  # other services' scenarios leave the transport alone
    ],
)
def test_fault_scenarios(scenario: str, header: bool, meta: bool, captured: Telemetry) -> None:
    seen: list[httpx2.Request] = []

    async def go() -> None:
        async with make_client(scenario, seen) as client:
            stale = {"traceparent": "00-33333333333333333333333333333333-4444444444444444-01", "tracestate": "x=1"}
            await client.post(URL, json=CALL, headers=stale)

    asyncio.run(go())
    [request] = seen
    sent = json.loads(request.content)
    expected_meta = dict(CALL["params"]["_meta"])
    if not meta:
        del expected_meta["traceparent"], expected_meta["tracestate"]
    assert sent["params"]["_meta"] == expected_meta  # progressToken and protocol keys always preserved
    assert sent["params"]["arguments"] == CALL["params"]["arguments"]
    assert int(request.headers["content-length"]) == len(request.content)
    assert ("traceparent" in request.headers) is header
    if header:
        [span] = http_spans(captured)
        assert header_ids(request)[1] == ctx(span).span_id  # stale header replaced, not kept
        assert "tracestate" not in request.headers
    else:
        assert "tracestate" not in request.headers


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        json.dumps([CALL, CALL]).encode(),  # batch: left alone
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "x", "params": None}).encode(),
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"_meta": "traceparent"}}).encode(),
    ],
)
def test_meta_strip_passes_other_bodies_through_unchanged(body: bytes) -> None:
    seen: list[httpx2.Request] = []

    async def go() -> None:
        async with make_client("meta-removed", seen) as client:
            await client.post(URL, content=body)

    asyncio.run(go())
    assert seen[0].content == body
