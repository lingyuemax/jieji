"""驱动注册表：一个字符串决定这台机器人怎么动。"""
from __future__ import annotations

import logging
from typing import Any

from .. import config as C
from ..sim import Sim
from .base import Driver, ExecResult, NullDriver
from .bridge import Bridge
from .mock import MockDriver
from .remote import ButtonDriver, IRDriver, RemoteDriver, load_codes, save_codes
from .serial import SerialDriver

log = logging.getLogger("jieji.drivers")

DRIVER_KINDS = {
    "mock": "模拟小车（无需硬件，默认）",
    "ir": "红外遥控：学习遥控器按键后回放",
    "button": "舵机压按钮：物理挤压真遥控器",
    "serial": "串口直驱电机（Arduino/ESP32 + L298N）",
    "none": "不接身体：只说话",
}


def build_driver(settings: C.Settings, sim: Sim | None = None) -> Driver:
    kind = (settings.driver or "mock").lower()
    if kind == "ir":
        return IRDriver(Bridge(settings.driver_bridge_url), settings)
    if kind == "button":
        return ButtonDriver(Bridge(settings.driver_bridge_url), settings)
    if kind == "serial":
        return SerialDriver(settings)
    if kind == "none":
        return NullDriver()
    if kind != "mock":
        log.warning("未知驱动 %s，退回 mock", kind)
    return MockDriver(sim or Sim())


def driver_info(settings: C.Settings) -> dict[str, Any]:
    return {"kinds": DRIVER_KINDS, "current": settings.driver}


__all__ = [
    "Driver",
    "ExecResult",
    "NullDriver",
    "MockDriver",
    "IRDriver",
    "ButtonDriver",
    "RemoteDriver",
    "SerialDriver",
    "Bridge",
    "build_driver",
    "driver_info",
    "load_codes",
    "save_codes",
    "DRIVER_KINDS",
]
