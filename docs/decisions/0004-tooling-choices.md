# 0004 — Small tooling deviations from the proposed layout

**Status:** accepted (15 Oct 2026).

| Proposed | Chosen | Why |
|---|---|---|
| pnpm workspace + root `package.json` | two independent npm projects (`apps/web`, `tests/e2e`) | pnpm not installed on the reference host; npm ships with Node; no shared JS code needs a workspace |
| `apps/shipping-stub/` service | module `api.shipping_stub` in the API image, different command | same FastAPI + OTel setup; one image, one lockfile |
| `demo.sh` and `demo.ps1` implementing the verbs | both are 5-line wrappers over `scripts/demo.py` (uv inline script) | one implementation, identical behaviour on Windows and Linux |
| Playwright on the host | Playwright in the pinned `mcr.microsoft.com/playwright` image on the Compose network | no host browser install; same-origin `http://web:8080` |
| `contracts/trace-expectations.schema.json` | expectations live in `experiments/scenarios.yaml` (check ids) | the assertion tool is the only consumer |
| stateful MCP sessions | C# SDK default stateless mode, protocol 2026-07-28 | `Stateful` is obsolete in SDK 2.2.0 (MCP9006) and 2026-07-28 has no sessions |
