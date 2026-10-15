"""HTTP surface of the API and the shipping stub, with the telemetry each request leaves behind."""

from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from opentelemetry import propagate
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from api import main, shipping_stub
from api.agent import Settings
from api.models import AskError, AskResponse
from tests.conftest import CANARIES, InProcessMcp, Telemetry, ctx

ASK = {
    "message": "Can I get the demo lantern to DEMO-NORTH? CANARY-PROMPT-91c4",
    "product_code": "DEMO-LANTERN",
    "destination_zone": "DEMO-NORTH",
}


@pytest.fixture
def api(mcp_server: InProcessMcp, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DEMO_MCP_URL", mcp_server.url)
    monkeypatch.setenv("DEMO_MODE", "scripted")
    monkeypatch.setenv("DEMO_SCENARIO", "baseline")
    with TestClient(main.app) as client:
        yield client
    mcp_server.fail = False


def server_span(captured: Telemetry, name: str) -> ReadableSpan:
    [span] = [s for s in captured.spans.get_finished_spans() if s.kind == SpanKind.SERVER and s.name == name]
    return span


def logs(captured: Telemetry, body: str) -> list[ReadableLogRecord]:
    return [r for r in captured.logs.get_finished_logs() if r.log_record.body == body]


def test_ask_success(api: TestClient, captured: Telemetry) -> None:
    assert api.get("/api/healthz").status_code == 200
    assert api.get("/api/config").json() == {"scenario": "baseline", "mode": "scripted"}
    captured.clear()

    response = api.post("/api/ask", json=ASK, headers={"X-Demo-Canary": "CANARY-HEADER-e620"})

    assert response.status_code == 200
    body = AskResponse.model_validate(response.json())
    assert "CANARY-COMPLETION-3d7a" in body.answer
    assert body.evidence.shipping.reference == "CANARY-SHIPREF-5b1e"
    assert (body.scenario, body.mode) == ("baseline", "scripted")

    span = server_span(captured, "POST /api/ask")
    assert f"{ctx(span).trace_id:032x}" == body.trace_id
    assert span.attributes is not None
    assert span.attributes["demo.run_id"] == body.run_id
    assert (span.attributes["demo.scenario"], span.attributes["demo.mode"]) == ("baseline", "scripted")
    assert span.attributes["demo.outcome"] == "ok"
    agent_span = next(s for s in captured.spans.get_finished_spans() if s.name == "invoke_agent request-router")
    assert agent_span.parent and agent_span.parent.span_id == ctx(span).span_id

    for name in ("ask received", "ask completed"):
        [record] = logs(captured, name)
        assert record.log_record.trace_id == ctx(span).trace_id
        assert record.log_record.span_id == ctx(span).span_id
        assert record.log_record.attributes and record.log_record.attributes["demo.run_id"] == body.run_id
    [completed] = logs(captured, "ask completed")
    assert completed.log_record.attributes and completed.log_record.attributes["demo.outcome"] == "ok"

    # healthz/config are excluded from instrumentation; no canary reached any span or log.
    assert {s.name for s in captured.spans.get_finished_spans() if s.kind == SpanKind.SERVER} >= {"POST /api/ask"}
    assert not [s for s in captured.spans.get_finished_spans() if "healthz" in s.name or "config" in s.name]
    dumped = captured.dump()
    assert not [c for c in CANARIES if c in dumped]


def test_ask_tool_error(api: TestClient, mcp_server: InProcessMcp, captured: Telemetry) -> None:
    mcp_server.fail = True

    response = api.post("/api/ask", json=ASK)

    assert response.status_code == 502
    error = AskError.model_validate(response.json())
    assert error.error == "tool_error"
    assert "CANARY" not in error.detail
    span = server_span(captured, "POST /api/ask")
    assert span.attributes and span.attributes["demo.outcome"] == "tool_error"
    assert f"{ctx(span).trace_id:032x}" == error.trace_id
    [completed] = logs(captured, "ask completed")
    assert completed.log_record.attributes and completed.log_record.attributes["error.type"] == "ToolError"
    # The tool's error text carries the shipping canary; it must not reach spans, exception events or logs.
    assert "CANARY-SHIPREF-5b1e" not in captured.dump()


def test_ask_timeout(api: TestClient, captured: Telemetry, monkeypatch: pytest.MonkeyPatch) -> None:
    router = main.app.state.router
    expired = replace(router, settings=replace(router.settings, request_timeout_seconds=0))
    monkeypatch.setattr(main.app.state, "router", expired)

    response = api.post("/api/ask", json=ASK)

    assert response.status_code == 504
    assert AskError.model_validate(response.json()).error == "timeout"
    span = server_span(captured, "POST /api/ask")
    assert span.attributes and span.attributes["demo.outcome"] == "error"


def test_ask_without_tool_call_is_a_workload_failure(
    api: TestClient, captured: Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_tool(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("answered without the tool")])

    router = main.app.state.router
    monkeypatch.setattr(main.app.state, "router", replace(router, live_model=FunctionModel(no_tool)))

    response = api.post("/api/ask", json=ASK)

    assert response.status_code == 502
    assert AskError.model_validate(response.json()).error == "workload_failure"
    span = server_span(captured, "POST /api/ask")
    assert span.attributes and span.attributes["demo.outcome"] == "error"


@pytest.mark.parametrize(
    "payload",
    [
        {**ASK, "message": ""},
        {**ASK, "message": "x" * 2001},
        {**ASK, "product_code": "DEMO-UNKNOWN"},
        {**ASK, "extra": "field"},
    ],
)
def test_ask_validation(api: TestClient, captured: Telemetry, payload: dict[str, str]) -> None:
    assert api.post("/api/ask", json=payload).status_code == 422
    assert "CANARY-PROMPT-91c4" not in captured.dump()


def test_tracecontext_only_propagator() -> None:
    assert propagate.get_global_textmap().fields == {"traceparent", "tracestate"}


def test_unknown_scenario_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_SCENARIO", "not-a-scenario")
    with pytest.raises(ValueError, match="DEMO_SCENARIO"):
        Settings.from_env()
    with pytest.raises(ValueError, match="DEMO_SCENARIO"), TestClient(shipping_stub.app):
        pass


def test_live_without_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        Settings.from_env()


@pytest.fixture
def shipping(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DEMO_SHIPPING_DELAY_MS", "0")
    with TestClient(shipping_stub.app) as client:
        yield client


def test_shipping_quote(shipping: TestClient, captured: Telemetry) -> None:
    response = shipping.get("/quotes/DEMO-SOUTH")

    assert response.status_code == 200
    assert response.json() == {
        "destination_zone": "DEMO-SOUTH",
        "carrier": "DEMO-CARRIER",
        "price_cents": 900,
        "currency": "EUR",
        "eta_days": 3,
        "reference": "CANARY-SHIPREF-5b1e",
    }
    span = server_span(captured, "GET /quotes/{zone}")
    [record] = logs(captured, "quote served")
    assert (record.log_record.trace_id, record.log_record.span_id) == (ctx(span).trace_id, ctx(span).span_id)
    assert "CANARY-SHIPREF-5b1e" not in captured.dump()


def test_shipping_errors(shipping: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert shipping.get("/healthz").status_code == 200
    assert shipping.get("/quotes/DEMO-WEST").status_code == 404
    monkeypatch.setenv("DEMO_SHIPPING_FAIL", "true")
    assert shipping.get("/quotes/DEMO-NORTH").status_code == 503
