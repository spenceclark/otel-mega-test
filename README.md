# One request, several runtimes, one trace

A reproducible demonstrator that follows **one browser click** through
**React/TypeScript → FastAPI → Pydantic AI agent → MCP (Streamable HTTP) → ASP.NET Core MCP server →
Microsoft Agent Framework agent → local async tool → concurrent PostgreSQL + HTTP**, proves with raw OTLP
evidence that it stays **one trace**, and then breaks and repairs that continuity on purpose.

- Specification: [docs/project-spec.md](docs/project-spec.md)
- What each boundary does: [docs/propagation-contract.md](docs/propagation-contract.md)
- Who emits which span: [docs/instrumentation-inventory.md](docs/instrumentation-inventory.md)
- Pinned stack and observed behaviour: [docs/compatibility.md](docs/compatibility.md)
- Experiments and results: [experiments/](experiments/) · Article draft: [docs/article/draft.md](docs/article/draft.md)
- Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md) · Security: [SECURITY.md](SECURITY.md)

## Prerequisites

Docker (with Compose v2), `uv` (0.12+), git. Nothing else on the host: the .NET, Python and Node builds,
the browser, and Playwright all run in pinned containers. Ports `127.0.0.1:5173`, `14318`, `55432` must be free.

## Clean-checkout quickstart

Linux / macOS:

```bash
./scripts/demo.sh doctor
./scripts/demo.sh up --mode scripted
./scripts/demo.sh run --scenario baseline
./scripts/demo.sh verify
```

Windows (PowerShell):

```powershell
.\scripts\demo.ps1 doctor
.\scripts\demo.ps1 up --mode scripted
.\scripts\demo.ps1 run --scenario baseline
.\scripts\demo.ps1 verify
```

Then open <http://127.0.0.1:5173>, submit the default question, and copy the trace id shown under the answer.

| Verb | What it does |
|---|---|
| `doctor [--mode live] [--logfire]` | prerequisites, port checks; asks for `ANTHROPIC_API_KEY` / `LOGFIRE_TOKEN` only when that profile is selected |
| `up --mode scripted\|live [--logfire]` | build, start, seed, wait on healthchecks (DB query, worker `/healthz`, API's live MCP session) |
| `run --scenario <name>` | applies the scenario's config, drives the real UI with Playwright, writes `evidence/runs/<run_id>.json` |
| `verify [--run <id>]` | polls the Collector's OTLP evidence and asserts the trace contract; prints the first broken edge |
| `experiment <name>\|all` | fault → capture → expected-failure check → restore baseline → passing rerun; writes `experiments/<name>/result.json` |
| `down` | stop containers, keep data and evidence |
| `reset --yes` | **destructive**: drop the Postgres volume and `evidence/` |

`run --scenario concurrency` submits twenty requests at once over the API's single shared MCP session;
`verify --manifest evidence/runs/latest-concurrency.json` checks that none cross-parent.

## What has been verified (scripted mode, 15 Oct 2026)

From a clean clone on Windows via `demo.ps1`:

- Baseline: one Playwright click → 19 spans in **one trace** across `trace-demo-web`, `-api`, `-worker`,
  `-shipping`; all 16 graph checks pass, including the message-first MCP parent, the HTTP pair, a log record
  attached to a span in every layer, zero privacy canaries, and **≈320 ms SQL/HTTP overlap** under the local tool.
- Concurrency: 20 simultaneous clicks over one shared MCP client → 20 distinct, complete traces, no cross-parenting.
- Eight break/repair experiments and a malformed/conflicting-carrier probe behave as predicted
  ([experiments/README.md](experiments/README.md)).

Live mode (`claude-haiku-5-5` in both agents) + Logfire fan-out: one run, all 16 checks pass, Collector reported no
export errors ([live run record](docs/compatibility.md#live-run-record-15-oct-2026-commit-3fd6e6a--live-fixes)).
The same trace was inspected in Logfire: one waterfall across all four services, span links shown, both agents
detected ([Logfire observations](docs/compatibility.md#logfire-observations-same-trace-inspected-in-the-logfire-ui-eu-region)). The GitHub workflows have not run yet. See
[docs/compatibility.md](docs/compatibility.md) for the full ledger.

## Modes

- **scripted** (default, CI): scripted models are injected at each framework's model abstraction (Pydantic AI
  `FunctionModel`, an `IChatClient` for MAF). The real agent loops, real MCP transport, real PostgreSQL and real
  HTTP stub all run. No network access to model providers.
- **live**: Anthropic `claude-haiku-5-5` in both agents (`ANTHROPIC_API_KEY`, optional `ANTHROPIC_BASE_URL`),
  capped requests/tool calls/tokens. A missed tool call is reported as `workload_failure`.

## Telemetry routing

Every process exports OTLP/HTTP protobuf to one Collector. By default the Collector writes OTLP JSON lines to
`evidence/otel/{traces,logs}.jsonl` — the authoritative input for `verify`. `up --logfire` switches to
`infra/collector/otelcol.yaml`, which fans out to the same files **and** one Logfire project; only the Collector
holds `LOGFIRE_TOKEN`. The browser exports through the same-origin gateway (`/telemetry/v1/*`).

## Repository layout

```text
apps/web            React + OTel JS (browser root span, fetch instrumentation, OTLP via gateway)
apps/api            FastAPI + Pydantic AI request-router + MCP client (+ shipping stub module)
apps/worker         ASP.NET Core MCP server + MAF availability-analyst + lookup_availability tool
contracts/          shared JSON Schemas (API, MCP args/results, local DTOs)
infra/              Collector configs, gateway (nginx), Postgres seed
tests/e2e           Playwright specs (run in the pinned Playwright container)
tests/trace-contracts  unit tests for the trace assertion tool
experiments/        scenario definitions + recorded results
scripts/            demo CLI (demo.sh / demo.ps1 → demo.py) and assert-traces.py
```
