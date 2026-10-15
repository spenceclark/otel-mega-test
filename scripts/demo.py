# /// script
# requires-python = ">=3.14"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Developer command interface (spec §10). Called by scripts/demo.sh and scripts/demo.ps1.

demo doctor [--mode scripted|live] [--logfire]
demo up [--mode scripted|live] [--logfire]
demo run [--scenario baseline|concurrency|<experiment>]
demo verify [--run <run_id> | --manifest <path>] [--profile baseline|logical-error]
demo experiment <name>|all
demo down          # stop; keeps the Postgres volume and evidence/
demo reset         # DESTRUCTIVE: drop volumes and delete evidence/
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"
STATE = EVIDENCE / ".demo-state.json"
SCENARIOS: dict[str, dict[str, Any]] = yaml.safe_load((ROOT / "experiments" / "scenarios.yaml").read_text(encoding="utf-8"))["scenarios"]
# Variables a scenario may set; anything not set by the scenario is reset to its baseline default.
SCENARIO_VARS = {"DEMO_SCENARIO": "baseline", "DEMO_CONTENT_CAPTURE": "false", "API_OTLP_ENDPOINT": "http://otel-collector:4318"}


def say(msg: str) -> None:
    print(f"[demo] {msg}", flush=True)


def sh(*cmd: str, env: dict[str, str] | None = None, check: bool = True) -> int:
    say("$ " + " ".join(cmd))
    rc = subprocess.run(cmd, check=False, cwd=ROOT, env=env).returncode
    if check and rc != 0:
        raise SystemExit(f"[demo] command failed ({rc}): {' '.join(cmd)}")
    return rc


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return "dev"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {"mode": "scripted", "logfire": False}


def compose_env(state: dict[str, Any], scenario: str = "baseline") -> dict[str, str]:
    env = dict(os.environ)
    env.update(SCENARIO_VARS)
    env.update({k: str(v) for k, v in (SCENARIOS[scenario].get("env") or {}).items()})
    env["GIT_SHA"] = git_sha()
    env["DEMO_MODE"] = state["mode"]
    env["OTELCOL_CONFIG"] = "otelcol.yaml" if state.get("logfire") else "otelcol.ci.yaml"
    return env


def prepare_evidence() -> None:
    for d in ("otel", "runs", "experiments"):
        (EVIDENCE / d).mkdir(parents=True, exist_ok=True)
    if os.name == "posix":  # collector-contrib runs as uid 10001 and must write the bind mount
        (EVIDENCE / "otel").chmod(0o777)


def configured(name: str) -> bool:
    """Set in the shell or in the repo-root .env (Compose reads both; the shell wins)."""
    if os.environ.get(name):
        return True
    try:
        lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(k.strip() == name and v.strip().strip("\"'") for k, _, v in (ln.partition("=") for ln in lines if not ln.lstrip().startswith("#")))


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def cmd_doctor(args: argparse.Namespace) -> int:
    problems: list[str] = []
    for tool in ("docker", "git", "uv"):
        if not shutil.which(tool):
            problems.append(f"missing prerequisite: {tool}")
    if shutil.which("docker"):
        if subprocess.run(["docker", "compose", "version"], check=False, capture_output=True).returncode != 0:
            problems.append("docker compose v2 plugin not available")
        if subprocess.run(["docker", "info"], check=False, capture_output=True).returncode != 0:
            problems.append("docker daemon not reachable")
        running = subprocess.run(["docker", "compose", "ps", "-q"], check=False, cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if not running:
            for port in (5173, 14318, 55432):
                if not port_free(port):
                    problems.append(f"127.0.0.1:{port} is already in use by another process")
    if args.mode == "live" and not configured("ANTHROPIC_API_KEY"):
        problems.append("live mode needs ANTHROPIC_API_KEY (environment or .env)")
    if args.logfire and not configured("LOGFIRE_TOKEN"):
        problems.append("--logfire needs LOGFIRE_TOKEN in the environment or .env (and LOGFIRE_OTLP_ENDPOINT for a non-EU project)")
    for p in problems:
        say("PROBLEM: " + p)
    say("doctor: OK" if not problems else f"doctor: {len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_up(args: argparse.Namespace) -> int:
    if cmd_doctor(args) != 0 and not args.force:
        return 1
    state = {"mode": args.mode, "logfire": args.logfire}
    prepare_evidence()
    STATE.write_text(json.dumps(state), encoding="utf-8")
    sh("docker", "compose", "up", "-d", "--build", "--wait", "--wait-timeout", "300", env=compose_env(state))
    say("stack ready: http://127.0.0.1:5173")
    return 0


def apply_scenario(scenario: str) -> dict[str, Any]:
    state = load_state()
    prepare_evidence()
    # Readiness comes from compose healthchecks (Postgres query, worker /healthz, API MCP session), not sleeps.
    sh("docker", "compose", "up", "-d", "--wait", "--wait-timeout", "300", env=compose_env(state, scenario))
    return state


def run_e2e(scenario: str) -> Path:
    spec = SCENARIOS[scenario].get("spec", "ask.spec.ts")
    state = apply_scenario(scenario)
    env = compose_env(state, scenario)
    # --no-deps: the stack was just brought to the scenario's config; the label goes to the runner only.
    sh(
        "docker",
        "compose",
        "--profile",
        "e2e",
        "run",
        "--rm",
        "--build",
        "--no-deps",
        "-e",
        f"SCENARIO={scenario}",
        "e2e",
        "npx",
        "playwright",
        "test",
        spec,
        env=env,
    )
    manifest = EVIDENCE / "runs" / ("latest-concurrency.json" if spec.startswith("concurrency") else "latest.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mode"], data["git_sha"] = state["mode"], git_sha()
    data["scenario"] = scenario
    # The Playwright container writes as root; replacing (not rewriting) the file needs only directory access.
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, manifest)
    say(f"manifest: {manifest.relative_to(ROOT)}")
    return manifest


def assert_traces(manifest: Path, profile: str, expect_fail: list[str] | None = None, expect_pass: list[str] | None = None, out: Path | None = None) -> int:
    cmd = [sys.executable, str(ROOT / "scripts" / "assert-traces.py"), "--manifest", str(manifest), "--evidence", str(EVIDENCE / "otel"), "--profile", profile]
    if expect_fail:
        cmd += ["--expect-fail", ",".join(expect_fail)]
    if expect_pass:
        cmd += ["--expect-pass", ",".join(expect_pass)]
    if out:
        cmd += ["--out", str(out)]
    return sh(*cmd, check=False)


def cmd_run(args: argparse.Namespace) -> int:
    run_e2e(args.scenario)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest) if args.manifest else EVIDENCE / "runs" / (f"{args.run}.json" if args.run else "latest.json")
    return assert_traces(manifest, args.profile)


