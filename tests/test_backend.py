"""Backend ownership and lifecycle checks using in-memory drivers only."""

from __future__ import annotations

import builtins
import errno
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from owon_mcp.backend import BackendError, DemoBackend, UsbBackend


def candidate(device_id="usb-1-1"):
    return SimpleNamespace(
        location=SimpleNamespace(id=device_id), vid=0x5345, pid=0x1234, usbtmc=False
    )


class FakeDriver:
    protocol = "owon-scpi"

    def __init__(self):
        self.info = {"model": "fake"}
        self.close_count = 0
        self.close_error = None
        self.settings_error = None
        self.last_raw = json.dumps({"head": {"MODEL": "fake"}, "CH1": "0001"}).encode()

    def close(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error

    def get_settings(self):
        if self.settings_error is not None:
            raise self.settings_error
        return {"channels": {"CH1": {"display": True}}}

    def capture(self):
        return SimpleNamespace(to_json=lambda: {"waveforms": [], "timestamp": 123.0})

    def query(self, command):
        return "fake-response"

    def capabilities(self):
        return {"control": True}


class FakeDevice:
    def __init__(self, driver, *, device_id="usb-1-1", status="ready", error=""):
        self.driver = driver
        self.id = device_id
        self.status = status
        self.error = error
        self.metadata_error = None

    def to_json(self):
        if self.metadata_error is not None:
            raise self.metadata_error
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "info": {"model": "fake"},
            "capabilities": {"control": True},
            "usb": {"vid": "5345", "pid": "1234"},
        }


def connected_backend(driver=None):
    driver = driver or FakeDriver()
    backend = UsbBackend(scanner=lambda: [candidate()], detector=lambda _: FakeDevice(driver))
    backend.connect()
    return backend, driver


def test_constructor_and_cached_status_never_import_or_scan_usb(monkeypatch):
    original_import = builtins.__import__

    def forbid_usb_import(name, *args, **kwargs):
        if name == "usb" or name.startswith(("usb.", "owonscope")):
            raise AssertionError("A cached operation attempted to load the USB backend")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_usb_import)
    scanner = Mock(side_effect=AssertionError("USB discovery was attempted"))
    detector = Mock(side_effect=AssertionError("USB probing was attempted"))
    default = UsbBackend()
    injected = UsbBackend(scanner=scanner, detector=detector)

    for backend in (default, injected):
        assert backend.status() == {"connected": False, "device_id": None, "device": None}
        backend.disconnect()
    scanner.assert_not_called()
    detector.assert_not_called()


def test_discovery_does_not_probe_or_claim_candidates():
    detector = Mock()
    backend = UsbBackend(scanner=lambda: [candidate()], detector=detector)

    devices = backend.discover()

    assert devices == [{"id": "usb-1-1", "vid": "5345", "pid": "1234", "usbtmc": False}]
    assert backend.status()["connected"] is False
    detector.assert_not_called()


def test_ambiguous_selection_does_not_claim_either_instrument():
    detector = Mock()
    backend = UsbBackend(
        scanner=lambda: [candidate("usb-1-1"), candidate("usb-1-2")], detector=detector
    )

    with pytest.raises(BackendError, match="Specify a device_id"):
        backend.connect()

    detector.assert_not_called()
    assert backend.status()["connected"] is False


def test_explicit_selection_and_repeated_connect_preserve_ownership():
    driver = FakeDriver()
    scanner = Mock(return_value=[candidate("usb-1-1"), candidate("usb-1-2")])
    detector = Mock(side_effect=lambda selected: FakeDevice(driver, device_id=selected.location.id))
    backend = UsbBackend(scanner=scanner, detector=detector)

    state = backend.connect("usb-1-2")
    assert state["device_id"] == "usb-1-2"
    state["device"]["info"]["model"] = "caller mutation"
    assert backend.status()["device"]["info"]["model"] == "fake"
    backend.connect("usb-1-2")
    backend.connect()
    with pytest.raises(BackendError, match="Disconnect before"):
        backend.connect("usb-1-1")

    scanner.assert_called_once()
    detector.assert_called_once()
    assert driver.close_count == 0
    backend.disconnect()


@pytest.mark.parametrize("failure", ["probe", "metadata"])
def test_failed_connection_releases_a_returned_driver(failure):
    driver = FakeDriver()
    device = FakeDevice(driver)
    if failure == "probe":
        device.status = "error"
        device.error = "USB is busy"
    else:
        device.metadata_error = ValueError("Bad identity metadata")
    backend = UsbBackend(scanner=lambda: [candidate()], detector=lambda _: device)

    with pytest.raises(BackendError):
        backend.connect()

    assert driver.close_count == 1
    assert backend.status() == {"connected": False, "device_id": None, "device": None}


