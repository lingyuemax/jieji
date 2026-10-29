"""驱动层接口：把「动作」变成某个具体身体的电信号。

四个实现，从零硬件到真机器人：

  mock   模拟器（默认，没硬件也能跑完整闭环）
  ir     红外遥控：学一次遥控器的码，之后发同样的码（NEC 之类的玩具机器人最省事）
  button 舵机压按钮：对着一台真遥控器压，任何遥控器都能用，零协议知识
  serial 直接驱动电机：跳过遥控器，串口给 Arduino/ESP32 下 PWM

所有驱动都遵守两条约定：
1. execute() 必须**立刻返回**，不能把主循环堵住；动作时长由驱动自己收尾；
2. halt() 必须随时能停，且停完要回一个 stop 的真实回执。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..actions import Action


@dataclass
class ExecResult:
    ok: bool
    detail: str = ""
    duration_ms: int = 0
    moved_m: float = 0.0
    button: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "moved_m": round(self.moved_m, 3),
            "button": self.button,
            "state": self.state,
            "at": round(self.at, 3),
        }


class Driver:
    """身体。子类只需要把这几个方法填上。"""

    name = "base"
    label = "抽象身体"
    capabilities: dict[str, Any] = {"drive": False, "press": False, "buttons": []}

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, action: Action) -> ExecResult:
        raise NotImplementedError

    async def halt(self, reason: str = "") -> ExecResult:
        raise NotImplementedError

    def telemetry(self) -> dict[str, Any]:
        return {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "capabilities": self.capabilities,
            "telemetry": self.telemetry(),
            "connection": self.connection(),
        }

    def connection(self) -> dict[str, Any]:
        return {"connected": True}


class NullDriver(Driver):
    """什么都不做的身体：只让大脑说话，不动。"""

    name = "none"
    label = "只说话（不接身体）"
    capabilities = {"drive": False, "press": False, "buttons": []}

    async def execute(self, action: Action) -> ExecResult:
        return ExecResult(ok=True, detail="只说话，不执行动作", state={})

    async def halt(self, reason: str = "") -> ExecResult:
        return ExecResult(ok=True, detail=reason or "停")
