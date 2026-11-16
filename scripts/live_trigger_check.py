"""Explicit HDS200 trigger check through MCP; requires a quiet trigger input."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import tomllib
import uuid
from pathlib import Path

from mcp import Client, StdioServerParameters


async def check(config_path: Path, leave_armed: bool = False) -> dict:
    entry = tomllib.loads(config_path.read_text())["mcp_servers"]["owon"]
    if not entry.get("enabled", True) or "--allow-control" not in entry.get("args", []):
        raise RuntimeError("Project OWON MCP must be enabled with --allow-control")
    params = StdioServerParameters(
        command=entry["command"],
        args=entry.get("args", []),
        cwd=entry.get("cwd"),
        env=entry.get("env"),
    )
    report = {"started_at": time.time(), "tests": [], "leave_armed": leave_armed}
    baseline = None
    async with Client(params, read_timeout_seconds=30) as client:

        async def call(name: str, arguments: dict | None = None) -> dict:
            result = await client.call_tool(name, arguments or {})
            if result.is_error:
                raise RuntimeError(f"{name}: {result.content}")
            return result.structured_content

        try:
            report["connection"] = await call("owon_connect")
            baseline = await call("owon_get_settings")
            report["before"] = baseline
            for value in ("AUTO", "NORMal"):
                result = await call("owon_control", {"action": "trigger_sweep", "value": value})
                report["tests"].append(result)
                if not result["state_verified"]:
                    raise RuntimeError(f"Sweep {value} was not verified by MCP")
            for action in ("single", "stop", "single"):
                result = await call("owon_control", {"action": action})
                settings = await call("owon_get_settings")
                if action == "stop":
                    # STOP is a sent-only upstream control; let firmware apply it.
                    for _ in range(20):
                        if settings["running"].upper() == "STOP":
                            break
                        await asyncio.sleep(0.05)
                        settings = await call("owon_get_settings")
                report["tests"].append({**result, "settings": settings})
                if action == "single":
                    if not result["state_verified"] or not result["result"].get("armed"):
                        raise RuntimeError("Single is not waiting; this test needs a quiet input")
                    if settings["running"].upper() != "READY":
                        raise RuntimeError("Single did not remain armed with the quiet input")
                elif settings["running"].upper() != "STOP":
                    raise RuntimeError("Stop did not stop acquisition")
            report["passed"] = True
        except Exception as exc:
            report["error"] = str(exc)
        finally:
            try:
                if baseline is not None and not (leave_armed and report.get("passed")):
                    await call(
                        "owon_control",
                        {"action": "trigger_sweep", "value": baseline["trigger"]["sweep"]},
                    )
                    action = "stop" if baseline["running"].upper() == "STOP" else "run"
                    await call("owon_control", {"action": action})
                if baseline is not None:
                    report["after"] = await call("owon_get_settings")
            except Exception as exc:
                report["restore_error"] = str(exc)
            finally:
                report["disconnection"] = await call("owon_disconnect")
    report["finished_at"] = time.time()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leave-armed", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = asyncio.run(check(root / ".codex" / "config.toml", args.leave_armed))
    directory = root / "exports"
    directory.mkdir(exist_ok=True)
    path = directory / f"live_trigger_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
    with path.open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    after = report.get("after", {})
    print(
        json.dumps(
            {
                "report": str(path),
                "passed": report.get("passed", False),
                "trigger": after.get("trigger"),
                "running": after.get("running"),
                "disconnected": not report["disconnection"]["connected"],
                "error": report.get("error"),
                "restore_error": report.get("restore_error"),
            },
            indent=2,
        )
    )
    if not report.get("passed") or report.get("restore_error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
