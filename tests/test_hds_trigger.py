"""HDS trigger control and readback regressions; all device state is replayed."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from owon_mcp.backend import BackendError, UsbBackend, _apply_control


def connected_replay(snapshots, model="HDS2102S"):
    """Replay fresh upstream settings and the HDS firmware's nested header."""
    snapshots = iter(snapshots)
    last = None
    driver = SimpleNamespace(
        protocol="owon-scpi",
        info={"model": model},
        send=Mock(),
        apply=Mock(return_value={"sent": "upstream"}),
        head={},
    )

    def get_settings():
        nonlocal last
        last = next(snapshots, last)
        if last is None:
            raise AssertionError("Unexpected settings query")
        sweep, running = last
        driver.head = {
            "RUNSTATUS": running,
            "Trig": {
                "Mode": "SINGle",
                "Type": "Edge",
                "Items": {
                    "Channel": "CH1",
                    "Level": "960mV",
                    "Edge": "RISe",
                    "Coupling": "DC",
                    "Sweep": sweep,
                },
            },
        }
        # The generic upstream decoder omits Trig.Items; verification must use
        # the current header, not assume that sending the command succeeded.
        return {"channels": {}, "running": running, "trigger": {}}

    driver.get_settings = Mock(side_effect=get_settings)
    candidate = SimpleNamespace(location=SimpleNamespace(id="replay"))
    device = SimpleNamespace(
        status="ready",
        driver=driver,
        to_json=lambda: {"id": "replay", "info": {"model": model}},
    )
    backend = UsbBackend(scanner=lambda: [candidate], detector=lambda _: device)
    backend.connect()
    return backend, driver


def test_hds_single_selects_working_sweep_path_then_rearms_acquisition():
    _, driver = connected_replay([])

    result = _apply_control(driver, "single", {})

    assert driver.send.call_args_list == [
        call(":TRIGger:SWEep SINGle"),
        call(":RUNning RUN"),
    ]
    assert result["sent"] == [":TRIGger:SWEep SINGle", ":RUNning RUN"]
    driver.apply.assert_not_called()


@pytest.mark.parametrize("action", ["single", "trigger_sweep"])
def test_hds_control_rejects_a_silently_ignored_command(monkeypatch, action):
    sleep = Mock()
    monkeypatch.setattr("owon_mcp.backend.time.sleep", sleep)
    backend, driver = connected_replay([("AUTo", "AUTo")])
    # A previously cached desired state must not mask a fresh AUTO readback.
    driver.head = {"Trig": {"Items": {"Sweep": "SINGle"}}, "RUNSTATUS": "READy"}
    params = {} if action == "single" else {"value": "SINGle"}

    with pytest.raises(BackendError, match="readback failed.*observed AUTo / AUTo"):
        backend.control(action, params)

    assert driver.get_settings.call_count > 1
    assert sleep.call_count == driver.get_settings.call_count - 1
    driver.send.assert_called()


def test_single_waits_for_both_updated_sweep_and_ready_status(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("owon_mcp.backend.time.sleep", sleep)
    backend, driver = connected_replay(
        [("AUTo", "AUTo"), ("SINGle", "AUTo"), ("SINGle", "READy")]
    )

    result = backend.control("single", {})

    assert result["state_verified"] is True
    assert result["armed"] is True
    assert result["readback"] == {"sweep": "SINGle", "running": "READy"}
    assert driver.get_settings.call_count == 3
    assert sleep.call_count == 2


def test_single_stopped_readback_does_not_claim_verified_arming_or_completion(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("owon_mcp.backend.time.sleep", sleep)
    backend, driver = connected_replay([("SINGle", "STOP")])

    result = backend.control("single", {})

    assert result["state_verified"] is False
    assert result["armed"] is False
    assert result["readback"] == {"sweep": "SINGle", "running": "STOP"}
    assert driver.get_settings.call_count == 10
    assert sleep.call_count == 9


def test_single_waits_for_stopped_scope_to_rearm(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("owon_mcp.backend.time.sleep", sleep)
    backend, driver = connected_replay([("SINGle", "STOP"), ("SINGle", "READy")])

    result = backend.control("single", {})

    assert result["state_verified"] is True
    assert result["armed"] is True
    assert driver.get_settings.call_count == 2
    sleep.assert_called_once_with(0.05)


def test_trigger_sweep_waits_for_updated_firmware_state_without_rearming(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr("owon_mcp.backend.time.sleep", sleep)
    backend, driver = connected_replay([("AUTo", "STOP"), ("NORMal", "STOP")])

    result = backend.control("trigger_sweep", {"value": "NORMal"})

    assert result["state_verified"] is True
    assert result["readback"] == {"sweep": "NORMal", "running": "STOP"}
    assert "armed" not in result
    driver.send.assert_called_once_with(":TRIGger:SWEep NORMal")
    assert sleep.call_count == 1


@pytest.mark.parametrize("model", ["SDS1102", "XDS3104", "HDS2062M-N"])
@pytest.mark.parametrize("action,params", [("single", {}), ("trigger_sweep", {"value": "SINGle"})])
def test_other_models_keep_upstream_trigger_controls_without_hds_readback(model, action, params):
    backend, driver = connected_replay([], model)

    result = backend.control(action, deepcopy(params))

    assert result == {"sent": "upstream"}
    driver.apply.assert_called_once_with(action, params)
    driver.send.assert_not_called()
    driver.get_settings.assert_not_called()
