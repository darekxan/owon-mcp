"""Native recording jobs use in-memory instruments and temporary files only."""

import json
import threading
import time

import pytest

from owon_mcp.backend import BackendError
from owon_mcp.recording import Recorder, _FrameLog


class FakeBackend:
    demo = False

    def __init__(self, fail_after=None):
        self.calls = []
        self.frames = 0
        self.connected = True
        self.fail_after = fail_after

    def status(self):
        self.calls.append("status")
        return {"device_id": "replay", "device": {"info": {"model": "HDS2102S"}}}

    def control(self, action, params):
        self.calls.append((action, params))
        return {"state_verified": action == "trigger_sweep"}

    def capture(self):
        self.calls.append("capture")
        if self.fail_after is not None and self.frames >= self.fail_after:
            raise RuntimeError("simulated capture error")
        self.frames += 1
        return {
            "timestamp": 123,
            "settings": {"running": "AUTo"},
            "waveforms": [
                {"name": "CH1", "volts": [0, 5, float("nan")]},
                {"name": "CH2", "volts": [5, 0, 5]},
            ],
        }

    def raw(self):
        self.calls.append("raw")
        return {"format": "json", "data": {"head": {"RUNSTATUS": "AUTo"}, "CH1": "ff"}}

    def disconnect(self):
        self.calls.append("disconnect")
        self.connected = False
        return {"connected": False}


def wait_for(recorder, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = recorder.status()
        if predicate(status):
            return status
        time.sleep(0.005)
    raise AssertionError(recorder.status())


def test_constructor_and_status_do_not_touch_instrument(tmp_path):
    backend = FakeBackend()
    recorder = Recorder(backend, tmp_path, threading.RLock())

    assert recorder.status()["state"] == "idle"
    assert not recorder.active
    recorder.close()
    assert backend.calls == []
    assert list(tmp_path.iterdir()) == []


def test_natural_completion_streams_full_frames_and_stops_and_disconnects(tmp_path):
    backend = FakeBackend()
    recorder = Recorder(backend, tmp_path, threading.RLock())
    started_at = time.time()

    recorder.start(0.18, 0.05, "natural.json")
    state = wait_for(recorder, lambda state: state["state"] == "complete")
    recorder.close()
    report = json.loads((tmp_path / "natural.json").read_text())

    assert 1 <= state["frame_count"] <= 4
    assert len(report["frames"]) == state["frame_count"]
    assert report["summary"]["state"] == "complete"
    assert report["acquisition_atomic"] is False
    for frame in report["frames"]:
        assert frame["raw"]["data"]["CH1"] == "ff"
        assert frame["waveforms"][0]["volts"] == [0, 5, None]
        assert frame["timestamp"] == 123
        assert started_at <= frame["captured_at"] <= time.time()
        assert frame["instrument"]["model"] == "HDS2102S"
    assert backend.calls[:3] == ["status", ("trigger_sweep", {"value": "AUTO"}), ("run", {})]
    assert backend.calls[-2:] == [("stop", {}), "disconnect"]
    assert backend.connected is False


def test_stop_completes_partial_file_and_releases_session_lock(tmp_path):
    backend = FakeBackend()
    access_lock = threading.RLock()
    recorder = Recorder(backend, tmp_path, access_lock)
    recorder.start(60, 0.05, "manual.json")
    wait_for(recorder, lambda state: state["frame_count"] >= 2)

    state = recorder.stop()
    report = json.loads((tmp_path / "manual.json").read_text())

    assert state["state"] == "stopped"
    assert state["stop_requested"] is True
    assert not recorder.active
    assert report["summary"]["state"] == "stopped"
    assert len(report["frames"]) >= 2
    assert access_lock.acquire(blocking=False)
    access_lock.release()


def test_failed_capture_retains_previous_frames_and_cleans_up(tmp_path):
    backend = FakeBackend(fail_after=1)
    recorder = Recorder(backend, tmp_path, threading.RLock())
    recorder.start(60, 0.05, "failed.json")
    state = wait_for(recorder, lambda state: state["state"] == "failed")
    recorder.close()
    report = json.loads((tmp_path / "failed.json").read_text())

    assert "simulated capture error" in state["error"]
    assert len(report["frames"]) == 1
    assert report["summary"]["state"] == "failed"
    assert backend.calls[-2:] == [("stop", {}), "disconnect"]


@pytest.mark.parametrize(
    "duration,interval",
    [(0, 0.2), (601, 0.2), (float("inf"), 0.2), (60, 0.01), (600, 0.05)],
)
def test_invalid_limits_do_not_start_a_job_or_touch_usb(tmp_path, duration, interval):
    backend = FakeBackend()
    recorder = Recorder(backend, tmp_path, threading.RLock())

    with pytest.raises(BackendError):
        recorder.start(duration, interval)

    assert backend.calls == []
    assert recorder.status()["state"] == "idle"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "filename", ["../outside.json", "/tmp/out.json", "nested/out.json", "out.csv"]
)
def test_filename_must_be_a_json_basename(tmp_path, filename):
    recorder = Recorder(FakeBackend(), tmp_path, threading.RLock())

    with pytest.raises(BackendError, match="basename"):
        recorder.start(1, 0.2, filename)


def test_existing_filename_is_never_overwritten(tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("original")
    backend = FakeBackend()
    recorder = Recorder(backend, tmp_path, threading.RLock())

    with pytest.raises(BackendError, match="Cannot create recording"):
        recorder.start(1, 0.2, output.name)

    assert output.read_text() == "original"
    assert backend.calls == []


def test_bounded_stop_reports_stopping_while_a_capture_is_in_flight(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingBackend(FakeBackend):
        def capture(self):
            entered.set()
            assert release.wait(2)
            return super().capture()

    recorder = Recorder(BlockingBackend(), tmp_path, threading.RLock())
    recorder.start(60, 0.2, "blocking.json")
    assert entered.wait(1)
    try:
        state = recorder.stop(timeout_s=0.01)
        assert state["state"] == "stopping"
        assert state["running"] is True
        assert recorder.active
    finally:
        release.set()
        recorder.close()
    assert recorder.status()["state"] == "stopped"


def test_terminal_status_is_published_only_after_final_file_checkpoint(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original = _FrameLog.checkpoint

    def checkpoint(log, summary):
        if summary["state"] == "complete":
            entered.set()
            assert release.wait(2)
        return original(log, summary)

    monkeypatch.setattr(_FrameLog, "checkpoint", checkpoint)
    recorder = Recorder(FakeBackend(), tmp_path, threading.RLock())
    recorder.start(0.06, 0.05, "checkpoint.json")
    assert entered.wait(1)
    try:
        assert recorder.status()["state"] == "running"
        assert recorder.active
    finally:
        release.set()
    wait_for(recorder, lambda state: state["state"] == "complete")
    report = json.loads((tmp_path / "checkpoint.json").read_text())
    assert report["summary"]["state"] == "complete"
