#!/usr/bin/env python3
"""Trace-contract assertions over the Collector's OTLP JSON-lines evidence (stdlib only).

Reads `evidence/otel/traces.jsonl` + `logs.jsonl` (collector-contrib `file` exporter, `format: json`:
one ExportTrace/LogsServiceRequest per line, hex ids, int span kinds, nanos as strings) and checks the
graph contract in docs/propagation-contract.md for one run manifest written by the Playwright tests.

    uv run scripts/assert-traces.py --manifest evidence/runs/latest.json
    uv run scripts/assert-traces.py --manifest evidence/runs/latest-concurrency.json
    uv run scripts/assert-traces.py --manifest ... --expect-fail mcp-message-parent --expect-pass required-spans

Exit 0 when expectations hold (default: every check passes), 1 otherwise, 2 on usage/evidence errors.
Causality uses trace/parent ids only; timestamps are compared only between two spans of the same
.NET process (the SQL/HTTP overlap), never across hosts.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

INTERNAL, SERVER, CLIENT = 1, 2, 3
STATUS_ERROR = 2
WEB, API, WORKER, SHIPPING = "trace-demo-web", "trace-demo-api", "trace-demo-worker", "trace-demo-shipping"
CANARIES = (
    "CANARY-PROMPT-91c4",
    "CANARY-COMPLETION-3d7a",
    "CANARY-SHIPREF-5b1e",
    "CANARY-HEADER-e620",
    "CANARY-ANALYST-0f2b",
)


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str
    name: str
    kind: int
    start: int
    end: int
    service: str
    scope: str
    attrs: dict[str, Any]
    status: int
    links: list[tuple[str, str]]
    raw: str = field(repr=False, default="")


@dataclass
class Log:
    trace_id: str
    span_id: str
    service: str
    body: str
    raw: str = field(repr=False, default="")


@dataclass
class Check:
    id: str
    ok: bool
    detail: str


def _value(v: dict[str, Any]) -> Any:
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in v:
            return v[key]
    if "intValue" in v:
        return int(v["intValue"])
    if "arrayValue" in v:
        return [_value(x) for x in v["arrayValue"].get("values", [])]
    return None


def _attrs(items: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
    return {a["key"]: _value(a.get("value", {})) for a in items or []}


def load(evidence: Path) -> tuple[list[Span], list[Log]]:
    spans: list[Span] = []
    logs: list[Log] = []
    traces_file, logs_file = evidence / "traces.jsonl", evidence / "logs.jsonl"
    if traces_file.exists():
        for line in traces_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            for rs in json.loads(line).get("resourceSpans", []):
                service = str(_attrs(rs.get("resource", {}).get("attributes")).get("service.name", "?"))
                for ss in rs.get("scopeSpans", []):
                    scope = ss.get("scope", {}).get("name", "")
                    for s in ss.get("spans", []):
                        spans.append(
                            Span(
                                trace_id=s["traceId"],
                                span_id=s["spanId"],
                                parent_id=s.get("parentSpanId", ""),
                                name=s.get("name", ""),
                                kind=int(s.get("kind", 0)),
                                start=int(s.get("startTimeUnixNano", 0)),
                                end=int(s.get("endTimeUnixNano", 0)),
                                service=service,
                                scope=scope,
                                attrs=_attrs(s.get("attributes")),
                                status=int(s.get("status", {}).get("code", 0)),
                                links=[(lk["traceId"], lk["spanId"]) for lk in s.get("links", [])],
                                raw=json.dumps(s) + json.dumps(rs.get("resource", {})),
                            )
                        )
    if logs_file.exists():
        for line in logs_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            for rl in json.loads(line).get("resourceLogs", []):
                service = str(_attrs(rl.get("resource", {}).get("attributes")).get("service.name", "?"))
                for sl in rl.get("scopeLogs", []):
                    for r in sl.get("logRecords", []):
                        logs.append(
                            Log(
                                trace_id=r.get("traceId", ""),
                                span_id=r.get("spanId", ""),
                                service=service,
                                body=str(_value(r.get("body", {}))),
                                raw=json.dumps(r),
                            )
                        )
    return spans, logs


class Trace:
    """Index of one trace's spans by span id, with ancestry helpers."""

    def __init__(self, trace_id: str, spans: list[Span]) -> None:
        self.trace_id = trace_id
        self.spans = [s for s in spans if s.trace_id == trace_id]
        self.by_id = {s.span_id: s for s in self.spans}

    def parent(self, s: Span | None) -> Span | None:
        return self.by_id.get(s.parent_id) if s else None

    def ancestors(self, s: Span) -> list[Span]:
        out: list[Span] = []
        seen: set[str] = set()
        cur = self.parent(s)
        while cur and cur.span_id not in seen:
            seen.add(cur.span_id)
            out.append(cur)
            cur = self.parent(cur)
        return out

    def is_ancestor(self, anc: Span | None, s: Span | None) -> bool:
        return bool(anc and s and any(a.span_id == anc.span_id for a in self.ancestors(s)))

    def find(self, pred: Callable[[Span], bool]) -> list[Span]:
        return [s for s in self.spans if pred(s)]


