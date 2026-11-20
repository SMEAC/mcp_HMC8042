"""Driver for the Rohde & Schwarz HMC8042 two-channel power supply.

Safety model:
  * Setpoints are checked against configurable caps (HMC_MAX_VOLTAGE / HMC_MAX_CURRENT)
    before anything is sent, and re-checked on the instrument before an output is enabled
    (the front panel may have changed them).
  * On connect the supply is put into a known safe state: all outputs off, then both
    channels set to the default voltage / current limit (5 V, 100 mA unless configured).
  * Outputs are only switched on by an explicit call to set_output(..., True).

Physical output = channel state ON *and* master ON. "Off" always switches the master
off first when all channels are requested, so everything drops at once.
"""

from __future__ import annotations

import os
import threading

from .transport import Transport, VisaTransport, resource_from_env

CHANNELS = (1, 2)
INSTRUMENT_MAX_V = 32.05
INSTRUMENT_MAX_A = 5.0
CC_THRESHOLD = 0.98  # measured current >= 98 % of the limit -> constant-current mode

DEFAULT_VOLTAGE = 5.0
DEFAULT_CURRENT = 0.1


def _on(reply: str) -> bool:
    return reply.strip().upper() in ("1", "ON")


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


class Supply:
    def __init__(
        self,
        transport: Transport,
        *,
        max_voltage: float = INSTRUMENT_MAX_V,
        max_current: float = INSTRUMENT_MAX_A,
        default_voltage: float = DEFAULT_VOLTAGE,
        default_current: float = DEFAULT_CURRENT,
    ) -> None:
        if not 0 < max_voltage <= INSTRUMENT_MAX_V:
            raise ValueError(f"max_voltage must be in (0, {INSTRUMENT_MAX_V}] V, got {max_voltage}")
        if not 0 < max_current <= INSTRUMENT_MAX_A:
            raise ValueError(f"max_current must be in (0, {INSTRUMENT_MAX_A}] A, got {max_current}")
        self.max_voltage = max_voltage
        self.max_current = max_current
        self._t = transport
        self._lock = threading.RLock()
        self._check_setpoint(default_voltage, default_current)
        self.default_voltage = default_voltage
        self.default_current = default_current

    @classmethod
    def from_env(cls) -> "Supply":
        """Connect using HMC_* environment variables and apply the safe defaults
        (unless HMC_APPLY_DEFAULTS=0)."""
        library = os.environ.get("HMC_VISA_LIBRARY") or None
        resource = resource_from_env(library)
        if resource.upper() == "SIM":
            from .simulator import SimulatedTransport

            transport: Transport = SimulatedTransport()
        else:
            transport = VisaTransport(resource, library, int(os.environ.get("HMC_TIMEOUT_MS", "5000")))
        try:
            supply = cls(
                transport,
                max_voltage=_env_float("HMC_MAX_VOLTAGE", INSTRUMENT_MAX_V),
                max_current=_env_float("HMC_MAX_CURRENT", INSTRUMENT_MAX_A),
                default_voltage=_env_float("HMC_DEFAULT_VOLTAGE", DEFAULT_VOLTAGE),
                default_current=_env_float("HMC_DEFAULT_CURRENT", DEFAULT_CURRENT),
            )
            if os.environ.get("HMC_APPLY_DEFAULTS", "1") != "0":
                supply.apply_defaults()
        except Exception:
            transport.close()
            raise
        return supply

    # --- raw access -------------------------------------------------------
    def write(self, cmd: str) -> None:
        with self._lock:
            self._t.write(cmd)

    def query(self, cmd: str) -> str:
        with self._lock:
            return self._t.query(cmd)

    def close(self) -> None:
        with self._lock:
            try:
                self._t.write("SYSTem:LOCal")  # give the front panel back
            finally:
                self._t.close()

    def errors(self) -> list[str]:
        """Drain the instrument error queue."""
        errs = []
        with self._lock:
            for _ in range(32):
                e = self._t.query("SYSTem:ERRor?")
                if e.startswith(("+0,", "0,")):
                    break
                errs.append(e)
        return errs

    def identify(self) -> dict:
        # HMC8042 replies "Rohde&Schwarz,HMC8042,<serial>,HW<hw>,SW<fw>"
        parts = [p.strip() for p in self.query("*IDN?").split(",")]
        parts += [""] * (5 - len(parts))
        hw = next((p for p in parts[3:] if p.upper().startswith("HW")), "")
        fw = next((p for p in parts[3:] if p.upper().startswith("SW")), parts[3])
        return {"manufacturer": parts[0], "model": parts[1], "serial": parts[2], "firmware": fw,
                "hardware": hw, "resource": getattr(self._t, "resource", "")}

    def limits(self) -> dict:
        return {
            "max_voltage_v": self.max_voltage,
            "max_current_a": self.max_current,
            "default_voltage_v": self.default_voltage,
            "default_current_a": self.default_current,
        }

    # --- validation -------------------------------------------------------
    @staticmethod
    def _check_channel(ch: int) -> int:
        if ch not in CHANNELS:
            raise ValueError(f"channel must be 1 or 2, got {ch!r}")
        return ch

    def _check_setpoint(self, voltage: float | None, current: float | None) -> None:
        if voltage is not None and not 0 <= voltage <= self.max_voltage:
            raise ValueError(f"voltage {voltage} V outside the allowed range 0..{self.max_voltage} V "
                             "(cap set by HMC_MAX_VOLTAGE)")
        if current is not None and not 0 < current <= self.max_current:
            raise ValueError(f"current limit {current} A outside the allowed range (0, {self.max_current}] A "
                             "(cap set by HMC_MAX_CURRENT)")

    def _select(self, ch: int) -> None:
        self._t.write(f"INSTrument:NSELect {ch}")

    # --- settings ---------------------------------------------------------
    def all_off(self) -> None:
        """Switch every output off: master first (drops everything at once), then each channel."""
        with self._lock:
            self._t.write("OUTPut:MASTer:STATe OFF")
            for ch in CHANNELS:
                self._select(ch)
                self._t.write("OUTPut:CHANnel:STATe OFF")

    def apply_defaults(self) -> None:
        """Safe start-up state: outputs off, then default voltage and current limit on both channels."""
        with self._lock:
            self.all_off()
            for ch in CHANNELS:
                self._select(ch)
                self._t.write(f"SOURce:VOLTage:LEVel:IMMediate:AMPLitude {self.default_voltage:g}")
                self._t.write(f"SOURce:CURRent:LEVel:IMMediate:AMPLitude {self.default_current:g}")

    def configure(
        self,
        ch: int,
        voltage: float | None = None,
        current: float | None = None,
        ovp: float | None = None,
        fuse: bool | None = None,
    ) -> None:
        """Change only the given settings of one channel. Everything is validated before sending."""
        self._check_channel(ch)
        self._check_setpoint(voltage, current)
        if ovp is not None and not 0 < ovp <= INSTRUMENT_MAX_V + 1:
            raise ValueError(f"OVP level must be in (0, {INSTRUMENT_MAX_V + 1}] V, got {ovp}")
        with self._lock:
            self._select(ch)
            # Protection first, so it is already in place when the new setpoint takes effect.
            if ovp is not None:
                self._t.write(f"SOURce:VOLTage:PROTection:LEVel {ovp:g}")
            if fuse is not None:
                self._t.write(f"FUSE:STATe {'ON' if fuse else 'OFF'}")
            if current is not None:
                self._t.write(f"SOURce:CURRent:LEVel:IMMediate:AMPLitude {current:g}")
            if voltage is not None:
                self._t.write(f"SOURce:VOLTage:LEVel:IMMediate:AMPLitude {voltage:g}")

    def set_output(self, ch: int | None, on: bool) -> None:
        """Switch one channel (or all, ch=None) on or off.

        Before switching on, the setpoints are read back from the instrument and checked
        against the caps; nothing is enabled if any requested channel violates them.
        """
        chans = CHANNELS if ch is None else (self._check_channel(ch),)
        with self._lock:
            if not on:
                if ch is None:
                    self.all_off()
                else:
                    self._select(ch)
                    self._t.write("OUTPut:CHANnel:STATe OFF")
                return

            for c in chans:
                self._select(c)
                v = float(self._t.query("SOURce:VOLTage:LEVel:IMMediate:AMPLitude?"))
                i = float(self._t.query("SOURce:CURRent:LEVel:IMMediate:AMPLitude?"))
                try:
                    self._check_setpoint(v, i)
                except ValueError as exc:
                    raise ValueError(f"channel {c} not enabled: instrument setpoint violates the caps: {exc}") from exc

            # If the master is off, a channel left "armed" from the front panel would come on
            # together with the requested one. It isn't outputting now, so disarm it.
            if not _on(self._t.query("OUTPut:MASTer:STATe?")):
                for c in CHANNELS:
                    if c not in chans:
                        self._select(c)
                        self._t.write("OUTPut:CHANnel:STATe OFF")

            for c in chans:
                self._select(c)
                self._t.write("OUTPut:CHANnel:STATe ON")
            self._t.write("OUTPut:MASTer:STATe ON")

    def clear_protection(self, ch: int) -> None:
        """Reset a tripped overvoltage protection on a channel (the output stays off)."""
        self._check_channel(ch)
        with self._lock:
            self._select(ch)
            self._t.write("SOURce:VOLTage:PROTection:CLEar")

    # --- readback ---------------------------------------------------------
    def measure(self, ch: int) -> dict:
        self._check_channel(ch)
        with self._lock:
            self._select(ch)
            v = float(self._t.query("MEASure:SCALar:VOLTage:DC?"))
            i = float(self._t.query("MEASure:SCALar:CURRent:DC?"))
        return {"channel": ch, "voltage_v": v, "current_a": i, "power_w": round(v * i, 6)}

    def channel_status(self, ch: int, master_on: bool | None = None) -> dict:
        self._check_channel(ch)
        with self._lock:
            if master_on is None:
                master_on = _on(self._t.query("OUTPut:MASTer:STATe?"))
            self._select(ch)
            q = self._t.query
            st = {
                "channel": ch,
                "voltage_set_v": float(q("SOURce:VOLTage:LEVel:IMMediate:AMPLitude?")),
                "current_limit_a": float(q("SOURce:CURRent:LEVel:IMMediate:AMPLitude?")),
                "channel_enabled": _on(q("OUTPut:CHANnel:STATe?")),
                "ovp_level_v": float(q("SOURce:VOLTage:PROTection:LEVel?")),
                "ovp_tripped": _on(q("SOURce:VOLTage:PROTection:TRIPped?")),
                "fuse_enabled": _on(q("FUSE:STATe?")),
                "fuse_tripped": _on(q("FUSE:TRIPped?")),
            }
            st.update({k: v for k, v in self.measure(ch).items() if k != "channel"})
        st["output_active"] = st["channel_enabled"] and master_on
        if not st["output_active"]:
            st["mode"] = "off"
        elif st["current_a"] >= CC_THRESHOLD * st["current_limit_a"]:
            st["mode"] = "CC"  # current limit reached, voltage has dropped below the setpoint
        else:
            st["mode"] = "CV"
        return st

    def status(self) -> dict:
        with self._lock:
            master = _on(self._t.query("OUTPut:MASTer:STATe?"))
            return {
                "master_enabled": master,
                "channels": [self.channel_status(ch, master) for ch in CHANNELS],
                "limits": self.limits(),
            }

    def screenshot(self) -> bytes:
        with self._lock:
            self._t.write("HCOPy:FORMat PNG")
            return self._t.query_block("HCOPy:DATA?")
