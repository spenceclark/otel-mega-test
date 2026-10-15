"""The `request-router` Pydantic AI agent: one remote MCP tool, client-side execution, over the traced transport."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import fastmcp
import httpx2
from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from api.mcp_adapter import TracingTransport
from api.models import SCENARIOS, AskRequest, AvailabilityReport, BuildAvailabilityReportArgs, Mode

TOOL = "build_availability_report"
SCRIPTED_ANSWER = "Scripted router answer from the availability report. CANARY-COMPLETION-3d7a"
INSTRUCTIONS = (
    f"You answer product-availability questions. Call {TOOL} exactly once with the product_code and "
    "destination_zone given in the request, then answer in at most two sentences using only the tool result."
)
# Caps both modes; a scripted run needs 2 requests and 1 tool call.
USAGE_LIMITS = UsageLimits(request_limit=3, tool_calls_limit=1, total_tokens_limit=20_000)


class WorkloadFailure(Exception):
    """The agent finished without calling the MCP tool."""


@dataclass(frozen=True)
class Settings:
    mode: Mode
    scenario: str
    mcp_url: str
    content_capture: bool
    request_timeout_seconds: float
    model_id: str
    max_output_tokens: int

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        mode = env.get("DEMO_MODE", "scripted")
        scenario = env.get("DEMO_SCENARIO", "baseline")
        if mode not in ("scripted", "live"):
            raise ValueError(f"unknown DEMO_MODE {mode!r}")
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown DEMO_SCENARIO {scenario!r}")
        if mode == "live" and not env.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("DEMO_MODE=live requires ANTHROPIC_API_KEY")
        return cls(
            mode="live" if mode == "live" else "scripted",
            scenario=scenario,
            mcp_url=env.get("DEMO_MCP_URL", "http://127.0.0.1:18080/mcp"),
            content_capture=env.get("DEMO_CONTENT_CAPTURE", "false").lower() == "true",
            request_timeout_seconds=float(env.get("DEMO_REQUEST_TIMEOUT_SECONDS", "30")),
            model_id=env.get("DEMO_MODEL_ID", "claude-haiku-5-5"),
            max_output_tokens=int(env.get("DEMO_MAX_OUTPUT_TOKENS", "512")),
        )


@dataclass(frozen=True)
class Router:
    settings: Settings
    agent: Agent[None, str]
    toolset: MCPToolset[None]
    live_model: Model | None

    @property
    def connected(self) -> bool:
        return self.toolset.is_running and self.toolset.client.is_connected()

    async def run(self, request: AskRequest) -> tuple[str, AvailabilityReport]:
        args = BuildAvailabilityReportArgs(product_code=request.product_code, destination_zone=request.destination_zone)
        prompt = f"{request.message}\n\nproduct_code={args.product_code} destination_zone={args.destination_zone}"
        result = await self.agent.run(
            prompt,
            model=self.live_model or scripted_model(args),
            # Live Haiku sometimes emitted the one allowed tool call twice in a single response, which trips
            # tool_calls_limit=1 (workload_failure). Anthropic's disable_parallel_tool_use is the tool-choice control.
            model_settings=ModelSettings(max_tokens=self.settings.max_output_tokens, parallel_tool_calls=False),
            usage_limits=USAGE_LIMITS,
        )
        for message in result.all_messages():
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == TOOL:
                    return result.output, AvailabilityReport.model_validate(part.content)
        raise WorkloadFailure(f"model never called {TOOL}")


def scripted_model(args: BuildAvailabilityReportArgs) -> FunctionModel:
    """Deterministic stand-in at the model abstraction: call the tool once, then answer with fixed text."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # FunctionModel estimates token usage whenever none is returned, so the usage on this span is synthetic.
        trace.get_current_span().set_attribute("demo.model.synthetic", True)
        if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart(SCRIPTED_ANSWER)])
        return ModelResponse(parts=[ToolCallPart(TOOL, args.model_dump())])

    return FunctionModel(respond, model_name="scripted-router")


@asynccontextmanager
async def connect(settings: Settings) -> AsyncGenerator[Router]:
    """Open the long-lived MCP session (and warm its tool list) so submissions only send tools/call."""
    # The mcp dispatcher's `MCP send tools/call <tool>` CLIENT span is the logical MCP call and owns `_meta`;
    # FastMCP's native `tools/call <tool>` CLIENT span would duplicate it (and record mcp.session.id).
    fastmcp.settings.telemetry_mode = "propagation_only"
    live_model = (
        AnthropicModel(
            settings.model_id,
            provider=AnthropicProvider(
                api_key=os.environ["ANTHROPIC_API_KEY"], base_url=os.environ.get("ANTHROPIC_BASE_URL") or None
            ),
        )
        if settings.mode == "live"
        else None
    )
    # The toolset borrows this client and ignores FastMCP's timeouts, so no read timeout here:
    # DEMO_REQUEST_TIMEOUT_SECONDS around the run is the deadline, and the GET stream may idle.
    async with httpx2.AsyncClient(
        transport=TracingTransport(settings.scenario), timeout=httpx2.Timeout(5, read=None)
    ) as http_client:
        # 'error' surfaces server tool failures as fastmcp ToolError (mapped to tool_error) instead of
        # retrying through the model.
        toolset = MCPToolset(settings.mcp_url, tool_error_behavior="error", http_client=http_client)
        agent = Agent(
            name="request-router",
            instructions=INSTRUCTIONS,
            toolsets=[toolset.filtered(lambda _ctx, tool_def: tool_def.name == TOOL)],
            capabilities=[Instrumentation(settings=InstrumentationSettings(include_content=settings.content_capture))],
        )
        async with toolset:
            await toolset.list_tools()
            yield Router(settings, agent, toolset, live_model)
