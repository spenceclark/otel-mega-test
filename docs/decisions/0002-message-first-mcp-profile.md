# 0002 — Message-first MCP graph profile, native carriers only

**Status:** accepted (15 Oct 2026).

## Context
Both MCP SDKs implement SEP-414 natively: `mcp` 2.3.0 injects W3C context into `params._meta`; the C# SDK 2.2.0
extracts it, parents the server operation to it, and links the ASP.NET request activity. The spec forbids a
custom MCP bridge unless a probe shows the built-in path fails.

## Decision
Accept the **message-first** profile: the .NET `tools/call` SERVER span's parent is the Python `MCP send …`
CLIENT span; the HTTP server span is related by a link. No `_meta` adapter is enabled. FastMCP's duplicate client
span is turned off (`telemetry_mode="propagation_only"`); propagation is unaffected because the mcp dispatcher
injects last.

## Consequences
`meta-removed` keeps continuity through the HTTP fallback but changes message parentage (a surviving carrier, not a
failed experiment). `headers-removed` shows the opposite. Assertions check graph relationships, not visual nesting.
