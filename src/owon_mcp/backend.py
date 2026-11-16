"""Lazy adapters for OWON Scope Web and an entirely offline demo instrument.

Importing this module or constructing either backend never enumerates USB.
OWON Scope Web is loaded only when USB discovery or connection is requested.
The upstream implementation and its capture representation remain authoritative.
"""

from __future__ import annotations

import base64
import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any

# These option lists mirror the pinned upstream SCPI driver's capabilities.
# Keeping them local lets the offline demo run without importing USB libraries.
_VOLTS_DIV = [
    "1mV",
    "2mV",
    "5mV",
    "10mV",
    "20mV",
    "50mV",
    "100mV",
    "200mV",
    "500mV",
    "1V",
    "2V",
    "5V",
    "10V",
    "20V",
    "50V",
    "100V",
]
_TIMEBASES = [
    "1ns",
    "2ns",
    "5ns",
    "10ns",
    "20ns",
    "50ns",
    "100ns",
    "200ns",
    "500ns",
    "1us",
    "2us",
    "5us",
    "10us",
    "20us",
    "50us",
    "100us",
    "200us",
    "500us",
    "1ms",
    "2ms",
    "5ms",
    "10ms",
    "20ms",
    "50ms",
    "100ms",
    "200ms",
    "500ms",
    "1s",
    "2s",
    "5s",
    "10s",
    "20s",
    "50s",
    "100s",
]


class BackendError(RuntimeError):
    """An instrument/backend failure suitable for reporting through MCP."""


