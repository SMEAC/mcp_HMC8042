"""Low-level I/O: a real VISA transport and resolution of the supply address."""

from __future__ import annotations

import os
import re
import sys
from typing import Protocol

# Matches the R&S HMC804x family, e.g. "Rohde&Schwarz,HMC8042,...".
IDN_PATTERN = re.compile(r"HMC804\d", re.IGNORECASE)


class Transport(Protocol):
    def write(self, cmd: str) -> None: ...
    def query(self, cmd: str) -> str: ...
    def query_block(self, cmd: str) -> bytes: ...
    def close(self) -> None: ...


def _refresh_windows_path() -> None:
    """Append PATH entries from the registry that this process doesn't have yet.

    VISA vendors (Keysight, NI) add their DLL folders to the system PATH at install time.
    Processes started earlier (e.g. an editor that launches this server) keep the old PATH,
    and viOpenDefaultRM then fails with VI_ERROR_LIBRARY_NFOUND.
    """
    if sys.platform != "win32":
        return
    import winreg

    current = os.environ.get("PATH", "")
    have = {p.rstrip("\\/").lower() for p in current.split(os.pathsep) if p}
    extra = []
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ]
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
        except OSError:
            continue
        for p in os.path.expandvars(value).split(os.pathsep):
            if p and p.rstrip("\\/").lower() not in have:
                have.add(p.rstrip("\\/").lower())
                extra.append(p)
    if extra:
        os.environ["PATH"] = os.pathsep.join([current, *extra]) if current else os.pathsep.join(extra)


def _resource_manager(visa_library: str | None):
    import pyvisa

    _refresh_windows_path()

    if visa_library:
        return pyvisa.ResourceManager(visa_library)
    try:
        return pyvisa.ResourceManager()  # system VISA (Keysight IO Libraries, NI-VISA)
    except (OSError, ValueError):
        return pyvisa.ResourceManager("@py")  # pure-Python fallback


class VisaTransport:
    def __init__(self, resource: str, visa_library: str | None = None, timeout_ms: int = 5000):
        self._rm = _resource_manager(visa_library)
        self._inst = self._rm.open_resource(resource)
        self._inst.timeout = timeout_ms
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"
        self._inst.chunk_size = 1024 * 1024
        self.resource = resource

    def write(self, cmd: str) -> None:
        self._inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

    def query_block(self, cmd: str) -> bytes:
        return self._inst.query_binary_values(cmd, datatype="B", container=bytes)

    def close(self) -> None:
        try:
            self._inst.close()
        finally:
            self._rm.close()


def list_resources(visa_library: str | None = None) -> list[dict[str, str]]:
    """List VISA resources and, where possible, their *IDN? strings."""
    rm = _resource_manager(visa_library)
    out = []
    try:
        for res in rm.list_resources():
            entry = {"resource": res, "idn": ""}
            try:
                inst = rm.open_resource(res)
                inst.timeout = 2000
                entry["idn"] = inst.query("*IDN?").strip()
                inst.close()
            except Exception as exc:  # noqa: BLE001 - probing unknown devices
                entry["idn"] = f"<no response: {type(exc).__name__}>"
            out.append(entry)
    finally:
        rm.close()
    return out


def find_supply(visa_library: str | None = None) -> str:
    """Return the VISA address of the first HMC804x the VISA library can list (USB only)."""
    for entry in list_resources(visa_library):
        if IDN_PATTERN.search(entry["idn"]):
            return entry["resource"]
    raise RuntimeError(
        "No HMC804x found. For LAN set HMC_HOST to the supply's IP address (see the "
        "instrument's SETUP > Interface menu), or set HMC_RESOURCE to a full VISA address."
    )


def resource_from_env(visa_library: str | None = None) -> str:
    """HMC_RESOURCE (VISA address or "SIM") wins; else HMC_HOST[:HMC_PORT] -> raw SCPI socket."""
    resource = os.environ.get("HMC_RESOURCE", "").strip()
    if resource:
        return resource
    host = os.environ.get("HMC_HOST", "").strip()
    if host:
        port = int(os.environ.get("HMC_PORT", "5025"))
        return f"TCPIP0::{host}::{port}::SOCKET"
    return find_supply(visa_library)
