import pytest

from hmc8042_mcp.psu import Supply
from hmc8042_mcp.simulator import SimulatedTransport


@pytest.fixture
def sim():
    t = SimulatedTransport()
    s = Supply(t, max_voltage=12, max_current=1)
    s.apply_defaults()
    return s, t


def test_identify(sim):
    assert sim[0].identify()["model"] == "HMC8042"


def test_defaults_are_5v_100ma_and_off(sim):
    s, t = sim
    st = s.status()
    assert not st["master_enabled"]
    for ch in st["channels"]:
        assert ch["voltage_set_v"] == 5 and ch["current_limit_a"] == 0.1
        assert not ch["channel_enabled"] and not ch["output_active"] and ch["mode"] == "off"
        assert ch["voltage_v"] == 0 and ch["current_a"] == 0
    # outputs are switched off before any setpoint is written
    first_setpoint = next(i for i, c in enumerate(t.log) if c.startswith("SOURce:VOLTage"))
    assert t.log.index("OUTPut:MASTer:STATe OFF") < first_setpoint
    assert s.errors() == []


def test_defaults_applied_on_connect_even_if_supply_was_on(monkeypatch):
    t = SimulatedTransport()
    t.master, t.ch[1]["on"], t.ch[1]["volt"] = True, True, 30.0
    monkeypatch.setenv("HMC_RESOURCE", "SIM")
    monkeypatch.setattr("hmc8042_mcp.simulator.SimulatedTransport", lambda: t)
    s = Supply.from_env()
    st = s.status()
    assert not st["master_enabled"] and st["channels"][0]["voltage_set_v"] == 5


def test_caps_refuse_setpoints(sim):
    s, t = sim
    n = len(t.log)
    with pytest.raises(ValueError, match="HMC_MAX_VOLTAGE"):
        s.configure(1, voltage=12.5)
    with pytest.raises(ValueError, match="HMC_MAX_CURRENT"):
        s.configure(1, current=2)
    with pytest.raises(ValueError):
        s.configure(3, voltage=1)
    assert len(t.log) == n  # nothing was sent


def test_enable_checks_instrument_setpoints(sim):
    s, t = sim
    t.ch[2]["volt"] = 20.0  # changed on the front panel, above the 12 V cap
    with pytest.raises(ValueError, match="channel 2 not enabled"):
        s.set_output(None, True)
    assert not t.master and not t.ch[1]["on"]  # all-or-nothing


def test_cv_and_cc_modes(sim):
    s, _ = sim
    s.set_output(None, True)
    ch1, ch2 = s.status()["channels"]
    assert ch1["mode"] == "CV" and ch1["current_a"] == pytest.approx(0.05)  # 5 V / 100 ohm
    assert ch2["mode"] == "CC" and ch2["voltage_v"] == pytest.approx(1.0)  # 100 mA into 10 ohm
    assert ch1["power_w"] == pytest.approx(0.25)


def test_enable_one_channel_disarms_the_other(sim):
    s, t = sim
    t.ch[2]["on"] = True  # armed from the front panel while master is off
    s.set_output(1, True)
    assert t.master and t.ch[1]["on"] and not t.ch[2]["on"]


def test_off(sim):
    s, t = sim
    s.set_output(None, True)
    s.set_output(2, False)
    assert t.ch[1]["on"] and not t.ch[2]["on"]
    s.all_off()
    assert not t.master and not t.ch[1]["on"]


def test_ovp_trip_and_clear(sim):
    s, t = sim
    s.configure(1, ovp=6)
    s.set_output(1, True)
    s.configure(1, voltage=7)
    st = s.channel_status(1)
    assert st["ovp_tripped"] and not st["output_active"]
    s.clear_protection(1)
    assert not s.channel_status(1)["ovp_tripped"]


def test_bad_caps_rejected():
    with pytest.raises(ValueError):
        Supply(SimulatedTransport(), max_voltage=40)
    with pytest.raises(ValueError):
        Supply(SimulatedTransport(), max_voltage=3)  # default 5 V above the cap


def test_screenshot_is_png(sim):
    assert sim[0].screenshot().startswith(b"\x89PNG")
