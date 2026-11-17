"""Command line entry point: ``mcp-fault-proxy [options] -- <server command>``."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys

from .config import Config, ConfigError, load_config
from .injector import FaultLog, FaultProxy


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-fault-proxy",
        description="Run an MCP stdio server behind a fault-injecting proxy.",
        usage="%(prog)s [-c CONFIG] [--seed N] [--log FILE] [-q] -- <server command> [args...]",
    )
    parser.add_argument("-c", "--config", help="YAML fault configuration (no file: forward only)")
    parser.add_argument("--seed", type=int, help="override the seed in the configuration")
    parser.add_argument("--log", help="fault log file (JSON Lines); overrides the configuration")
    parser.add_argument("-q", "--quiet", action="store_true", help="do not echo faults to stderr")
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
    try:
        config = load_config(args.config) if args.config else Config()
        if args.seed is not None:
            config = dataclasses.replace(config, seed=args.seed)
        log = FaultLog(args.log or config.log, config.seed, echo=None if args.quiet else sys.stderr)
        proxy = FaultProxy(args.server_cmd, config, log)
    except (ConfigError, OSError) as e:
        print(f"mcp-fault-proxy: {e}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(proxy.run())
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
