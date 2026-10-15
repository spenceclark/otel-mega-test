# 0003 — Collector file exporter is the source of truth; Logfire is a fan-out

**Status:** accepted (15 Oct 2026).

## Decision
Every process exports OTLP/HTTP protobuf to one Collector. The default (CI) profile writes OTLP JSON lines per
signal (`evidence/otel/{traces,logs}.jsonl`) with collector-contrib's `file` exporter; `scripts/assert-traces.py`
reads only that. The Logfire profile (`otelcol.yaml`) adds `otlphttp/logfire` beside the same file exporters, and
only the Collector holds `LOGFIRE_TOKEN`. UI rendering never decides pass/fail.

## Consequences
Assertions are offline and reproducible; the Collector does not repair parents. The evidence file appends forever
(`demo reset --yes` clears it); the reader filters by run id and trace id.