# ---- span selectors (stable semantic/custom attributes; see docs/instrumentation-inventory.md) ----


def is_mcp_call_client(s: Span) -> bool:
    return s.service == API and s.kind == CLIENT and s.attrs.get("mcp.method.name") == "tools/call"


def is_mcp_call_server(s: Span) -> bool:
    return s.service == WORKER and s.kind == SERVER and s.attrs.get("mcp.method.name") == "tools/call"


def is_api_http_client(s: Span) -> bool:
    return s.service == API and s.kind == CLIENT and "http.request.method" in s.attrs and "/mcp" in str(s.attrs.get("url.full", ""))


def is_agent(s: Span, name: str) -> bool:
    # Chat/tool spans also carry gen_ai.agent.name (Pydantic AI); only the invoke_agent span is the agent.
    invoke = s.attrs.get("gen_ai.operation.name") == "invoke_agent" or s.name.startswith("invoke_agent")
    return invoke and (s.attrs.get("gen_ai.agent.name") == name or name in s.name)


def is_local_tool(s: Span) -> bool:
    return s.service == WORKER and (s.attrs.get("gen_ai.tool.name") == "lookup_availability" or s.name == "execute_tool lookup_availability")


def is_db_client(s: Span) -> bool:
    # The query span, not Npgsql's first-use `CONNECT` span.
    system = str(s.attrs.get("db.system.name", "")) + str(s.attrs.get("db.system", ""))
    has_query = "db.query.text" in s.attrs or "db.statement" in s.attrs
    return s.service == WORKER and s.kind == CLIENT and "postgresql" in system and has_query


def is_shipping_client(s: Span) -> bool:
    return s.service == WORKER and s.kind == CLIENT and "/quotes/" in str(s.attrs.get("url.full", ""))


def _first(items: list[Span]) -> Span | None:
    return items[0] if items else None


def _desc(s: Span | None) -> str:
    return f"{s.service}:{s.name!r}({s.span_id})" if s else "<missing>"


