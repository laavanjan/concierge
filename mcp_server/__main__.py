"""Run the MCP server: `python -m mcp_server --transport stdio` (Claude Desktop/Code) or `--transport http`."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Concierge MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8100")))
    args = parser.parse_args()

    from common.logging import setup_logging
    from common.telemetry import setup_telemetry

    setup_logging()
    setup_telemetry("concierge-mcp")
    from mcp_server.server import server

    if args.transport == "stdio":
        server.run("stdio")
        return

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed), allowed_hosts=allowed, allowed_origins=[]
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=security, host=args.host
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
