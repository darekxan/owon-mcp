"""Explicit reversible hardware control test using this project's MCP configuration."""

from __future__ import annotations

import asyncio
import json
import time
import tomllib
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from owonscope.model import parse_si


async def check(config_path: Path) -> dict[str, Any]:
    entry = tomllib.loads(config_path.read_text())["mcp_servers"]["owon"]
    if not entry.get("enabled", True) or "--allow-control" not in entry.get("args", []):
        raise RuntimeError("Project OWON MCP must be enabled with --allow-control")
    params = StdioServerParameters(
        command=entry["command"],
        args=entry.get("args", []),
        cwd=entry.get("cwd"),
        env=entry.get("env"),
    )
    report: dict[str, Any] = {"config": str(config_path), "started_at": time.time(), "tests": []}
    baseline = None
    async with Client(params, read_timeout_seconds=30) as client:

        async def call(name: str, arguments: dict | None = None) -> dict:
            result = await client.call_tool(name, arguments or {})
            if result.is_error:
                raise RuntimeError(f"{name}: {result.content}")
            return result.structured_content

        report["status"] = await call("owon_status")
        assert report["status"]["controls_enabled"] and not report["status"]["demo"]
        try:
            report["connection"] = await call("owon_connect")
            baseline = await call("owon_get_settings")
            report["before"] = baseline
            caps = (await call("owon_capabilities"))["capabilities"]["controls"]
            original_seconds = parse_si(baseline["timebase"], "s")
            original = next(
                value for value in caps["timebase"] if parse_si(value, "s") == original_seconds
            )
            alternate = "10ms" if original_seconds != 0.01 else "5ms"
            if alternate not in caps["timebase"]:
                raise RuntimeError("Expected reversible test timebase is unavailable")

            async def verify_timebase(expected: str) -> dict:
                for _ in range(15):
                    observed = await call("owon_get_settings")
                    if parse_si(observed["timebase"], "s") == parse_si(expected, "s"):
                        return observed
                    await asyncio.sleep(0.1)
                raise RuntimeError(f"Timebase readback did not become {expected}")

            timebase_test = {"action": "timebase", "original": original, "test_value": alternate}
            report["tests"].append(timebase_test)
            try:
                timebase_test["command"] = await call(
                    "owon_control", {"action": "timebase", "value": alternate}
                )
                timebase_test["readback"] = (await verify_timebase(alternate))["timebase"]
                timebase_test["verified"] = True
            finally:
                timebase_test["restore_command"] = await call(
                    "owon_control", {"action": "timebase", "value": original}
                )
                timebase_test["restored_readback"] = (await verify_timebase(original))["timebase"]
                timebase_test["restored"] = True

            channel = "CH2" if "CH2" in baseline["channels"] else "CH1"
            original_display = baseline["channels"][channel]["display"]

            async def verify_display(expected: bool) -> dict:
                for _ in range(15):
                    observed = await call("owon_get_settings")
                    if observed["channels"][channel]["display"] is expected:
                        return observed
                    await asyncio.sleep(0.1)
                raise RuntimeError(f"{channel} display readback did not become {expected}")

            display_test = {
                "action": "channel_display",
                "channel": channel,
                "original": original_display,
                "test_value": not original_display,
            }
            report["tests"].append(display_test)
            try:
                display_test["command"] = await call(
                    "owon_control",
                    {
                        "action": "channel_display",
                        "channel": channel,
                        "value": not original_display,
                    },
                )
                after = await verify_display(not original_display)
                display_test["readback"] = after["channels"][channel]["display"]
                display_test["verified"] = True
            finally:
                display_test["restore_command"] = await call(
                    "owon_control",
                    {"action": "channel_display", "channel": channel, "value": original_display},
                )
                after = await verify_display(original_display)
                display_test["restored_readback"] = after["channels"][channel]["display"]
                display_test["restored"] = True

        except Exception as exc:
            report["error"] = str(exc)
        finally:
            try:
                if baseline is not None:
                    report["after"] = await call("owon_get_settings")
                    report["configuration_restored"] = report["after"]["channels"] == baseline[
                        "channels"
                    ] and parse_si(report["after"]["timebase"], "s") == parse_si(
                        baseline["timebase"], "s"
                    )
                    if not report["configuration_restored"]:
                        report.setdefault("error", "Final configuration differs from the baseline")
            finally:
                report["disconnection"] = await call("owon_disconnect")
    report["finished_at"] = time.time()
    return report


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    report = asyncio.run(check(root / ".codex" / "config.toml"))
    directory = root / "exports"
    directory.mkdir(exist_ok=True)
    path = directory / f"live_control_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with path.open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "report": str(path),
                "tests": report["tests"],
                "configuration_restored": report.get("configuration_restored", False),
                "disconnected": not report["disconnection"]["connected"],
                "error": report.get("error"),
            },
            indent=2,
        )
    )
    if report.get("error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
