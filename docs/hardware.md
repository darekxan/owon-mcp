# Hardware setup and validation

OWON MCP uses the USB transport and protocol detection from
[OWON Scope Web](https://github.com/fmesasc/owon-scope-web). The dependency is pinned
to revision `cb86012000048b8441318a293fe7a41a83f98023`. Its supported-device list
describes upstream coverage; the hardware results below describe this integration's
tested setup.

## Prepare the USB connection

Install the project as described in the [README](../README.md). Install libusb on
macOS with `brew install libusb`. For Linux USB permissions and Windows WinUSB
driver setup, follow the corresponding sections of the
[upstream setup instructions](https://github.com/fmesasc/owon-scope-web#linux-usb-permissions).

Select the instrument's HID/control USB mode rather than MSC storage mode.
Disconnect it in OWON Scope Web and close other clients that have claimed the USB
interface. The browser and this process cannot own that interface simultaneously.
Then use `owon_discover`, `owon_connect`, and finally `owon_disconnect` in your MCP
client. If discovery returns several instruments, pass one returned ID to
`owon_connect`.

Server startup and tool listing do not enumerate or open USB. There are no
background scans or implicit connections. Automatic USB-reset recovery is
disabled; a failed read is reported instead of resetting the instrument.

## What was tested

Hardware checks were performed on macOS / Apple Silicon with an HDS2102S running
firmware V4.7.1. MCP stdio initialization, discovery, identity, settings, both
600-value screen traces, JSON export, and USB release succeeded. A separate
control check changed the timebase and CH2 display, verified the changes, and
restored the original settings.

The HDS200 adapter uses HDS channel scale, coupling, probe, offset, and trigger
command names. For example, this firmware requires `:CH2:DISPlay` rather than the
generic upstream `:CHANnel2:DISPlay`.

These results do not establish compatibility with every OWON model, firmware, or
control. Other control paths and the MCP DMM tool have offline test coverage;
they have not all been exercised through this server on hardware. Offset
controls pass through the upstream driver's native values and are not calibrated
divisions.

### Trigger sweep and Single rearming

On the tested HDS2102S firmware, the verified commands are
`:TRIGger:SWEep SINGle` and `:TRIGger:SWEep?` directly under `TRIGger`. The extra
`:SINGle:` node in the published HDS200 manual and generic upstream driver is
silently ignored for this setting. The adapter uses the verified path for all
sweep values. Its `single` action sets Single sweep and sends `:RUNning RUN` to
start or rearm acquisition.

AUTO, Normal, Single, and the `READy → STOP → READy` rearm sequence were checked
against instrument readback. HDS200 `trigger_sweep` and `single` return
`state_verified=true` after successful verification. For `single`,
`result.armed=true` means the instrument is waiting for a trigger. A stopped
Single readback returns `state_verified=false`: it cannot distinguish a completed
acquisition from a RUN command that did not take effect. Other controls report
`state_verified=false`; sending a command alone does not prove its physical
effect.

## Optional checks on a connected instrument

The following scripts use real USB and run only when explicitly invoked. Run
them from the repository root after disconnecting other USB clients. The
automated test suite does not run them.

Read identity, settings, and one capture without invoking scope controls:

```sh
.venv/bin/python scripts/live_smoke.py
```

Use `--device-id` when several instruments are connected. Add `--dmm` only when
the instrument's multimeter mode is ready. DMM replies may omit units; a bare
number does not establish a unit or active DMM mode. Reports are written to
`exports/`.

The control and trigger checks read a local project configuration. If you have
not created one, copy the supplied template:

```sh
mkdir -p .codex
cp examples/codex.toml .codex/config.toml
```

Replace every placeholder path with your repository and export directory's
absolute paths. Enable `--allow-control` in its arguments, and ensure the entry
is enabled. Keep an existing local configuration if you already have one.
`.codex/config.toml` is machine-specific and excluded from the repository.

Temporarily change and verify the timebase and one channel's display, restore
and verify both, then disconnect:

```sh
.venv/bin/python scripts/live_control_check.py
```

Check the HDS200 sweep and rearm sequence with a quiet trigger input:

```sh
.venv/bin/python scripts/live_trigger_check.py
```

The trigger check restores the original sweep/run state by default and releases
USB. Its `--leave-armed` option deliberately leaves a successful Single capture
waiting for the next trigger. Both checks save reports under `exports/`; inspect
their reported verification, restoration, and cleanup outcomes.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No instrument discovered | Check the cable, USB port, powered-on instrument, and HID/control mode. Discovery enumerates candidates without probing their protocol. |
| libusb unavailable / no backend | Install libusb and restart the MCP process so it can load the library. |
| Access denied / interface busy | Disconnect the browser or other USB client. Check Linux permissions or the Windows WinUSB setup using the upstream instructions. |
| Several candidates | Pass the exact ID returned by `owon_discover` to `owon_connect`. |
| Instrument stopped responding | Disconnect explicitly. Check the physical connection and reconnect manually; the server does not reset USB to recover. |
| Controls disabled | Start the process with `--allow-control`, then restart/reload the MCP entry in your client. |
| No cached capture | Call `owon_capture` before raw/export operations. Export before disconnecting: disconnect clears the cache. |
| Recording active | Poll `owon_record_status` or call `owon_record_stop`. Wait for cleanup to finish before reconnecting or handing USB to another client. |
| DMM rejected / no meaningful reading | Activate the required mode on the instrument. Preserve its raw reply; do not infer units or a successful measurement from zero. |

Use `--verbose` to send diagnostic logs to stderr. Stdout is reserved for MCP
messages. Firmware images, instrument captures, and local validation reports are
excluded from this repository. No firmware installation or modification is
required by this integration.
