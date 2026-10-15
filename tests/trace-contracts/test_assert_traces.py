"""The trace assertion tool must pass a real baseline trace and fail each deliberately corrupted copy.

uv run --python 3.14 python -m unittest discover -s tests/trace-contracts -v
"""

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures"
_spec = importlib.util.spec_from_file_location("assert_traces", ROOT / "scripts" / "assert-traces.py")
assert _spec and _spec.loader
at = importlib.util.module_from_spec(_spec)
sys.modules["assert_traces"] = at
_spec.loader.exec_module(at)

TRACES: dict[str, Any] = json.loads((FIX / "baseline.traces.json").read_text(encoding="utf-8"))
LOGS: dict[str, Any] = json.loads((FIX / "baseline.logs.json").read_text(encoding="utf-8"))
MANIFEST: dict[str, Any] = json.loads((FIX / "baseline.manifest.json").read_text(encoding="utf-8"))


def spans_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for rs in doc["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]


def find(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in spans_of(doc) if s["name"] == name)


def run_checks(traces: dict[str, Any], logs: dict[str, Any] = LOGS, profile: str = "baseline") -> dict[str, bool]:
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "traces.jsonl").write_text(json.dumps(traces) + "\n", encoding="utf-8")
        (Path(d) / "logs.jsonl").write_text(json.dumps(logs) + "\n", encoding="utf-8")
        spans, log_records = at.load(Path(d))
    checks, _ = at.check_run(MANIFEST, spans, log_records, profile)
    return {c.id: c.ok for c in checks}


class AssertTracesTest(unittest.TestCase):
    def test_baseline_passes_every_check(self) -> None:
        results = run_checks(TRACES)
        self.assertEqual([k for k, ok in results.items() if not ok], [])

    def test_mcp_server_parented_to_http_server_fails_message_parent(self) -> None:
        doc = copy.deepcopy(TRACES)
        mcp = find(doc, "tools/call build_availability_report")
        mcp["parentSpanId"] = find(doc, "POST /mcp/")["spanId"]
        results = run_checks(doc)
        self.assertFalse(results["mcp-message-parent"])
        self.assertTrue(results["api-http-client->worker-http-server"])

    def test_worker_in_new_trace_fails_required_spans(self) -> None:
        doc = copy.deepcopy(TRACES)
        for rs in doc["resourceSpans"]:
            service = next(a["value"]["stringValue"] for a in rs["resource"]["attributes"] if a["key"] == "service.name")
            if service in ("trace-demo-worker", "trace-demo-shipping"):
                for ss in rs["scopeSpans"]:
                    for s in ss["spans"]:
                        s["traceId"] = "f" * 32
        results = run_checks(doc)
        self.assertFalse(results["required-spans"])
        self.assertFalse(results["mcp-message-parent"])
        self.assertTrue(results["browser-client->api-server"])

    def test_serialized_branches_fail_overlap(self) -> None:
        doc = copy.deepcopy(TRACES)
        db = next(s for s in spans_of(doc) if any(a["key"] == "db.query.text" for a in s.get("attributes", [])))
        http = find(doc, "GET")
        duration = int(db["endTimeUnixNano"]) - int(db["startTimeUnixNano"])
        db["endTimeUnixNano"] = str(int(http["startTimeUnixNano"]) - 1)
        db["startTimeUnixNano"] = str(int(db["endTimeUnixNano"]) - duration)
        results = run_checks(doc)
        self.assertFalse(results["io-overlap"])
        self.assertTrue(results["io-under-tool"])

    def test_missing_api_export_is_reported_as_dangling_parents(self) -> None:
        doc = copy.deepcopy(TRACES)
        doc["resourceSpans"] = [
            rs for rs in doc["resourceSpans"] if not any(a["value"].get("stringValue") == "trace-demo-api" for a in rs["resource"]["attributes"])
        ]
        results = run_checks(doc)
        self.assertFalse(results["parents-resolve"])
        self.assertFalse(results["browser-client->api-server"])
        self.assertTrue(results["browser-root"])

    def test_canary_in_attribute_fails_privacy(self) -> None:
        doc = copy.deepcopy(TRACES)
        find(doc, "invoke_agent request-router").setdefault("attributes", []).append(
            {"key": "gen_ai.input.messages", "value": {"stringValue": "CANARY-PROMPT-91c4"}}
        )
        self.assertFalse(run_checks(doc)["privacy-canaries"])

    def test_log_outside_span_fails_log_correlation(self) -> None:
        logs = copy.deepcopy(LOGS)
        for rl in logs["resourceLogs"]:
            for sl in rl["scopeLogs"]:
                for r in sl["logRecords"]:
                    r["spanId"] = ""
        self.assertFalse(run_checks(TRACES, logs)["logs-correlated"])


if __name__ == "__main__":
    unittest.main()
