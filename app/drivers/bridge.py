"""ESP32 桥：主机 ↔ 单片机的一条 WebSocket 长连接。

为什么走 Wi-Fi 而不是 USB 串口：机器人要到处跑，一根线会把「自主」这件事
直接毁掉。ESP32 开热点（或连家里 Wi-Fi），主机推 JSON，单片机照着做。

协议见 docs/protocol.md。这里只管三件事：连上、自动重连、别把旧动作补发出去。
—— 断线重连之后补发上一条「前进」，是机器人项目里最经典的翻车方式。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger("jieji.bridge")


class Bridge:
    def __init__(
        self,
        url: str,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        reconnect: bool = True,
    ) -> None:
        self.url = url
        self.on_event = on_event
        self.reconnect = reconnect
        self.connected = False
        self.last_error = ""
        self.last_rx: str = ""
        self.sent = 0
        self.fw = ""
        self.caps: list[str] = []
        self.device_status: dict[str, Any] = {}
        self._ws = None
        self._task: asyncio.Task | None = None
        self._rx_task: asyncio.Task | None = None
        self._stop = False
        self._learn_future: asyncio.Future | None = None

    # ------------------------------------------------------------ 生命周期
    async def open(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._run(), name="bridge")

    async def close(self) -> None:
        self._stop = True
        if self._rx_task:
            self._rx_task.cancel()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.connected = False

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                import websockets

                log.info("连接桥 %s", self.url)
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=10, open_timeout=6) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_error = ""
                    backoff = 1.0
                    self._rx_task = asyncio.create_task(self._receive(ws))
                    await self._rx_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("桥断开：%s", self.last_error)
            finally:
                self.connected = False
                self._ws = None
            if not self.reconnect or self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(8.0, backoff * 1.6)

    async def _receive(self, ws) -> None:
        async for raw in ws:
            self.last_rx = str(raw)[:300]
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("event") == "hello":
                self.fw = str(msg.get("fw") or "")
                self.caps = list(msg.get("caps") or [])
                log.info("设备在线：%s caps=%s", self.fw, self.caps)
            elif msg.get("event") == "status":
                self.device_status = {k: v for k, v in msg.items() if k != "event"}
            elif msg.get("event") == "ir_learned":
                if self._learn_future and not self._learn_future.done():
                    self._learn_future.set_result(msg)
            if self.on_event:
                await self.on_event(msg)

    # ------------------------------------------------------------ 收发
    async def send(self, payload: dict[str, Any]) -> bool:
        if not self.connected or self._ws is None:
            return False
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
            self.sent += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.connected = False
            return False

    async def request_learn(self, timeout: float = 12.0) -> dict[str, Any] | None:
        """让单片机进入学习模式，等它把抓到的码回传。"""
        if not self.connected:
            return None
        self._learn_future = asyncio.get_running_loop().create_future()
        try:
            if not await self.send({"cmd": "ir_learn"}):
                return None
            return await asyncio.wait_for(self._learn_future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._learn_future = None

    def status(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "connected": self.connected,
            "fw": self.fw,
            "caps": self.caps,
            "sent": self.sent,
            "error": self.last_error,
            "device": self.device_status,
            "last_rx": self.last_rx,
            "at": round(time.time(), 3),
        }
