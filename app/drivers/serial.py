"""直驱身体：跳过遥控器，串口直接给电机下 PWM。

这是最"正规"的一条路，也是最需要动手的一条：把 Arduino / ESP32 接到机器人
电机驱动板上（L298N / TB6612 / DRV8833 都行），主机发 JSON，线的一端是电机。

协议（一行一个 JSON，115200）：
  {"t":"drive","l":0.4,"r":0.4,"ms":800}     # 左右轮 -1.0 ~ 1.0
  {"t":"stop"}
  {"t":"estop"}
  {"t":"ping"}
单片机会回 {"t":"status","front_cm":42,"battery":7.4}
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from .. import config as C
from ..actions import Action
from .base import Driver, ExecResult

log = logging.getLogger("jieji.driver.serial")


def _pyserial():
    try:
        import serial  # type: ignore
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError("直驱模式需要 pyserial：python -m pip install pyserial") from exc
    return serial


class SerialDriver(Driver):
    name = "serial"
    label = "串口直驱电机"
    capabilities = {"drive": True, "press": False, "buttons": []}

    def __init__(self, settings: C.Settings) -> None:
        self.settings = settings
        self.ser = None
        self.error = ""
        self.connected = False
        self.device_status: dict[str, Any] = {}
        self._stop_task: asyncio.Task | None = None
        self._reader: threading.Thread | None = None
        self._closing = False

    # ------------------------------------------------------------ 连接
    async def open(self) -> None:
        try:
            serial = _pyserial()
            self.ser = serial.Serial(
                self.settings.driver_serial_port,
                self.settings.driver_serial_baud,
                timeout=0.5,
                write_timeout=1.0,
            )
            self.connected = True
            self.error = ""
            self._closing = False
            self._reader = threading.Thread(target=self._read_loop, name="serial-rx", daemon=True)
            self._reader.start()
            await self.halt("开机对齐")
            log.info("串口已连接：%s", self.settings.driver_serial_port)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.connected = False
            log.error("串口打不开：%s", exc)

    async def close(self) -> None:
        self._closing = True
        await self.halt("关闭")
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:  # noqa: BLE001
                pass
        self.ser = None
        self.connected = False

    def _read_loop(self) -> None:
        while not self._closing and self.ser is not None:
            try:
                line = self.ser.readline()
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
                continue
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8", "ignore").strip())
            except ValueError:
                continue
            if msg.get("t") == "status":
                self.device_status = msg

    def _write(self, payload: dict[str, Any]) -> bool:
        if self.ser is None or not self.connected:
            return False
        try:
            self.ser.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            return True
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.connected = False
            return False

    # ------------------------------------------------------------ 执行
    async def execute(self, action: Action) -> ExecResult:
        if action.action == "say":
            return ExecResult(ok=True, detail="说话")
        if action.action in ("stop", "press"):
            await self.halt("模型要求停下")
            return ExecResult(ok=True, detail="停止" if action.action == "stop" else "这台身体没有按键")

        speed = action.speed
        left = right = 0.0
        if action.action == "forward":
            left = right = speed
        elif action.action == "backward":
            left = right = -speed
        elif action.action == "turn_left":
            left, right = -speed, speed
        elif action.action == "turn_right":
            left, right = speed, -speed

        ok = self._write({"t": "drive", "l": round(left, 3), "r": round(right, 3), "ms": action.duration_ms})
        if not ok:
            return ExecResult(ok=False, detail="串口没连上")
        if self._stop_task and not self._stop_task.done():
            self._stop_task.cancel()
        self._stop_task = asyncio.create_task(self._auto_stop(action.duration_ms))
        return ExecResult(ok=True, detail=f"PWM L={left:.2f} R={right:.2f}", duration_ms=action.duration_ms)

    async def _auto_stop(self, duration_ms: int) -> None:
        await asyncio.sleep(max(0.05, duration_ms / 1000.0))
        self._write({"t": "stop"})

    async def halt(self, reason: str = "") -> ExecResult:
        if self._stop_task and not self._stop_task.done():
            self._stop_task.cancel()
        self._write({"t": "stop"})
        return ExecResult(ok=True, detail=reason or "停", state=self.telemetry())

    def telemetry(self) -> dict[str, Any]:
        front = self.device_status.get("front_cm")
        return {
            "connected": self.connected,
            "front_m": front / 100.0 if isinstance(front, (int, float)) else None,
            "battery": self.device_status.get("battery"),
            "port": self.settings.driver_serial_port,
        }

    def connection(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "port": self.settings.driver_serial_port,
            "baud": self.settings.driver_serial_baud,
            "error": self.error,
        }
