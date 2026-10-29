"""身体：模拟驱动、红外遥控映射、能力协商。"""
from __future__ import annotations

import asyncio
import json
import time

from app.actions import normalize
from app.config import Settings
from app.drivers.mock import MockDriver
from app.drivers.remote import IRDriver, load_codes, save_codes
from app.sim import Sim


# ------------------------------------------------------------------ mock
def test_mock_driver_moves_sim():
    sim = Sim(160, 120)
    driver = MockDriver(sim)

    async def run():
        x0 = sim.state.x
        result = await driver.execute(normalize({"action": "forward", "speed": 0.6, "duration_ms": 500}))
        assert result.ok
        await asyncio.sleep(0.7)
        assert sim.state.x > x0
        await driver.halt("测试结束")

    asyncio.run(run())


def test_mock_driver_execute_returns_immediately():
    sim = Sim(160, 120)
    driver = MockDriver(sim)

    async def run():
        t0 = time.time()
        await driver.execute(normalize({"action": "forward", "speed": 0.6, "duration_ms": 2000}))
        elapsed = time.time() - t0
        assert elapsed < 0.5, "execute 不能把主循环堵住 2 秒"
        assert driver.telemetry()["moving"] is True
        await driver.halt()
        assert driver.telemetry()["moving"] is False

    asyncio.run(run())


def test_mock_driver_stop():
    sim = Sim(160, 120)
    driver = MockDriver(sim)

    async def run():
        await driver.execute(normalize({"action": "forward", "speed": 0.6, "duration_ms": 300}))
        result = await driver.halt("停")
        assert result.ok
        assert driver.telemetry()["moving"] is False

    asyncio.run(run())


# ------------------------------------------------------------------ 红外
class FakeBridge:
    """假桥：只记录收到什么，不真的发红外。"""

    def __init__(self):
        self.sent: list[dict] = []
        self.connected = True
        self.device_status: dict = {}

    async def open(self):
        return None

    async def close(self):
        return None

    async def send(self, payload):
        self.sent.append(payload)
        return True

    async def request_learn(self, timeout=12.0):
        return {"code": "0x20DF10EF", "protocol": "NEC", "bits": 32}

    def status(self):
        return {"connected": self.connected, "url": "ws://fake"}


def make_ir(tmp_path, codes):
    path = tmp_path / "ir_codes.json"
    save_codes(codes, path)
    monkey_loaded = load_codes(path)
    settings = Settings()
    settings.ir_repeat_interval_ms = 100
    bridge = FakeBridge()
    driver = IRDriver(bridge, settings)
    driver.codes = monkey_loaded
    return driver, bridge


def test_ir_sends_repeat_for_motion(tmp_path):
    driver, bridge = make_ir(tmp_path, {
        "forward": {"slot": "up", "code": "0x1", "protocol": "NEC", "bits": 32},
        "stop": {"slot": "ok", "code": "0x2", "protocol": "NEC", "bits": 32},
    })

    async def run():
        result = await driver.execute(normalize({"action": "forward", "speed": 0.5, "duration_ms": 500}))
        assert result.ok
        assert bridge.sent[0]["cmd"] == "ir_send"
        assert bridge.sent[0]["code"] == "0x1"
        assert bridge.sent[0]["repeat"] >= 1
        await asyncio.sleep(0.7)
        assert any(msg.get("code") == "0x2" for msg in bridge.sent), "时长到了应该按停止键"

    asyncio.run(run())


def test_ir_refuses_unmapped_action(tmp_path):
    driver, _ = make_ir(tmp_path, {})

    async def run():
        result = await driver.execute(normalize({"action": "forward", "speed": 0.5}))
        assert not result.ok
        assert "没绑定" in result.detail

    asyncio.run(run())


def test_ir_capabilities_follow_learned_codes(tmp_path):
    driver, _ = make_ir(tmp_path, {})
    assert driver.capabilities["drive"] is False
    driver.codes = {"forward": {"slot": "up"}, "stop": {"slot": "ok"}}
    assert driver.capabilities["drive"] is True
    assert "forward" in driver.capabilities["buttons"]


def test_ir_learn_saves_code(tmp_path):
    driver, _ = make_ir(tmp_path, {})

    async def run():
        result = await driver.learn("ok")
        assert result["ok"]
        assert driver.codes["ok"]["code"] == "0x20DF10EF"

    asyncio.run(run())


def test_save_and_load_codes(tmp_path):
    path = tmp_path / "codes.json"
    save_codes({"forward": {"slot": "up", "code": "0x1"}}, path)
    loaded = load_codes(path)
    assert loaded["forward"]["code"] == "0x1"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "_meta" in raw


def test_driver_registry(tmp_path):
    from app.drivers import build_driver

    s = Settings()
    s.driver = "mock"
    assert build_driver(s, Sim(80, 60)).name == "mock"
    s.driver = "none"
    assert build_driver(s).name == "none"
    s.driver = "ir"
    driver = build_driver(s)
    assert driver.name == "ir"
    asyncio.run(driver.close())
