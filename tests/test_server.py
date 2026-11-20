import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("HMC_RESOURCE", "SIM")
    monkeypatch.setenv("HMC_MAX_VOLTAGE", "12")
    monkeypatch.setenv("HMC_MAX_CURRENT", "1")
    from hmc8042_mcp import server as srv

    monkeypatch.setattr(srv, "_supply", None)
    return srv


def test_tools_registered(server):
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"get_status", "configure_channel", "set_output", "all_outputs_off", "apply_defaults",
            "measure", "clear_protection", "screenshot"} <= names
    assert "scpi_write" not in names


def test_status_reports_caps(server):
    st = server.get_status()
    assert st["limits"]["max_voltage_v"] == 12 and not st["master_enabled"]


def test_configure_then_enable(server):
    out = server.configure_channel(1, voltage_v=3.3, current_limit_a=0.2)
    assert out["ok"] and out["voltage_set_v"] == 3.3 and not out["output_active"]
    out = server.set_output(1, True)
    assert out["ok"] and out["channels"][0]["mode"] == "CV"
    assert server.measure(1)[0]["voltage_v"] == pytest.approx(3.3)
    assert not server.all_outputs_off()["master_enabled"]


def test_cap_violation_reaches_the_model(server):
    with pytest.raises(ToolError, match="HMC_MAX_VOLTAGE"):
        asyncio.run(server.mcp.call_tool("configure_channel", {"channel": 1, "voltage_v": 24}))