def fake_upstream_probes(monkeypatch, *, scpi_result=None, legacy_result=None):
    """Replace link allocation and both probes before invoking the default detector."""
    from owonscope import legacy, scpi, usbio

    links = []

    class FakeLink:
        def __init__(self, location):
            self.location = location
            self.close_count = 0
            links.append(self)

        def close(self):
            self.close_count += 1

    def probe(result):
        if isinstance(result, BaseException):
            return Mock(side_effect=result)
        return Mock(return_value=result)

    scpi_probe = probe(scpi_result)
    legacy_probe = probe(legacy_result)
    monkeypatch.setattr(usbio, "BulkLink", FakeLink)
    monkeypatch.setattr(scpi, "probe", scpi_probe)
    monkeypatch.setattr(legacy, "probe", legacy_probe)
    return links, scpi_probe, legacy_probe


@pytest.mark.parametrize("failure", ["unsupported", "scpi_error", "legacy_error"])
def test_default_detector_releases_owned_link_when_no_driver_is_returned(monkeypatch, failure):
    scpi_error = OSError("simulated SCPI probe failure") if failure == "scpi_error" else None
    legacy_error = OSError("simulated legacy probe failure") if failure == "legacy_error" else None
    links, scpi_probe, legacy_probe = fake_upstream_probes(
        monkeypatch, scpi_result=scpi_error, legacy_result=legacy_error
    )
    backend = UsbBackend(scanner=lambda: [candidate()])

    with pytest.raises(BackendError):
        backend.connect()

    assert len(links) == 1
    assert links[0].close_count == 1
    scpi_probe.assert_called_once_with(links[0])
    if failure == "scpi_error":
        legacy_probe.assert_not_called()
    else:
        legacy_probe.assert_called_once_with(links[0])
    assert backend.status()["connected"] is False


def test_usbtmc_probe_failure_closes_link_without_legacy_fallback(monkeypatch):
    links, _, legacy_probe = fake_upstream_probes(monkeypatch)
    selected = candidate()
    selected.usbtmc = True
    backend = UsbBackend(scanner=lambda: [selected])

    with pytest.raises(BackendError):
        backend.connect()

    legacy_probe.assert_not_called()
    assert links[0].close_count == 1


def test_automatic_upstream_usb_reset_is_blocked_before_touching_device(monkeypatch):
    links, _, legacy_probe = fake_upstream_probes(monkeypatch)
    legacy_probe.side_effect = lambda link: link.open(reset=True)
    backend = UsbBackend(scanner=lambda: [candidate()])

    with pytest.raises(BackendError, match="USB reset recovery is disabled"):
        backend.connect()

    assert links[0].reset_blocked is True
    assert links[0].close_count == 1
    assert backend.status()["connected"] is False


def test_successful_default_probe_hands_link_to_driver_until_disconnect(monkeypatch):
    class LinkedDriver(FakeDriver):
        def __init__(self, link):
            super().__init__()
            self.link = link

        def close(self):
            super().close()
            self.link.close()

    links, scpi_probe, legacy_probe = fake_upstream_probes(monkeypatch)
    scpi_probe.side_effect = LinkedDriver
    backend = UsbBackend(scanner=lambda: [candidate()])

    assert backend.connect()["connected"] is True
    assert links[0].close_count == 0
    legacy_probe.assert_not_called()
    backend.disconnect()
    assert links[0].close_count == 1


def test_disconnect_clears_cached_state_even_when_driver_close_fails():
    backend, driver = connected_backend()
    driver.close_error = OSError("USB release failed")

    with pytest.raises(BackendError, match="USB release failed"):
        backend.disconnect()

    assert backend.status()["connected"] is False
    assert backend.status()["device"] is None
    backend.disconnect()
    assert driver.close_count == 1


def test_capture_and_disconnect_cannot_interleave_device_ownership():
    began = threading.Event()
    release_capture = threading.Event()
    disconnect_attempted = threading.Event()
    closed = threading.Event()
    events = []

    class BlockingDriver(FakeDriver):
        def capture(self):
            events.append("capture-start")
            began.set()
            assert release_capture.wait(2), "Test did not release its fake capture"
            events.append("capture-end")
            return super().capture()

        def close(self):
            events.append("close")
            closed.set()
            super().close()

    backend, _ = connected_backend(BlockingDriver())

    def disconnect():
        disconnect_attempted.set()
        return backend.disconnect()

    with ThreadPoolExecutor(max_workers=2) as executor:
        capture_future = executor.submit(backend.capture)
        try:
            assert began.wait(1)
            disconnect_future = executor.submit(disconnect)
            assert disconnect_attempted.wait(1)
            assert not closed.wait(0.05)
        finally:
            release_capture.set()
        assert capture_future.result(timeout=1)["source"] == "owon-scope-web"
        assert disconnect_future.result(timeout=1)["connected"] is False

    assert events == ["capture-start", "capture-end", "close"]


