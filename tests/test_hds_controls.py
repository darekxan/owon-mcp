"""HDS command regression tests never access USB."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from owon_mcp.backend import BackendError, _apply_control


@pytest.mark.parametrize("channel", ["CH1", "CH2"])
@pytest.mark.parametrize(
    ("action", "value", "suffix"),
    [
        ("channel_display", False, "DISPlay OFF"),
        ("channel_display", True, "DISPlay ON"),
        ("volts_div", "1mV", "SCALe 1mv"),
        ("coupling", "AC", "COUPling AC"),
        ("probe", "10X", "PROBe 10X"),
        ("position", -50, "OFFSet -50"),
    ],
)
def test_hds_channel_commands_use_ch_prefix(channel, action, value, suffix):
    driver = SimpleNamespace(
        protocol="owon-scpi", info={"model": "HDS2102S"}, send=Mock(), apply=Mock()
    )
    result = _apply_control(driver, action, {"channel": channel, "value": value})
    assert result == {"sent": f":{channel}:{suffix}", "profile": "owon-hds200"}
    driver.send.assert_called_once_with(result["sent"])
    driver.apply.assert_not_called()


@pytest.mark.parametrize(
    ("action", "value", "expected"),
    [
        ("trigger_source", "CH2", ":TRIGger:SINGle:SOURce CH2"),
        ("trigger_slope", "FALL", ":TRIGger:SINGle:EDGe FALL"),
        ("trigger_sweep", "AUTO", ":TRIGger:SWEep AUTO"),
        ("trigger_sweep", "normal", ":TRIGger:SWEep NORMal"),
        ("trigger_sweep", "single", ":TRIGger:SWEep SINGle"),
    ],
)
def test_hds_trigger_dialect(action, value, expected):
    driver = SimpleNamespace(
        protocol="owon-scpi", info={"model": "HDS2102S"}, send=Mock(), apply=Mock()
    )
    _apply_control(driver, action, {"value": value})
    driver.send.assert_called_once_with(expected)
    driver.apply.assert_not_called()


@pytest.mark.parametrize("model", ["SDS1102", "XDS3104", "HDS2062M-N"])
def test_other_families_keep_upstream_controls(model):
    driver = SimpleNamespace(
        protocol="owon-scpi",
        info={"model": model},
        send=Mock(),
        apply=Mock(return_value={"sent": "upstream"}),
    )
    params = {"channel": "CH2", "value": False}
    assert _apply_control(driver, "channel_display", params) == {"sent": "upstream"}
    driver.apply.assert_called_once_with("channel_display", params)
    driver.send.assert_not_called()


def test_malformed_channel_value_cannot_send_a_command():
    driver = SimpleNamespace(
        protocol="owon-scpi", info={"model": "HDS2102S"}, send=Mock(), apply=Mock()
    )
    with pytest.raises(BackendError):
        _apply_control(driver, "probe", {"value": "10X\n*RST"})
    driver.send.assert_not_called()
