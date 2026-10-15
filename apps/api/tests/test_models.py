"""Pydantic models are semantically equivalent to contracts/*.schema.json (names, required, enums, bounds)."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from api.models import (
    AskError,
    AskRequest,
    AskResponse,
    AvailabilityReport,
    BuildAvailabilityReportArgs,
    ShippingQuote,
    StockLevel,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"  # apps/api/tests -> repo root
DOCS: dict[str, dict[str, Any]] = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in CONTRACTS.glob("*.json")}


def shape(node: dict[str, Any], doc: dict[str, Any]) -> Any:
    """Normalise a schema node, inlining `$ref`s (local or `file.json#/$defs/X`) and dropping titles."""
    if "$ref" in node:
        file, _, pointer = node["$ref"].partition("#")
        target = DOCS[file] if file else doc
        return shape(target["$defs"][pointer.removeprefix("/$defs/")], target)
    if "properties" in node:
        return {
            "required": sorted(node.get("required", [])),
            "additionalProperties": node.get("additionalProperties", True),
            "properties": {name: shape(prop, doc) for name, prop in node["properties"].items()},
        }
    keys = ("type", "minLength", "maxLength", "minimum", "pattern")
    return {k: node[k] for k in keys if k in node} | ({"enum": sorted(node["enum"])} if "enum" in node else {})


@pytest.mark.parametrize(
    ("model", "file", "name"),
    [
        (AskRequest, "ask.schema.json", "AskRequest"),
        (AskResponse, "ask.schema.json", "AskResponse"),
        (AskError, "ask.schema.json", "AskError"),
        (BuildAvailabilityReportArgs, "report.schema.json", "BuildAvailabilityReportArgs"),
        (ShippingQuote, "report.schema.json", "ShippingQuote"),
        (StockLevel, "report.schema.json", "StockLevel"),
        (AvailabilityReport, "report.schema.json", "AvailabilityReport"),
    ],
)
def test_model_matches_contract(model: type[BaseModel], file: str, name: str) -> None:
    contract = DOCS[file]
    generated = model.model_json_schema()
    assert shape(generated, generated) == shape(contract["$defs"][name], contract)
