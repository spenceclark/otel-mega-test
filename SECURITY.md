# Security notes

This is a **local educational workload** with synthetic data. It is not a production security profile.

## Reporting

Open a private security advisory on the repository rather than a public issue.

## What the demo does to stay safe by default

| Area | Control | Where |
|---|---|---|
| Credentials | Logfire write token lives only in the Collector container (`LOGFIRE_TOKEN`); model keys only in the API and worker containers. No `VITE_*` secrets; the browser bundle holds no credential. `.env` is git-ignored. | `compose.yaml`, `infra/collector/otelcol.yaml` |
| Network | Only the gateway (`127.0.0.1:5173`) and optional diagnostics (`127.0.0.1:14318` OTLP, `127.0.0.1:55432` Postgres) are published, on loopback. API, worker, shipping stub, DB and Collector are private to the Compose network. | `compose.yaml` |
| Browser telemetry gateway | Exactly two OTLP paths (`/telemetry/v1/traces`, `/telemetry/v1/logs`), POST only, `application/x-protobuf` only, 1 MiB body cap, rate-limited, origin allowlist, Cookie/Authorization stripped. Not a general forwarder. Origin checks are not authentication. | `infra/gateway/nginx.conf` |
| Content capture | GenAI prompt/completion/tool argument/result capture is **off** in Pydantic AI and in MAF / Microsoft.Extensions.AI (`DEMO_CONTENT_CAPTURE=false`). The `privacy` experiment proves canaries leak when it is turned on and vanish when it is off. | `apps/api`, `apps/worker`, `experiments/scenarios.yaml` |
| SQL | Parameterized query, read-only role `demo_reader` (`default_transaction_read_only`), SELECT on one table. Npgsql tracing records the statement text with placeholders, not bind values. Models never generate SQL. | `infra/postgres/init.sql`, `apps/worker` |
| Outbound HTTP | One allow-listed base URL from configuration, redirects disabled. Model output never chooses a destination. | `apps/worker` |
| Trace context | Incoming `traceparent`/`_meta` is untrusted correlation data and never grants access. Baggage is disabled (`OTEL_PROPAGATORS=tracecontext`). The returned `trace_id` is diagnostic only. | all runtimes |
| Agent limits | Bounded request/tool-call/token limits and request deadlines in both frameworks; input capped at 2,000 characters; enumerated product/zone values. | `apps/api`, `apps/worker` |
| MCP | The local MCP endpoint has no authorization and is reachable only on the Compose network. A remote deployment needs TLS, origin validation and MCP authorization per the protocol revision in use. | `apps/worker` |

## Evidence retention

`evidence/` (raw OTLP JSON lines, run manifests, experiment outputs) is git-ignored and local. `demo reset --yes`
deletes it. Committed `experiments/*/result.json` files contain only synthetic ids, check names and sanitized
details. Do not publish MCP session ids or full carriers in screenshots; Logfire retention and access are governed
by the Logfire project you configure.
