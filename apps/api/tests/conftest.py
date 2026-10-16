"""Session-wide in-memory telemetry (never OTLP) and an in-process MCP server standing in for the .NET worker."""

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext
from starlette.types import Message, Receive, Scope, Send

from api import telemetry
from api.models import AvailabilityReport, DestinationZone, ProductCode, ShippingQuote

# Same as compose: stable HTTP semantic-convention names from the FastAPI and httpx2 instrumentors. Read once, at
# the first instrumentation, so it must be set before any test instruments anything.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

CANARIES = ("CANARY-PROMPT-91c4", "CANARY-COMPLETION-3d7a", "CANARY-SHIPREF-5b1e", "CANARY-HEADER-e620")


@dataclass
class Telemetry:
    spans: InMemorySpanExporter
    logs: InMemoryLogRecordExporter

    def clear(self) -> None:
        self.spans.clear()
        self.logs.clear()

    def dump(self) -> str:
        """Everything exported, as text, for canary scans."""
        parts: list[str] = [span.to_json() for span in self.spans.get_finished_spans()]
        parts += [record.to_json() for record in self.logs.get_finished_logs()]
        return "\n".join(parts)


@pytest.fixture(scope="session", autouse=True)
def otel() -> Iterator[Telemetry]:
    captured = Telemetry(InMemorySpanExporter(), InMemoryLogRecordExporter())
    telemetry.setup("trace-demo-api-test", span_exporter=captured.spans, log_exporter=captured.logs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(telemetry, "shutdown", lambda: None)  # app lifespans must not stop the session's providers
        yield captured


@pytest.fixture
def captured(otel: Telemetry) -> Telemetry:
    otel.clear()
    return otel


def ctx(span: ReadableSpan) -> SpanContext:
    assert span.context is not None
    return span.context


@dataclass
class SeenRequest:
    method: str
    headers: dict[str, str]
    body: bytearray = field(default_factory=bytearray)

    @property
    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


@dataclass
class InProcessMcp:
    url: str
    seen: list[SeenRequest] = field(default_factory=list[SeenRequest])
    fail: bool = False

    def rpc(self, rpc_method: str) -> list[SeenRequest]:
        return [r for r in self.seen if r.json and r.json["method"] == rpc_method]


def report(product_code: ProductCode, destination_zone: DestinationZone) -> AvailabilityReport:
    return AvailabilityReport(
        product_code=product_code,
        destination_zone=destination_zone,
        available=True,
        quantity_on_hand=12,
        warehouse="DEMO-WH-1",
        shipping=ShippingQuote(
            destination_zone=destination_zone,
            carrier="DEMO-CARRIER",
            price_cents=1200,
            currency="EUR",
            eta_days=2,
            reference="CANARY-SHIPREF-5b1e",
        ),
        summary="in stock",
    )


@pytest.fixture(scope="session")
def mcp_server() -> Iterator[InProcessMcp]:
    """mcp SDK MCPServer over uvicorn on a free port, recording each raw HTTP request (headers + body)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    state = InProcessMcp(url=f"http://127.0.0.1:{sock.getsockname()[1]}/mcp")

    server = MCPServer("in-process-worker")

    @server.tool(name="build_availability_report", structured_output=True)
    def build_availability_report(  # pyright: ignore[reportUnusedFunction]
        product_code: ProductCode, destination_zone: DestinationZone
    ) -> AvailabilityReport:
        if state.fail:
            # A deliberate ToolError reaches the client verbatim (an unexpected exception would be masked),
            # modelling a worker whose error text carries tool-result content.
            raise ToolError("stock lookup failed CANARY-SHIPREF-5b1e")
        return report(product_code, destination_zone)

    @server.tool(name="unrelated_tool")
    def unrelated_tool() -> str:  # pyright: ignore[reportUnusedFunction]
        return "must be filtered out of the router's tool list"

    inner = server.streamable_http_app(host="127.0.0.1")

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        seen = SeenRequest(scope["method"], {k.decode(): v.decode() for k, v in scope["headers"]})
        state.seen.append(seen)

        async def recording_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                seen.body += message.get("body", b"")
            return message

        await inner(scope, recording_receive, send)

    uv_server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=uv_server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not uv_server.started:
        time.sleep(0.01)
    yield state
    uv_server.should_exit = True
    thread.join(timeout=10)