def check_run(run: dict[str, Any], spans: list[Span], logs: list[Log], profile: str) -> tuple[list[Check], Trace | None]:
    run_id = run["run_id"]
    checks: list[Check] = []

    def add(cid: str, ok: bool, detail: str) -> None:
        checks.append(Check(cid, ok, detail))

    roots = [s for s in spans if s.service == WEB and s.name == "ui.ask" and s.attrs.get("demo.run_id") == run_id]
    if not roots:
        add("browser-root", False, f"no ui.ask span with demo.run_id={run_id} (browser export lost or not flushed)")
        return checks, None
    root = roots[0]
    t = Trace(root.trace_id, spans)
    add("browser-root", not root.parent_id, f"ui.ask {root.span_id} trace {root.trace_id} parent={root.parent_id or 'none'}")

    web_client = _first(t.find(lambda s: s.service == WEB and s.kind == CLIENT and s.parent_id == root.span_id))
    api_server = _first([s for s in spans if s.service == API and s.kind == SERVER and s.attrs.get("demo.run_id") == run_id])
    router = _first(t.find(lambda s: s.service == API and is_agent(s, "request-router")))
    mcp_server = _first(t.find(is_mcp_call_server))
    # The message parent is whichever MCP client wrapper was current at `_meta` injection (innermost one).
    mcp_parent = t.parent(mcp_server)
    candidates = t.find(is_mcp_call_client)
    innermost = [c for c in candidates if not any(t.is_ancestor(c, o) for o in candidates)]
    mcp_client = mcp_parent if mcp_parent and is_mcp_call_client(mcp_parent) else _first(innermost)
    http_server_ids = {sid for tid, sid in (mcp_server.links if mcp_server else [])}
    http_server = _first([s for s in spans if s.span_id in http_server_ids and s.service == WORKER and s.kind == SERVER])
    analyst = _first(t.find(lambda s: s.service == WORKER and is_agent(s, "availability-analyst")))
    tool = _first(t.find(is_local_tool))
    db = _first(t.find(is_db_client))
    ship = _first(t.find(is_shipping_client))
    ship_server = _first(t.find(lambda s: s.service == SHIPPING and s.kind == SERVER))

    required = {
        "web CLIENT POST /api/ask": web_client,
        "api SERVER /api/ask": api_server if api_server and api_server.trace_id == t.trace_id else None,
        "api invoke_agent request-router": router,
        "api MCP tools/call CLIENT": mcp_client,
        "worker MCP tools/call SERVER": mcp_server,
        "worker invoke_agent availability-analyst": analyst,
        "worker local tool lookup_availability": tool,
        "worker db CLIENT (postgresql)": db,
        "worker HTTP CLIENT (shipping)": ship,
    }
    missing = [k for k, v in required.items() if v is None]
    detail = "all present" if not missing else "missing from submission trace: " + "; ".join(missing)
    if missing:
        detail += diagnose_missing(t, spans, run)
    add("required-spans", not missing, detail)

    ids = [s.span_id for s in t.spans]
    add("unique-span-ids", len(ids) == len(set(ids)), f"{len(ids)} spans in trace")
    dangling = [s for s in t.spans if s.parent_id and s.parent_id not in t.by_id]
    cyclic = [s for s in t.spans if any(a.span_id == s.span_id for a in t.ancestors(s))]
    add(
        "parents-resolve",
        not dangling and not cyclic,
        "ok"
        if not dangling and not cyclic
        else f"dangling parents: {[_desc(s) for s in dangling][:6]} cycles: {len(cyclic)}"
        + (" — children still carry the submission trace id, so this is EXPORT loss of their parents, not propagation loss" if dangling else ""),
    )

    add(
        "browser-client->api-server",
        bool(api_server and web_client and api_server.parent_id == web_client.span_id),
        f"api server parent={api_server.parent_id if api_server else None} web client={web_client.span_id if web_client else None}"
        + (f" (api server is in trace {api_server.trace_id})" if api_server and api_server.trace_id != t.trace_id else ""),
    )
    http_client = t.parent(http_server)
    add(
        "api-http-client->worker-http-server",
        bool(http_server and http_client and is_api_http_client(http_client) and t.is_ancestor(mcp_client, http_client)),
        f"worker HTTP server {_desc(http_server)} in trace {http_server.trace_id if http_server else '-'}; parent={_desc(http_client)}",
    )
    add(
        "mcp-message-parent",
        bool(mcp_server and mcp_client and mcp_server.parent_id == mcp_client.span_id),
        f"MCP server parent={_desc(t.parent(mcp_server))} expected={_desc(mcp_client)}",
    )
    add(
        "mcp-http-link",
        bool(http_server),
        f"MCP server links={mcp_server.links if mcp_server else []} -> {_desc(http_server)}",
    )
    add(
        "agent-ancestry",
        bool(t.is_ancestor(router, mcp_client) and t.is_ancestor(mcp_server, analyst) and t.is_ancestor(analyst, tool)),
        f"router>{_desc(mcp_client)}: {t.is_ancestor(router, mcp_client)}; MCP server>analyst: {t.is_ancestor(mcp_server, analyst)}; "
        f"analyst>tool: {t.is_ancestor(analyst, tool)}",
    )
    add(
        "io-under-tool",
        bool(t.is_ancestor(tool, db) and t.is_ancestor(tool, ship)),
        f"tool>db: {t.is_ancestor(tool, db)}; tool>shipping: {t.is_ancestor(tool, ship)}",
    )
    if db and ship and tool:
        overlap = min(db.end, ship.end) - max(db.start, ship.start)
        parent_after = tool.end >= max(db.end, ship.end)
        sum_ms = ((db.end - db.start) + (ship.end - ship.start)) / 1e6
        add(
            "io-overlap",
            overlap > 0 and parent_after,
            f"overlap={overlap / 1e6:.1f}ms tool_ends_after_both={parent_after} db={(db.end - db.start) / 1e6:.1f}ms "
            f"http={(ship.end - ship.start) / 1e6:.1f}ms sum={sum_ms:.1f}ms tool={(tool.end - tool.start) / 1e6:.1f}ms",
        )
    else:
        add("io-overlap", False, "db/shipping/tool span missing from submission trace")
    add(
        "shipping-server-hop",
        bool(ship_server and ship and ship_server.parent_id == ship.span_id),
        f"shipping server parent={ship_server.parent_id if ship_server else None} expected={ship.span_id if ship else None}",
    )

    trace_logs = [lg for lg in logs if lg.trace_id == t.trace_id]
    by_service = {svc: [lg for lg in trace_logs if lg.service == svc and lg.span_id in t.by_id] for svc in (WEB, API, WORKER, SHIPPING)}
    add(
        "logs-correlated",
        all(by_service.values()),
        ", ".join(f"{svc}={len(v)}" for svc, v in by_service.items()) + " log records attached to spans in the trace",
    )

    related = {t.trace_id} | {s.trace_id for s in spans if s.span_id in http_server_ids}
    leaked = sorted(
        {c for s in spans if s.trace_id in related for c in CANARIES if c in s.raw}
        | {c for lg in logs if lg.trace_id in related for c in CANARIES if c in lg.raw}
    )
    add("privacy-canaries", not leaked, "no canaries in spans/logs of the submission" if not leaked else f"LEAKED: {leaked}")

    exporter = t.find(lambda s: "/v1/traces" in str(s.attrs.get("url.full", "")) or "/telemetry/" in str(s.attrs.get("url.full", "")))
    add("no-export-recursion", not exporter, f"{len(exporter)} telemetry-export spans in trace")

    outcome = api_server.attrs.get("demo.outcome") if api_server else None
    if profile == "logical-error":
        add(
            "logical-error-mapped",
            bool(
                outcome == "tool_error"
                and mcp_client
                and mcp_client.status == STATUS_ERROR
                and http_client
                and http_client.attrs.get("http.response.status_code") == 200
                and http_client.status != STATUS_ERROR
            ),
            f"api outcome={outcome}; MCP client status={mcp_client.status if mcp_client else None}; "
            f"HTTP client status_code={http_client.attrs.get('http.response.status_code') if http_client else None} "
            f"status={http_client.status if http_client else None}; MCP server status={mcp_server.status if mcp_server else None}",
        )
    else:
        add("outcome-ok", outcome == "ok", f"api demo.outcome={outcome}")
    return checks, t


