"""Deterministic shipping-quote stub (service trace-demo-shipping); same image as the API, different command."""

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from api import telemetry
from api.models import SCENARIOS, ShippingQuote

log = logging.getLogger(__name__)

_REFERENCE = "CANARY-SHIPREF-5b1e"  # privacy canary: returned to the caller, never logged or put on spans
QUOTES = {
    quote.destination_zone: quote
    for quote in (
        ShippingQuote(
            destination_zone="DEMO-NORTH",
            carrier="DEMO-CARRIER",
            price_cents=1200,
            currency="EUR",
            eta_days=2,
            reference=_REFERENCE,
        ),
        ShippingQuote(
            destination_zone="DEMO-SOUTH",
            carrier="DEMO-CARRIER",
            price_cents=900,
            currency="EUR",
            eta_days=3,
            reference=_REFERENCE,
        ),
        ShippingQuote(
            destination_zone="DEMO-EAST",
            carrier="DEMO-CARRIER",
            price_cents=1500,
            currency="EUR",
            eta_days=4,
            reference=_REFERENCE,
        ),
    )
}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    scenario = os.environ.get("DEMO_SCENARIO", "baseline")
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown DEMO_SCENARIO {scenario!r}")
    telemetry.setup("trace-demo-shipping")
    try:
        yield
    finally:
        telemetry.shutdown()


app = FastAPI(title="trace-demo-shipping", lifespan=lifespan)


@app.get("/quotes/{zone}")
async def quote(zone: str) -> ShippingQuote:
    found = QUOTES.get(zone)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown zone")
    await asyncio.sleep(int(os.environ.get("DEMO_SHIPPING_DELAY_MS", "500")) / 1000)
    if os.environ.get("DEMO_SHIPPING_FAIL", "false").lower() == "true":
        raise HTTPException(status_code=503, detail="shipping unavailable")
    log.info("quote served", extra={"destination_zone": zone})
    return found


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz", exclude_spans=["receive", "send"])