def test_disappearing_instrument_releases_driver_and_requires_reconnection():
    backend, driver = connected_backend()
    driver.settings_error = OSError(errno.ENODEV, "Device disappeared")

    with pytest.raises(BackendError, match="disconnected"):
        backend.settings()

    assert driver.close_count == 1
    assert backend.status()["connected"] is False
    with pytest.raises(BackendError, match="Call connect first"):
        backend.capture()


def test_busy_connection_error_explains_exclusive_usb_ownership():
    detector = Mock(side_effect=OSError(errno.EBUSY, "Resource busy"))
    backend = UsbBackend(scanner=lambda: [candidate()], detector=detector)

    with pytest.raises(BackendError, match="Disconnect the instrument in OWON Scope Web"):
        backend.connect()

    assert backend.status()["connected"] is False


def test_raw_diagnostics_are_cached_and_do_not_query_instrument():
    backend, driver = connected_backend()
    driver.query = Mock(side_effect=AssertionError("Unexpected USB query"))
    driver.capture = Mock(side_effect=AssertionError("Unexpected capture"))

    raw = backend.raw()
    assert raw["data"]["head"]["MODEL"] == "fake"
    raw["data"]["head"]["MODEL"] = "caller mutation"
    assert backend.raw()["data"]["head"]["MODEL"] == "fake"
    backend.disconnect()


def test_settings_preserve_model_specific_header_fields():
    backend, driver = connected_backend()
    driver.head = {"Trig": {"Mode": "SINGle", "Items": {"Level": "2.64V"}}}
    result = backend.settings()
    assert result["raw_header"]["Trig"]["Items"]["Level"] == "2.64V"
    result["raw_header"]["Trig"]["Items"]["Level"] = "caller mutation"
    assert driver.head["Trig"]["Items"]["Level"] == "2.64V"
    backend.disconnect()


def test_demo_scales_and_channel_control_work_without_upstream_imports(monkeypatch):
    original_import = builtins.__import__

    def forbid_usb_import(name, *args, **kwargs):
        if name == "usb" or name.startswith(("usb.", "owonscope")):
            raise AssertionError("Offline demo attempted to load a USB dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_usb_import)
    demo = DemoBackend()
    assert demo.demo is True
    state = demo.connect()
    choices = state["device"]["capabilities"]["controls"]
    assert "100mV" in choices["volts_div"]
    assert "2us" in choices["timebase"]
    demo.control("volts_div", {"channel": "CH1", "value": "100mV"})
    demo.control("timebase", {"value": "2us"})
    demo.control("channel_display", {"channel": "CH2", "value": False})

    capture = demo.capture()
    assert capture["time_per_div"] == pytest.approx(2e-6)
    assert [waveform["name"] for waveform in capture["waveforms"]] == ["CH1"]
    assert capture["waveforms"][0]["volts_per_div"] == pytest.approx(0.1)
    assert demo.query(":DMM:MEAS?") == "0.0123"
    demo.disconnect()


def test_demo_measurements_match_synthetic_waveforms_without_usb_imports(monkeypatch):
    from owon_mcp.server import scalar_reading

    original_import = builtins.__import__

    def forbid_usb_import(name, *args, **kwargs):
        if name == "usb" or name.startswith(("usb.", "owonscope")):
            raise AssertionError("Offline measurements attempted to load a USB dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_usb_import)
    demo = DemoBackend()
    with pytest.raises(BackendError, match="Call connect first"):
        demo.query(":MEASUREMENT:CH1:MAX?")
    demo.connect()
    for waveform in demo.capture()["waveforms"]:
        channel = waveform["name"]
        samples = waveform["volts"]
        expected = {
            "MIN": min(samples),
            "MAX": max(samples),
            "PKPK": max(samples) - min(samples),
            "VAMP": max(samples) - min(samples),
            "AVERAGE": sum(samples) / len(samples),
            "FREQUENCY": waveform["frequency"],
            "PERIOD": 1 / waveform["frequency"],
        }
        for item, value in expected.items():
            reading = scalar_reading(demo.query(f":MEASUREMENT:{channel}:{item}?"))
            assert reading["value"] == pytest.approx(value, abs=1e-9)
            assert reading["unit"] == {"FREQUENCY": "Hz", "PERIOD": "s"}.get(item, "V")

    demo.control("coupling", {"channel": "CH2", "value": "GND"})
    assert scalar_reading(demo.query(":MEASUREMENT:CH2:MAX?"))["value"] == 0
    for item in ("PERIOD", "FREQUENCY"):
        reading = scalar_reading(demo.query(f":MEASUREMENT:CH2:{item}?"))
        assert reading["value"] is None
        assert reading["status"] == "non_numeric"
    demo.disconnect()
