"""安全层：大模型说的话，只能在这里变成电机的动作。

四道闸：
1. 能力闸 —— 这台身体会不会做这个动作（能不能连续走？有没有这个按键？）
2. 限幅闸 —— 速度、时长、按键白名单，一律夹到配置范围内
3. 频率闸 —— 两次动作之间至少间隔 N 毫秒，防止模型"抖"
4. 生命闸 —— 急停锁存 / 看门狗 / 画面过期，任何一条触发都强制 stop

设计取向：宁可停着不动，也不要瞎动。所有拦截都会留下原因，前端能看见。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import config as C
from .actions import Action, stop_action


@dataclass
class Verdict:
    """安全层的判定结果。"""

    allowed: bool
    action: Action
    reason: str = ""
    forced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action.to_dict(),
            "reason": self.reason,
            "forced": self.forced,
        }


@dataclass
class SafetyState:
    estop: bool = False
    estop_reason: str = ""
    last_action_at: float = 0.0
    last_heartbeat: float = field(default_factory=time.time)
    actions: int = 0
    blocked: int = 0
    forced_stops: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)


class SafetyGate:
    def __init__(self, settings: C.Settings):
        self.settings = settings
        self.state = SafetyState(estop=bool(settings.estop_on_start),
                                 estop_reason="启动时急停" if settings.estop_on_start else "")
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 状态
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = self.state
            return {
                "estop": s.estop,
                "estop_reason": s.estop_reason,
                "last_action_ms_ago": int((time.time() - s.last_action_at) * 1000) if s.last_action_at else None,
                "actions": s.actions,
                "blocked": s.blocked,
                "forced_stops": s.forced_stops,
                "events": list(s.events[-12:]),
            }

    def estop(self, reason: str = "手动急停") -> None:
        with self._lock:
            self.state.estop = True
            self.state.estop_reason = reason
            self._log("estop", reason)

    def clear_estop(self) -> None:
        with self._lock:
            self.state.estop = False
            self.state.estop_reason = ""
            self._log("estop_clear", "解除急停")

    def heartbeat(self) -> None:
        with self._lock:
            self.state.last_heartbeat = time.time()

    def _log(self, kind: str, reason: str, action: Action | None = None) -> None:
        self.state.events.append(
            {
                "at": round(time.time(), 3),
                "kind": kind,
                "reason": reason,
                "action": action.action if action else None,
            }
        )
        if len(self.state.events) > 200:
            del self.state.events[:-200]

    # ------------------------------------------------------------ 判定
    def check(self, action: Action, *, now: float | None = None) -> Verdict:
        """返回能否执行；不能执行时给一个 stop 动作。"""
        now = time.time() if now is None else now
        s = self.settings

        # 1) 急停优先，除了 stop 什么都不许
        with self._lock:
            estop = self.state.estop
            estop_reason = self.state.estop_reason
        if estop and action.action != "stop":
            return self._block(action, f"急停中：{estop_reason or '已锁定'}", forced=True)

        # 2) 限幅
        action.speed = min(max(action.speed, 0.0), s.max_speed)
        if action.is_motion:
            action.duration_ms = int(min(max(action.duration_ms, 100), s.max_duration_ms))
            if action.speed <= 0:
                return self._block(action, "速度被夹到 0，改成停止", forced=True)
        elif action.action == "press":
            action.duration_ms = int(min(max(action.duration_ms, 40), 600))
        else:
            action.duration_ms = 0

        # 3) 频率
        with self._lock:
            last = self.state.last_action_at
        if last and (now - last) * 1000.0 < s.min_action_interval_ms and action.action != "stop":
            return self._block(
                action,
                f"动作太密（间隔 <{s.min_action_interval_ms}ms），本次丢弃",
                forced=False,
            )

        with self._lock:
            self.state.last_action_at = now
            self.state.actions += 1
        return Verdict(allowed=True, action=action)

    def _block(self, action: Action, reason: str, *, forced: bool) -> Verdict:
        with self._lock:
            self.state.blocked += 1
            if forced:
                self.state.forced_stops += 1
            self._log("blocked" if not forced else "forced_stop", reason, action)
        if forced:
            return Verdict(allowed=False, action=stop_action(reason, source="safety"), reason=reason, forced=True)
        return Verdict(allowed=False, action=stop_action(reason, source="safety"), reason=reason, forced=False)

    # ------------------------------------------------------------ 看门狗
    def watchdog(
        self,
        now: float | None = None,
        *,
        moving: bool = False,
        last_decision_at: float | None = None,
        mode: str = "idle",
    ) -> Verdict | None:
        """只在「真的在动」的时候兜底。

        这很关键：静止状态下每 6 秒喊一次"看门狗"，只会把日志刷成垃圾，
        真正的危险是「已经发出去了、但没人收尾」——那才需要强制停。
        """
        now = time.time() if now is None else now
        s = self.settings
        with self._lock:
            if self.state.estop or not moving:
                return None
            last = self.state.last_action_at

        # 1) 动作早该结束了，驱动却没停
        if last and (now - last) * 1000.0 > s.max_duration_ms + 1500:
            with self._lock:
                self.state.last_action_at = now
                self.state.forced_stops += 1
                self._log("watchdog", "动作超时未收尾")
            return Verdict(
                allowed=False,
                action=stop_action("动作超时未收尾，兜底停车", source="watchdog"),
                reason="action_timeout",
                forced=True,
            )

        # 2) 自动模式下，大脑太久没给出新决策（模型卡住/网络抖动）
        if mode == "auto" and last_decision_at and (now - last_decision_at) * 1000.0 > s.watchdog_ms:
            with self._lock:
                self.state.forced_stops += 1
                self._log("watchdog", f"{int((now - last_decision_at) * 1000)}ms 没有新决策")
            return Verdict(
                allowed=False,
                action=stop_action("大脑太久没反应，先停下等你", source="watchdog"),
                reason="brain_stall",
                forced=True,
            )
        return None

    # ------------------------------------------------------------ 能力
    @staticmethod
    def check_capability(action: Action, caps: dict[str, Any]) -> str:
        """返回不满足的原因；空字符串表示这台身体能做。"""
        can_drive = bool(caps.get("drive", True))
        can_press = bool(caps.get("press", False))
        buttons = caps.get("buttons") or []

        if action.action in ("forward", "backward", "turn_left", "turn_right") and not can_drive:
            return "这台身体不能连续行走，只能按键或说话"
        if action.action == "press":
            if not can_press:
                return "这台身体没有可按的键"
            if not action.button:
                return "press 缺少 button 字段"
            if buttons and action.button not in buttons:
                return f"没有学会按键「{action.button}」，先到「教它按键」里录一下"
        return ""
