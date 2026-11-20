"""In-memory stand-in for the HMC8042, for development without hardware.

Each channel drives a fixed resistive load (CH1 100 ohm, CH2 10 ohm). With the 5 V / 100 mA
defaults CH1 runs in CV (50 mA) and CH2 hits the current limit (CC, 1 V at 100 mA).
OVP trips (and switches the channel off) when the output voltage would exceed the OVP level.
"""

from __future__ import annotations

import re
import struct
import zlib

LOAD_OHMS = {1: 100.0, 2: 10.0}
MAX_V = 32.05
MAX_A = 5.0


def _short(header: str) -> str:
    """SCPI long/mixed form -> short form: 'SOURce:VOLTage' -> 'SOUR:VOLT'."""
    return ":".join(re.sub(r"[a-z]", "", node) for node in header.strip(":").split(":"))


def _tiny_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b"")


def _channel_defaults() -> dict:
    return {"volt": 0.0, "curr": 0.1, "on": False, "ovp": MAX_V + 1, "ovp_trip": False,
            "fuse": False, "fuse_trip": False}


class SimulatedTransport:
    resource = "SIM"

    def __init__(self) -> None:
        self.ch = {c: _channel_defaults() for c in LOAD_OHMS}
        self.selected = 1
        self.master = False
        self.errors: list[str] = []
        self.log: list[str] = []
        self.hcopy_format = "BMP"

    # -- model ------------------------------------------------------------
    def _active(self, c: int) -> bool:
        return self.master and self.ch[c]["on"]

    def _output(self, c: int) -> tuple[float, float]:
        s = self.ch[c]
        if not self._active(c):
            return 0.0, 0.0
        i = s["volt"] / LOAD_OHMS[c]
        if i > s["curr"]:  # constant current
            return s["curr"] * LOAD_OHMS[c], s["curr"]
        return s["volt"], i

    def _check_ovp(self) -> None:
        for s in self.ch.values():
            if s["on"] and self.master and s["volt"] > s["ovp"]:
                s["ovp_trip"], s["on"] = True, False

    def _set_number(self, key: str, value: str, lo: float, hi: float) -> None:
        try:
            v = float(value)
        except ValueError:
            self.errors.append('-104,"Data type error"')
            return
        if not lo <= v <= hi:
            self.errors.append('-222,"Data out of range"')
            return
        self.ch[self.selected][key] = v
        self._check_ovp()

    # -- Transport interface -------------------------------------------------
    def write(self, cmd: str) -> None:
        self.log.append(cmd)
        header, _, arg = cmd.strip().partition(" ")
        h, arg, s = _short(header), arg.strip(), self.ch[self.selected]
        if h == "*RST":
            self.__init__()
        elif h == "INST:NSEL":
            if arg in ("1", "2"):
                self.selected = int(arg)
            else:
                self.errors.append('-222,"Data out of range"')
        elif h == "SOUR:VOLT:LEV:IMM:AMPL":
            self._set_number("volt", arg, 0, MAX_V)
        elif h == "SOUR:CURR:LEV:IMM:AMPL":
            self._set_number("curr", arg, 0.0005, MAX_A)
        elif h == "SOUR:VOLT:PROT:LEV":
            self._set_number("ovp", arg, 0, MAX_V + 1)
        elif h == "SOUR:VOLT:PROT:CLE":
            s["ovp_trip"] = False
        elif h == "FUSE:STAT":
            s["fuse"] = arg.upper() in ("1", "ON")
        elif h == "OUTP:CHAN:STAT":
            s["on"] = arg.upper() in ("1", "ON") and not s["ovp_trip"]
            self._check_ovp()
        elif h == "OUTP:MAST:STAT":
            self.master = arg.upper() in ("1", "ON")
            self._check_ovp()
        elif h in ("SYST:LOC", "SYST:REM", "HCOP:FORM"):
            if h == "HCOP:FORM":
                self.hcopy_format = arg.upper()
        else:
            self.errors.append(f'-113,"Undefined header;{cmd}"')

    def query(self, cmd: str) -> str:
        self.log.append(cmd)
        h = _short(cmd.strip().rstrip("?"))
        s = self.ch[self.selected]
        if h == "*IDN":
            return "Rohde&Schwarz,HMC8042,SIM000001,HW42000000,SW01.303"
        if h == "*OPC":
            return "1"
        if h == "SYST:ERR":
            return self.errors.pop(0) if self.errors else '0,"No error"'
        values = {
            "SOUR:VOLT:LEV:IMM:AMPL": f"{s['volt']:.3f}",
            "SOUR:CURR:LEV:IMM:AMPL": f"{s['curr']:.4f}",
            "SOUR:VOLT:PROT:LEV": f"{s['ovp']:.3f}",
            "SOUR:VOLT:PROT:TRIP": str(int(s["ovp_trip"])),
            "FUSE:STAT": str(int(s["fuse"])),
            "FUSE:TRIP": str(int(s["fuse_trip"])),
            "OUTP:CHAN:STAT": str(int(s["on"])),
            "OUTP:MAST:STAT": str(int(self.master)),
            "MEAS:SCAL:VOLT:DC": f"{self._output(self.selected)[0]:.4f}",
            "MEAS:SCAL:CURR:DC": f"{self._output(self.selected)[1]:.5f}",
        }
        if h in values:
            return values[h]
        self.errors.append(f'-113,"Undefined header;{cmd}"')
        raise TimeoutError(f"simulator: no reply to {cmd!r}")  # real instrument just stays silent

    def query_block(self, cmd: str) -> bytes:
        self.log.append(cmd)
        if _short(cmd.rstrip("?")) == "HCOP:DATA":
            return _tiny_png()
        raise TimeoutError(f"simulator: no reply to {cmd!r}")

    def close(self) -> None:
        pass
