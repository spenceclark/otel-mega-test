"""Contract models; must stay equivalent to contracts/*.schema.json (see tests/test_models.py)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProductCode = Literal["DEMO-LANTERN", "DEMO-KETTLE", "DEMO-TENT"]
DestinationZone = Literal["DEMO-NORTH", "DEMO-SOUTH", "DEMO-EAST"]
Mode = Literal["scripted", "live"]
ErrorCode = Literal["tool_error", "timeout", "workload_failure", "internal_error"]

# Every service validates DEMO_SCENARIO against the full set and only acts on its own scenarios.
SCENARIOS = frozenset(
    {"baseline", "headers-removed", "meta-removed", "both-removed", "context-detached", "serial-io", "logical-error"}
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AskRequest(_Strict):
    message: str = Field(min_length=1, max_length=2000)
    product_code: ProductCode
    destination_zone: DestinationZone


class BuildAvailabilityReportArgs(_Strict):
    product_code: ProductCode
    destination_zone: DestinationZone


class ShippingQuote(_Strict):
    destination_zone: DestinationZone
    carrier: str
    price_cents: int = Field(ge=0)
    currency: str
    eta_days: int = Field(ge=0)
    reference: str


class StockLevel(_Strict):
    product_code: ProductCode
    quantity_on_hand: int = Field(ge=0)
    warehouse: str


class AvailabilityReport(_Strict):
    product_code: ProductCode
    destination_zone: DestinationZone
    available: bool
    quantity_on_hand: int = Field(ge=0)
    warehouse: str
    shipping: ShippingQuote
    summary: str


class AskResponse(_Strict):
    run_id: str
    answer: str
    evidence: AvailabilityReport
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    scenario: str
    mode: Mode


class AskError(_Strict):
    run_id: str
    trace_id: str
    error: ErrorCode
    detail: str
