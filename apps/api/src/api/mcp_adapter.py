"""Transport for the MCP client's HTTP hop, with the explicit fault seam for the carrier-removal scenarios.

The HTTP CLIENT span and the `traceparent` header on POST /mcp come from OpenTelemetry's own
`HTTPX2ClientInstrumentor` (opentelemetry-instrumentation-httpx), applied to the MCP client with
`instrument_client`. It wraps whatever transport the client has, so it creates the span and injects first; this
seam sits underneath and can then remove a carrier on purpose. The message carrier (`params._meta`) belongs to the
mcp SDK.

  headers-removed  traceparent/tracestate headers stripped after injection; `_meta` kept
  meta-removed     `params._meta.traceparent`/`tracestate` dropped from JSON-RPC bodies; headers kept
  both-removed     both

Every other scenario gets the plain httpx2 transport: no wrapper, no behaviour of our own.
"""

import json
from typing import Any, TypeGuard

import httpx2

_TRACE_KEYS = ("traceparent", "tracestate")
_FAULTS = ("headers-removed", "meta-removed", "both-removed")


def mcp_transport(scenario: str, inner: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncBaseTransport:
    """The transport for the MCP client: plain httpx2, or the fault seam around it for the carrier scenarios."""
    base = inner or httpx2.AsyncHTTPTransport()
    return _CarrierFault(scenario, base) if scenario in _FAULTS else base


class _CarrierFault(httpx2.AsyncBaseTransport):
    def __init__(self, scenario: str, inner: httpx2.AsyncBaseTransport) -> None:
        self._strip_headers = scenario in ("headers-removed", "both-removed")
        self._strip_meta = scenario in ("meta-removed", "both-removed")
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if self._strip_headers:
            for key in _TRACE_KEYS:
                request.headers.pop(key, None)
        if self._strip_meta:
            request = _strip_meta(request)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


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
