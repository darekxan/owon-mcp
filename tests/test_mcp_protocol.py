"""Exercise the real MCP protocol using only synthetic instrument backends.

The in-process tests forbid the real USB scan path. The subprocess smoke test
adds the same guard through sitecustomize before the server is imported.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters

from owon_mcp.backend import BackendError, DemoBackend, UsbBackend
from owon_mcp.server import create_server


@pytest.fixture(autouse=True)
def forbid_real_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    """A regression must fail before it can discover or claim real hardware."""

    def forbidden_scan(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Hardware access is forbidden in offline MCP tests")

    monkeypatch.setattr(UsbBackend, "_scan", forbidden_scan)


def payload(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict), result
    return result.structured_content


def error_text(result: Any) -> str:
    assert result.is_error, result
    assert result.structured_content is None
    return " ".join(getattr(block, "text", "") for block in result.content)


class RecordingDemo(DemoBackend):
    def __init__(self) -> None:
        super().__init__()
        self.control_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_calls: list[str] = []
        self.capture_calls = 0
        self.disconnect_calls = 0

    def capture(self) -> dict[str, Any]:
        self.capture_calls += 1
        return super().capture()

    def control(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.control_calls.append((action, dict(params)))
        return super().control(action, params)

    def query(self, command: str) -> str:
        self.query_calls.append(command)
        return super().query(command)

    def disconnect(self) -> dict[str, Any]:
        self.disconnect_calls += 1
        return super().disconnect()


@pytest.mark.asyncio
async def test_real_backend_startup_and_status_never_scan_usb(tmp_path: Path) -> None:
    """Use the actual lazy backend, but never discover/connect an instrument."""
    server = create_server(export_dir=tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        listed = await client.list_tools()
        assert listed.tools
        state = payload(await client.call_tool("owon_status", {}))
        assert state["connected"] is False
        assert state["controls_enabled"] is False
        assert state["cached_capture"] is False
        resource = await client.read_resource("owon://status")
        assert json.loads(resource.contents[0].text)["connected"] is False


@pytest.mark.asyncio
async def test_tool_schemas_and_annotations(tmp_path: Path) -> None:
    async with Client(create_server(DemoBackend(), export_dir=tmp_path)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    read_tools = {
        "owon_status",
        "owon_discover",
        "owon_capabilities",
        "owon_get_settings",
        "owon_capture",
        "owon_dmm_read",
        "owon_scope_measurements",
        "owon_get_raw",
        "owon_record_status",
    }
    write_tools = {
        "owon_connect",
        "owon_disconnect",
        "owon_control",
        "owon_export_capture",
        "owon_record_start",
        "owon_record_stop",
    }
    assert set(tools) == read_tools | write_tools
    for name, tool in tools.items():
        assert tool.output_schema is not None, name
        assert tool.annotations is not None, name
        assert tool.annotations.read_only_hint is (name in read_tools), name
        assert tool.annotations.open_world_hint is False, name
        assert tool.annotations.destructive_hint is False, name
    capture = tools["owon_capture"].input_schema["properties"]["max_points"]
    assert capture["minimum"] == 1
    assert capture["maximum"] == 10000
    assert tools["owon_control"].input_schema["properties"]["channel"]["enum"] == ["CH1", "CH2"]


@pytest.mark.asyncio
async def test_explicit_connection_and_shutdown_release(tmp_path: Path) -> None:
    backend = RecordingDemo()
    async with Client(create_server(backend, export_dir=tmp_path)) as client:
        assert "Not connected" in error_text(await client.call_tool("owon_capture", {}))
        assert backend.capture_calls == 0
        discovered = payload(await client.call_tool("owon_discover", {}))
        assert discovered["probed"] is False
        assert discovered["devices"][0]["id"] == DemoBackend.DEVICE_ID
        connected = payload(await client.call_tool("owon_connect", {}))
        assert connected["connected"] is True
        assert connected["device"]["info"]["serial"] == "DEMO"
        assert backend.status()["connected"] is True
    assert backend.status()["connected"] is False
    assert backend.disconnect_calls >= 1


@pytest.mark.asyncio
async def test_read_only_mode_cannot_reach_control_backend(tmp_path: Path) -> None:
    backend = RecordingDemo()
    async with Client(create_server(backend, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        message = error_text(await client.call_tool("owon_control", {"action": "stop"}))
        assert "disabled" in message.lower()
        assert backend.control_calls == []
        state = payload(await client.call_tool("owon_status", {}))
        assert state["demo"] is True
        assert payload(await client.call_tool("owon_get_settings", {}))["running"] == "RUN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("owon_capture", {"max_points": 0}),
        ("owon_capture", {"max_points": 10001}),
        ("owon_dmm_read", {"count": 0}),
        ("owon_dmm_read", {"count": 21}),
        ("owon_dmm_read", {"interval_s": -1}),
        ("owon_dmm_read", {"interval_s": 6}),
        ("owon_dmm_read", {"count": 4, "interval_s": 5}),
        ("owon_scope_measurements", {"channel": "CH3"}),
        ("owon_scope_measurements", {"items": ["UNKNOWN"]}),
        ("owon_scope_measurements", {"items": []}),
        ("owon_control", {"action": "factory_reset"}),
        ("owon_control", {"action": "coupling", "channel": "CH3", "value": "DC"}),
        ("owon_control", {"action": "channel_display", "value": "false"}),
        ("owon_control", {"action": "position", "value": 201}),
        ("owon_control", {"action": "stop", "value": True}),
    ],
)
async def test_invalid_requests_do_not_reach_instrument(
    tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> None:
    backend = RecordingDemo()
    async with Client(create_server(backend, allow_control=True, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        error_text(await client.call_tool(tool, arguments))
    assert backend.capture_calls == 0
    assert backend.control_calls == []
    assert backend.query_calls == []


@pytest.mark.asyncio
async def test_bounded_capture_keeps_full_cache_and_raw_data(tmp_path: Path) -> None:
    async with Client(create_server(DemoBackend(), export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        frame = payload(
            await client.call_tool("owon_capture", {"max_points": 7, "include_raw": True})
        )
        assert frame["source"] == "demo"
        assert frame["acquisition_atomic"] is False
        assert frame["decoder"]["project"] == "owon-scope-web"
        assert frame["raw"]["format"] == "json"
        assert {wave["name"] for wave in frame["waveforms"]} == {"CH1", "CH2"}
        for wave in frame["waveforms"]:
            assert wave["sample_count"] == 600
            assert 1 <= len(wave["volts"]) <= 7
            assert wave["returned_sample_count"] == len(wave["volts"])
            assert wave["sample_stride"] == 86
            assert wave["dt"] == pytest.approx(wave["source_dt"] * 86)
            assert wave["statistics"]["rms_v"] > 0
        summary = payload(await client.call_tool("owon_capture", {"include_samples": False}))
        for wave in summary["waveforms"]:
            assert "volts" not in wave
            assert wave["returned_sample_count"] == 0
            assert wave["sample_count"] == 600
        exported = payload(await client.call_tool("owon_export_capture", {"filename": "full.json"}))
        saved = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        assert all(len(wave["volts"]) == 600 for wave in saved["waveforms"])
        cached = payload(await client.call_tool("owon_get_raw", {}))
        assert cached["format"] == "json"
        resource = await client.read_resource("owon://capture/latest")
        cached_waveforms = json.loads(resource.contents[0].text)["waveforms"]
        assert all("volts" not in wave for wave in cached_waveforms)


@pytest.mark.asyncio
async def test_dmm_keeps_missing_units_and_nonnumeric_values(tmp_path: Path) -> None:
    class RepliesDemo(DemoBackend):
        def __init__(self) -> None:
            super().__init__()
            self.replies = iter(["0.0123", "0.25 V", "OL", "nan"])

        def query(self, command: str) -> str:
            assert command == ":DMM:MEAS?"
            return next(self.replies)

    async with Client(create_server(RepliesDemo(), export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        response = payload(await client.call_tool("owon_dmm_read", {"count": 4, "interval_s": 0}))
    readings = response["readings"]
    assert response["settings_changed"] is False
    assert readings[0]["value"] == pytest.approx(0.0123)
    assert readings[0]["unit"] is None
    assert readings[1]["value"] == pytest.approx(0.25)
    assert readings[1]["unit"] == "V"
    for reading, raw in zip(readings[2:], ["OL", "nan"], strict=True):
        assert reading["raw"] == raw
        assert reading["value"] is None
        assert reading["unit"] is None
        assert reading["status"] == "non_numeric"


@pytest.mark.asyncio
async def test_dmm_error_reply_is_a_failed_mcp_tool_call(tmp_path: Path) -> None:
    class RejectedDemo(DemoBackend):
        def query(self, command: str) -> str:
            assert command == ":DMM:MEAS?"
            return "error\n"

    async with Client(create_server(RejectedDemo(), export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        result = await client.call_tool("owon_dmm_read", {})
    message = error_text(result)
    assert "rejected :DMM:MEAS?" in message
    assert "reply: error" in message


@pytest.mark.asyncio
async def test_backend_failure_is_mcp_tool_error(tmp_path: Path) -> None:
    class TimeoutDemo(DemoBackend):
        def capture(self) -> dict[str, Any]:
            raise BackendError("Synthetic USB timeout")

    async with Client(create_server(TimeoutDemo(), export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        result = await client.call_tool("owon_capture", {})
        assert "Synthetic USB timeout" in error_text(result)
        assert payload(await client.call_tool("owon_status", {}))["cached_capture"] is False


@pytest.mark.asyncio
async def test_device_disappearance_and_reconnect_invalidate_capture(tmp_path: Path) -> None:
    class DisappearingDemo(DemoBackend):
        disappear_on_read = False

        def settings(self) -> dict[str, Any]:
            if self.disappear_on_read:
                self.disappear_on_read = False
                super().disconnect()
                raise BackendError("Synthetic instrument disconnected during settings read")
            return super().settings()

    backend = DisappearingDemo()
    async with Client(create_server(backend, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        payload(await client.call_tool("owon_capture", {}))
        assert payload(await client.call_tool("owon_status", {}))["cached_capture"] is True
        backend.disappear_on_read = True
        message = error_text(await client.call_tool("owon_get_settings", {}))
        assert "disconnected" in message
        state = payload(await client.call_tool("owon_status", {}))
        assert state["connected"] is False
        assert state["cached_capture"] is False
        assert "No cached capture" in error_text(await client.call_tool("owon_export_capture", {}))
        payload(await client.call_tool("owon_connect", {}))
        state = payload(await client.call_tool("owon_status", {}))
        assert state["connected"] is True
        assert state["cached_capture"] is False
        assert "No cached capture" in error_text(await client.call_tool("owon_export_capture", {}))
        payload(await client.call_tool("owon_capture", {}))
        assert payload(await client.call_tool("owon_status", {}))["cached_capture"] is True


@pytest.mark.asyncio
async def test_disconnect_failure_still_invalidates_cached_capture(tmp_path: Path) -> None:
    class DisconnectFailureDemo(DemoBackend):
        fail_next_disconnect = False

        def disconnect(self) -> dict[str, Any]:
            state = super().disconnect()
            if self.fail_next_disconnect:
                self.fail_next_disconnect = False
                raise BackendError("Synthetic USB release error")
            return state

    backend = DisconnectFailureDemo()
    async with Client(create_server(backend, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        payload(await client.call_tool("owon_capture", {}))
        backend.fail_next_disconnect = True
        assert "USB release error" in error_text(await client.call_tool("owon_disconnect", {}))
        state = payload(await client.call_tool("owon_status", {}))
        assert state["connected"] is False
        assert state["cached_capture"] is False
        assert "No cached capture" in error_text(await client.call_tool("owon_export_capture", {}))


@pytest.mark.asyncio
async def test_scope_measurements_use_selected_channel_and_deduplicate(tmp_path: Path) -> None:
    class MeasurementsDemo(RecordingDemo):
        def query(self, command: str) -> str:
            self.query_calls.append(command)
            return {
                ":MEASUREMENT:CH2:MIN?": "-1.0 V",
                ":MEASUREMENT:CH2:MAX?": "1.0 V",
                ":MEASUREMENT:CH2:FREQUENCY?": "500 Hz",
            }[command]

    backend = MeasurementsDemo()
    async with Client(create_server(backend, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        result = payload(
            await client.call_tool(
                "owon_scope_measurements",
                {"channel": "CH2", "items": ["MIN", "MAX", "MIN", "FREQUENCY"]},
            )
        )
    assert result["channel"] == "CH2"
    assert list(result["measurements"]) == ["MIN", "MAX", "FREQUENCY"]
    assert result["measurements"]["MIN"]["value"] == -1.0
    assert result["measurements"]["MAX"]["unit"] == "V"
    assert result["measurements"]["FREQUENCY"]["value"] == 500
    assert backend.query_calls == [
        ":MEASUREMENT:CH2:MIN?",
        ":MEASUREMENT:CH2:MAX?",
        ":MEASUREMENT:CH2:FREQUENCY?",
    ]


@pytest.mark.asyncio
async def test_enabled_control_is_typed_and_not_reported_as_verified(tmp_path: Path) -> None:
    backend = RecordingDemo()
    async with Client(create_server(backend, allow_control=True, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        result = payload(await client.call_tool("owon_control", {"action": "stop"}))
        assert result["state_verified"] is False
        assert backend.control_calls == [("stop", {})]
        assert payload(await client.call_tool("owon_get_settings", {}))["running"] == "STOP"
        payload(await client.call_tool("owon_control", {"action": "coupling", "value": "ac"}))
        assert backend.control_calls[-1] == ("coupling", {"channel": "CH1", "value": "AC"})


@pytest.mark.asyncio
async def test_export_is_contained_exclusive_and_uses_full_samples(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    async with Client(create_server(DemoBackend(), export_dir=exports)) as client:
        assert "No cached capture" in error_text(await client.call_tool("owon_export_capture", {}))
        payload(await client.call_tool("owon_connect", {}))
        payload(await client.call_tool("owon_capture", {"max_points": 2}))
        for filename in ("../escape.json", "/tmp/escape.json", "nested/capture.json", "wrong.csv"):
            error_text(await client.call_tool("owon_export_capture", {"filename": filename}))
        assert not (tmp_path / "escape.json").exists()
        saved = payload(await client.call_tool("owon_export_capture", {"filename": "capture.json"}))
        destination = Path(saved["path"])
        assert destination.parent == exports
        original = destination.read_bytes()
        error_text(await client.call_tool("owon_export_capture", {"filename": "capture.json"}))
        assert destination.read_bytes() == original
        exported = payload(
            await client.call_tool(
                "owon_export_capture", {"format": "csv", "filename": "capture.csv"}
            )
        )
        with Path(exported["path"]).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.reader(stream))
        assert rows[0] == ["waveform", "index", "time_s", "volts"]
        assert len(rows) == 1201
        assert {row[0] for row in rows[1:]} == {"CH1", "CH2"}
        payload(await client.call_tool("owon_disconnect", {}))
        assert payload(await client.call_tool("owon_status", {}))["cached_capture"] is False
        error_text(await client.call_tool("owon_export_capture", {}))


@pytest.mark.asyncio
async def test_concurrent_tool_calls_serialize_backend_access(tmp_path: Path) -> None:
    class SlowDemo(DemoBackend):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.peak = 0
            self.observation_lock = threading.Lock()

        def capture(self) -> dict[str, Any]:
            # Observe entry before DemoBackend takes its own lock. This tests
            # the MCP session's serialization rather than the synthetic lock.
            with self.observation_lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(0.03)
                return super().capture()
            finally:
                with self.observation_lock:
                    self.active -= 1

    backend = SlowDemo()
    async with Client(create_server(backend, export_dir=tmp_path)) as client:
        payload(await client.call_tool("owon_connect", {}))
        async with asyncio.timeout(10):
            results = await asyncio.gather(
                *(client.call_tool("owon_capture", {"max_points": 1}) for _ in range(6))
            )
        assert all(not result.is_error for result in results)
    assert backend.peak == 1
    assert backend.active == 0


@pytest.mark.asyncio
async def test_stdio_demo_wire_contract_without_usb(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    hooks = tmp_path / "python-hooks"
    hooks.mkdir()
    forbidden_marker = tmp_path / "hardware-access-attempted"
    (hooks / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "from owon_mcp.backend import UsbBackend\n"
        "def forbid_scan(*args, **kwargs):\n"
        f"    Path({str(forbidden_marker)!r}).write_text('forbidden', encoding='utf-8')\n"
        "    raise AssertionError('Real USB access is forbidden in stdio smoke tests')\n"
        "UsbBackend._scan = forbid_scan\n",
        encoding="utf-8",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "owon_mcp", "--demo", "--export-dir", str(tmp_path / "exports")],
        env={"PYTHONPATH": os.pathsep.join((str(hooks), str(project / "src")))},
    )
    async with asyncio.timeout(20):
        async with Client(parameters) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "owon_capture" in names
            status = payload(await client.call_tool("owon_status", {}))
            assert status["connected"] is False
            assert status["demo"] is True
            discovered = payload(await client.call_tool("owon_discover", {}))
            assert discovered["devices"][0]["id"] == DemoBackend.DEVICE_ID
            payload(await client.call_tool("owon_connect", {}))
            frame = payload(await client.call_tool("owon_capture", {"max_points": 3}))
            assert len(frame["waveforms"]) == 2
            assert all(len(wave["volts"]) <= 3 for wave in frame["waveforms"])
            reading = payload(await client.call_tool("owon_dmm_read", {}))["readings"][0]
            assert reading["value"] == pytest.approx(0.0123)
            assert reading["unit"] is None
            payload(await client.call_tool("owon_disconnect", {}))
    assert not forbidden_marker.exists()
