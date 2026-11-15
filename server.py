"""Run only when invoked by the configured local MCP client."""

from msp_bonds_mcp.core.server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
