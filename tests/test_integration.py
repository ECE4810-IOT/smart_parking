"""End-to-end: gateway + 2 bays over real HTTP on localhost, mock sensors."""

import time

import pytest

from smartpark.sim import DEMO_USERS, Simulator


def wait_for(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    s = Simulator(n_bays=2, base_port=19300, kiosk_port=19390, data_dir=str(tmp_path_factory.mktemp("sim")),
                  fast_kdf=True)
    s.tg.echo = False
    s.start()
    assert s.wait_connected(20)
    yield s
    s.stop()


def test_access_flow_and_authorised_parking(sim):
    r = sim.kiosk_access("alice")
    assert r["ok"] and r["bay"] in ("bay1", "bay2")
    i = int(r["bay"][-1])
    assert wait_for(lambda: sim.gw.bay_status(r["bay"]) == "assigned")
    sim.sensors[i].arrive(duration=1.0)
    assert wait_for(lambda: sim.gw.bay_status(r["bay"]) == "occupied", 20)
    assert sim.bays[i].lights.pattern == "occupied"
    texts = [m["text"] for m in sim.tg.sent if m["chat_id"] == DEMO_USERS["alice"][0]]
    assert any("Parked at" in t for t in texts)
    sim.sensors[i].leave(duration=1.0)
    assert wait_for(lambda: sim.gw.bay_status(r["bay"]) == "vacant", 20)


def test_unauthorised_parking_alert(sim):
    free = sim.gw.free_bays()
    i = int(free[0][-1])
    sim.sensors[i].arrive(duration=1.0)
    assert wait_for(lambda: sim.gw.bay_status(free[0]) == "unauthorised", 20)
    assert any("Unauthorised" in m["text"] for m in sim.tg.sent if m["chat_id"] == sim.gw.admin_chat)
    sim.sensors[i].leave(duration=1.0)
    assert wait_for(lambda: sim.gw.bay_status(free[0]) == "vacant", 20)


def test_wrong_pin_rejected(sim):
    r = sim.kiosk_access("bob", "111112")
    assert not r["ok"] and r["attempts_left"] == 2


def test_telegram_roles(sim):
    sim.tg.inject("55555", "/status all")      # stranger
    assert "register" in sim.tg.sent[-1]["text"]
    sim.tg.inject(sim.gw.admin_chat, "/status all")
    assert wait_for(lambda: "bay(s) free" in sim.tg.sent[-1]["text"])


def test_session_recovery_after_gateway_forgets(sim):
    node = sim.gw.nodes["bay1"]
    node.session = None                          # e.g. gateway restarted
    assert wait_for(lambda: sim.gw.nodes["bay1"].session is not None, 15)


def test_permit_survives_bay_reboot(sim):
    sim.gw.access._last_request.clear()
    r = sim.kiosk_access("bob")
    assert r["ok"]
    i = int(r["bay"][-1])
    sim.stop_bay(i)
    sim.start_bay(i)
    assert wait_for(lambda: sim.bays[i].assignment is not None, 15)
    assert sim.bays[i].assignment["user"] == "bob"
