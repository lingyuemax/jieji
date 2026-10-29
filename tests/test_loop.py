"""端到端：真跑一遍主循环（假模型 + 模拟身体），确保"看→想→动"接得上。"""
from __future__ import annotations

import asyncio
import json
import time

from app.actions import normalize
from app.config import Settings
from app.llm import LLMResult
from app.loop import AgentLoop
from app.vision import VisionCache


class FakeLLM:
    def __init__(self, payload: dict, delay: float = 0.0):
        self.payload = payload
        self.delay = delay
        self.calls = 0
        self.prompts: list[str] = []

    async def chat(self, system, user, **kwargs):
        self.calls += 1
        self.prompts.append(user if isinstance(user, str) else json.dumps(user, ensure_ascii=False))
        if self.delay:
            await asyncio.sleep(self.delay)
        return LLMResult(text=json.dumps(self.payload, ensure_ascii=False), model="fake", latency_ms=3, tokens=42)

    def stats(self):
        return {"calls": self.calls}

    async def close(self):
        return None


def make_loop(payload: dict | None = None, tmp_path=None, delay: float = 0.0) -> tuple[AgentLoop, FakeLLM]:
    s = Settings()
    s.camera_source = "mock"
    s.audio_source = "mock"
    s.asr_provider = "mock"
    s.driver = "mock"
    s.record_episodes = False
    s.tick_ms = 80
    s.camera_fps = 2
    s.min_action_interval_ms = 0
    s.max_speed = 1.0
    s.stale_frame_ms = 5000
    s.wake_required = False
    loop = AgentLoop(s)
    loop.cache = VisionCache(tmp_path / "cache") if tmp_path else loop.cache
    loop.brain.cache = loop.cache
    fake = FakeLLM(payload or {
        "say": "走了", "mood": "专注", "action": "forward",
        "speed": 0.8, "duration_ms": 500, "why": "前方通畅",
    }, delay=delay)
    loop.llm = fake
    loop.brain.llm = fake
    return loop, fake


async def wait_until(predicate, timeout=6.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


def test_loop_executes_forward(tmp_path):
    loop, fake = make_loop(tmp_path=tmp_path)

    async def run():
        await loop.start()
        try:
            assert await wait_until(lambda: loop.camera.latest() is not None, 3)
            x0 = loop.sim.state.x
            await loop.submit_text("往前走一点")
            ok = await wait_until(lambda: bool(loop._actions), 8)
            assert ok, "一直没有动作"
            assert loop._actions[-1]["action"]["action"] == "forward"
            await asyncio.sleep(0.9)
            assert loop.sim.state.x > x0, "说走就得真走"
            assert fake.calls >= 1
        finally:
            await loop.stop()

    asyncio.run(run())


def test_loop_estop_blocks_motion(tmp_path):
    loop, _ = make_loop(tmp_path=tmp_path)

    async def run():
        await loop.start()
        try:
            await wait_until(lambda: loop.camera.latest() is not None, 3)
            await loop.estop("测试")
            x0 = loop.sim.state.x
            result = await loop.manual({"action": "forward", "speed": 0.8, "duration_ms": 500})
            await asyncio.sleep(0.6)
            assert result.get("stopped") is True
            assert abs(loop.sim.state.x - x0) < 1e-6, "急停时不该动"
        finally:
            await loop.stop()

    asyncio.run(run())


def test_loop_reflex_stops_before_obstacle(tmp_path):
    loop, _ = make_loop(tmp_path=tmp_path)

    async def run():
        await loop.start()
        try:
            # 把车怼到路人面前
            loop.sim.state.x = 2.20
            loop.sim.state.y = 3.35
            loop.sim.state.theta = 0.0
            loop.sim.state.distance_m = loop.sim.front_distance()
            assert loop.sim.state.distance_m < 0.4
            payload = await loop._apply(normalize({"action": "forward", "speed": 0.5, "duration_ms": 500}))
            assert payload["action"]["action"] == "stop"
            assert payload["action"]["source"] == "reflex"
        finally:
            await loop.stop()

    asyncio.run(run())


def test_loop_auto_mode_thinks_periodically(tmp_path):
    loop, fake = make_loop(tmp_path=tmp_path)

    async def run():
        await loop.start()
        try:
            await wait_until(lambda: loop.camera.latest() is not None, 3)
            loop.settings.decision_interval_ms = 300
            await loop.set_mode("auto")
            ok = await wait_until(lambda: fake.calls >= 2, 8)
            assert ok, "自动模式下应该自己反复思考"
            assert loop.mode == "auto"
        finally:
            await loop.stop()

    asyncio.run(run())


def test_snapshot_contract(tmp_path):
    loop, _ = make_loop(tmp_path=tmp_path)

    async def run():
        await loop.start()
        try:
            await wait_until(lambda: loop.camera.latest() is not None, 3)
            snap = loop.snapshot()
            for key in ("started", "mode", "camera", "audio", "driver", "safety", "brain", "sim"):
                assert key in snap
            assert snap["camera"]["source"] == "mock"
            assert snap["driver"]["name"] == "mock"
        finally:
            await loop.stop()

    asyncio.run(run())


def test_second_text_while_thinking_is_not_dropped(tmp_path):
    """回归：思考期间来的第二句话曾经会被永远挂着（idle 模式下没人再触发）。"""
    loop, fake = make_loop(tmp_path=tmp_path, delay=0.35)

    async def run():
        await loop.start()
        try:
            await wait_until(lambda: loop.camera.latest() is not None, 3)
            await loop.submit_text("往前走")
            await asyncio.sleep(0.05)          # 保证第一轮已经在思考
            await loop.submit_text("再往前一点")
            ok = await wait_until(lambda: fake.calls >= 2, 6)
            assert ok, "第二句指令被吞了"
            prompts = "\n".join(fake.prompts)
            assert "再往前一点" in prompts
        finally:
            await loop.stop()

    asyncio.run(run())
