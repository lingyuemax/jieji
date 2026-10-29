"""模拟身体：动作直接作用在 sim.py 的小车上。

关键点是「不堵主循环」：execute() 立刻返回，真正的运动交给一个后台任务，
按 100ms 一格推进模拟器，时间到了自己停。这样即使大模型给了一个 2.5 秒的
长动作，主循环仍然能看画面、能急停、能画曲奇。
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..actions import Action
from ..sim import Sim
from .base import Driver, ExecResult

log = logging.getLogger("jieji.driver.mock")


class MockDriver(Driver):
    name = "mock"
    label = "模拟小车（无硬件）"
    capabilities = {"drive": True, "press": False, "buttons": []}

    def __init__(self, sim: Sim | None = None) -> None:
        self.sim = sim or Sim()
        self._task: asyncio.Task | None = None
        self._busy = False
        self._last: ExecResult | None = None
        self.moved_total = 0.0

    def connection(self) -> dict:
        return {"connected": True, "link": "in-process"}

    async def execute(self, action: Action) -> ExecResult:
        if action.action == "stop" or action.action == "say" or action.action == "press":
            await self.halt("新指令")
            self._last = ExecResult(
                ok=True,
                detail="说话" if action.action == "say" else ("按键（模拟身体没有键）" if action.action == "press" else "停止"),
                duration_ms=0,
                state=self.telemetry(),
            )
            return self._last

        await self.halt("切换动作")
        self._busy = True
        self._task = asyncio.create_task(self._drive(action))
        return ExecResult(
            ok=True,
            detail=f"{action.action} {action.speed:.2f} × {action.duration_ms}ms",
            duration_ms=action.duration_ms,
            state=self.telemetry(),
        )

    async def _drive(self, action: Action) -> None:
        deadline = time.time() + action.duration_ms / 1000.0
        moved = 0.0
        try:
            while time.time() < deadline:
                result = self.sim.step(action.action, action.speed, 0.1)
                moved += result["moved_m"]
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        finally:
            self._busy = False
            self.moved_total += moved
            self._last = ExecResult(
                ok=True,
                detail=f"{action.action} 完成，走了 {moved:.2f}m",
                duration_ms=action.duration_ms,
                moved_m=moved,
                state=self.telemetry(),
            )

    async def halt(self, reason: str = "") -> ExecResult:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._busy = False
        return ExecResult(ok=True, detail=reason or "停", state=self.telemetry())

    def telemetry(self) -> dict:
        snap = self.sim.snapshot()
        return {
            "battery": snap["battery"],
            "front_m": snap["front_m"],
            "heading_deg": snap["heading_deg"],
            "x": snap["x"],
            "y": snap["y"],
            "bump": snap["bump"],
            "odometer_m": snap["odometer_m"],
            "moving": self._busy,
            "last_event": snap["last_event"],
        }
