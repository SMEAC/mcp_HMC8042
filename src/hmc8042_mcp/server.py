"""MCP server exposing the R&S HMC8042 power supply as tools.

Configuration (environment variables):
  HMC_HOST             IP address / hostname of the supply on the LAN (raw SCPI socket).
  HMC_PORT             SCPI socket port (default 5025).
  HMC_RESOURCE         Full VISA address instead of HMC_HOST, e.g. TCPIP0::192.168.1.60::5025::SOCKET.
                       "SIM" = built-in simulator (no hardware needed).
  HMC_VISA_LIBRARY     Optional PyVISA backend, e.g. "@py". Default: system VISA, else @py.
  HMC_TIMEOUT_MS       I/O timeout in ms (default 5000).
  HMC_MAX_VOLTAGE      Highest voltage setpoint the server will accept (default 32.05 V).
  HMC_MAX_CURRENT      Highest current limit the server will accept (default 5 A).
  HMC_DEFAULT_VOLTAGE  Voltage applied to both channels on connect (default 5 V).
  HMC_DEFAULT_CURRENT  Current limit applied to both channels on connect (default 0.1 A).
  HMC_APPLY_DEFAULTS   "0" to leave the supply untouched on connect (default: outputs off + defaults).
  HMC_ALLOW_RAW_SCPI   "1" to expose the scpi_write / scpi_query tools.
"""

from __future__ import annotations

import functools
import os
import sys
import threading
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import transport
from .psu import Supply

mcp = MCPServer(
    "hmc8042",
    instructions=(
        "Controls a Rohde & Schwarz HMC8042 two-channel lab power supply (0-32 V, 0-5 A per channel). "
        "This drives real circuits: set voltage and current limit with configure_channel and check them "
        "with get_status BEFORE calling set_output(..., enabled=True). Outputs are off when the server "
        "connects. Use all_outputs_off immediately if something looks wrong (unexpected CC mode, "
        "tripped protection, current far above what the circuit should draw)."
    ),
)

_supply: Supply | None = None
_supply_lock = threading.Lock()

Channel = Literal[1, 2]


def tool(fn):
    """Register fn as a tool, reporting driver/VISA failures to the model as readable errors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface the cause to the model
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return mcp.tool()(wrapper)


def supply() -> Supply:
    """Connect lazily (applying the safe defaults) so the server starts even when the supply is off."""
    global _supply
    with _supply_lock:
        if _supply is None:
            _supply = Supply.from_env()
        return _supply


def _checked(result: dict | None = None) -> dict:
    out = dict(result or {})
    errs = supply().errors()
    out["ok"] = not errs
    if errs:
        out["instrument_errors"] = errs
    return out


# --- system ---------------------------------------------------------------
@tool
def list_instruments() -> list[dict]:
    """List VISA instruments the VISA library can see (USB; LAN sockets are not discoverable)."""
    return transport.list_resources(os.environ.get("HMC_VISA_LIBRARY") or None)


@tool
def identify() -> dict:
    """Manufacturer, model, serial number, firmware and connection address of the supply."""
    return supply().identify()


@tool
def get_status() -> dict:
    """Setpoints, output state, measured voltage/current/power, CV/CC mode and protection state
    of both channels, plus the server's safety caps."""
    return supply().status()


# --- settings ---------------------------------------------------------------
@tool
def configure_channel(
    channel: Channel,
    voltage_v: float | None = None,
    current_limit_a: float | None = None,
    ovp_level_v: float | None = None,
    fuse_enabled: bool | None = None,
) -> dict:
    """Change a channel's settings; only the arguments you pass are changed. Does NOT switch the output.

    voltage_v and current_limit_a are refused if they exceed the server's caps (see get_status
    limits). ovp_level_v sets the overvoltage protection, which switches the output off if the
    voltage rises above it. fuse_enabled turns on the electronic fuse, which switches the output
    off when the current limit is reached instead of regulating to constant current.
    If the output is already on, a new setpoint takes effect immediately.
    """
    s = supply()
    s.configure(channel, voltage_v, current_limit_a, ovp_level_v, fuse_enabled)
    return _checked(s.channel_status(channel))


@tool
def set_output(channel: Literal[1, 2, "all"], enabled: bool) -> dict:
    """Switch a channel's output (or all outputs) on or off.

    Enabling first re-reads the setpoints from the instrument and refuses if they exceed the
    caps. Check the returned mode: "CC" means the current limit is active (load draws more
    than the limit, voltage has dropped), which may indicate a short or wrong wiring.
    """
    s = supply()
    s.set_output(None if channel == "all" else channel, enabled)
    return _checked(s.status())


@tool
def all_outputs_off() -> dict:
    """Emergency off: switch every output off immediately."""
    s = supply()
    s.all_off()
    return _checked(s.status())


@tool
def apply_defaults() -> dict:
    """Return to the safe start-up state: all outputs off, then both channels set to the default
    voltage and current limit (5 V / 100 mA unless configured)."""
    s = supply()
    s.apply_defaults()
    return _checked(s.status())


@tool
def clear_protection(channel: Channel) -> dict:
    """Reset a tripped overvoltage protection on a channel. The output stays off; fix the cause
    before switching it on again."""
    s = supply()
    s.clear_protection(channel)
    return _checked(s.channel_status(channel))


# --- readback -------------------------------------------------------------
@tool
def measure(channel: Channel | None = None) -> list[dict]:
    """Measured output voltage, current and power of one channel, or both if channel is omitted."""
    s = supply()
    return [s.measure(ch) for ch in ((channel,) if channel else (1, 2))]


@tool
def screenshot() -> Image:
    """Capture the supply's display as a PNG image."""
    return Image(data=supply().screenshot(), format="png")


# --- optional raw SCPI ------------------------------------------------------
def scpi_write(command: str) -> dict:
    """Send a raw SCPI command (bypasses the safety caps). See the HMC804x SCPI manual."""
    supply().write(command)
    return _checked({"command": command})


def scpi_query(command: str) -> dict:
    """Send a raw SCPI query and return the text response."""
    return _checked({"command": command, "response": supply().query(command)})


if os.environ.get("HMC_ALLOW_RAW_SCPI") == "1":
    tool(scpi_write)
    tool(scpi_query)


def _connect_at_startup() -> None:
    """Put the supply into its safe state as soon as the server starts, not only on first use."""
    try:
        supply()
    except Exception as exc:  # noqa: BLE001 - supply may be off; tools retry later
        print(f"hmc8042: not connected at startup ({type(exc).__name__}: {exc}); will retry on first use",
              file=sys.stderr)


def main() -> None:
    threading.Thread(target=_connect_at_startup, daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()
