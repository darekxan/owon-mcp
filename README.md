# OWON MCP

A local **stdio MCP server for OWON oscilloscopes**. Connect an MCP client to read
settings, capture waveforms, query measurements and the multimeter, export data,
and optionally control acquisition or record a sequence of screen traces.

USB transport, protocol detection and waveform decoding come from
[OWON Scope Web](https://github.com/fmesasc/owon-scope-web), pinned to a source
revision in `pyproject.toml` and `uv.lock`. This project adds MCP tools, explicit
USB ownership, response limits, recording and an HDS200 control adapter.

**Hardware validation:** HDS2102S, firmware V4.7.1, on macOS / Apple Silicon.
Other OWON models use upstream drivers and have not been validated here. See
[hardware support and troubleshooting](docs/hardware.md) for the tested controls
and protocol caveats.

## Install from source

Requires Python 3.11+, Git, [uv](https://docs.astral.sh/uv/), and libusb for real
hardware. This repository is the installation source; the instructions do not
depend on a PyPI release.

```sh
git clone https://github.com/darekxan/owon-mcp.git
cd owon-mcp
# macOS hardware access:
brew install libusb
uv sync --locked
uv run --no-sync owon-mcp --version
```

On Linux, install libusb-1.0 and configure permissions using the upstream
[USB setup instructions](https://github.com/fmesasc/owon-scope-web#linux-usb-permissions).
On Windows, follow the upstream WinUSB driver instructions. The offline demo and
test suite do not require a scope or libusb. Hardware compatibility on Linux and
Windows is not established by the offline tests.

## Configure an MCP client

Use the installed executable as a local stdio server. Copy
[examples/mcp.json](examples/mcp.json) into a client that accepts `mcpServers`
configuration, replacing both paths with your checkout location:

```json
{
  "mcpServers": {
    "owon": {
      "command": "/absolute/path/to/owon-mcp/.venv/bin/owon-mcp",
      "args": ["--export-dir", "/absolute/path/to/owon-mcp/exports"]
    }
  }
}
```

On Windows, the executable is `.venv/Scripts/owon-mcp.exe`; use forward slashes or
JSON-escaped backslashes in paths. An absolute executable path avoids dependence
on a GUI client's shell environment. Restart the configured server after changes.

For Codex, [examples/codex.toml](examples/codex.toml) provides the equivalent
`mcp_servers.owon` entry. For a project-local configuration, copy it to
`.codex/config.toml` and replace its paths. The local configuration is ignored by
Git. The optional hardware control scripts also read this file.

The default profile disables instrument setting and acquisition writes. It still
allows explicit USB discovery/connection, queries, and local capture exports.
Add `--allow-control` to `args` to enable controls and recording. There are no
unrestricted SCPI writes, USB-reset, firmware-flashing or function-generator tools.

The MCP server does not open a network port. Protocol output goes to stdout and
logs to stderr. Starting it, initializing MCP, listing tools and reading status
do not scan or open USB. There are no automatic connections or background scans.

## First capture

1. Release the scope in OWON Scope Web or any other USB application. Only one
   process can claim its interface at a time.
2. Put the instrument in **HID/control** mode instead of MSC storage mode.
3. Call `owon_discover` to list candidates without probing their protocol.
4. Call `owon_connect`. Supply a discovered `device_id` if several scopes exist.
5. Call `owon_get_settings`, then `owon_capture` with, for example:

   ```json
   {"max_points": 300, "include_samples": true, "include_raw": false}
   ```

6. Optionally call `owon_export_capture` with `{"format": "json"}` or
   `{"format": "csv"}`. Export **before disconnecting**: disconnect clears the
   cached capture.
7. Call `owon_disconnect` to release USB. Normal server shutdown also disconnects.

You can ask a client: “Discover my OWON scope, connect, read its settings, capture
both enabled channels, export the full capture as JSON, then disconnect.” Discovery
and connection happen only when those tools are explicitly called.

## Tools

| Tool | Purpose |
| --- | --- |
| `owon_status` | Cached connection, demo, control and recording state |
| `owon_discover` | List USB candidates without protocol probing |
| `owon_connect` / `owon_disconnect` | Claim and identify / release one instrument |
| `owon_capabilities` | Cached driver capabilities and accepted control values |
| `owon_get_settings` | Read channels, timebase, trigger and acquisition state |
| `owon_capture` | Capture enabled traces and statistics, with optional samples/raw data |
| `owon_dmm_read` | Query multimeter values without changing its mode |
| `owon_scope_measurements` | Query instrument-calculated extrema, amplitude, period or frequency |
| `owon_control` | Typed setting/acquisition writes; requires `--allow-control` |
| `owon_record_start` | Start bounded background recording; requires `--allow-control` |
| `owon_record_status` / `owon_record_stop` | Check progress / request stop and cleanup |
| `owon_get_raw` | Read cached upstream diagnostics |
| `owon_export_capture` | Write a full cached capture as JSON or CSV |

Resources `owon://status` and `owon://capture/latest` expose cached status and a
capture summary. See [tool reference](docs/tools.md) for arguments, limits, control
values, cache lifetime and data semantics.

## Recording

After explicitly connecting, set the channels, probe ratios and timebase, then call
`owon_record_start`:

```json
{"duration_s": 60, "interval_s": 0.2, "filename": "sequence.json"}
```

It selects AUTO sweep and RUN, saves full traces and raw diagnostics under
`--export-dir`, and returns immediately. Poll `owon_record_status` or call
`owon_record_stop`. Recordings are limited to 600 seconds, intervals of 0.05–5
seconds, and 10,000 scheduled frames. Slow USB reads can reduce the achieved rate.

**Recording changes the instrument state:** cleanup attempts STOP and disconnect
on completion, manual stop or failure. It does not restore the previous sweep or
run state. Reconnect before further scope work. A `stopping` result means cleanup
is still pending; inspect the terminal status and any `error`/`cleanup_errors`.
Only status and recording stop are available while recording is active.

## Limits that matter when interpreting data

- A capture is a sequence of header/channel reads during acquisition, not an
  atomic hardware snapshot. Recordings have gaps and do not extend ADC memory.
- Voltage scaling, probe metadata and point spacing come from the pinned upstream
  decoder. Screen-point spacing is not necessarily the acquisition sample period.
- `max_points` decimates each returned trace; it is not a peak-preserving filter.
  Full samples stay cached for export. `include_raw=true` attaches full diagnostics,
  which are outside the sample limit and can produce large responses.
- Bare numeric DMM replies have `unit=null`; units and overload text are never
  inferred. Activate DMM mode on the instrument when needed.
- Most controls confirm command dispatch only. HDS200 sweep/Single readback is
  checked; use settings readback to assess other controls. Native position offsets
  are not advertised as calibrated divisions.
- Automatic USB reset recovery is disabled. A timeout is reported to the client;
  manual reconnection may be necessary.

## Try it without hardware

Add `--demo` to the MCP client's server arguments to use simulated traces, settings,
DMM values and measurements. Discovery and connection remain explicit, and status
and captures identify the simulation. Add `--allow-control` for simulated writes.
Running the executable directly starts stdio MCP and waits for a client:

```sh
uv run --no-sync owon-mcp --demo
```

## Development and checks

```sh
uv sync --locked
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv build
```

Tests use synthetic drivers, replay data and real MCP clients, including a stdio
subprocess. They do not access hardware. GitHub Actions runs the offline checks
and builds across the configured Python/platform matrix.

For an **explicit hardware read check**, disconnect other USB clients, then run:

```sh
uv run --no-sync python scripts/live_smoke.py
```

It connects, reads settings/capture, exports and disconnects, without scope control
writes. Add `--dmm` only when DMM mode is ready. Control and trigger checks are
documented separately in [hardware support](docs/hardware.md); they change settings.

Local recordings, captures, firmware and client configuration are excluded from
the repository and package archives.

## CLI options

| Option | Default / meaning |
| --- | --- |
| `--demo` | Use a USB-free simulated instrument |
| `--allow-control` | Enable setting/acquisition writes and recording |
| `--export-dir PATH` | Output directory; defaults to `./exports` in the process working directory |
| `--verbose` | Send diagnostic logs to stderr |
| `--help` / `--version` | Print usage/version without loading the USB backend |

## License and upstream

MIT; see [LICENSE](LICENSE). OWON Scope Web is MIT-licensed by Francisco Mesas
Cervilla; see [NOTICE](NOTICE). The upstream revision is
`cb86012000048b8441318a293fe7a41a83f98023`. This project is independently developed
and is not affiliated with OWON. Firmware is not included.
