"""Read-only Reddit MCP server with a live (session cookie) and an archive backend."""

from __future__ import annotations

import argparse
import os

__version__ = "0.1.0"


def main() -> None:
    parser = argparse.ArgumentParser(prog="reddit-mcp", description=__doc__)
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="stdio (default) or streamable-http; env MCP_TRANSPORT",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="env HOST")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8000")), help="env PORT"
    )
    args = parser.parse_args()

    if args.transport == "streamable-http":
        from .http_app import serve

        serve(args.host, args.port)
        return

    from .server import build_server

    build_server().run(transport="stdio")
