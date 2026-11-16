"""Start the local stdio MCP server; USB is only opened by an explicit tool call."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"owon-mcp {__version__}")
    parser.add_argument("--demo", action="store_true", help="Use simulated data; never access USB")
    parser.add_argument(
        "--allow-control",
        action="store_true",
        help="Enable oscilloscope setting/acquisition writes",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=Path.cwd() / "exports",
        help="Directory for cached JSON/CSV exports (default: ./exports)",
    )
    parser.add_argument("--verbose", action="store_true", help="Log diagnostics to stderr")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Keep --help/--version independent of the SDK and USB dependency imports.
    from .backend import DemoBackend, UsbBackend
    from .server import create_server

    backend = DemoBackend() if args.demo else UsbBackend()
    server = create_server(
        backend, allow_control=args.allow_control, export_dir=args.export_dir.resolve()
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
