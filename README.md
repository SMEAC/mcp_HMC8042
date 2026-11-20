# HMC8042 MCP server

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) control a
**Rohde & Schwarz HMC8042** two-channel power supply via SCPI over LAN (or USB-TMC).
Should also work with the HMC8041 / HMC8043 (single / three channel) after adjusting `CHANNELS`.

## Safety

This drives real circuits, so the server is deliberately conservative:

* **On connect** (server start), all outputs are switched off, then both channels are set to
  **5 V / 100 mA**. Change with `HMC_DEFAULT_VOLTAGE` / `HMC_DEFAULT_CURRENT`, or disable with
  `HMC_APPLY_DEFAULTS=0`. Restarting the server therefore switches the outputs off.
* **Caps**: setpoints above `HMC_MAX_VOLTAGE` / `HMC_MAX_CURRENT` are refused before anything is sent.
* **Enabling** an output re-reads the setpoints from the instrument (the front panel may have
  changed them) and refuses if they exceed the caps.
* Outputs are only switched on by `set_output(..., enabled=True)`. `all_outputs_off` is the emergency stop.

## Tools

| Tool | What it does |
|---|---|
| `list_instruments` | VISA instruments visible to this PC (USB; LAN sockets are not discoverable) |
| `identify` | Model, serial, firmware, address |
| `get_status` | Setpoints, output state, measured V/I/P, CV/CC mode, OVP/fuse state, caps |
| `configure_channel` | Voltage, current limit, OVP level, electronic fuse (does not switch the output) |
| `set_output` | Channel 1, 2 or `all` on/off |
| `all_outputs_off` | Emergency off |
| `apply_defaults` | Back to the safe start-up state |
| `clear_protection` | Reset a tripped OVP |
| `measure` | Measured voltage, current, power |
| `screenshot` | Display as PNG |
| `scpi_write`, `scpi_query` | Raw SCPI (bypasses the caps), only when `HMC_ALLOW_RAW_SCPI=1` |

## Setup (LAN)

1. On the supply: *SETUP > Interface > Ethernet*, note (or set) the IP address.
2. `uv sync`
3. Put the IP into `HMC_HOST` in `.mcp.json`. The server connects to the raw SCPI socket
   `TCPIP0::<ip>::5025::SOCKET`; this works with Keysight/NI VISA or the pure-Python `@py` backend.
4. Check: `uv run python -c "from hmc8042_mcp.psu import Supply; print(Supply.from_env().identify())"`
   (with `HMC_HOST` set in the environment; note this applies the safe defaults).

The front-panel USB-A socket is for memory sticks; for USB remote control use the rear USB-B port in
USB-TMC mode and set `HMC_RESOURCE` to its VISA address.

## Configuration

| Variable | Meaning |
|---|---|
| `HMC_HOST`, `HMC_PORT` | IP/hostname and SCPI socket port (default 5025) |
| `HMC_RESOURCE` | Full VISA address instead of `HMC_HOST`; `SIM` = simulator |
| `HMC_VISA_LIBRARY` | PyVISA backend, e.g. `@py` |
| `HMC_TIMEOUT_MS` | I/O timeout (default 5000) |
| `HMC_MAX_VOLTAGE`, `HMC_MAX_CURRENT` | Safety caps (default: instrument maximum 32.05 V / 5 A) |
| `HMC_DEFAULT_VOLTAGE`, `HMC_DEFAULT_CURRENT` | Applied on connect (default 5 V / 0.1 A) |
| `HMC_APPLY_DEFAULTS` | `0` = leave the supply untouched on connect |
| `HMC_ALLOW_RAW_SCPI` | `1` to expose raw SCPI tools |

The server is launched with `python -m hmc8042_mcp.server` because Windows Application Control
can block the generated `.exe` launcher (tests likewise run with `python -m pytest`).

## Development

```
uv run python -m pytest   # simulator, no hardware needed
```

Reference: *R&S HMC804x SCPI Programmers Manual*.
