"""MCP tools around the OWON Scope Web driver API.

Constructing this server never discovers, opens, or probes USB devices.
"""

from __future__ import annotations

import asyncio
import copy
import csv
import json
import math
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .backend import BackendError, UsbBackend
from .recording import Recorder

UPSTREAM_REVISION = "cb86012000048b8441318a293fe7a41a83f98023"
Channel = Literal["CH1", "CH2"]
ScopeAction = Literal[
    "run",
    "stop",
    "single",
    "autoset",
    "channel_display",
    "volts_div",
    "coupling",
    "probe",
    "position",
    "timebase",
    "h_position",
    "trigger_source",
    "trigger_slope",
    "trigger_level",
    "trigger_sweep",
]
Measurement = Literal["MAX", "MIN", "PKPK", "VAMP", "AVERAGE", "PERIOD", "FREQUENCY"]

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
CONTROL = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
LOCAL_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)


def json_safe(value: Any) -> Any:
    """Avoid invalid JSON when an instrument reports a non-finite scalar."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def scalar_reading(raw: str) -> dict[str, Any]:
    """Keep units and overload/error text verbatim; never invent a unit."""
    text = raw.strip()
    parts = text.split(maxsplit=1)
    value = None
    try:
        number = float(parts[0])
        if math.isfinite(number):
            value = number
    except (ValueError, IndexError):
        pass
    return {
        "raw": text,
        "value": value,
        "unit": parts[1] if value is not None and len(parts) == 2 else None,
        "status": "numeric" if value is not None else "non_numeric",
    }


def prepare_capture(capture: dict[str, Any], max_points: int, include_samples: bool) -> dict:
    """Bound MCP response size without misrepresenting decimated point spacing."""
    result = copy.deepcopy(capture)
    for waveform in result.get("waveforms", []):
        samples = waveform.pop("volts", [])
        finite = [float(value) for value in samples if math.isfinite(value)]
        waveform["sample_count"] = len(samples)
        waveform["statistics"] = {
            "min_v": min(finite) if finite else None,
            "max_v": max(finite) if finite else None,
            "mean_v": math.fsum(finite) / len(finite) if finite else None,
            "rms_v": math.hypot(*finite) / math.sqrt(len(finite)) if finite else None,
        }
        stride = max(1, math.ceil(len(samples) / max_points))
        waveform["sample_stride"] = stride
        waveform["source_dt"] = waveform.get("dt")
        if include_samples:
            waveform["volts"] = samples[::stride]
            waveform["returned_sample_count"] = len(waveform["volts"])
            waveform["dt"] = waveform["dt"] * stride
        else:
            waveform["returned_sample_count"] = 0
    result["decoder"] = {"project": "owon-scope-web", "revision": UPSTREAM_REVISION}
    result["acquisition_atomic"] = False
    return json_safe(result)


class Session:
    def __init__(self, backend: Any, allow_control: bool, export_dir: Path):
        self.backend = backend
        self.allow_control = allow_control
        self.export_dir = export_dir
        self.lock = threading.RLock()
        self.last_capture: dict | None = None
        self.recorder = Recorder(backend, export_dir, self.lock)

    def require_connected(self) -> dict:
        state = self.backend.status()
        if not state["connected"]:
            self.last_capture = None
            raise ToolError("Not connected. Call owon_discover, then owon_connect explicitly.")
        return state

    def close(self) -> None:
        # Joining the worker while holding its USB lock would deadlock cleanup.
        recording = self.recorder.close()
        if recording["running"]:
            # A bounded stop can outlast slow instrument I/O. The worker retains
            # cleanup ownership; do not block shutdown on its access lock again.
            return
        with self.lock:
            self.backend.disconnect()

    def validate_control(self, action: str, channel: str, value: Any) -> dict:
        if not self.allow_control:
            raise ToolError(
                "Controls are disabled. Start the server with --allow-control to enable them."
            )
        if channel not in ("CH1", "CH2"):
            raise ToolError("channel must be CH1 or CH2")
        no_value = {"run", "stop", "single", "autoset"}
        if action in no_value:
            if value is not None:
                raise ToolError(f"{action} does not accept a value")
        elif value is None:
            raise ToolError(f"{action} requires a value")
        elif action == "channel_display":
            if type(value) is not bool:
                raise ToolError("channel_display requires a JSON boolean")
        elif action in ("position", "h_position", "trigger_level"):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ToolError(f"{action} requires a finite number")
            bound = {"position": 200, "h_position": 2000}.get(action)
            if bound is not None and abs(value) > bound:
                raise ToolError(f"{action} must be between {-bound} and {bound}")
        elif action not in no_value:
            if not isinstance(value, str):
                raise ToolError(
                    f"{action} requires a string; use owon_capabilities for supported values"
                )
            choices = {
                "coupling": ["DC", "AC", "GND"],
                "probe": ["1X", "10X", "100X", "1000X"],
                "trigger_source": ["CH1", "CH2"],
                "trigger_slope": ["RISE", "FALL"],
                "trigger_sweep": ["AUTO", "NORMal", "SINGle"],
            }.get(action)
            if choices is not None:
                match = next((item for item in choices if item.lower() == value.lower()), None)
                if match is None:
                    raise ToolError(f"Unsupported {action}: choose from {choices}")
                value = match
        state = self.require_connected()
        caps = (state.get("device") or {}).get("capabilities") or {}
        if not caps.get("control"):
            raise ToolError("The connected driver does not support oscilloscope controls")
        if action in ("volts_div", "timebase"):
            choices = caps.get("controls", {}).get(action, [])
            match = next(
                (str(item) for item in choices if str(item).lower() == value.lower()), None
            )
            if match is None:
                raise ToolError(
                    f"Unsupported {action}; call owon_capabilities for supported values"
                )
            value = match
        return {} if action in no_value else {"channel": channel, "value": value}


def create_server(
    backend: Any | None = None,
    *,
    allow_control: bool = False,
    export_dir: Path | None = None,
) -> MCPServer:
    session = Session(
        backend if backend is not None else UsbBackend(),
        allow_control,
        export_dir if export_dir is not None else Path.cwd() / "exports",
    )

    @asynccontextmanager
    async def lifespan(_: MCPServer):
        try:
            yield session
        finally:
            await asyncio.to_thread(session.close)

    server = MCPServer(
        "owon",
        version=__version__,
        instructions=(
            "OWON Scope Web MCP. Startup/status/tool discovery do not access USB. "
            "Discover and connect only when the user is ready to give this server USB ownership; "
            "disconnect when finished so the browser or another application can use the scope. "
            "Capture and settings preserve the upstream decoder. Do not interpret screen point "
            "spacing as the instrument's acquisition sample rate. DMM responses may omit units; "
            "never infer a unit for a bare number. Controls require --allow-control."
        ),
        lifespan=lifespan,
    )

    def guarded(function):
        @wraps(function)
        def call(*args, **kwargs):
            with session.lock:
                if not session.backend.status()["connected"]:
                    session.last_capture = None
                try:
                    if (
                        session.recorder.status()["state"] in ("running", "stopping")
                        and function.__name__ not in ("owon_status", "owon_record_status")
                    ):
                        raise ToolError(
                            "Recording is active. Use owon_record_status or owon_record_stop "
                            "before other instrument operations."
                        )
                    return function(*args, **kwargs)
                except ToolError:
                    if not session.backend.status()["connected"]:
                        session.last_capture = None
                    raise
                except (
                    BackendError,
                    OSError,
                    ValueError,
                    RuntimeError,
                    NotImplementedError,
                ) as exc:
                    if not session.backend.status()["connected"]:
                        session.last_capture = None
                    raise ToolError(str(exc)) from exc

        return call

    @server.tool(annotations=READ)
    @guarded
    def owon_status() -> dict[str, Any]:
        """Return cached connection state and control policy without accessing USB."""
        return {
            **session.backend.status(),
            "controls_enabled": allow_control,
            "demo": bool(getattr(session.backend, "demo", False)),
            "cached_capture": session.last_capture is not None,
            "upstream_revision": UPSTREAM_REVISION,
            "recording": session.recorder.status(),
        }

    @server.tool(annotations=CONTROL)
    @guarded
    def owon_record_start(
        duration_s: Annotated[float, Field(gt=0, le=600)] = 60,
        interval_s: Annotated[float, Field(ge=0.05, le=5)] = 0.2,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Start a background recording of full waveforms and raw diagnostics.

        Requires an explicit connection and --allow-control. Uses current channel,
        probe and timebase settings; selects AUTO sweep and RUN, then saves repeated
        live screen snapshots to JSON in the export directory. Channel reads are
        sequential, not atomic; gaps and upstream screen timing limits still apply.
        Returns immediately. Poll owon_record_status or use owon_record_stop.
        Completion, stop and failure stop acquisition and release USB. Other scope
        operations are blocked during recording. Existing files are never replaced.
        """
        if not session.allow_control:
            raise ToolError("Recording requires --allow-control to start acquisition.")
        session.validate_control("run", "CH1", None)
        session.last_capture = None
        return session.recorder.start(duration_s, interval_s, filename)

    @server.tool(annotations=READ)
    @guarded
    def owon_record_status() -> dict[str, Any]:
        """Return cached recording progress, output path and errors; no USB access."""
        return session.recorder.status()

    @server.tool(annotations=CONTROL)
    def owon_record_stop() -> dict[str, Any]:
        """Request recording stop, save partial data and release USB.

        Waits up to five seconds for worker cleanup. If still running/stopping,
        poll owon_record_status until cleanup finishes. Do not assume USB has been
        released until the returned status confirms recording has ended.
        """
        try:
            return session.recorder.stop()
        except (BackendError, OSError, ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ)
    @guarded
    def owon_discover() -> dict[str, Any]:
        """Enumerate OWON USB candidates without claiming them or sending instrument commands."""
        return {"devices": session.backend.discover(), "probed": False}

    @server.tool(annotations=LOCAL_WRITE)
    @guarded
    def owon_connect(device_id: str | None = None) -> dict[str, Any]:
        """Explicitly claim and identify one scope. Supply an ID from discovery if several exist."""
        before = session.backend.status()
        result = session.backend.connect(device_id)
        if not before["connected"] or before["device_id"] != result["device_id"]:
            session.last_capture = None
        return result

    @server.tool(annotations=LOCAL_WRITE)
    @guarded
    def owon_disconnect() -> dict[str, Any]:
        """Release the USB interface so another application can use the scope."""
        try:
            return session.backend.disconnect()
        finally:
            session.last_capture = None

    @server.tool(annotations=READ)
    @guarded
    def owon_capabilities() -> dict[str, Any]:
        """Return cached driver capabilities and accepted control values without a device query."""
        state = session.require_connected()
        return {
            "capabilities": state["device"].get("capabilities"),
            "controls_enabled": allow_control,
        }

    @server.tool(annotations=READ)
    @guarded
    def owon_get_settings() -> dict[str, Any]:
        """Read current oscilloscope channel, timebase, trigger, and acquisition settings."""
        session.require_connected()
        return json_safe(session.backend.settings())

    @server.tool(annotations=READ)
    @guarded
    def owon_capture(
        max_points: Annotated[int, Field(ge=1, le=10000)] = 1000,
        include_samples: bool = True,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        """Capture enabled channels without stopping acquisition; return upstream-scaled traces.

        max_points bounds each returned trace using explicit stride/dt metadata. The full
        capture is cached for export. include_raw adds the upstream diagnostic payload.
        Header/channel reads are sequential and are not an atomic hardware snapshot.
        """
        state = session.require_connected()
        session.last_capture = session.backend.capture()
        session.last_capture.update(
            {
                "device_id": state["device_id"],
                "instrument": copy.deepcopy(state["device"].get("info", {})),
                "demo": bool(getattr(session.backend, "demo", False)),
                "decoder": {"project": "owon-scope-web", "revision": UPSTREAM_REVISION},
                "acquisition_atomic": False,
            }
        )
        result = prepare_capture(session.last_capture, max_points, include_samples)
        result["demo"] = bool(getattr(session.backend, "demo", False))
        if include_raw:
            result["raw"] = session.backend.raw()
        return result

    @server.tool(annotations=READ)
    @guarded
    def owon_dmm_read(
        count: Annotated[int, Field(ge=1, le=20)] = 1,
        interval_s: Annotated[float, Field(ge=0, le=5, allow_inf_nan=False)] = 0.2,
    ) -> dict[str, Any]:
        """Read :DMM:MEAS? without changing DMM mode, range, hold, or relative settings.

        Bare numeric replies have unit=null. Non-numeric/overload replies are preserved.
        The DMM may need activating on the instrument; zero alone does not establish this.
        """
        if (count - 1) * interval_s > 10:
            raise ToolError("The requested sample interval exceeds the 10-second batch limit")
        session.require_connected()
        readings = []
        for index in range(count):
            if index:
                time.sleep(interval_s)
            raw = session.backend.query(":DMM:MEAS?")
            if raw.strip().lower() == "error":
                raise ToolError("The instrument rejected :DMM:MEAS? (reply: error)")
            readings.append({"timestamp": time.time(), **scalar_reading(raw)})
        return {
            "readings": readings,
            "settings_changed": False,
            "demo": bool(getattr(session.backend, "demo", False)),
        }

    @server.tool(annotations=READ)
    @guarded
    def owon_scope_measurements(
        channel: Channel = "CH1",
        items: list[Measurement] | None = None,
    ) -> dict[str, Any]:
        """Query instrument-calculated extrema, amplitude, average, period, or frequency.

        These are device measurements, separate from statistics of the downloaded trace.
        The original response is retained when its unit/number format cannot be parsed.
        """
        session.require_connected()
        selected = list(dict.fromkeys(items if items is not None else ["MIN", "MAX", "PKPK"]))
        if not selected:
            raise ToolError("Select at least one measurement")
        return {
            "channel": channel,
            "measurements": {
                item: scalar_reading(session.backend.query(f":MEASUREMENT:{channel}:{item}?"))
                for item in selected
            },
        }

    @server.tool(annotations=CONTROL)
    @guarded
    def owon_control(
        action: ScopeAction,
        channel: Channel = "CH1",
        value: str | float | bool | None = None,
    ) -> dict[str, Any]:
        """Send one typed upstream oscilloscope control; requires --allow-control.

        run/stop/single/autoset take no value. channel_display takes a boolean.
        volts_div/timebase take a string from owon_capabilities. coupling, probe,
        trigger_source/slope/sweep take their named values. position/h_position use
        upstream native offset values; trigger_level is volts. HDS200 single sets
        the sweep and starts/re-arms acquisition, then verifies sweep/status.
        Its armed flag is true only while waiting for a trigger. HDS200 trigger_sweep
        also verifies readback; other controls report state_verified=false.
        """
        params = session.validate_control(action, channel, value)
        session.require_connected()
        result = session.backend.control(action, params)
        return {
            "action": action,
            "result": result,
            "state_verified": result.get("state_verified", False),
        }

    @server.tool(annotations=READ)
    @guarded
    def owon_get_raw() -> dict[str, Any]:
        """Return cached upstream diagnostic bytes from the last capture; no new device query."""
        session.require_connected()
        return session.backend.raw()

    @server.tool(annotations=LOCAL_WRITE)
    @guarded
    def owon_export_capture(
        format: Literal["json", "csv"] = "json",
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Export the full cached capture to the configured output directory, without USB access.

        Existing files are never overwritten. CSV columns: waveform,index,time_s,volts.
        JSON retains settings and upstream metadata. Capture once before exporting.
        """
        if session.last_capture is None:
            raise ToolError("No cached capture. Call owon_capture first.")
        name = filename or f"owon_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{format}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name):
            raise ToolError("filename must be a simple filename without directories")
        if not name.endswith("." + format):
            raise ToolError(f"filename must end in .{format}")
        session.export_dir.mkdir(parents=True, exist_ok=True)
        path = session.export_dir / name
        capture = json_safe(session.last_capture)
        with path.open("x", newline="", encoding="utf-8") as stream:
            if format == "json":
                json.dump(capture, stream, indent=2, allow_nan=False)
                stream.write("\n")
            else:
                writer = csv.writer(stream)
                writer.writerow(["waveform", "index", "time_s", "volts"])
                for waveform in capture.get("waveforms", []):
                    for index, volts in enumerate(waveform.get("volts", [])):
                        writer.writerow(
                            [
                                waveform["name"],
                                index,
                                waveform.get("t0", 0) + index * waveform["dt"],
                                volts,
                            ]
                        )
        return {"path": str(path.resolve()), "format": format, "bytes": path.stat().st_size}

    @server.resource("owon://status", mime_type="application/json")
    @guarded
    def status_resource() -> str:
        """Cached status; reading this resource never accesses the scope."""
        return json.dumps(owon_status(), allow_nan=False)

    @server.resource("owon://capture/latest", mime_type="application/json")
    @guarded
    def capture_resource() -> str:
        """Latest cached capture summary; call owon_capture to update it."""
        if session.last_capture is None:
            raise ToolError("No cached capture")
        return json.dumps(prepare_capture(session.last_capture, 1000, False), allow_nan=False)

    return server
