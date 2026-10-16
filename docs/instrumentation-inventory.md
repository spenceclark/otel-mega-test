# Instrumentation inventory

One owner per layer, one TracerProvider + one LoggerProvider per runtime, one export route (OTLP/HTTP protobuf →
Collector). "Selector" is what `scripts/assert-traces.py` uses to find the span; it relies on stable attributes,
not on a total span count.

## Browser — `trace-demo-web` (`apps/web/src/telemetry.ts`)

| Layer | Owner | Span / record | Selector |
|---|---|---|---|
| Interaction | app, `tracer.startActiveSpan('ui.ask', …, ROOT_CONTEXT, …)`, scope `trace-demo-web` | `ui.ask` INTERNAL root; `demo.scenario`, `demo.mode`, `demo.run_id`, `demo.outcome` (`ok`/`error`/`canceled`), `demo.context_after_await` | name `ui.ask` + `demo.run_id` |
| HTTP | `@opentelemetry/instrumentation-fetch` 0.223.0 (only owner; no manual HTTP span) | `POST` CLIENT; `http.request.method`, `url.full`, `server.address/port`, `http.response.status_code`, resource-timing events | CLIENT child of `ui.ask` |
| Logs | OTel JS logs SDK, explicit `context.with(…ui.ask…)` | `ui.ask completed` (INFO) with `demo.run_id`, `demo.outcome` | trace/span id |
| Export | `OTLPTraceExporter`/`OTLPLogExporter` (proto) → `/telemetry/v1/{traces,logs}` | export requests are excluded from fetch instrumentation (`ignoreUrls`) and use the unpatched fetch | `no-export-recursion` |
| Init | once at module scope before `createRoot`, guarded by `globalThis.__traceDemoTelemetry` (StrictMode/HMR safe) | `StackContextManager`, `W3CTraceContextPropagator` only | — |

`GET /api/config` is in `ignoreUrls` too: the API does not trace it, so its browser span would be a childless root.

## Python — `trace-demo-api` and `trace-demo-shipping` (`apps/api/src/api`)

| Layer | Owner | Span / record | Selector |
|---|---|---|---|
| Providers | `telemetry.setup()` (idempotent): `TracerProvider` (sampler from `OTEL_TRACES_SAMPLER`), `LoggerProvider`, OTLP HTTP exporters, stdlib logging bridge (`opentelemetry-instrumentation-logging` handler) | — | — |
| ASGI server | `FastAPIInstrumentor.instrument_app` before serving; `/api/healthz`, `/api/config`, `/healthz` excluded; receive/send child spans off | `POST /api/ask` SERVER, `http.route=/api/ask`, old HTTP semconv; app adds `demo.run_id`, `demo.scenario`, `demo.mode`, `demo.outcome` | SERVER + `demo.run_id` |
| Agent | Pydantic AI instrumentation (`include_content = DEMO_CONTENT_CAPTURE`, default false) | `invoke_agent request-router` INTERNAL | `gen_ai.operation.name=invoke_agent` + `gen_ai.agent.name` |
| Model | Pydantic AI | `chat scripted-router` CLIENT ×2, `gen_ai.provider.name=function`, estimated usage, app adds `demo.model.synthetic=true` | — |
| Tool | Pydantic AI | `execute_tool build_availability_report` INTERNAL | ancestor of the MCP CLIENT span |
| MCP message | `mcp` 2.3.0 dispatcher (native; FastMCP spans disabled via `telemetry_mode="propagation_only"`) | `MCP send tools/call build_availability_report` CLIENT, `mcp.method.name`, `jsonrpc.request.id`; injects `params._meta` | `mcp.method.name=tools/call`, CLIENT |
| MCP transport | **app adapter** `api.mcp_adapter.TracingTransport` (httpx2 has no instrumentor) | `POST` CLIENT, stable HTTP semconv, sanitized `url.full`; injects the header | CLIENT with `url.full` containing `/mcp` |
| Logs | stdlib `logging` | `ask received` / `ask completed` (`demo.run_id`, `demo.scenario`, `demo.outcome`, `demo.duration_ms`); shipping: `quote served` | trace/span id |
| Shipping server | `FastAPIInstrumentor` (same image, `api.shipping_stub`) | `GET /quotes/{zone}` SERVER | SERVER in `trace-demo-shipping` |

