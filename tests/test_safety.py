"""安全层：这是唯一能拦住"大模型想撞墙"的地方，所以要一条条测。"""
from __future__ import annotations

import time

from app.actions import normalize, stop_action
from app.config import Settings
from app.safety import SafetyGate


def make_gate(**kwargs) -> SafetyGate:
    s = Settings()
    s.max_speed = 0.5
    s.max_duration_ms = 1000
    s.min_action_interval_ms = 200
    s.watchdog_ms = 3000
    for key, value in kwargs.items():
        setattr(s, key, value)
    return SafetyGate(s)


def test_clamps_speed_and_duration():
    gate = make_gate()
    verdict = gate.check(normalize({"action": "forward", "speed": 0.9, "duration_ms": 5000}))
    assert verdict.allowed
    assert verdict.action.speed == 0.5
    assert verdict.action.duration_ms == 1000


def test_zero_speed_motion_becomes_stop():
    gate = make_gate()
    verdict = gate.check(normalize({"action": "forward", "speed": 0.0}))
    assert not verdict.allowed
    assert verdict.action.action == "stop"
    assert verdict.forced


def test_rate_limit_blocks_second_action():
    gate = make_gate()
    now = time.time()
    first = gate.check(normalize({"action": "forward", "speed": 0.3}), now=now)
    second = gate.check(normalize({"action": "forward", "speed": 0.3}), now=now + 0.05)
    assert first.allowed
    assert not second.allowed
    assert "太密" in second.reason


def test_stop_is_never_rate_limited():
    gate = make_gate()
    now = time.time()
    gate.check(normalize({"action": "forward"}), now=now)
    verdict = gate.check(stop_action("测试"), now=now + 0.01)
    assert verdict.allowed


def test_estop_blocks_everything_but_stop():
    gate = make_gate()
    gate.estop("测试急停")
    blocked = gate.check(normalize({"action": "forward", "speed": 0.4}))
    assert not blocked.allowed
    assert blocked.action.action == "stop"
    assert "急停" in blocked.reason
    # 急停状态下 stop 仍然允许
    assert gate.check(stop_action("停车")).allowed
    gate.clear_estop()
    assert gate.check(normalize({"action": "forward"}), now=time.time() + 10).allowed


def test_watchdog_silent_when_idle():
    gate = make_gate()
    assert gate.watchdog(moving=False) is None
    assert gate.watchdog(moving=True) is None  # 没动作过，不该乱停


def test_watchdog_stops_overrunning_action():
    gate = make_gate()
    now = time.time()
    gate.check(normalize({"action": "forward", "duration_ms": 300}), now=now)
    assert gate.watchdog(now + 0.5, moving=True) is None
    verdict = gate.watchdog(now + 5.0, moving=True)
    assert verdict is not None
    assert verdict.action.action == "stop"
    assert verdict.reason == "action_timeout"


def test_watchdog_brain_stall_in_auto_mode():
    gate = make_gate(watchdog_ms=1000)
    now = time.time()
    gate.check(normalize({"action": "forward", "duration_ms": 300}), now=now)
    verdict = gate.watchdog(now + 2.0, moving=True, last_decision_at=now, mode="auto")
    assert verdict is not None
    assert verdict.reason in ("brain_stall", "action_timeout")


def test_capability_drive_and_press():
    caps = {"drive": False, "press": True, "buttons": ["ok"]}
    assert "只能按键" in SafetyGate.check_capability(normalize({"action": "forward"}), caps)
    assert SafetyGate.check_capability(normalize({"action": "press", "button": "ok"}), caps) == ""
    assert "没有学会" in SafetyGate.check_capability(normalize({"action": "press", "button": "x"}), caps)
    assert "缺少 button" in SafetyGate.check_capability(normalize({"action": "press"}), caps)


def test_snapshot_reports_counters():
    gate = make_gate()
    gate.check(normalize({"action": "forward"}))
    gate.check(normalize({"action": "forward"}), now=time.time() + 10)
    snap = gate.snapshot()
    assert snap["actions"] >= 2
    assert isinstance(snap["events"], list)
