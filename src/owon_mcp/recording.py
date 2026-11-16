"""Explicit background recordings; construction never accesses an instrument."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .backend import BackendError


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class _FrameLog:
    """Keep a valid checkpoint while streaming frames without retaining them."""

    def __init__(self, path: Path, metadata: dict) -> None:
        self.stream = path.open("x+", encoding="utf-8")
        self.stream.write(json.dumps(_json_safe(metadata), allow_nan=False)[:-1])
        self.stream.write(', "frames": [')
        self.tail = self.stream.tell()
        self.count = 0

    def checkpoint(self, summary: dict) -> None:
        self.stream.seek(self.tail)
        self.stream.write(
            '], "summary": ' + json.dumps(_json_safe(summary), allow_nan=False) + "}\n"
        )
        self.stream.truncate()
        self.stream.flush()

    def append(self, frame: dict, summary: dict) -> None:
        # Serialize before touching the checkpoint so serialization failures do
        # not damage the preceding complete JSON document.
        encoded = json.dumps(_json_safe(frame), allow_nan=False)
        self.stream.seek(self.tail)
        self.stream.write((",\n" if self.count else "\n") + encoded)
        self.tail = self.stream.tell()
        self.count += 1
        self.checkpoint(summary)

    def close(self) -> None:
        self.stream.close()


class Recorder:
    """Own one explicit recording job and serialize USB work with its session."""

    def __init__(self, backend: Any, export_dir: Path, access_lock: Any) -> None:
        self.backend = backend
        self.export_dir = Path(export_dir).resolve()
        self._access_lock = access_lock
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._state: dict[str, Any] = {
            "state": "idle",
            "running": False,
            "stop_requested": False,
            "frame_count": 0,
            "elapsed_s": 0.0,
            "output": None,
            "error": None,
        }

    @property
    def active(self) -> bool:
        with self._state_lock:
            return self._state["state"] in ("running", "stopping")

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            state = dict(self._state)
            if state["running"]:
                state["elapsed_s"] = max(0.0, time.monotonic() - self._started)
            return state

    def start(
        self, duration_s: float = 60, interval_s: float = 0.2, filename: str | None = None
    ) -> dict[str, Any]:
        for name, value in (("duration_s", duration_s), ("interval_s", interval_s)):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise BackendError(f"{name} must be a finite number.")
        if not 0 < duration_s <= 600:
            raise BackendError("duration_s must be greater than 0 and at most 600.")
        if not 0.05 <= interval_s <= 5:
            raise BackendError("interval_s must be between 0.05 and 5 seconds.")
        if duration_s / interval_s > 10000:
            raise BackendError("A recording may contain at most 10000 scheduled frames.")
        if filename is None:
            filename = f"recording_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
        if (
            not isinstance(filename, str)
            or not filename
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
            or Path(filename).name != filename
            or Path(filename).suffix.lower() != ".json"
        ):
            raise BackendError("filename must be a simple JSON basename.")

        with self._state_lock:
            if self._state["running"]:
                raise BackendError(
                    "A recording is already running; stop it before starting another."
                )
            self.export_dir.mkdir(parents=True, exist_ok=True)
            path = self.export_dir / filename
            metadata = {
                "started_at": time.time(),
                "duration_s": duration_s,
                "interval_s": interval_s,
                "acquisition_atomic": False,
                "notes": [
                    "Live AUTO acquisition; channel reads are sequential and not an atomic pair.",
                    "Host elapsed times and captured_at timestamps are not an ADC sample clock.",
                    "USB readout and scheduling introduce gaps between screen frames.",
                    "Full raw SCREEN bytes are retained; HDS bytes may be envelope pairs.",
                ],
            }
            try:
                log = _FrameLog(path, metadata)
            except OSError as exc:
                raise BackendError(f"Cannot create recording: {exc}") from exc
            self._started = time.monotonic()
            self._stop = threading.Event()
            self._state = {
                "state": "running",
                "running": True,
                "stop_requested": False,
                "frame_count": 0,
                "elapsed_s": 0.0,
                "output": str(path),
                "error": None,
                "duration_s": duration_s,
                "interval_s": interval_s,
            }
            log.checkpoint(self._state)
            self._thread = threading.Thread(
                target=self._run,
                args=(log, float(duration_s), float(interval_s)),
                name="owon-recording",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception as exc:
                self._state.update(state="failed", running=False, error=str(exc))
                log.checkpoint(self._state)
                log.close()
                raise BackendError(f"Cannot start recording: {exc}") from exc
        return self.status()

    def stop(self, timeout_s: float = 5) -> dict[str, Any]:
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s):
            raise BackendError("timeout_s must be a finite number.")
        if not 0 <= timeout_s <= 5:
            raise BackendError("timeout_s must be between 0 and 5 seconds.")
        with self._state_lock:
            if self._state["running"]:
                self._state.update(state="stopping", stop_requested=True)
                self._stop.set()
            thread = self._thread
        # Never join while holding the session access lock or state lock.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
        return self.status()

    def close(self) -> dict[str, Any]:
        return self.stop()

    def _run(self, log: _FrameLog, duration_s: float, interval_s: float) -> None:
        error = None
        cleanup_errors = []
        metadata = {}
        try:
            with self._access_lock:
                device = self.backend.status()
                metadata = {
                    "device_id": device.get("device_id"),
                    "instrument": (device.get("device") or {}).get("info", {}),
                    "demo": bool(getattr(self.backend, "demo", False)),
                }
                self.backend.control("trigger_sweep", {"value": "AUTO"})
                self.backend.control("run", {})
            deadline = self._started + duration_s
            next_frame = time.monotonic()
            max_frames = math.ceil(duration_s / interval_s)
            count = 0
            while not self._stop.is_set() and time.monotonic() < deadline:
                if count >= max_frames:
                    self._stop.wait(max(0, deadline - time.monotonic()))
                    break
                if self._stop.wait(max(0, min(next_frame, deadline) - time.monotonic())):
                    break
                if time.monotonic() >= deadline:
                    break
                with self._access_lock:
                    capture = self.backend.capture()
                    raw = self.backend.raw()
                frame = {
                    **capture,
                    **metadata,
                    "raw": raw,
                    "captured_at": time.time(),
                    "elapsed_s": time.monotonic() - self._started,
                    "acquisition_atomic": False,
                }
                count += 1
                with self._state_lock:
                    self._state["frame_count"] = count
                log.append(frame, {**self.status(), **metadata})
                next_frame = max(next_frame + interval_s, time.monotonic())
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        finally:
            for operation in ("stop", "disconnect"):
                try:
                    with self._access_lock:
                        if operation == "stop":
                            self.backend.control("stop", {})
                        else:
                            self.backend.disconnect()
                except Exception as exc:
                    cleanup_errors.append(f"{operation}: {exc}")
            with self._state_lock:
                if cleanup_errors and error is None:
                    error = "; ".join(cleanup_errors)
                terminal = dict(self._state)
                terminal.update(
                    state="failed" if error else "stopped" if self._stop.is_set() else "complete",
                    running=False,
                    elapsed_s=time.monotonic() - self._started,
                    error=error,
                )
                if cleanup_errors:
                    terminal["cleanup_errors"] = cleanup_errors
            try:
                log.checkpoint({**terminal, **metadata, "finished_at": time.time()})
            except Exception as exc:
                terminal.update(state="failed", error=f"Cannot save recording: {exc}")
            finally:
                try:
                    log.close()
                except Exception as exc:
                    terminal.update(state="failed", error=f"Cannot close recording: {exc}")
                with self._state_lock:
                    self._state = terminal
