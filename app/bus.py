"""事件总线：一层薄薄的 async pub/sub，给 SSE 用。

只做一件事：有事件就分发给所有订阅者，谁的队列满了就丢最老的，
绝不让一个卡住的浏览器把机器人主循环拖死。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self, per_client: int = 200) -> None:
        self.per_client = per_client
        self._subs: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._seq = 0
        self.history: list[dict[str, Any]] = []

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.per_client)
        async with self._lock:
            self._subs.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subs.discard(queue)

    async def publish(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        self._seq += 1
        event = {"seq": self._seq, "kind": kind, "at": round(time.time(), 3), **(payload or {})}
        if kind in ("decision", "utterance", "action", "error", "estop", "mode"):
            self.history.append(event)
            del self.history[:-120]
        async with self._lock:
            targets = list(self._subs)
        for queue in targets:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    @property
    def subscribers(self) -> int:
        return len(self._subs)