def cmd_experiment(args: argparse.Namespace) -> int:
    names = [n for n in SCENARIOS if n not in ("baseline", "concurrency")] if args.name == "all" else [args.name]
    failures = 0
    for name in names:
        sc = SCENARIOS[name]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_dir = EVIDENCE / "experiments" / name / stamp
        out_dir.mkdir(parents=True, exist_ok=True)
        say(f"=== experiment {name}: {sc['fault']}")
        broken = run_e2e(name)
        shutil.copy(broken, out_dir / "broken-manifest.json")
        broken_rc = assert_traces(
            out_dir / "broken-manifest.json", sc.get("profile", "baseline"), sc.get("expect_fail"), sc.get("expect_pass"), out_dir / "broken-assertions.json"
        )
        say(f"=== repair {name}: {sc['fix']}")
        fixed = run_e2e("baseline")
        shutil.copy(fixed, out_dir / "fixed-manifest.json")
        fixed_rc = assert_traces(out_dir / "fixed-manifest.json", "baseline", out=out_dir / "fixed-assertions.json")
        verdict = "confirmed" if broken_rc == 0 and fixed_rc == 0 else "UNEXPECTED"
        failures += verdict != "confirmed"
        summary = {
            "schema_version": 1,
            "experiment": name,
            "git_sha": git_sha(),
            "mode": load_state()["mode"],
            "recorded_at": stamp,
            "hypothesis": sc["hypothesis"],
            "fault": sc["fault"],
            "fix": sc["fix"],
            "expected_graph": sc.get("expected_graph"),
            "broken": _summarize(out_dir / "broken-manifest.json", out_dir / "broken-assertions.json"),
            "fixed": _summarize(out_dir / "fixed-manifest.json", out_dir / "fixed-assertions.json"),
            "verdict": verdict,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        # Sanitized, committable copy (synthetic ids only, no bodies or headers).
        (ROOT / "experiments" / name).mkdir(exist_ok=True)
        (ROOT / "experiments" / name / "result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        say(f"=== {name}: {verdict} (evidence: {out_dir.relative_to(ROOT)})")
    return 1 if failures else 0


def _summarize(manifest: Path, assertions: Path) -> dict[str, Any]:
    m = json.loads(manifest.read_text(encoding="utf-8"))
    a = json.loads(assertions.read_text(encoding="utf-8")) if assertions.exists() else {}
    checks = a.get("runs", [{}])[0].get("checks", [])
    return {
        "run_id": m.get("run_id"),
        "api_trace_id": m.get("trace_id"),
        "outcome": m.get("outcome"),
        "expectations_met": a.get("ok"),
        "checks": {c["id"]: {"ok": c["ok"], "detail": c["detail"]} for c in checks},
    }


def _assert_traces_module() -> Any:
    spec = importlib.util.spec_from_file_location("assert_traces", ROOT / "scripts" / "assert-traces.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["assert_traces"] = module
    spec.loader.exec_module(module)
    return module


def _judge_carrier_case(case: dict[str, Any], spans: list[Any], at: Any) -> tuple[bool, str]:
    by_id = {(s.trace_id, s.span_id): s for s in spans}
    if not case["ok"]:
        return False, "tool call failed: bad context broke business handling"
    trace = case.get("meta_trace") if case["case"] in ("conflict", "header-all-zero") else case.get("header_trace")
    mcp = next((s for s in spans if s.trace_id == trace and at.is_mcp_call_server(s)), None)
    if mcp is None:
        return False, f"no MCP tools/call server span in trace {trace}"
    link = next((by_id[lk] for lk in mcp.links if lk in by_id), None)
    if case["case"] == "conflict":
        ok = mcp.parent_id == case["meta_span"] and link is not None and link.trace_id == case["header_trace"] and link.parent_id == case["header_span"]
        return ok, (
            f"MCP op follows _meta (parent {mcp.parent_id}, expected {case['meta_span']}); HTTP server stays in header trace "
            f"{link.trace_id if link else None} (expected {case['header_trace']}); joined only by a link"
        )
    if case["case"] == "header-all-zero":
        ok = mcp.parent_id == case["meta_span"] and link is not None and link.trace_id != "0" * 32 and not link.parent_id
        return ok, f"all-zero header discarded: HTTP server span is a new root ({link.trace_id if link else None}); MCP op parent from _meta"
    parent = by_id.get((mcp.trace_id, mcp.parent_id))
    ok = parent is not None and parent.kind == at.SERVER and parent.parent_id == case["header_span"]
    return ok, f"invalid _meta discarded; MCP op falls back to the HTTP server span {at._desc(parent)}"


def cmd_probe_carriers(_: argparse.Namespace) -> int:
    apply_scenario("baseline")
    probe = (ROOT / "tests" / "trace-contracts" / "carrier_probe.py").read_bytes()
    say("$ docker compose exec -T api python - < tests/trace-contracts/carrier_probe.py")
    proc = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-"], cwd=ROOT, input=probe, capture_output=True, check=False)
    if proc.returncode != 0:
        print(proc.stderr.decode(errors="replace"))
        return 1
    cases: list[dict[str, Any]] = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    at = _assert_traces_module()
    deadline = time.monotonic() + 30
    while True:
        spans, _ = at.load(EVIDENCE / "otel")
        results = [(c["case"], *_judge_carrier_case(c, spans, at)) for c in cases]
        if all(ok for _, ok, _ in results) or time.monotonic() >= deadline:
            break
        time.sleep(2)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    passed = all(ok for _, ok, _ in results)
    summary = {
        "schema_version": 1,
        "experiment": "carrier-policy",
        "git_sha": git_sha(),
        "recorded_at": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "hypothesis": "Malformed carriers are discarded without breaking the tool call; conflicting carriers are never merged.",
        "policy": "Each SDK extracts only its own carrier: _meta parents the MCP operation, the header parents the HTTP server span; they are joined only by a link.",
        "cases": [{"case": n, "ok": ok, "detail": d} for n, ok, d in results],
        "verdict": "confirmed" if passed else "UNEXPECTED",
    }
    (ROOT / "experiments" / "carrier-policy").mkdir(exist_ok=True)
    (ROOT / "experiments" / "carrier-policy" / "result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    say(f"carrier-policy: {summary['verdict']}")
    return 0 if passed else 1


def cmd_down(_: argparse.Namespace) -> int:
    return sh("docker", "compose", "--profile", "e2e", "down", check=False)


def cmd_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        say("reset deletes the Postgres volume and evidence/. Re-run with --yes to confirm.")
        return 1
    sh("docker", "compose", "--profile", "e2e", "down", "-v", check=False)
    shutil.rmtree(EVIDENCE, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="demo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    for verb in ("doctor", "up"):
        p = sub.add_parser(verb)
        p.add_argument("--mode", choices=["scripted", "live"], default="scripted")
        p.add_argument("--logfire", action="store_true", help="fan out to Logfire through the Collector (needs LOGFIRE_TOKEN)")
        if verb == "up":
            p.add_argument("--force", action="store_true", help="start even if doctor reports problems")
    p = sub.add_parser("run")
    p.add_argument("--scenario", default="baseline", choices=sorted(SCENARIOS))
    p = sub.add_parser("verify")
    p.add_argument("--run")
    p.add_argument("--manifest")
    p.add_argument("--profile", default="baseline", choices=["baseline", "logical-error"])
    p = sub.add_parser("experiment")
    p.add_argument("name", choices=[*sorted(n for n in SCENARIOS if n not in ("baseline", "concurrency")), "all"])
    sub.add_parser("probe-carriers")
    sub.add_parser("down")
    p = sub.add_parser("reset")
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    handlers = {
        "doctor": cmd_doctor,
        "up": cmd_up,
        "run": cmd_run,
        "verify": cmd_verify,
        "experiment": cmd_experiment,
        "probe-carriers": cmd_probe_carriers,
        "down": cmd_down,
        "reset": cmd_reset,
    }
    return handlers[args.verb](args)


if __name__ == "__main__":
    sys.exit(main())
