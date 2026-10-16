# 0001 — A narrow httpx2 transport hook for the Python→.NET HTTP hop

**Status:** superseded (16 Oct 2026). The removal condition was already met when this was written: OpenTelemetry's
`opentelemetry-instrumentation-httpx` 0.66b1 (6 Oct 2026) ships `HTTPX2ClientInstrumentor`. Phase zero looked only at
the empty `opentelemetry-instrumentation-httpx2` placeholder and assumed the httpx package covered legacy httpx only.
The API now calls `HTTPX2ClientInstrumentor.instrument_client(...)` on the MCP client; `mcp_adapter.py` keeps only the
experiments' fault seam, placed *under* the instrumentor. `api-http-client->worker-http-server`, all three carrier
experiments and the 20-way concurrency check pass with it. The original record follows.

## Context
Pydantic AI 2.56 → FastMCP 4.1 → mcp 2.3 send MCP over **httpx2**. Phase zero found no instrumentor for it
(`opentelemetry-instrumentation-httpx2` on PyPI is a 0.0.0 placeholder; the httpx instrumentor patches legacy
httpx only). Without a fix there is no Python HTTP CLIENT span and no `traceparent` header on `POST /mcp`;
the .NET HTTP server span would root a new trace in every run.

## Decision
One `httpx2.AsyncBaseTransport` wrapper passed via `MCPToolset(http_client=httpx2.AsyncClient(transport=…))`.
Per request it starts a CLIENT span with stable HTTP attributes, injects the current context into a **fresh
carrier** on that request only, and ends the span when the response body closes. It never touches `_meta`
(the mcp SDK owns the message carrier) except in the explicit fault scenarios.

## Alternatives rejected
- Pin FastMCP 3 / legacy httpx to reuse the httpx instrumentor: older transport and protocol for one header.
- Monkey-patch httpx2 globally: hidden global patch, affects the Anthropic client too.
- Headers-only "compatibility profile": would hide the gap rather than close it.
