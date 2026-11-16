"""Decoder adaptation tests use a replay pipe; they never enumerate USB."""

import json
import math
from types import SimpleNamespace

import pytest
from owonscope.scpi import ScpiDriver

from owon_mcp.backend import BackendError, DemoBackend
from owon_mcp.server import Session, prepare_capture, scalar_reading


@pytest.mark.parametrize(
    ("reply", "value", "unit"),
    [
        ("2.0000e-04\n", 0.0002, None),
        ("-0.0123 V\n", -0.0123, "V"),
        ("OL\n", None, None),
        ("error\n", None, None),
        ("nan", None, None),
    ],
)
def test_scalar_preserves_unknown_units_and_overload(reply, value, unit):
    parsed = scalar_reading(reply)
    assert parsed["raw"] == reply.strip()
    assert parsed["value"] == value
    assert parsed["unit"] == unit


def test_decimation_keeps_each_returned_point_at_its_original_time():
    source = {"waveforms": [{"name": "CH1", "volts": list(range(11)), "dt": 0.01, "t0": -1}]}
    result = prepare_capture(source, max_points=4, include_samples=True)
    wave = result["waveforms"][0]
    assert wave["volts"] == [0, 3, 6, 9]
    assert wave["dt"] == 0.03
    assert wave["source_dt"] == 0.01
    assert wave["sample_count"] == 11
    assert wave["returned_sample_count"] == 4
    assert wave["statistics"]["mean_v"] == 5
    assert source["waveforms"][0]["dt"] == 0.01
    assert len(source["waveforms"][0]["volts"]) == 11


def test_nonfinite_measurements_are_json_safe():
    source = {"waveforms": [{"volts": [1.0, math.nan, math.inf], "dt": 1.0}]}
    result = prepare_capture(source, max_points=10, include_samples=True)
    assert result["waveforms"][0]["volts"] == [1.0, None, None]
    assert result["waveforms"][0]["statistics"]["mean_v"] == 1.0
    json.dumps(result, allow_nan=False)


def test_actual_upstream_driver_with_hds_shaped_replay():
    header = {
        "TIMEBASE": {"SCALE": "1.0ms", "HOFFSET": 0},
        "SAMPLE": {"FULLSCREEN": 600, "DATALEN": 600, "SAMPLERATE": "250KSa/s"},
        "CHANNEL": [
            {
                "NAME": "CH1",
                "DISPLAY": "ON",
                "SCALE": "1.00 V",
                "OFFSET": 50,
                "PROBE": "10X",
                "COUPLING": "DC",
                "FREQUENCE": 0.0,
            },
            {
                "NAME": "CH2",
                "DISPLAY": "ON",
                "SCALE": "1.00 V",
                "OFFSET": -50,
                "PROBE": "10X",
                "COUPLING": "DC",
                "FREQUENCE": 0.0,
            },
        ],
        "RUNSTATUS": "AUTo",
    }

    class ReplayPipe:
        link = SimpleNamespace(usbtmc=False)

        def query_block(self, command):
            if command.endswith("HEAD?"):
                return json.dumps(header).encode()
            if command.endswith("CH1?"):
                return bytes([50, 51, 49] * 200)
            if command.endswith("CH2?"):
                return bytes([206, 207, 205] * 200)
            raise AssertionError(command)

    driver = ScpiDriver(ReplayPipe(), "OWON,HDS2102S,REPLAY,V4.7.1")
    decoded = driver.capture().to_json()
    result = prepare_capture(decoded, max_points=100, include_samples=True)
    assert [wave["name"] for wave in result["waveforms"]] == ["CH1", "CH2"]
    assert all(wave["sample_count"] == 600 for wave in result["waveforms"])
    assert all(len(wave["volts"]) == 100 for wave in result["waveforms"])
    assert json.loads(driver.last_raw)["head"] == header
    assert result["acquisition_atomic"] is False


def test_control_scale_is_canonicalized_like_upstream(tmp_path):
    backend = DemoBackend()
    backend.connect()
    session = Session(backend, True, tmp_path)
    params = session.validate_control("volts_div", "CH1", "1MV")
    assert params["value"] == "1mV"
    backend.control("volts_div", params)
    assert backend.capture()["waveforms"][0]["volts_per_div"] == 0.001


def test_demo_raw_data_is_not_reused_after_reconnect():
    backend = DemoBackend()
    backend.connect()
    backend.capture()
    assert backend.raw()
    backend.disconnect()
    backend.connect()
    with pytest.raises(BackendError, match="No raw capture"):
        backend.raw()
