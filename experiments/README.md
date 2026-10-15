# Break/repair experiments

Definitions: [scenarios.yaml](scenarios.yaml). Each `<name>/result.json` is the sanitized record written by
`demo experiment <name>`: hypothesis, fault, the broken run's assertion results, the repaired (baseline) rerun's
results, and the verdict. Raw OTLP evidence stays in the git-ignored `evidence/experiments/<name>/<timestamp>/`.

```bash
./scripts/demo.sh experiment all      # 8 break/fix pairs
./scripts/demo.sh probe-carriers      # malformed + conflicting traceparent cases
```

| Experiment | Fault (one variable) | What broke (failing check → evidence) | Repair → rerun |
|---|---|---|---|
| `headers-removed` | httpx2 hook skips the `traceparent` header on `POST /mcp` | `api-http-client->worker-http-server`: the .NET `POST /mcp/` span roots a **new trace**; the MCP operation stays in the browser trace via `_meta` and links across | restore header → all checks pass |
| `meta-removed` | only `traceparent`/`tracestate` removed from `params._meta` | `mcp-message-parent`: MCP op's parent becomes `POST /mcp/` (HTTP fallback). Same trace id end to end — a surviving carrier, as predicted | restore `_meta` → pass |
| `both-removed` | both carriers | `required-spans`: every .NET category found in **one other trace** (propagation loss) | restore both → pass |
| `context-detached` | shipping branch created under `ExecutionContext.SuppressFlow()` | `required-spans`/`io-under-tool`: shipping CLIENT span (and the stub's server span) in another trace; SQL branch unaffected | launch under tool context → pass |
| `serial-io` | SQL awaited before HTTP task exists | `io-overlap`: overlap −3 ms, tool 956 ms vs ≈550 ms in parallel; parents unchanged | start both, then `WhenAll` → overlap ≈320 ms |
| `logical-error` | tool returns `isError=true` after doing the work | (verification) HTTP `POST` 200/UNSET, `MCP send` UNSET, Python `execute_tool` ERROR, .NET MCP op ERROR `error.type=tool_error`, API outcome `tool_error` → `logical-error-mapped` passes | baseline → `outcome-ok` |
| `privacy` | `DEMO_CONTENT_CAPTURE=true` | `privacy-canaries`: prompt, completion, analyst-summary and shipping-reference canaries leak; the header canary never does | default off → zero canaries |
| `export-failure` | only the API exports to an unresolvable endpoint | `required-spans` (API categories "not exported") and `parents-resolve` (dangling parents): .NET spans still carry the **browser trace id** → export loss, not propagation loss | endpoint restored → pass |
| `carrier-policy` | conflicting header vs `_meta` trace ids; garbage / truncated / all-zero / `ff` version `_meta`; all-zero header | tool call always succeeds; invalid `_meta` → MCP op falls back to HTTP parent; all-zero header → fresh root; conflict → never merged, joined only by the link | — (policy verification) |

Not implemented from the spec's wider list (no code path exists for them yet): browser allowlist and CORS
(the baseline is same-origin), stale client context (no test-only reuse seam), missing .NET source, duplicate
setup, sampling, slow/canceled caller. The worker's unit tests cover branch timeout/cancellation/failure semantics.
