"""Interface adapters over the textflowkit core.

Adapters are deliberately thin: they translate a transport into a call on
`textflowkit.core.runner` and translate the result back. No pipeline logic lives
here, so the MCP server, the HTTP API, and any future frontend cannot drift.

- `mcp_server` - Model Context Protocol (stdio or Streamable HTTP)
- `http_server` - JSON HTTP API for software products and web frontends
"""

__all__ = ["mcp_server", "http_server"]
