"""Replay real HDS header structure without accessing an instrument."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from owonscope.scpi import ScpiDriver

from owon_mcp.backend import UsbBackend

HEADER = {
    "TIMEBASE": {"SCALE": "5.0ms", "HOFFSET": 0},
    "SAMPLE": {"FULLSCREEN": 600, "DATALEN": 600, "SAMPLERATE": "50KSa/s"},
    "CHANNEL": [
        {
            "NAME": "CH1",
            "DISPLAY": "ON",
            "COUPLING": "DC",
            "PROBE": "10X",
            "SCALE": "1.00 V",
            "OFFSET": 50,
            "FREQUENCE": 0,
        }
    ],
    "RUNSTATUS": "READy",
    "Trig": {
        "Mode": "SINGle",
        "Type": "Edge",
        "Items": {
            "Channel": "CH1",
            "Level": "960mV",
            "Edge": "RISe",
            "Coupling": "DC",
            "Sweep": "SINGle",
        },
    },
}
TRIGGER = {
    "mode": "SINGle",
    "type": "Edge",
    "source": "CH1",
    "level": "960mV",
    "slope": "RISe",
    "coupling": "DC",
    "sweep": "SINGle",
}


def replay_backend(model="HDS2102S"):
    class ReplayPipe:
        link = SimpleNamespace(usbtmc=False)

        def __init__(self):
            self.commands = []

        def query_block(self, command):
            self.commands.append(command)
            if command.endswith("HEAD?"):
                return json.dumps(HEADER).encode()
            if command.endswith("CH1?"):
                return bytes([50, 51, 49] * 200)
            raise AssertionError(command)

    pipe = ReplayPipe()
    driver = ScpiDriver(pipe, f"OWON,{model},REPLAY,V4.7.1")
    candidate = SimpleNamespace(location=SimpleNamespace(id="replay"))
    device = SimpleNamespace(
        status="ready",
        driver=driver,
        to_json=lambda: {"id": "replay", "info": {"model": model}},
    )
    backend = UsbBackend(scanner=lambda: [candidate], detector=lambda _: device)
    backend.connect()
    return backend, driver, pipe


def test_hds_settings_expose_nested_trigger_and_keep_raw_header_independent():
    backend, driver, pipe = replay_backend()
    original = driver._settings_from_head
    driver._settings_from_head = lambda head: {
        **original(head),
        "trigger": {"vendor_field": 123, "sweep": "stale"},
    }

    settings = backend.settings()

    assert settings["trigger"] == {"vendor_field": 123, **TRIGGER}
    assert settings["raw_header"] == HEADER
    assert pipe.commands == [":DATA:WAVE:SCREen:HEAD?"]
    settings["raw_header"]["Trig"]["Items"]["Channel"] = "caller mutation"
    settings["trigger"]["source"] = "caller mutation"
    assert driver.head == HEADER


@pytest.mark.parametrize("model", ["HDS2102S", "SDS1102"])
def test_capture_trigger_metadata_uses_its_own_header_without_an_extra_query(model):
    backend, driver, pipe = replay_backend(model)

    capture = backend.capture()

    assert pipe.commands == [":DATA:WAVE:SCREen:HEAD?", ":DATA:WAVE:SCREen:CH1?"]
    assert capture["settings"]["running"] == "READy"
    if model == "HDS2102S":
        assert capture["settings"]["trigger"] == TRIGGER
        assert capture["settings"]["raw_header"] == HEADER
        capture["settings"]["raw_header"].clear()
        assert driver.head == HEADER
    else:
        assert capture["settings"]["trigger"] == {}
        assert "raw_header" not in capture["settings"]


def test_other_model_settings_keep_upstream_trigger_and_raw_header():
    backend, driver, _ = replay_backend("SDS1102")
    original = driver._settings_from_head
    driver._settings_from_head = lambda head: {
        **original(head),
        "trigger": {"vendor_field": 123},
    }

    settings = backend.settings()

    assert settings["trigger"] == {"vendor_field": 123}
    assert settings["raw_header"] == deepcopy(HEADER)
