"""Negative carrier tests against the live worker, run inside the api container (it has mcp + httpx2):

    docker compose exec -T api python - < tests/trace-contracts/carrier_probe.py

Sends real MCP tool calls with hand-made carriers and prints the ids it used as JSON. No OTel SDK is configured
in this process, so the mcp SDK's own `_meta` injection is a no-op and our test carriers reach the server as-is.
`scripts/demo.py probe-carriers` runs this and asserts the resulting spans.
"""

import asyncio
import json
import secrets

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

URL = "http://worker:8080/mcp"
ARGS = {"product_code": "DEMO-LANTERN", "destination_zone": "DEMO-NORTH"}


def tp(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


async def call(header: str | None, meta: dict[str, str]) -> bool:
    headers = {"traceparent": header} if header else {}
    async with httpx2.AsyncClient(headers=headers, timeout=30) as http, Client(streamable_http_client(URL, http_client=http)) as client:
        result = await client.call_tool("build_availability_report", ARGS, meta=meta)
    return not result.is_error


async def main() -> None:
    cases: list[dict[str, object]] = []
    a, b, h, m = secrets.token_hex(16), secrets.token_hex(16), secrets.token_hex(8), secrets.token_hex(8)
    cases.append({"case": "conflict", "header_trace": a, "header_span": h, "meta_trace": b, "meta_span": m, "header": tp(a, h), "meta": tp(b, m)})
    for name, bad in [
        ("meta-garbage", "not-a-traceparent"),
        ("meta-truncated", "00-" + secrets.token_hex(5)),
        ("meta-all-zero", tp("0" * 32, "0" * 16)),
        ("meta-bad-version", "ff-" + secrets.token_hex(16) + "-" + secrets.token_hex(8) + "-01"),
    ]:
        c, hc = secrets.token_hex(16), secrets.token_hex(8)
        cases.append({"case": name, "header_trace": c, "header_span": hc, "header": tp(c, hc), "meta": bad})
    d, md = secrets.token_hex(16), secrets.token_hex(8)
    cases.append({"case": "header-all-zero", "meta_trace": d, "meta_span": md, "header": tp("0" * 32, "0" * 16), "meta": tp(d, md)})
    for case in cases:
        case["ok"] = await call(str(case["header"]), {"traceparent": str(case["meta"])})
    print(json.dumps(cases))


asyncio.run(main())
