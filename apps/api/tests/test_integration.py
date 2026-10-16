"""Phase-3 gate without .NET: the real request-router loop + MCPToolset + traced transport against an in-process
MCP server, spans captured in memory, raw HTTP requests captured on the server side.

Run with `-s` to print the observed span tree (`SPAN_TREE` output) for the trace-contract docs.
"""

import asyncio
import dataclasses
from typing import Any

import pytest
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind
from pydantic_ai.models.anthropic import AnthropicModel

from api.agent import Settings, connect
from api.models import AskRequest, AvailabilityReport
from tests.conftest import CANARIES, InProcessMcp, Telemetry, ctx, report

_tracer = trace.get_tracer(__name__)
SEND = "MCP send tools/call build_availability_report"
REQUEST = AskRequest(
    message="Can I get the demo lantern to DEMO-NORTH? CANARY-PROMPT-91c4",
    product_code="DEMO-LANTERN",
    destination_zone="DEMO-NORTH",
)


def settings(scenario: str, url: str) -> Settings:
    return Settings(
        mode="scripted",
        scenario=scenario,
        mcp_url=url,
        content_capture=False,
        request_timeout_seconds=30,
        model_id="unused",
        max_output_tokens=512,
    )


def span_tree(spans: tuple[ReadableSpan, ...]) -> str:
    """Name, kind, scope, parent and attributes of every span (the observed graph)."""
    by_id = {ctx(s).span_id: s for s in spans if s.context}
    lines: list[str] = []
    for s in sorted(spans, key=lambda s: s.start_time or 0):
        parent = by_id.get(s.parent.span_id) if s.parent else None
        scope = s.instrumentation_scope.name if s.instrumentation_scope else "?"
        attrs = {k: v for k, v in (s.attributes or {}).items() if len(str(v)) < 120}
        lines.append(f"{s.name!r} {s.kind.name} scope={scope} parent={parent.name if parent else None!r} {attrs}")
    return "\n".join(lines)


def one(spans: tuple[ReadableSpan, ...], name: str, kind: SpanKind) -> ReadableSpan:
    [found] = [s for s in spans if s.name == name and s.kind == kind]
    return found


def parent_id(traceparent: str) -> tuple[str, str]:
    """(trace-id, parent-id) of a W3C traceparent."""
    _version, trace_id, span_id, _flags = traceparent.split("-")
    return trace_id, span_id