def diagnose_missing(t: Trace, spans: list[Span], run: dict[str, Any]) -> str:
    """Explain where missing worker spans went: another trace (propagation) or nowhere (export)."""
    try:
        lo = int(datetime.fromisoformat(run["started_at"]).timestamp() * 1e9) - 5_000_000_000
        hi = int(datetime.fromisoformat(run["finished_at"]).timestamp() * 1e9) + 5_000_000_000
    except KeyError, ValueError:
        return ""
    elsewhere = sorted(
        {
            (s.service, s.trace_id)
            for s in spans
            if s.trace_id != t.trace_id and lo <= s.start <= hi and s.service in (WORKER, API, SHIPPING) and not s.attrs.get("demo.run_id")
        }
    )
    if not elsewhere:
        return " | no candidate spans in other traces near this run (export loss or not emitted)"
    return " | candidate spans in OTHER traces during the run (propagation loss): " + ", ".join(f"{svc}@{tid[:8]}" for svc, tid in elsewhere[:6])


def evaluate(manifest: dict[str, Any], evidence: Path, profile: str, timeout: float) -> list[tuple[dict[str, Any], list[Check]]]:
    runs: list[dict[str, Any]] = manifest["runs"] if manifest.get("kind") == "concurrency" else [manifest]
    deadline = time.monotonic() + timeout
    while True:
        spans, logs = load(evidence)
        results = [(r, check_run(r, spans, logs, profile)[0]) for r in runs]
        complete = all(
            next((c.ok for c in cs if c.id == "required-spans"), False) and next((c.ok for c in cs if c.id == "logs-correlated"), False) for _, cs in results
        )
        if complete or time.monotonic() >= deadline:
            break
        time.sleep(2)
    if manifest.get("kind") == "concurrency":
        results.append(({"run_id": "<all>"}, concurrency_checks(runs, spans)))
    return results


