# Tool reference

All examples below are **tool arguments**, not shell commands. The client obtains
the full schemas from MCP tool discovery. Connect explicitly before instrument
queries. `CH1` and `CH2` are the supported channel names in this integration.

## Connection and cached state

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `owon_status` | `{}` | Cached connection, control policy, simulation flag, cache presence and recording state; no USB access |
| `owon_discover` | `{}` | Enumerates USB candidates, without claiming or probing |
| `owon_connect` | `{"device_id": "ID_FROM_DISCOVERY"}` or `{}` | Claims and identifies one scope; an ID is required when several candidates exist |
| `owon_disconnect` | `{}` | Releases USB and clears the capture cache |
| `owon_capabilities` | `{}` | Requires a connection; returns cached driver capabilities and accepted control strings |
| `owon_get_settings` | `{}` | Reads instrument settings; retains the driver's original `raw_header` when available |
| `owon_get_raw` | `{}` | Requires a connection; returns upstream diagnostics cached by the last capture |

Startup, tool discovery and cached status/resource reads never open a device.
Operations are serialized with recording's USB accesses. While a recording is
running or stopping, only `owon_status`, `owon_record_status` and
`owon_record_stop` are available; other tools and resources are blocked.

`owon://status` returns cached JSON status. `owon://capture/latest` returns a
cached capture summary without samples. Missing capture state is an MCP error.

## Capture and export

```json
{"max_points": 300, "include_samples": true, "include_raw": false}
```

`owon_capture` defaults to `max_points=1000`, `include_samples=true`, and
`include_raw=false`. `max_points` accepts 1–10,000 **per trace**. It captures the
enabled channels and returns min/max/mean/RMS statistics over finite full-trace
values. These statistics are distinct from instrument-calculated measurements.

Each waveform reports `sample_count`, `returned_sample_count`, `sample_stride`,
`source_dt`, and `dt`. Returned samples use a stride of
`ceil(sample_count / max_points)`; returned `dt` is source `dt` times that stride.
With `include_samples=false`, no `volts` array is returned and `dt` stays at its
source value. Non-finite scalar values are represented as JSON `null`.

This is simple decimation and may omit narrow events. Full samples remain cached.
`include_raw=true` attaches full upstream diagnostics without decimation, so
`max_points` does not bound the entire response size. Header and channel reads
are sequential; `acquisition_atomic=false` describes this explicitly.

Export the full cache using `owon_export_capture`:

```json
{"format": "json", "filename": "capture.json"}
```

The format is `json` (default) or `csv`. The filename is optional, must be a simple
basename starting with an ASCII letter/digit, may contain letters/digits/`_`/`.`/`-`,
and must end in the matching extension. Its maximum length is 120 characters.
Exports stay within `--export-dir` and never replace existing files. The tool
returns the absolute path, format and byte count. JSON retains settings/metadata;
CSV has columns `waveform,index,time_s,volts`, using the upstream `t0` and `dt`.

**Export before disconnecting.** The cache is cleared on disconnect, a new/different
connection, recording start, or an operation that observes a disconnected backend.
It belongs to one server process and is lost on restart. `owon_get_raw` exposes
the upstream raw cache rather than the exported capture object.

## DMM and device measurements

`owon_dmm_read` queries `:DMM:MEAS?` without changing mode, range, hold or relative
settings. Activate the instrument's multimeter mode when required.

```json
{"count": 5, "interval_s": 0.2}
```

Defaults are one reading and a 0.2-second interval. `count` accepts 1–20;
`interval_s` accepts 0–5 seconds. Requested waits must satisfy
`(count - 1) * interval_s <= 10`; USB query time is additional. Readings include
a host timestamp, `raw`, numeric `value` or `null`, `unit` or `null`, and
`status` (`numeric` or `non_numeric`). Bare numbers carry no inferred unit;
overload text is retained, and an `error` reply fails the tool call.

`owon_scope_measurements` queries the instrument's own measurement functions:

```json
{"channel": "CH2", "items": ["MIN", "MAX", "PKPK", "FREQUENCY"]}
```

Defaults are CH1 and `MIN`, `MAX`, `PKPK`. Accepted items are `MAX`, `MIN`, `PKPK`,
`VAMP`, `AVERAGE`, `PERIOD`, and `FREQUENCY`. Duplicate items are queried once;
an empty list is rejected. Replies use the same raw/value/unit parsing as DMM.
Unsupported or non-numeric replies remain visible for diagnosis.

## Controls

`owon_control` requires `--allow-control` and a connected, control-capable driver.

| `action` | `value` | Notes |
| --- | --- | --- |
| `run`, `stop`, `single`, `autoset` | Omit | Changes acquisition |
| `channel_display` | JSON boolean | Selected `channel`, default CH1 |
| `volts_div`, `timebase` | String from `owon_capabilities` | Use supported values, such as `"1ms"` for timebase |
| `coupling` | `DC`, `AC`, `GND` | Selected channel |
| `probe` | `1X`, `10X`, `100X`, `1000X` | Software ratio must match the physical probe |
| `position` | Finite number, −200 to 200 | Upstream native offset units |
| `h_position` | Finite number, −2000 to 2000 | Upstream native offset units |
| `trigger_source` | `CH1`, `CH2` | Source is the value, not the `channel` argument |
| `trigger_slope` | `RISE`, `FALL` | Trigger edge |
| `trigger_level` | Finite number | Volts; device-specific physical range applies |
| `trigger_sweep` | `AUTO`, `NORMal`, `SINGle` | HDS200 readback verified |

```json
{"action": "channel_display", "channel": "CH2", "value": true}
```

```json
{"action": "single"}
```

Named string values are matched without case sensitivity. HDS200 `single` sets
Single sweep and sends RUN to rearm; it checks actual sweep and acquisition state.
`result.armed=true` means the scope is waiting for a trigger. A stopped Single
readback returns `state_verified=false`: it cannot distinguish an already completed
capture from RUN failing to take effect. HDS200 `trigger_sweep` also verifies its
readback. Other controls report `state_verified=false` and need independent
settings readback. Position values are not calibrated screen divisions.

## Recording

`owon_record_start` requires `--allow-control` and an explicit connection:

```json
{"duration_s": 60, "interval_s": 0.2, "filename": "sequence.json"}
```

Defaults are 60 seconds, 0.2-second interval and a generated JSON filename.
Duration must be greater than zero and at most 600 seconds, interval 0.05–5
seconds, and `duration_s / interval_s` at most 10,000. A supplied filename must
be a simple `.json` basename. Existing files are never overwritten.

Recording selects AUTO sweep and RUN using current channels, probe ratios and
timebase. Each frame saves full traces, raw diagnostics, host timing and instrument
metadata. JSON is streamed with a complete checkpoint after each frame rather
than retaining the recording in memory. Checkpoints help preserve partial results;
they do not guarantee recovery from a process kill or interrupted disk write.

`owon_record_status` returns `state`, `running`, `stop_requested`, `frame_count`,
`elapsed_s`, `output`, and `error`. States are `idle`, `running`, `stopping`,
`complete`, `stopped`, or `failed`. Cleanup may add `cleanup_errors`.
`owon_record_stop` waits up to five seconds; poll until cleanup finishes when it
returns `stopping`. A terminal state with an error warrants inspecting the error
before assuming USB release succeeded.

Completion, stop, failure and normal shutdown attempt STOP and disconnect.
Previous run/sweep state is not restored. Reconnect explicitly afterwards.
The requested interval is a scheduling target: reads take time and introduce gaps,
channel pairs are not atomic, and host time is not an ADC sample clock.
