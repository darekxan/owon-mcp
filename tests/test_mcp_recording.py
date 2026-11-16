"""Exercise background recording through the real MCP protocol without USB."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from owon_mcp.backend import BackendError, DemoBackend, UsbBackend
from owon_mcp.server import create_server


@pytest.fixture(autouse=True)
def forbid_real_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_scan(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Hardware access is forbidden in recording tests")

    monkeypatch.setattr(UsbBackend, "_scan", forbidden_scan)


def payload(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict), result
    return result.structured_content


def error_text(result: Any) -> str:
    assert result.is_error, result
    return " ".join(getattr(block, "text", "") for block in result.content)


class InstrumentDemo(DemoBackend):
    def __init__(self, fail_after: int | None = None) -> None:
        super().__init__()
        self.control_calls: list[tuple[str, dict[str, Any]]] = []
        self.capture_calls = 0
        self.fail_after = fail_after

    def control(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.control_calls.append((action, dict(params)))
        return super().control(action, params)

    def capture(self) -> dict[str, Any]:
        self.capture_calls += 1
        if self.fail_after is not None and self.capture_calls > self.fail_after:
            raise BackendError("synthetic capture failure")
        return super().capture()


async def wait_for_recording(client: Client, predicate, timeout_s: float = 3) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        status = payload(await client.call_tool("owon_record_status", {}))
        if predicate(status):
            return status
        assert time.monotonic() < deadline, status
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_recording_status_is_cached_before_any_connection(tmp_path: Path) -> None:
    async with Client(create_server(export_dir=tmp_path)) as client:
        status = payload(await client.call_tool("owon_record_status", {}))
        assert status["state"] == "idle"
        assert status["running"] is False
        assert status["frame_count"] == 0
        assert payload(await client.call_tool("owon_status", {}))["connected"] is False
        assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_control,connected", [(True, False), (False, True)])
async def test_recording_start_requires_connection_and_control_authorization(
    tmp_path: Path, allow_control: bool, connected: bool
) -> None:
    backend = InstrumentDemo()
    server = create_server(backend, allow_control=allow_control, export_dir=tmp_path)
    async with Client(server) as client:
        if connected:
            payload(await client.call_tool("owon_connect", {}))
        error_text(await client.call_tool("owon_record_start", {"duration_s": 0.3}))
        assert backend.control_calls == []
        assert backend.capture_calls == 0
        assert backend.status()["connected"] is connected
        assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_recording_completes_in_background_and_exports_full_frames(tmp_path: Path) -> None:
    backend = InstrumentDemo()
    async with Client(create_server(backend, allow_control=True, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        before = time.time()
        started = payload(
            await client.call_tool(
                "owon_record_start",
                {"duration_s": 0.3, "interval_s": 0.05, "filename": "automatic.json"},
            )
        )
        assert started["state"] in ("running", "complete")
        status = await wait_for_recording(client, lambda state: not state["running"])
        after = time.time()
        assert status["state"] == "complete", status
        assert status["frame_count"] >= 2
        assert payload(await client.call_tool("owon_status", {}))["connected"] is False
        assert backend.control_calls[:2] == [
            ("trigger_sweep", {"value": "AUTO"}),
            ("run", {}),
        ]
        assert backend.control_calls[-1] == ("stop", {})

    output = Path(status["output"])
    assert output == tmp_path / "automatic.json"
    saved = json.loads(output.read_text())
    frames = saved["frames"]
    assert len(frames) == status["frame_count"]
    assert saved["summary"]["state"] == "complete"
    assert saved["summary"]["frame_count"] == len(frames)
    elapsed = [frame["elapsed_s"] for frame in frames]
    assert elapsed == sorted(elapsed)
    for frame in frames:
        assert before <= frame["captured_at"] <= after
        assert frame["acquisition_atomic"] is False
        assert frame["device_id"] == DemoBackend.DEVICE_ID
        assert frame["instrument"]["serial"] == "DEMO"
        assert frame["demo"] is True
        assert frame["raw"]["format"] == "json"
        assert {wave["name"] for wave in frame["waveforms"]} == {"CH1", "CH2"}
        assert all(len(wave["volts"]) == 600 for wave in frame["waveforms"])
        assert all(len(frame["raw"]["data"][name]) == 1200 for name in ("CH1", "CH2"))
    # Capture time from the fake instrument remains distinct from host recording time.
    assert frames[0]["timestamp"] == 1700000000.0
    assert before <= saved["started_at"] <= after


@pytest.mark.asyncio
async def test_manual_stop_blocks_conflicting_tools_and_releases_usb(tmp_path: Path) -> None:
    backend = InstrumentDemo()
    async with Client(create_server(backend, allow_control=True, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        began = time.monotonic()
        started = payload(
            await client.call_tool("owon_record_start", {"filename": "manual.json"})
        )
        assert time.monotonic() - began < 1
        assert started["running"] is True
        await wait_for_recording(client, lambda state: state["frame_count"] >= 2)
        for tool, arguments in (
            ("owon_capture", {}),
            ("owon_control", {"action": "stop"}),
            ("owon_disconnect", {}),
            ("owon_record_start", {}),
        ):
            message = error_text(await client.call_tool(tool, arguments))
            assert "owon_record_stop" in message
        assert backend.status()["connected"] is True
        assert [action for action, _ in backend.control_calls] == ["trigger_sweep", "run"]
        stopped = payload(await client.call_tool("owon_record_stop", {}))
        if stopped["running"]:
            stopped = await wait_for_recording(client, lambda state: not state["running"])
        assert stopped["state"] == "stopped"
        assert stopped["stop_requested"] is True
        assert stopped["frame_count"] >= 2
        assert stopped["elapsed_s"] < 60
        assert backend.status()["connected"] is False
        assert backend.control_calls[-1] == ("stop", {})
        saved = json.loads(Path(stopped["output"]).read_text())
        assert len(saved["frames"]) == stopped["frame_count"]
        assert saved["summary"]["state"] == "stopped"


@pytest.mark.asyncio
async def test_capture_failure_preserves_partial_recording_and_releases_usb(tmp_path: Path) -> None:
    backend = InstrumentDemo(fail_after=2)
    async with Client(create_server(backend, allow_control=True, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        payload(
            await client.call_tool(
                "owon_record_start",
                {"duration_s": 5, "interval_s": 0.05, "filename": "failed.json"},
            )
        )
        status = await wait_for_recording(client, lambda state: not state["running"])
        assert status["state"] == "failed"
        assert "synthetic capture failure" in status["error"]
        assert status["frame_count"] == 2
        assert backend.status()["connected"] is False
        assert backend.control_calls[-1] == ("stop", {})
        saved = json.loads(Path(status["output"]).read_text())
        assert len(saved["frames"]) == 2
        assert saved["summary"]["state"] == "failed"
        assert "synthetic capture failure" in saved["summary"]["error"]


@pytest.mark.asyncio
async def test_client_shutdown_stops_an_active_recording_and_preserves_output(
    tmp_path: Path,
) -> None:
    backend = InstrumentDemo()
    async with Client(create_server(backend, allow_control=True, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        payload(
            await client.call_tool("owon_record_start", {"filename": "shutdown.json"})
        )
        await wait_for_recording(client, lambda state: state["frame_count"] >= 1)

    assert backend.status()["connected"] is False
    assert backend.control_calls[-1] == ("stop", {})
    saved = json.loads((tmp_path / "shutdown.json").read_text())
    assert saved["summary"]["state"] == "stopped"
    assert len(saved["frames"]) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"duration_s": 0},
        {"duration_s": 601},
        {"interval_s": 0.01},
        {"interval_s": 6},
        {"filename": "../escaped.json"},
        {"filename": "/escaped.json"},
        {"filename": "nested/escaped.json"},
        {"filename": r"..\escaped.json"},
        {"filename": "wrong.csv"},
    ],
)
async def test_invalid_recording_requests_do_not_change_instrument(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    backend = InstrumentDemo()
    async with Client(create_server(backend, allow_control=True, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        error_text(await client.call_tool("owon_record_start", arguments))
        assert backend.control_calls == []
        assert backend.capture_calls == 0
        assert backend.status()["connected"] is True
        assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_recording_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("original content")
    backend = InstrumentDemo()
    async with Client(create_server(backend, allow_control=True, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        error_text(await client.call_tool("owon_record_start", {"filename": output.name}))
        assert output.read_text() == "original content"
        assert backend.control_calls == []
        assert backend.capture_calls == 0
        assert backend.status()["connected"] is True
