# Troubleshooting

`verify` prints every check with a one-line reason and finishes with the **first unexpected result**. Start there.

## Reading a failed check

| Failing check | Most likely cause | Look at |
|---|---|---|
| `browser-root` | browser spans never exported: page closed before flush, gateway rejected the OTLP POST (405/415/403/429), or the run manifest is from an older run | gateway logs `docker compose logs web`; the `telemetry-flushed` marker in the UI |
| `required-spans` with "candidate spans in OTHER traces" | **propagation loss**: a carrier was dropped and the receiver started a new trace | which service moved; compare with `headers-removed` / `both-removed` / `context-detached` results |
| `required-spans` with "no candidate spans … (export loss or not emitted)" | the service never exported (wrong endpoint, Collector down) or the instrumentation/source is not registered | `docker compose logs <service>` for exporter errors; `AddSource` list in the worker |
| `parents-resolve` "dangling parents … EXPORT loss" | children carry the right trace id but their parent's process didn't export | that parent's exporter config (`export-failure` experiment reproduces it) |
| `browser-client->api-server` | `traceparent` not sent (fetch propagation allowlist / CORS) or FastAPI instrumentation not applied before serving | request headers in the browser devtools; `apps/api/src/api/main.py` |
| `api-http-client->worker-http-server` | `HTTPX2ClientInstrumentor.instrument_client` was not applied to the MCP client (`apps/api/src/api/agent.py`), or the scenario `headers-removed` is active | `apps/api/src/api/mcp_adapter.py`, `GET /api/config` |
| `mcp-message-parent` | `params._meta.traceparent` missing (falls back to the HTTP server activity) or the MCP ActivitySource is not listened to | scenario `meta-removed`; `Experimental.ModelContextProtocol` in the worker's sources |
| `io-overlap` | branches serialized (`serial-io`) or the delay is not carried by the operations themselves | tool code; fixture delays `DEMO_SQL_DELAY_MS`, `DEMO_SHIPPING_DELAY_MS` |
| `io-under-tool` | a branch was started without the tool's execution context (`context-detached`) | `apps/worker/Tools/LookupAvailability.cs` |
| `logs-correlated` | a layer logged outside an active span, or its log exporter isn't configured | that runtime's logging bridge |
| `privacy-canaries` | content capture enabled, or application code logged content | `DEMO_CONTENT_CAPTURE`; the leaked canary name says which path |

## Known environment pitfalls

- **Port 4318 already in use.** Many machines run a local Collector on 4318. This stack publishes its OTLP
  receiver on `127.0.0.1:14318` for diagnostics only; containers talk to `otel-collector:4318` internally.
- **Linux bind-mount permissions.** collector-contrib runs as uid 10001. `demo up` makes `evidence/otel`
  world-writable on POSIX hosts; if you create it yourself, do the same or the file exporter cannot write.
- **CRLF line endings.** `.gitattributes` forces LF. A CRLF shell script or config copied into a Linux
  container fails with `\r` errors; re-checkout after fixing `core.autocrlf`.
- **OTLP base vs. signal endpoint.** `OTEL_EXPORTER_OTLP_ENDPOINT` is the *base* (`http://otel-collector:4318`);
  SDKs append `/v1/traces` and `/v1/logs`. A signal-specific variable (`…_TRACES_ENDPOINT`) must include the
  path. The browser uses explicit signal URLs (`/telemetry/v1/traces`). See docs/compatibility.md for what each
  SDK actually did.
- **`ANTHROPIC_BASE_URL` leaks in from your shell.** Compose passes `ANTHROPIC_BASE_URL` through to the API and
  worker. Some tools (e.g. Claude Code) set it to their own proxy; `demo up --mode live` would then send model calls
  there. Unset it (`unset ANTHROPIC_BASE_URL` / `Remove-Item Env:ANTHROPIC_BASE_URL`) or set it in `.env` to the
  endpoint you actually want before a live run.
- **Evidence grows forever.** The file exporter appends. `verify` filters by run id and trace id, but
  `demo reset --yes` is the clean slate.
- **Stale scenario.** `run --scenario X` re-applies configuration through Compose each time. Check the active
  scenario at `GET http://127.0.0.1:5173/api/config` or in the UI header.
- **Collector export is not storage.** Finite queues, retries and a memory limiter mean spans can be dropped
  under pressure; `verify` reports what is missing rather than assuming.