def _query_text(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise BackendError("A non-empty SCPI query is required.")
    command = command.strip()
    if any(character in command for character in "\r\n;\x00"):
        raise BackendError("Send exactly one SCPI query, without command separators.")
    if not command.endswith("?"):
        raise BackendError("A SCPI query must end with '?'.")
    try:
        command.encode("ascii")
    except UnicodeEncodeError as exc:
        raise BackendError("SCPI commands must contain ASCII characters only.") from exc
    return command


def _is_hds200(driver: Any) -> bool:
    model = str(getattr(driver, "info", {}).get("model", ""))
    return getattr(driver, "protocol", None) == "owon-scpi" and bool(
        re.fullmatch(r"HDS2\d{1,3}S?", model, re.IGNORECASE)
    )


def _apply_control(driver: Any, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Use the HDS200 command names; keep upstream behavior for other models."""
    channel_actions = {"channel_display", "volts_div", "coupling", "probe", "position"}
    hds_actions = channel_actions | {"trigger_source", "trigger_slope", "trigger_sweep", "single"}
    if not _is_hds200(driver) or action not in hds_actions:
        return driver.apply(action, params)

    channel = str(params.get("channel", "CH1")).upper()
    if channel not in ("CH1", "CH2"):
        raise BackendError("HDS channel must be CH1 or CH2.")
    value = params.get("value")

    def choice(options: list[str]) -> str:
        match = next((item for item in options if item.lower() == str(value).lower()), None)
        if match is None:
            raise BackendError(f"Unsupported HDS {action} value: {value!r}")
        return match

    if action == "single":
        # V4.7.1 registers SWEep directly below TRIGger, despite the manual's
        # extra SINGle node. RUN also re-arms a previously stopped acquisition.
        commands = [":TRIGger:SWEep SINGle", ":RUNning RUN"]
        for command in commands:
            driver.send(command)
        return {"sent": commands, "profile": "owon-hds200"}
    if action == "channel_display":
        if type(value) is not bool:
            raise BackendError("channel_display requires a boolean.")
        command = f":{channel}:DISPlay {'ON' if value else 'OFF'}"
    elif action == "volts_div":
        command = f":{channel}:SCALe {choice(_VOLTS_DIV).lower()}"
    elif action == "coupling":
        command = f":{channel}:COUPling {choice(['DC', 'AC', 'GND'])}"
    elif action == "probe":
        command = f":{channel}:PROBe {choice(['1X', '10X', '100X', '1000X'])}"
    elif action == "position":
        if type(value) not in (int, float) or not math.isfinite(value):
            raise BackendError("HDS position requires a finite native offset value.")
        command = f":{channel}:OFFSet {value:g}"
    elif action == "trigger_source":
        command = f":TRIGger:SINGle:SOURce {choice(['CH1', 'CH2'])}"
    elif action == "trigger_sweep":
        command = f":TRIGger:SWEep {choice(['AUTO', 'NORMal', 'SINGle'])}"
    else:
        command = f":TRIGger:SINGle:EDGe {choice(['RISE', 'FALL'])}"
    driver.send(command)
    return {"sent": command, "profile": "owon-hds200"}


def _detect_owned(candidate: Any) -> Any:
    """Use upstream probes while retaining cleanup ownership until success.

    Upstream ``detect`` can return an unsupported device after a probe leaves
    its link open. Keeping the link here guarantees that every unsuccessful
    detection releases it, including an invalid IDN or a raised probe error.
    """
    from owonscope import legacy, scpi
    from owonscope.manager import Device
    from owonscope.usbio import BulkLink

    class NonResettingLink(BulkLink):
        reset_blocked = False

        def open(self, reset: bool = False):
            if reset:
                self.reset_blocked = True
                raise BackendError(
                    "USB reset recovery is disabled. Reconnect the instrument manually if needed."
                )
            return super().open(reset=False)

    usb_info = {
        "vid": f"{candidate.vid:04x}",
        "pid": f"{candidate.pid:04x}",
        "location": candidate.location.id,
        "usbtmc": candidate.usbtmc,
    }
    link = NonResettingLink(candidate.location)
    handed_off = False
    try:
        driver = scpi.probe(link)
        if driver is None and not candidate.usbtmc:
            driver = legacy.probe(link)
        if driver is None:
            return Device(
                candidate.location.id,
                "unsupported",
                usb=usb_info,
                error=(
                    "The instrument did not answer. Automatic USB reset recovery was disabled."
                    if link.reset_blocked
                    else "The instrument did not answer a supported OWON protocol."
                ),
            )
        device = Device(candidate.location.id, "ready", driver=driver, usb=usb_info)
        handed_off = True
        return device
    except Exception as exc:
        status = "no-permission" if getattr(exc, "errno", None) == 13 else "error"
        return Device(
            candidate.location.id,
            status,
            usb=usb_info,
            error=str(exc) or type(exc).__name__,
        )
    finally:
        if not handed_off:
            link.close()


def _settings_with_header(driver: Any, settings: dict[str, Any]) -> dict[str, Any]:
    """Retain the read header and expose the HDS200's nested trigger fields."""
    settings = deepcopy(settings)
    head = getattr(driver, "head", None)
    if not isinstance(head, dict) or not head:
        return settings
    settings["raw_header"] = deepcopy(head)
    if not _is_hds200(driver):
        return settings

    def lower_keys(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {key.lower(): item for key, item in value.items() if isinstance(key, str)}

    header = lower_keys(head)
    trigger = lower_keys(header.get("trig", header.get("trigger")))
    if not trigger:
        return settings
    items = lower_keys(trigger.get("items"))
    normalized = settings.get("trigger")
    normalized = dict(normalized) if isinstance(normalized, dict) else {}
    for target, source in (
        ("mode", "mode"),
        ("type", "type"),
        ("source", "channel"),
        ("level", "level"),
        ("slope", "edge"),
        ("coupling", "coupling"),
        ("sweep", "sweep"),
    ):
        fields = items if source in items else trigger
        if source in fields:
            normalized[target] = deepcopy(fields[source])
    settings["trigger"] = normalized
    return settings


class UsbBackend:
    """One explicitly connected OWON Scope Web driver, with serialized access.

    ``scanner`` and ``detector`` are optional offline-test seams with the same
    signatures as ``owonscope.usbio.scan`` and ``owonscope.manager.detect``.
    No background scans or implicit connections are performed here.
    """

    demo = False

    def __init__(
        self,
        scanner: Callable[[], Iterable[Any]] | None = None,
        detector: Callable[[Any], Any] | None = None,
    ) -> None:
        self._scanner = scanner
        self._detector = detector
        self._lock = threading.RLock()
        self._device: Any | None = None
        self._device_json: dict[str, Any] | None = None

    def _scan(self) -> list[Any]:
        try:
            if self._scanner is None:
                from owonscope.usbio import scan

                scanner = scan
            else:
                scanner = self._scanner
            return list(scanner())
        except Exception as exc:
            raise self._error("discover instruments", exc) from exc

    @staticmethod
    def _error(operation: str, exc: Exception) -> BackendError:
        if isinstance(exc, BackendError):
            return exc
        detail = str(exc) or type(exc).__name__
        lower = detail.lower()
        if isinstance(exc, (ImportError, ModuleNotFoundError)):
            return BackendError(
                f"Cannot {operation}: {detail}. Install this project's dependencies first."
            )
        if type(exc).__name__ == "NoBackendError" or "no backend available" in lower:
            return BackendError(
                f"Cannot {operation}: libusb is unavailable. On macOS install it with "
                "'brew install libusb'; on Linux install libusb-1.0."
            )
        if getattr(exc, "errno", None) in (13, 16) or any(
            text in lower
            for text in ("access denied", "resource busy", "permission denied", "claim interface")
        ):
            return BackendError(
                f"Cannot {operation}: {detail}. Disconnect the instrument in OWON Scope Web "
                "and close other USB clients before connecting here. Check the OS USB permissions."
            )
        return BackendError(f"Cannot {operation}: {detail}")

    @staticmethod
    def _candidate_json(candidate: Any) -> dict[str, Any]:
        return {
            "id": candidate.location.id,
            "vid": f"{candidate.vid:04x}",
            "pid": f"{candidate.pid:04x}",
            "usbtmc": bool(candidate.usbtmc),
        }

    def discover(self) -> list[dict[str, Any]]:
        """Enumerate candidates on explicit request; do not probe or claim them."""
        with self._lock:
            try:
                return [self._candidate_json(candidate) for candidate in self._scan()]
            except Exception as exc:
                raise self._error("discover instruments", exc) from exc

    def connect(self, device_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self._device is not None:
                current_id = self._device_json["id"] if self._device_json else None
                if device_id is None or device_id == current_id:
                    return self.status()
                raise BackendError(
                    f"Already connected to {current_id!r}. "
                    "Disconnect before selecting another instrument."
                )
            candidates = self._scan()
            if device_id is not None:
                candidates = [
                    candidate for candidate in candidates if candidate.location.id == device_id
                ]
            if not candidates:
                selected = f" {device_id!r}" if device_id is not None else ""
                raise BackendError(
                    f"No OWON USB instrument{selected} was found. Check the USB cable and select "
                    "the instrument's HID/control mode rather than MSC storage mode."
                )
            if len(candidates) != 1:
                identifiers = ", ".join(candidate.location.id for candidate in candidates)
                raise BackendError(
                    f"Several OWON instruments were found ({identifiers}). Specify a device_id."
                )

            device = None
            try:
                if self._detector is None:
                    detector = _detect_owned
                else:
                    detector = self._detector
                device = detector(candidates[0])
                if (
                    getattr(device, "status", None) != "ready"
                    or getattr(device, "driver", None) is None
                ):
                    detail = (
                        getattr(device, "error", "")
                        or "The instrument did not answer a supported protocol."
                    )
                    raise BackendError(
                        f"Cannot connect to {candidates[0].location.id!r}: {detail} "
                        "If it is in use, disconnect its other USB client first."
                    )
                metadata = deepcopy(device.to_json())
                if _is_hds200(device.driver):
                    metadata["info"]["control_profile"] = "owon-hds200"
                self._device = device
                self._device_json = metadata
                return self.status()
            except Exception as exc:
                # A detector or metadata failure must not leave a claimed driver.
                if device is not None and getattr(device, "driver", None) is not None:
                    try:
                        device.driver.close()
                    except Exception:
                        pass
                self._device = None
                self._device_json = None
                raise self._error("connect to the instrument", exc) from exc

    def status(self) -> dict[str, Any]:
        """Return cached connection metadata, without touching USB."""
        with self._lock:
            return {
                "connected": self._device is not None,
                "device_id": self._device_json.get("id") if self._device_json else None,
                "device": deepcopy(self._device_json),
            }

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            previous_id = self._device_json.get("id") if self._device_json else None
            device = self._device
            self._device = None
            self._device_json = None
            try:
                if device is not None:
                    device.driver.close()
            except Exception as exc:
                raise self._error("release the instrument's USB connection", exc) from exc
            return {**self.status(), "previous_device_id": previous_id}

    def _driver(self) -> Any:
        if self._device is None or getattr(self._device, "driver", None) is None:
            raise BackendError("No instrument is connected. Call connect first.")
        return self._device.driver

    def _call(self, operation: str, callback: Callable[[Any], Any]) -> Any:
        with self._lock:
            driver = self._driver()
            try:
                return callback(driver)
            except Exception as exc:
                gone = type(exc).__name__ == "DeviceGone" or getattr(exc, "errno", None) in (5, 19)
                if gone:
                    try:
                        self.disconnect()
                    except BackendError:
                        pass
                    raise BackendError(
                        f"The instrument disconnected during {operation}. "
                        "Reconnect it and call connect again."
                    ) from exc
                raise self._error(operation, exc) from exc

    def settings(self) -> dict[str, Any]:
        def perform(driver: Any) -> dict[str, Any]:
            return _settings_with_header(driver, driver.get_settings())

        return self._call("read settings", perform)

    def capture(self) -> dict[str, Any]:
        def perform(driver: Any) -> dict[str, Any]:
            result = deepcopy(driver.capture().to_json())
            if _is_hds200(driver):
                result["settings"] = _settings_with_header(driver, result.get("settings", {}))
            result["source"] = "owon-scope-web"
            return result

        return self._call("capture waveforms", perform)

    def raw(self) -> dict[str, Any]:
        """Read the last capture's cached diagnostics; issue no USB query."""

        def perform(driver: Any) -> dict[str, Any]:
            data = driver.last_raw
            if data is None:
                raise BackendError("No raw capture is cached. Capture a waveform first.")
            try:
                decoded = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                if isinstance(data, str):
                    data = data.encode("utf-8")
                return {
                    "source": "owon-scope-web",
                    "format": "binary",
                    "encoding": "base64",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            return {"source": "owon-scope-web", "format": "json", "data": decoded}

        return self._call("read cached raw capture", perform)

    def control(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise BackendError("Control parameters must be an object.")

        def perform(driver: Any) -> dict[str, Any]:
            result = deepcopy(_apply_control(driver, action, dict(params)))
            if _is_hds200(driver) and action in ("single", "trigger_sweep"):
                expected = "SINGle" if action == "single" else str(params["value"])
                for attempt in range(10):
                    settings = _settings_with_header(driver, driver.get_settings())
                    observed = str(settings.get("trigger", {}).get("sweep", ""))
                    running = str(settings.get("running", ""))
                    if observed.lower() == expected.lower() and (
                        action != "single" or running.upper() == "READY"
                    ):
                        result["state_verified"] = True
                        result["readback"] = {"sweep": observed, "running": running}
                        if action == "single":
                            result["armed"] = running.upper() == "READY"
                        return result
                    if attempt < 9:
                        time.sleep(0.05)
                if (
                    action == "single"
                    and observed.lower() == "single"
                    and running.upper() == "STOP"
                ):
                    # STOP may be a completed trigger or an unapplied RUN. Neither
                    # proves that this request armed a fresh acquisition.
                    result["state_verified"] = False
                    result["readback"] = {"sweep": observed, "running": running}
                    result["armed"] = False
                    return result
                raise BackendError(
                    f"HDS {action} readback failed: expected sweep {expected}, "
                    f"observed {observed or 'unknown'} / {running or 'unknown'}."
                )
            return result

        return self._call("apply instrument control", perform)

    def query(self, command: str) -> str:
        command = _query_text(command)

        def perform(driver: Any) -> str:
            if getattr(driver, "protocol", None) != "owon-scpi" or not callable(
                getattr(driver, "query", None)
            ):
                raise BackendError("SCPI queries are unavailable for this instrument's protocol.")
            return driver.query(command)

        return self._call("query the instrument", perform)


def _si_value(text: str, unit: str) -> float:
    match = re.fullmatch(
        r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([pnumkKMGµ]?)\s*"
        + re.escape(unit)
        + r"\s*",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise BackendError(f"Expected a value with {unit} units, for example '1{unit}'.")
    prefixes = {
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "µ": 1e-6,
        "m": 1e-3,
        "": 1.0,
        "k": 1e3,
        "K": 1e3,
        "M": 1e6,
        "G": 1e9,
    }
    prefix = match.group(2)
    if prefix == "M" and unit.lower() == "s":
        prefix = "m"
    factor = prefixes.get(prefix, prefixes.get(prefix.lower()))
    if factor is None:
        raise BackendError("Unknown SI prefix.")
    value = float(match.group(1)) * factor
    if not math.isfinite(value) or value <= 0:
        raise BackendError("The scale must be a finite positive value.")
    return value


class DemoBackend:
    """Deterministic two-channel instrument with no USB or upstream imports."""

    DEVICE_ID = "demo-hds2102s"
    demo = True

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._connected = False
        self._frame = 0
        self._raw: dict[str, Any] | None = None
        self._settings = self._defaults()

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "channels": {
                name: {
                    "display": True,
                    "volts_div": "1V",
                    "coupling": "DC",
                    "probe": "10X",
                    "position": 0.0,
                }
                for name in ("CH1", "CH2")
            },
            "timebase": "1ms",
            "h_position": 0.0,
            "running": "RUN",
            "trigger": {"source": "CH1", "slope": "RISE", "sweep": "AUTO", "level": 0.0},
        }

    @classmethod
    def _metadata(cls) -> dict[str, Any]:
        return {
            "id": cls.DEVICE_ID,
            "status": "ready",
            "error": "",
            "usb": {"location": "simulated", "vid": "5345", "pid": "1234", "usbtmc": False},
            "info": {
                "vendor": "OWON",
                "model": "HDS2102S",
                "serial": "DEMO",
                "firmware": "simulated",
                "idn": "OWON,HDS2102S,DEMO,simulated",
                "protocol": "owon-scpi",
                "protocol_name": "Offline demo",
            },
            "capabilities": {
                "control": True,
                "screenshot": False,
                "controls": {
                    "channels": ["CH1", "CH2"],
                    "volts_div": list(_VOLTS_DIV),
                    "timebase": list(_TIMEBASES),
                    "coupling": ["DC", "AC", "GND"],
                    "probe": ["1X", "10X", "100X", "1000X"],
                    "trigger_source": ["CH1", "CH2"],
                    "trigger_slope": ["RISE", "FALL"],
                    "trigger_sweep": ["AUTO", "NORMal", "SINGle"],
                },
                "note": "Synthetic instrument. No USB access is performed.",
            },
        }

    def discover(self) -> list[dict[str, Any]]:
        return [{"id": self.DEVICE_ID, "vid": "5345", "pid": "1234", "usbtmc": False}]

    def connect(self, device_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if device_id not in (None, self.DEVICE_ID):
                raise BackendError(f"No demo instrument {device_id!r} exists.")
            self._connected = True
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "device_id": self.DEVICE_ID if self._connected else None,
                "device": self._metadata() if self._connected else None,
            }

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            previous = self.DEVICE_ID if self._connected else None
            self._connected = False
            self._raw = None
            return {**self.status(), "previous_device_id": previous}

    def _require_connection(self) -> None:
        if not self._connected:
            raise BackendError("No instrument is connected. Call connect first.")

    def settings(self) -> dict[str, Any]:
        with self._lock:
            self._require_connection()
            return deepcopy(self._settings)

    def capture(self) -> dict[str, Any]:
        with self._lock:
            self._require_connection()
            sample_count = 600
            time_per_div = _si_value(self._settings["timebase"], "s")
            dt = time_per_div * 10 / sample_count
            waveforms = []
            raw = {
                "head": {
                    "TIMEBASE": {"SCALE": self._settings["timebase"]},
                    "SAMPLE": {"DATALEN": sample_count},
                    "CHANNEL": [],
                }
            }
            for index, (name, settings) in enumerate(self._settings["channels"].items()):
                vdiv = _si_value(settings["volts_div"], "V")
                raw["head"]["CHANNEL"].append(
                    {
                        "NAME": name,
                        "DISPLAY": "ON" if settings["display"] else "OFF",
                        "SCALE": settings["volts_div"],
                        "PROBE": settings["probe"],
                        "COUPLING": settings["coupling"],
                        "OFFSET": settings["position"],
                    }
                )
                if not settings["display"]:
                    continue
                frequency = 1000.0 / (index + 1)
                amplitude = 1.0 / (index + 1)
                phase = index * math.pi / 2
                volts = [
                    0.0
                    if settings["coupling"] == "GND"
                    else round(amplitude * math.sin(2 * math.pi * frequency * n * dt + phase), 6)
                    for n in range(sample_count)
                ]
                waveforms.append(
                    {
                        "name": name,
                        "dt": dt,
                        "t0": 0.0,
                        "volts": volts,
                        "volts_per_div": vdiv,
                        "position_div": settings["position"] / 25,
                        "probe": float(settings["probe"][:-1]),
                        "coupling": settings["coupling"],
                        "frequency": frequency,
                        "extra": {"simulated": True},
                    }
                )
                raw[name] = bytes(
                    max(-128, min(127, round(value / vdiv * 25 + settings["position"]))) & 255
                    for value in volts
                ).hex()
            self._raw = raw
            timestamp = 1700000000.0 + self._frame / 10
            if self._settings["running"] != "STOP":
                self._frame += 1
            return {
                "timestamp": timestamp,
                "time_per_div": time_per_div,
                "h_divisions": 10,
                "v_divisions": 8,
                "settings": deepcopy(self._settings),
                "waveforms": waveforms,
                "source": "demo",
            }

    def raw(self) -> dict[str, Any]:
        with self._lock:
            self._require_connection()
            if self._raw is None:
                raise BackendError("No raw capture is cached. Capture a waveform first.")
            return {"source": "demo", "format": "json", "data": deepcopy(self._raw)}

    def control(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_connection()
            if not isinstance(params, dict):
                raise BackendError("Control parameters must be an object.")
            value = params.get("value")
            if action in ("run", "stop", "single"):
                self._settings["running"] = {"run": "RUN", "stop": "STOP", "single": "SINGle"}[
                    action
                ]
                if action == "single":
                    self._settings["trigger"]["sweep"] = "SINGLE"
            elif action == "autoset":
                self._settings = self._defaults()
            elif action in ("channel_display", "volts_div", "coupling", "probe", "position"):
                channel = str(params.get("channel", "CH1")).upper()
                if channel not in self._settings["channels"]:
                    raise BackendError("Channel must be CH1 or CH2.")
                settings = self._settings["channels"][channel]
                if action == "channel_display":
                    if not isinstance(value, bool):
                        raise BackendError("Channel display must be a boolean.")
                    settings["display"] = value
                elif action == "volts_div":
                    _si_value(str(value), "V")
                    settings["volts_div"] = str(value)
                elif action == "position":
                    settings["position"] = self._finite_number(value)
                elif action == "coupling":
                    if str(value).upper() not in ("AC", "DC", "GND"):
                        raise BackendError("Coupling must be AC, DC, or GND.")
                    settings["coupling"] = str(value).upper()
                else:
                    if str(value).upper() not in ("1X", "10X", "100X", "1000X"):
                        raise BackendError("Unsupported probe attenuation.")
                    settings["probe"] = str(value).upper()
            elif action == "timebase":
                _si_value(str(value), "s")
                self._settings["timebase"] = str(value)
            elif action == "h_position":
                self._settings["h_position"] = self._finite_number(value)
            elif action in ("trigger_source", "trigger_slope", "trigger_sweep", "trigger_level"):
                field = action.removeprefix("trigger_")
                if field == "level":
                    self._settings["trigger"][field] = self._finite_number(value)
                else:
                    allowed = {
                        "source": ("CH1", "CH2"),
                        "slope": ("RISE", "FALL"),
                        "sweep": ("AUTO", "NORMAL", "SINGLE"),
                    }[field]
                    if str(value).upper() not in allowed:
                        raise BackendError(f"Unsupported trigger {field}.")
                    self._settings["trigger"][field] = str(value).upper()
            else:
                raise BackendError(f"Unknown control action {action!r}.")
            return {"simulated": True, "action": action, "settings": deepcopy(self._settings)}

    @staticmethod
    def _finite_number(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise BackendError("Expected a finite number.") from exc
        if not math.isfinite(number):
            raise BackendError("Expected a finite number.")
        return number

    def query(self, command: str) -> str:
        with self._lock:
            self._require_connection()
            command = _query_text(command).upper()
            responses = {
                "*IDN?": "OWON,HDS2102S,DEMO,simulated",
                ":DMM:MEAS?": "0.0123",
                "DMM:MEAS?": "0.0123",
                ":DMM:CONFIGURE?": "VOLTage",
                ":DMM:CONFIGURE:VOLTAGE?": "DC",
                ":DMM:CONFIGURE:CURRENT?": "error",
                ":DMM:RANGE?": "V",
                ":DMM:REL?": "OFF",
            }
            measurement = re.fullmatch(
                r":MEASUREMENT:(CH[12]):(MAX|MIN|PKPK|VAMP|AVERAGE|PERIOD|FREQUENCY)\?",
                command,
            )
            if measurement is not None:
                channel, item = measurement.groups()
                index = int(channel[-1]) - 1
                grounded = self._settings["channels"][channel]["coupling"] == "GND"
                amplitude = 0.0 if grounded else 1.0 / (index + 1)
                frequency = 1000.0 / (index + 1)
                if item in ("PERIOD", "FREQUENCY"):
                    if grounded:
                        return "nan"
                    value = 1.0 / frequency if item == "PERIOD" else frequency
                    return f"{value:.12g} {'s' if item == 'PERIOD' else 'Hz'}"
                value = {
                    "MAX": amplitude,
                    "MIN": -amplitude,
                    "PKPK": 2 * amplitude,
                    "VAMP": 2 * amplitude,
                    "AVERAGE": 0.0,
                }[item]
                return f"{value:.12g} V"
            if command not in responses:
                raise BackendError(f"The offline demo does not implement query {command!r}.")
            return responses[command]