@pytest.mark.parametrize("scenario", ["baseline", "headers-removed", "meta-removed", "both-removed"])
def test_carriers(scenario: str, mcp_server: InProcessMcp, captured: Telemetry) -> None:
    async def submit() -> tuple[str, AvailabilityReport, int]:
        async with connect(settings(scenario, mcp_server.url)) as router:
            captured.clear()  # connection setup (server/discover, tools/list) is not part of the submission
            mcp_server.seen.clear()
            # Baggage is set but OTEL_PROPAGATORS (unset -> tracecontext) must keep it off both carriers.
            token = context.attach(baggage.set_baggage("demo.leak", "CANARY-BAGGAGE"))
            try:
                with _tracer.start_as_current_span("submission") as root:
                    answer, evidence = await router.run(REQUEST)
            finally:
                context.detach(token)
            return answer, evidence, root.get_span_context().trace_id

    answer, evidence, root_trace_id = asyncio.run(submit())
    spans = captured.spans.get_finished_spans()
    print(f"\nSPAN_TREE {scenario}\n{span_tree(spans)}")

    # The real agent loop ran end to end through the real MCP transport.
    assert "CANARY-COMPLETION-3d7a" in answer
    assert evidence == report("DEMO-LANTERN", "DEMO-NORTH")

    # A submission sends exactly one MCP request: the tool list was cached at connect time.
    assert [r.json["method"] for r in mcp_server.seen] == ["tools/call"]
    [seen] = mcp_server.seen
    meta: dict[str, Any] = seen.json["params"]["_meta"]

    send = one(spans, SEND, SpanKind.CLIENT)
    httpx_scope = "opentelemetry.instrumentation.httpx"  # the official HTTPX2ClientInstrumentor
    [http] = [s for s in spans if s.instrumentation_scope and s.instrumentation_scope.name == httpx_scope]
    assert http.name == "POST"
    assert http.kind == SpanKind.CLIENT
    assert http.parent and http.parent.span_id == ctx(send).span_id
    assert http.attributes and http.attributes["http.response.status_code"] == 200
    client_side = [s for s in spans if s.kind != SpanKind.SERVER]
    assert {ctx(s).trace_id for s in client_side} == {root_trace_id}
    root = f"{root_trace_id:032x}"

    # Transport carrier: parent-id is the HTTP CLIENT span.
    if scenario in ("baseline", "meta-removed"):
        assert parent_id(seen.headers["traceparent"]) == (root, f"{ctx(http).span_id:016x}")
    else:
        assert "traceparent" not in seen.headers
        assert "tracestate" not in seen.headers

    # Message carrier: parent-id is the span current at message creation (`MCP send tools/call ...`).
    if scenario in ("baseline", "headers-removed"):
        assert parent_id(meta["traceparent"]) == (root, f"{ctx(send).span_id:016x}")
    else:
        assert "traceparent" not in meta
        assert "tracestate" not in meta
    # Only the tracing keys are ever stripped; the protocol envelope and progress token survive.
    assert {"progressToken", "io.modelcontextprotocol/protocolVersion"} <= meta.keys()
    assert "baggage" not in seen.headers
    assert "baggage" not in meta

    # The in-process mcp server parents its operation from `_meta` only (it has no HTTP instrumentation).
    server = one(spans, "tools/call build_availability_report", SpanKind.SERVER)
    assert server.attributes and server.attributes["mcp.protocol.version"] == "2026-07-28"
    if "traceparent" in meta:
        assert server.parent and server.parent.span_id == ctx(send).span_id
    else:
        assert server.parent is None

    chats = [s for s in spans if s.name == "chat scripted-router"]
    assert len(chats) == 2
    assert all(s.attributes and s.attributes["demo.model.synthetic"] is True for s in chats)
    # The server also exposes `unrelated_tool`; the router's filter offers the model exactly one tool.
    assert all(s.attributes and "unrelated_tool" not in str(s.attributes["gen_ai.tool.definitions"]) for s in chats)
    agent_span = one(spans, "invoke_agent request-router", SpanKind.INTERNAL)
    assert agent_span.attributes and agent_span.attributes["gen_ai.agent.name"] == "request-router"

    dumped = captured.dump()
    assert not [c for c in (*CANARIES, "CANARY-BAGGAGE") if c in dumped]


def test_content_capture_switch_is_wired(mcp_server: InProcessMcp, captured: Telemetry) -> None:
    """DEMO_CONTENT_CAPTURE=true (privacy-break experiment) really flips pydantic-ai include_content."""

    async def submit() -> None:
        capture = dataclasses.replace(settings("baseline", mcp_server.url), content_capture=True)
        async with connect(capture) as router:
            await router.run(REQUEST)

    asyncio.run(submit())
    dumped = captured.dump()
    assert "CANARY-PROMPT-91c4" in dumped
    assert "CANARY-COMPLETION-3d7a" in dumped


def test_concurrent_submissions_on_one_session_do_not_cross_parent(
    mcp_server: InProcessMcp, captured: Telemetry
) -> None:
    async def submit_two() -> set[int]:
        async with connect(settings("baseline", mcp_server.url)) as router:
            mcp_server.seen.clear()

            async def one_submission() -> int:
                with _tracer.start_as_current_span("submission") as root:
                    await router.run(REQUEST)
                    return root.get_span_context().trace_id

            return set(await asyncio.gather(one_submission(), one_submission()))

    roots = {f"{t:032x}" for t in asyncio.run(submit_two())}
    calls = mcp_server.rpc("tools/call")
    assert len(roots) == 2
    assert {parent_id(r.headers["traceparent"])[0] for r in calls} == roots
    assert {parent_id(r.json["params"]["_meta"]["traceparent"])[0] for r in calls} == roots
    # Each request carried its own HTTP CLIENT span id.
    assert len({parent_id(r.headers["traceparent"])[1] for r in calls}) == 2


def test_live_mode_constructs_anthropic_model(mcp_server: InProcessMcp, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live mode cannot run offline; only its construction is exercised (no request is sent)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-a-secret")

    async def build() -> tuple[str | None, bool | None]:
        live = dataclasses.replace(settings("baseline", mcp_server.url), mode="live", model_id="claude-haiku-5-5")
        async with connect(live) as router:
            if not isinstance(router.live_model, AnthropicModel):
                return None, None
            # The Anthropic SDK's own spans stay off; Pydantic AI's `chat` span is the single owner of the call.
            return router.live_model.model_name, router.live_model.client.open_telemetry["traces"]["enabled"]

    assert asyncio.run(build()) == ("claude-haiku-5-5", False)
