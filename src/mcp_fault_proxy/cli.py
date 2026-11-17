"""Command line entry point: ``mcp-fault-proxy [options] -- <server command>``."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .proxy import Relay


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-fault-proxy",
        description="Run an MCP stdio server behind a fault-injecting proxy.",
        usage="%(prog)s [options] -- <server command> [args...]",
    )
    if "--" in argv:
        split = argv.index("--")
        own, server_cmd = argv[:split], argv[split + 1 :]
    else:
        own, server_cmd = argv, []
    args = parser.parse_args(own)
    if not server_cmd:
        parser.error("missing server command after '--'")
    args.server_cmd = server_cmd
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(Relay(args.server_cmd).run())


if __name__ == "__main__":
    sys.exit(main())
