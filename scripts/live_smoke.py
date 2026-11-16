"""Explicit read-only end-to-end MCP hardware check; never run as an automatic test."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters


async def check(device_id: str | None, dmm: bool) -> dict:
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "owon_mcp", "--export-dir", str(root / "exports")],
    )
    report = {"started_at": time.time(), "transport": "stdio", "control_calls": 0}
    async with Client(params, read_timeout_seconds=30) as client:

        async def call(name, arguments=None):
            result = await client.call_tool(name, arguments or {})
            if result.is_error:
                raise RuntimeError(f"{name}: {result.content}")
            return result.structured_content

        tools = await client.list_tools()
        report["tools"] = [tool.name for tool in tools.tools]
        report["initial_status"] = await call("owon_status")
        report["discovery"] = await call("owon_discover")
        try:
            report["connection"] = await call(
                "owon_connect", {"device_id": device_id} if device_id else {}
            )
            report["settings"] = await call("owon_get_settings")
            report["capture"] = await call(
                "owon_capture", {"max_points": 16, "include_samples": True, "include_raw": True}
            )
            report["export"] = await call("owon_export_capture")
            if dmm:
                report["dmm"] = await call("owon_dmm_read", {"count": 3, "interval_s": 0.2})
        finally:
            report["disconnection"] = await call("owon_disconnect")
    report["finished_at"] = time.time()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id")
    parser.add_argument("--dmm", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(check(args.device_id, args.dmm))
    directory = Path(__file__).resolve().parents[1] / "exports"
    directory.mkdir(exist_ok=True)
    path = directory / f"live_smoke_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with path.open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "report": str(path),
                "device": report["connection"]["device"]["info"],
                "waveforms": [
                    {"name": wave["name"], "samples": wave["sample_count"]}
                    for wave in report["capture"]["waveforms"]
                ],
                "disconnected": not report["disconnection"]["connected"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