def concurrency_checks(runs: list[dict[str, Any]], spans: list[Span]) -> list[Check]:
    trace_of: dict[str, str] = {}
    for r in runs:
        root = next((s for s in spans if s.service == WEB and s.name == "ui.ask" and s.attrs.get("demo.run_id") == r["run_id"]), None)
        if root:
            trace_of[r["run_id"]] = root.trace_id
    traces = list(trace_of.values())
    checks = [Check("distinct-traces", len(traces) == len(runs) == len(set(traces)), f"{len(set(traces))} distinct submission traces for {len(runs)} runs")]
    owner = {s.span_id: s.trace_id for s in spans if s.trace_id in set(traces)}
    cross = [s for s in spans if s.trace_id in set(traces) and s.parent_id in owner and owner[s.parent_id] != s.trace_id]
    checks.append(Check("no-cross-trace-parents", not cross, f"{len(cross)} spans parented into another submission's trace"))
    dup: list[str] = []
    for tid in traces:
        t = Trace(tid, spans)
        n_api = len(t.find(lambda s: s.service == API and s.kind == SERVER and s.attrs.get("http.route") == "/api/ask"))
        n_mcp = len(t.find(is_mcp_call_server))
        if n_api != 1 or n_mcp != 1:
            dup.append(f"{tid[:8]}: api_server={n_api} mcp_server={n_mcp}")
    checks.append(Check("one-request-per-trace", not dup, "each trace has exactly one API request and one MCP tool call" if not dup else "; ".join(dup)))
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--evidence", type=Path, default=Path("evidence/otel"))
    ap.add_argument("--profile", default="baseline", choices=["baseline", "logical-error"])
    ap.add_argument("--timeout", type=float, default=30.0, help="seconds to poll for late exports")
    ap.add_argument("--expect-fail", default="", help="comma-separated check ids that MUST fail")
    ap.add_argument("--expect-pass", default="", help="comma-separated check ids that MUST pass (default: all when --expect-fail is empty)")
    ap.add_argument("--out", type=Path, help="write machine-readable results JSON here")
    args = ap.parse_args(argv)

    if not args.manifest.exists():
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 2
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    results = evaluate(manifest, args.evidence, args.profile, args.timeout)

    must_fail = {c for c in args.expect_fail.split(",") if c}
    must_pass = {c for c in args.expect_pass.split(",") if c}
    ok = True
    first_bad: str | None = None
    for run, checks in results:
        print(f"\n== run {run.get('run_id')} (trace reported by API: {run.get('trace_id', '-')})")
        for c in checks:
            mark = "PASS" if c.ok else "FAIL"
            if c.id in must_fail:
                expected = not c.ok
            elif c.id in must_pass:
                expected = c.ok
            else:  # with explicit expectations, other checks are informational
                expected = c.ok or bool(must_fail or must_pass)
            if not expected:
                ok = False
                first_bad = first_bad or f"{c.id}: {c.detail}"
            note = "" if c.id not in must_fail else " (expected FAIL)" if not c.ok else " (expected FAIL but passed!)"
            print(f"  [{mark}] {c.id}{note}: {c.detail}")
        seen = {c.id for c in checks}
        for cid in (must_fail | must_pass) - seen:
            if run.get("run_id") != "<all>":
                ok = False
                first_bad = first_bad or f"{cid}: check not evaluated"
    print("\nRESULT:", "EXPECTATIONS MET" if ok else f"FAILED — first unexpected result: {first_bad}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "checked_at": datetime.now(UTC).isoformat(),
                    "profile": args.profile,
                    "expect_fail": sorted(must_fail),
                    "expect_pass": sorted(must_pass),
                    "ok": ok,
                    "runs": [{"run_id": r.get("run_id"), "trace_id": r.get("trace_id"), "checks": [c.__dict__ for c in cs]} for r, cs in results],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
