"""Narrow httpx2 transport hook for the MCP client's HTTP hop.

removal condition: delete when an official httpx2 OpenTelemetry instrumentor ships.

Phase-zero gap: FastMCP 4 / mcp 2.x send over httpx2, which has no OpenTelemetry instrumentor, so without this
nothing creates an HTTP CLIENT span or injects `traceparent` on POST /mcp. The MCP message carrier
(`params._meta`) is injected by the mcp SDK itself; this module only adds the transport carrier.

It is also the explicit fault seam for the carrier-removal scenarios (fixed at construction):
  headers-removed  no header injection, inbound traceparent/tracestate headers stripped; `_meta` kept
  meta-removed     `params._meta.traceparent`/`tracestate` dropped from JSON-RPC bodies; headers kept
  both-removed     both
"""

import json
from collections.abc import AsyncIterator
from typing import Any, TypeGuard

import httpx2
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind, StatusCode

_TRACE_KEYS = ("traceparent", "tracestate")
_tracer = trace.get_tracer(__name__)


class TracingTransport(httpx2.AsyncBaseTransport):
    def __init__(self, scenario: str, inner: httpx2.AsyncBaseTransport | None = None) -> None:
        self._inject_headers = scenario not in ("headers-removed", "both-removed")
        self._strip_meta = scenario in ("meta-removed", "both-removed")
        self._inner = inner or httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        port = url.port or (443 if url.scheme == "https" else 80)
        span = _tracer.start_span(
            request.method,
            kind=SpanKind.CLIENT,
            attributes={
                "http.request.method": request.method,
                "url.full": f"{url.scheme}://{url.host}:{port}{url.path}",  # no userinfo, no query
                "server.address": url.host,
                "server.port": port,
            },
        )
        try:
            # request.headers belongs to this request only (the client copies its defaults into it).
            for key in _TRACE_KEYS:
                request.headers.pop(key, None)
            if self._inject_headers:
                carrier: dict[str, str] = {}
                propagate.inject(carrier, context=trace.set_span_in_context(span))
                request.headers.update(carrier)
            if self._strip_meta:
                request = _strip_meta(request)
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:
            span.set_attribute("error.type", type(exc).__qualname__)
            span.set_status(StatusCode.ERROR)
            span.end()
            raise

        span.set_attribute("http.response.status_code", response.status_code)
        if response.status_code >= 500:
            span.set_attribute("error.type", str(response.status_code))
            span.set_status(StatusCode.ERROR)
        # MCP answers tools/call on the response body (often SSE), so the span covers the body, not just headers.
        if response.is_closed or not isinstance(response.stream, httpx2.AsyncByteStream):
            span.end()  # body already in hand (pre-read response)
        else:
            response.stream = _EndSpanOnClose(response.stream, span)
        return response


class _EndSpanOnClose(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, span: Span) -> None:
        self._inner = inner
        self._span = span

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._span.end()


def _strip_meta(request: httpx2.Request) -> httpx2.Request:
    """Drop only the tracing keys from a JSON-RPC request's `params._meta`; anything else passes untouched."""
    try:
        body: object = json.loads(request.content)
    except httpx2.RequestNotRead, ValueError:
        return request
    params = body.get("params") if _is_object(body) else None
    meta = params.get("_meta") if _is_object(params) else None
    if not _is_object(meta) or not any(key in meta for key in _TRACE_KEYS):
        return request  # batch, notification without _meta, non-JSON, or nothing to strip
    for key in _TRACE_KEYS:
        meta.pop(key, None)
    headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() != "content-length"]
    return httpx2.Request(
        request.method, request.url, headers=headers, content=json.dumps(body).encode(), extensions=request.extensions
    )


def _is_object(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)
