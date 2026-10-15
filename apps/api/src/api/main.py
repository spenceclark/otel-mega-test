"""FastAPI entry point (service trace-demo-api): POST /api/ask runs the request-router agent."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastmcp.exceptions import ToolError
from mcp import MCPError
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

from api import telemetry
from api.agent import Router, Settings, WorkloadFailure, connect
from api.models import AskError, AskRequest, AskResponse, ErrorCode

log = logging.getLogger(__name__)

_ERRORS: dict[ErrorCode, tuple[int, str]] = {
    "tool_error": (502, "the MCP tool call failed"),
    "timeout": (504, "the request deadline expired"),
    "workload_failure": (502, "the agent did not complete the tool workflow"),
    "internal_error": (500, "internal error"),
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    telemetry.setup("trace-demo-api")
    try:
        async with connect(Settings.from_env()) as router:
            app.state.router = router
            yield
    finally:
        telemetry.shutdown()


app = FastAPI(title="trace-demo-api", lifespan=lifespan)


def _router(request: Request) -> Router:
    return request.app.state.router


@app.post("/api/ask", response_model=AskResponse, responses={s: {"model": AskError} for s, _ in _ERRORS.values()})
async def ask(body: AskRequest, request: Request) -> AskResponse | JSONResponse:
    router = _router(request)
    settings = router.settings
    run_id = uuid.uuid4().hex
    span = trace.get_current_span()  # the FastAPI SERVER span
    trace_id = format(span.get_span_context().trace_id, "032x")
    span.set_attributes({"demo.run_id": run_id, "demo.scenario": settings.scenario, "demo.mode": settings.mode})
    log.info("ask received", extra={"demo.run_id": run_id, "demo.scenario": settings.scenario})
    started = time.perf_counter()

    def completed(outcome: str, **extra: str) -> None:
        span.set_attribute("demo.outcome", outcome)
        duration_ms = round((time.perf_counter() - started) * 1000)
        log.info(
            "ask completed",
            extra={
                "demo.run_id": run_id,
                "demo.scenario": settings.scenario,
                "demo.outcome": outcome,
                "demo.duration_ms": duration_ms,
                **extra,
            },
        )

    try:
        async with asyncio.timeout(settings.request_timeout_seconds):
            answer, evidence = await router.run(body)
    except Exception as exc:
        error = _classify(exc)
        # Class name only: exception text can carry tool or model content.
        completed("tool_error" if error == "tool_error" else "error", **{"error.type": type(exc).__qualname__})
        status, detail = _ERRORS[error]
        payload = AskError(run_id=run_id, trace_id=trace_id, error=error, detail=detail)
        return JSONResponse(payload.model_dump(), status_code=status)

    completed("ok")
    return AskResponse(
        run_id=run_id,
        answer=answer,
        evidence=evidence,
        trace_id=trace_id,
        scenario=settings.scenario,
        mode=settings.mode,
    )


def _classify(exc: Exception) -> ErrorCode:
    def matches(*types: type[Exception]) -> bool:
        # FastMCP's session task group can wrap tool/protocol errors in an ExceptionGroup.
        if isinstance(exc, ExceptionGroup):
            return cast(ExceptionGroup[Exception], exc).subgroup(types) is not None
        return isinstance(exc, types)

    if matches(TimeoutError):
        return "timeout"
    if matches(ToolError, MCPError):
        return "tool_error"
    if matches(WorkloadFailure, UsageLimitExceeded, UnexpectedModelBehavior):
        return "workload_failure"
    return "internal_error"


@app.get("/api/healthz")
async def healthz(request: Request) -> Response:
    return Response(status_code=200 if _router(request).connected else 503)


@app.get("/api/config")
async def config(request: Request) -> dict[str, str]:
    settings = _router(request).settings
    return {"scenario": settings.scenario, "mode": settings.mode}


# Configured at import, before serving; its tracer resolves to the provider telemetry.setup() installs at startup.
FastAPIInstrumentor.instrument_app(app, excluded_urls="api/healthz,api/config", exclude_spans=["receive", "send"])
