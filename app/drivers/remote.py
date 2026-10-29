"""遥控器驱动：红外学习回放 + 舵机压按钮。

这两种是「在不拆机器人、不逆向 2.4G 协议」的前提下最靠谱的路子：

  红外（ir）：把遥控器对着接收头按一次，记下这串码；以后发同一串码。
              NEC 之类的协议就是几十个脉冲，ESP32 的 RMT 外设天生干这个。
  舵机（button）：把遥控器夹在架子上，用舵机压真按键。
              私有 2.4G / 蓝牙 / 射频遥控全都能用，因为「按」这个动作没变。

两种都只需要记住「哪个动作对应哪个键」，其余交给单片机。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from .. import config as C
from ..actions import Action
from .base import Driver, ExecResult
from .bridge import Bridge

log = logging.getLogger("jieji.driver.remote")

DIRECTION_SLOTS = ("forward", "backward", "turn_left", "turn_right", "stop")
SLOT_LABELS = {
    "forward": "前进",
    "backward": "后退",
    "turn_left": "左转",
    "turn_right": "右转",
    "stop": "停止/刹车",
}


def load_codes(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or C.IR_CODES_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def save_codes(codes: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    path = path or C.IR_CODES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"_meta": {"updated": time.time()}, **codes}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class RemoteDriver(Driver):
    """遥控器类身体的共同部分：按键表 + 按下/长按/松开。"""

    name = "remote"
    label = "遥控器"
    send_cmd = "ir_send"
    single_cmd = "press"

    def __init__(self, bridge: Bridge, settings: C.Settings) -> None:
        self.bridge = bridge
        self.settings = settings
        self.codes = load_codes()
        self._release_task: asyncio.Task | None = None

    # ------------------------------------------------------------ 生命周期
    async def open(self) -> None:
        await self.bridge.open()

    async def close(self) -> None:
        await self.bridge.close()

    # ------------------------------------------------------------ 能力
    @property
    def capabilities(self) -> dict[str, Any]:  # type: ignore[override]
        has_dir = any(slot in self.codes for slot in DIRECTION_SLOTS[:4])
        directional = [slot for slot in DIRECTION_SLOTS if slot in self.codes]
        return {
            "drive": has_dir,
            "press": True,
            "buttons": sorted(self.codes.keys()),
            "mapped": {slot: self.codes[slot].get("slot", "") for slot in directional},
            "kind": self.name,
        }

    def connection(self) -> dict[str, Any]:
        return self.bridge.status()

    def telemetry(self) -> dict[str, Any]:
        return {
            "connected": self.bridge.connected,
            "front_m": (self.bridge.device_status.get("front_cm") or 0) / 100.0 or None,
            "battery": self.bridge.device_status.get("battery"),
            "mapped": len(self.codes),
        }

    # ------------------------------------------------------------ 执行
    def _payload(self, slot: str, record: dict[str, Any]) -> dict[str, Any]:
        return {"cmd": self.send_cmd, "slot": record.get("slot") or slot, **{
            k: record[k] for k in ("code", "protocol", "bits", "servo", "angle") if k in record
        }}

    async def execute(self, action: Action) -> ExecResult:
        if action.action == "say":
            return ExecResult(ok=True, detail="说话")

        if action.action == "stop":
            return await self.halt("模型要求停下")

        if action.action == "press":
            record = self.codes.get(action.button)
            if not record:
                return ExecResult(ok=False, detail=f"还没学过按键「{action.button}」")
            if self.name == "ir":
                payload = {**self._payload(action.button, record), "cmd": self.send_cmd,
                           "repeat": 1, "interval_ms": self.settings.ir_repeat_interval_ms}
            else:
                payload = {**self._payload(action.button, record), "cmd": self.single_cmd,
                           "hold_ms": action.duration_ms or self.settings.press_hold_ms}
            ok = await self.bridge.send(payload)
            return ExecResult(ok=ok, detail=("按下 " if ok else "桥没连上，按不下去 ") + action.button,
                              button=action.button, duration_ms=action.duration_ms)

        record = self.codes.get(action.action)
        if not record:
            return ExecResult(ok=False, detail=f"「{SLOT_LABELS.get(action.action, action.action)}」还没绑定按键")

        interval = max(40, self.settings.ir_repeat_interval_ms)
        repeat = max(1, min(60, int(action.duration_ms / interval)))
        if self.name == "ir":
            payload = {**self._payload(action.action, record), "cmd": self.send_cmd,
                       "repeat": repeat, "interval_ms": interval}
        else:
            payload = {**self._payload(action.action, record), "cmd": "press",
                       "hold_ms": action.duration_ms}
        ok = await self.bridge.send(payload)
        if not ok:
            return ExecResult(ok=False, detail="桥没连上，动作没发出去")

        # 时间到了要自己收尾：按停止键（学过的话），否则就只是不再发码
        if self._release_task and not self._release_task.done():
            self._release_task.cancel()
        self._release_task = asyncio.create_task(self._release_later(action.duration_ms))
        return ExecResult(ok=True, detail=f"按下 {action.action} ×{repeat}",
                          duration_ms=action.duration_ms, button=str(record.get("slot") or action.action))

    async def _release_later(self, duration_ms: int) -> None:
        try:
            await asyncio.sleep(max(0.05, duration_ms / 1000.0))
            await self._send_stop()
        except asyncio.CancelledError:
            raise

    async def _send_stop(self) -> None:
        record = self.codes.get("stop")
        if not record:
            return
        if self.name == "ir":
            payload = {**self._payload("stop", record), "cmd": self.send_cmd,
                       "repeat": 1, "interval_ms": self.settings.ir_repeat_interval_ms}
        else:
            payload = {**self._payload("stop", record), "cmd": self.single_cmd,
                       "hold_ms": self.settings.press_hold_ms}
        await self.bridge.send(payload)

    async def halt(self, reason: str = "") -> ExecResult:
        task = self._release_task
        self._release_task = None
        if task and not task.done():
            task.cancel()
        await self._send_stop()
        await self.bridge.send({"cmd": "estop"})
        return ExecResult(ok=True, detail=reason or "松开按键", state=self.telemetry())

    # ------------------------------------------------------------ 学习
    async def learn(self, slot: str) -> dict[str, Any]:
        """进入学习模式，把用户按下的那个键记为 slot。"""
        if self.name == "button":
            index = self._next_servo_index()
            record = {"slot": slot, "servo": index}
            await self.bridge.send({"cmd": "servo_test", "servo": index})
            self.codes[slot] = record
            save_codes(self.codes)
            return {"ok": True, "slot": slot, "record": record,
                    "note": f"已把「{slot}」指到舵机 #{index}，刚才动的那一下就是它"}
        learned = await self.bridge.request_learn()
        if not learned:
            return {"ok": False, "error": "没等到红外码：确认单片机在学习模式、遥控器对着接收头"}
        record = {
            "slot": slot,
            "code": learned.get("code"),
            "protocol": learned.get("protocol"),
            "bits": learned.get("bits"),
        }
        self.codes[slot] = record
        save_codes(self.codes)
        return {"ok": True, "slot": slot, "record": record}

    def _next_servo_index(self) -> int:
        used = {int(v.get("servo", -1)) for v in self.codes.values() if "servo" in v}
        index = 0
        while index in used:
            index += 1
        return index

    def forget(self, slot: str) -> None:
        self.codes.pop(slot, None)
        save_codes(self.codes)

    def reload(self) -> None:
        self.codes = load_codes()


class IRDriver(RemoteDriver):
    name = "ir"
    label = "红外遥控（学习回放）"
    send_cmd = "ir_send"


class ButtonDriver(RemoteDriver):
    name = "button"
    label = "舵机压遥控器"
    single_cmd = "press"
