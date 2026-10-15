# Propagation contract

One browser submission must produce one trace. This page is the boundary ledger: for every hop, who creates
the span, who injects context, who extracts it, and how the receiver chooses its parent. Check ids refer to
`scripts/assert-traces.py`; the observed span names are from the pinned stack ([compatibility.md](compatibility.md)).

## The graph we assert (message-first MCP profile)

Recorded from a real baseline run (scripted mode; durations from that run):

```text
web    ui.ask                                         INTERNAL  root of the submission        811 ms
web    └ POST                                         CLIENT    fetch instrumentation          810 ms
api      └ POST /api/ask                              SERVER    FastAPI (header parent)        805 ms
api        └ invoke_agent request-router              INTERNAL  Pydantic AI                    781 ms
api          ├ chat scripted-router                   CLIENT    scripted FunctionModel          15 ms
api          ├ execute_tool build_availability_report INTERNAL  Pydantic AI                    761 ms
api          │ └ MCP send tools/call build_availa…    CLIENT    mcp SDK  ← params._meta parent 709 ms
api          │   ├ POST                               CLIENT    httpx2 adapter ← header parent 709 ms
worker       │   │ └ POST /mcp/                       SERVER    ASP.NET Core                   705 ms
worker       │   └ tools/call build_availability_report SERVER  MCP C# SDK (link → POST /mcp/) 701 ms
worker       │     └ invoke_agent availability-analyst(…) CLIENT MAF                          582 ms
worker       │       ├ chat scripted-analyst          CLIENT    M.E.AI                           5 ms
worker       │       ├ execute_tool lookup_availability INTERNAL FunctionInvokingChatClient    546 ms
worker       │       │ ├ CONNECT demo                 CLIENT    Npgsql (first use only)         71 ms
worker       │       │ ├ postgresql                   CLIENT    Npgsql query (pg_sleep 0.3)    321 ms ┐ overlap
worker       │       │ └ GET                          CLIENT    HttpClient                     510 ms ┘ 321 ms
shipping     │       │   └ GET /quotes/{zone}         SERVER    FastAPI                        502 ms
worker       │       └ chat scripted-analyst          CLIENT    M.E.AI                           3 ms
api          └ chat scripted-router                   CLIENT                                     1 ms
```

The MCP server operation is a **sibling** of the transport `POST` rather than its grandchild. That is the
intended message-first graph (spec §5, OTel MCP semantic conventions): the logical operation follows the
message carrier; the HTTP server span is related by a **link**, not by parentage.

## Boundary ledger

| # | Boundary | Span created by | Injector (carrier) | Extractor | Receiver's parent rule | Check |
|---|---|---|---|---|---|---|
| 1 | browser click → fetch | app: `ui.ask` via `startActiveSpan(…, ROOT_CONTEXT, …)` | — | — | explicit root (never a page-load span) | `browser-root` |
| 2 | browser → API | `@opentelemetry/instrumentation-fetch` CLIENT `POST` | fetch instrumentation, **header** `traceparent` (W3C only, no baggage; same-origin so no CORS allowlist needed) | FastAPI/ASGI instrumentation | parent = browser CLIENT span | `browser-client->api-server` |
| 3 | API → agent → tool | Pydantic AI `invoke_agent`, `chat`, `execute_tool` | in-process context | — | ambient context | `agent-ancestry` |
| 4 | MCP message | `mcp` SDK CLIENT `MCP send tools/call <tool>` | `mcp` dispatcher: `propagate.inject(params._meta)` per message, existing `_meta` keys preserved | C# SDK `McpSessionHandler`: `extract(params._meta)` | parent = `MCP send` span; **link** to current HTTP server activity | `mcp-message-parent`, `mcp-http-link` |
| 5 | MCP transport | app adapter `TracingTransport` CLIENT `POST` (httpx2) | adapter: `propagate.inject` into a **fresh carrier per request**, set on that request's headers only | ASP.NET Core instrumentation | parent = Python `POST` CLIENT span | `api-http-client->worker-http-server` |
| 6 | MCP op → MAF agent → tool | MAF `invoke_agent`, M.E.AI `chat`, `execute_tool` | in-process (`Activity.Current`) | — | ambient | `agent-ancestry` |
| 7 | tool → SQL ∥ HTTP | Npgsql CLIENT, HttpClient CLIENT | `ExecutionContext` flows into both branch tasks, which are created while `execute_tool` is current | — | ambient; tool span ends after `Task.WhenAll` | `io-under-tool`, `io-overlap` |
| 8 | worker → shipping stub | HttpClient CLIENT `GET` | HttpClient instrumentation header | FastAPI | parent = worker `GET` | `shipping-server-hop` |
| 9 | every layer → logs | stdlib logging bridge / `ILogger` OTel provider / OTel JS logs | log record emitted inside an active span | — | log carries that span's trace/span id | `logs-correlated` |

The header on hop 5 and the `_meta` on hop 4 share the trace id but carry **different parent span ids**, by
design: the header represents the nested HTTP client operation. Nothing copies one into the other.

## Rules every runtime follows

1. **Per-request carriers.** No trace header is ever stored on a client's default headers. The httpx2 adapter
   builds a new carrier per request from the span it just started; the long-lived shared MCP client is
   exercised by `concurrency` (20 simultaneous submissions, `no-cross-trace-parents`, `one-request-per-trace`).
2. **Baggage off.** `OTEL_PROPAGATORS=tracecontext` (Python, verified in tests); explicit
   `W3CTraceContextPropagator` in the browser; .NET's default propagator is W3C and nothing adds baggage.
3. **Untrusted input.** Incoming context is correlation data only. Malformed `traceparent` values are discarded by
   the SDK parsers (Python `extract_trace_context` returns `None` → ambient parent; C# `ActivityContext.TryParse`
   fails → `Activity.Current`), never crash handling, and are never logged. Trust policy for **conflicting**
   carriers (header trace id ≠ `_meta` trace id): the message carrier wins for the MCP operation and the header wins
   for the HTTP server span; the two are joined only by the link. No component rewrites a trace id.
4. **No compensation.** No component copies the browser's trace id into business arguments, request ids or
   session ids. `demo.run_id`, `jsonrpc.request.id` and `mcp.session.id` are diagnostic attributes, never parents.
5. **Restore context.** The browser re-enters `ui.ask` explicitly after `await`; the .NET fault seam restores
   `ExecutionContext` flow immediately after creating the detached task (`apps/worker/Tools/LookupAvailability.cs`).

## Fault seams (explicit, one variable each)

| Scenario | Seam | Effect |
|---|---|---|
| `headers-removed` | `apps/api/src/api/mcp_adapter.py` | no header injection, inbound trace headers stripped |
| `meta-removed` | same | drops only `traceparent`/`tracestate` from `params._meta` (other `_meta` keys preserved; unit-tested) |
| `both-removed` | same | both |
| `context-detached` | `apps/worker/Tools/LookupAvailability.cs` | HTTP branch created under `ExecutionContext.SuppressFlow()` + `Task.Yield()` |
| `serial-io` | same | SQL awaited before the HTTP task exists |
| `logical-error` | `apps/worker/McpTools/AvailabilityTools.cs` | tool returns `isError=true` after doing the work |
| `privacy` | `DEMO_CONTENT_CAPTURE=true` | GenAI content capture on in both frameworks |
| `export-failure` | `API_OTLP_ENDPOINT` (compose) | only the API exports to an unresolvable endpoint |

Results and evidence: [../experiments/](../experiments/).