Deliberately **not** installed: `opentelemetry-instrumentation-httpx` (instruments legacy httpx, which MCP no
longer uses — it would be the "transport mismatch" experiment), the Logfire SDK (one provider only).

## .NET — `trace-demo-worker` (`apps/worker`)

| Layer | Owner | Span / record | Selector |
|---|---|---|---|
| Providers | `Telemetry/WorkerTelemetry.cs`: `AddOpenTelemetry().WithTracing(…).WithLogging(…)`, `AddOtlpExporter()` from `OTEL_*` env | — | — |
| HTTP server | `AddAspNetCoreInstrumentation` (`/healthz` filtered) | `POST /mcp/` SERVER, `http.route=/mcp/` | span linked from the MCP server span |
| MCP message | MCP C# SDK 2.2.0, source `Experimental.ModelContextProtocol` | `tools/call build_availability_report` SERVER + link to `POST /mcp/`; app adds `demo.scenario`, `demo.mode`, `demo.outcome` | `mcp.method.name=tools/call`, SERVER |
| Agent | MAF `ChatClientAgent.AsBuilder().UseOpenTelemetry(…)`, source `Experimental.Microsoft.Agents.AI`, `EnableSensitiveData = true`, reduced to message shape by `Telemetry/GenAiContentRedactor.cs` before export unless `DEMO_CONTENT_CAPTURE=true` | `invoke_agent availability-analyst(availability-analyst)` CLIENT | `gen_ai.operation.name=invoke_agent` + `gen_ai.agent.name` |
| Model | M.E.AI `UseOpenTelemetry`, source `Experimental.Microsoft.Extensions.AI` | `chat scripted-analyst` CLIENT ×2 (no usage) | — |
| Local tool | M.E.AI `FunctionInvokingChatClient` (on the agent's source) — **no** app wrapper | `execute_tool lookup_availability` INTERNAL; event `lookup_availability.join` | name |
| SQL | `Npgsql.OpenTelemetry` `AddNpgsql()` | `postgresql` CLIENT (`db.system.name`, `db.namespace`, `db.query.text` with placeholders); `CONNECT demo` on first use | `db.system.name=postgresql` + `db.query.text` |
| Outbound HTTP | `AddHttpClientInstrumentation` | `GET` CLIENT, `url.full=…/quotes/<zone>` | CLIENT with `/quotes/` |
| Logs | `ILogger` → OTel logging provider (AspNetCore/HttpClient categories at Warning) | `build_availability_report started/completed`, `lookup_availability branch completed/failed` (`demo.branch=sql|http`), `lookup_availability join`, plus MCP SDK handler records | trace/span id |

No custom ActivitySource exists: every layer already had a framework span, so application correctness never
depends on a non-null `StartActivity()`.

## Ownership checks

- One provider per runtime: Python `setup()` is guarded; the browser is guarded at module scope; .NET registers
  through the single `AddOpenTelemetry()` builder.
- No duplicate spans for one operation in **scripted** mode: FastMCP's client span is disabled; the Anthropic .NET
  SDK's own OTel is off in live mode; fetch instrumentation is the only browser HTTP owner; no app `execute_tool`
  wrapper. The Anthropic *Python* SDK 1.14.0 traces by default (a second owner for the model call, plus account
  ids); the API builds its client with `open_telemetry=False`, so Pydantic AI's `chat` span is the only owner.
- `one-request-per-trace` (concurrency run) asserts exactly one API server span and one MCP server operation per trace.
