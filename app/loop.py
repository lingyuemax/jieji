"""主循环：把采集、感知、决策、安全、执行串成一个一直在跑的东西。

时序（默认参数）：

    每 250ms   一个 tick：看门狗、检查是否该思考、刷状态
    每 1800ms 一次决策：一帧画面 → 大模型 → 一个动作（约 1 秒）
    随时      人说话 / 人在网页上打字 → 立刻插一次决策
    随时      急停按钮 → 立刻停，锁存，除了"停"什么都不执行

两个模式：
    idle（听指令）—— 只在你说话时行动
    auto（自动巡逻）—— 每隔 N 毫秒自己看一眼前方再决定
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import config as C
from .actions import Action, Decision, normalize, stop_action
from .asr import BaseASR, Utterance, build_asr, strip_wake_word, wake_word_hit
from .brain import Brain
from .bus import EventBus
from .capture import AudioCapture, Camera
from .drivers import Driver, build_driver
from .llm import LLMClient
from .safety import SafetyGate
from .sim import Sim
from .vision import VisionCache

log = logging.getLogger("jieji.loop")


class AgentLoop:
    def __init__(self, settings: C.Settings | None = None, bus: EventBus | None = None) -> None:
        self.settings = settings or C.get_settings()
        self.bus = bus or EventBus()
        self.sim: Sim | None = None
        self.camera: Camera | None = None
        self.audio: AudioCapture | None = None
        self.asr: BaseASR | None = None
        self.driver: Driver | None = None
        self.llm = LLMClient(self.settings)
        self.cache = VisionCache()
        self.brain = Brain(self.settings, self.llm, self.cache)
        self.gate = SafetyGate(self.settings)

        self.mode = "idle"          # idle | auto
        self.started_at = time.time()
        self._utterances: deque[dict[str, Any]] = deque(maxlen=20)
        self._actions: deque[dict[str, Any]] = deque(maxlen=20)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._deciding = False
        self._next_decision_at = 0.0
        self._pending_user = ""
        self._episode_id = time.strftime("%Y%m%d-%H%M%S")
        self._episode_dir: Path | None = None
        self._episode_file: Path | None = None
        self._frames_saved = 0
        self._started = False
        self._last_action: dict[str, Any] | None = None
        self._say_queue: deque[dict[str, Any]] = deque(maxlen=5)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._current_frame: bytes | None = None
        self._last_decision_at = 0.0

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        s = self.settings
        s.clamp()
        self._loop = asyncio.get_running_loop()

        if (s.camera_source or "mock").lower() == "mock" or (s.driver or "mock").lower() == "mock":
            self.sim = Sim(480, 360)
        self.driver = build_driver(s, self.sim)
        await self.driver.open()

        provider = self.sim.render if self.sim is not None else None
        self.camera = Camera(s, provider)
        self.camera.start()

        self.asr = build_asr(s)
        self.audio = AudioCapture(s, self._on_segment_threadsafe, asr_enabled=s.audio_source != "mock")
        self.audio.start()

        if s.record_episodes:
            self._episode_dir = C.EPISODE_DIR / self._episode_id
            self._episode_dir.mkdir(parents=True, exist_ok=True)
            self._episode_file = self._episode_dir / "episode.jsonl"

        self._stop.clear()
        self._task = asyncio.create_task(self._tick_loop(), name="agent-loop")
        if s.auto_mode:
            self.mode = "auto"
            self._next_decision_at = time.time() + 1.0
        await self.bus.publish("hello", {
            "mode": self.mode,
            "driver": self.driver.name,
            "camera": self.camera.source,
            "audio": self.audio.source,
            "asr": s.asr_provider,
            "model": s.llm_model,
            "episode": self._episode_id,
        })
        log.info("AgentLoop 启动：driver=%s camera=%s mode=%s",
                 self.driver.name, self.camera.source, self.mode)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self.driver:
            try:
                await self.driver.halt("退出")
            except Exception:  # noqa: BLE001
                pass
            await self.driver.close()
        if self.camera:
            self.camera.stop()
        if self.audio:
            self.audio.stop()
        if self.asr:
            await self.asr.close()
        await self.llm.close()
        self._started = False
        log.info("AgentLoop 已停止")

    async def restart_io(self) -> dict[str, Any]:
        """改了摄像头/麦克风/驱动之后热重建，不用重启进程。"""
        s = self.settings
        s.clamp()
        if self.driver:
            try:
                await self.driver.halt("重建驱动")
                await self.driver.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("关闭旧驱动失败：%s", exc)
        if self.camera:
            self.camera.stop()
        if self.audio:
            self.audio.stop()
        if self.asr:
            await self.asr.close()

        if (s.camera_source or "mock").lower() == "mock" or (s.driver or "mock").lower() == "mock":
            if self.sim is None:
                self.sim = Sim(480, 360)
        self.driver = build_driver(s, self.sim)
        await self.driver.open()
        self.camera = Camera(s, self.sim.render if self.sim is not None else None)
        self.camera.start()
        self.asr = build_asr(s)
        self.audio = AudioCapture(s, self._on_segment_threadsafe, asr_enabled=s.audio_source != "mock")
        self.audio.start()

        # 这些对象都持有同一个 settings 引用，热改自动生效；这里显式同步一次更稳
        self.gate.settings = s
        self.brain.settings = s
        self.llm.settings = s

        payload = {
            "mode": self.mode,
            "driver": self.driver.name,
            "camera": self.camera.source,
            "audio": self.audio.source,
            "asr": s.asr_provider,
            "model": s.llm_model,
            "episode": self._episode_id,
            "rebuilt": True,
        }
        await self.bus.publish("hello", payload)
        return payload

    # ------------------------------------------------------------ 输入
    async def submit_text(self, text: str, *, source: str = "text") -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "空指令"}
        now = time.time()
        utterance = Utterance(text=text, start=now, end=now, at=now, source=source, woken=True)
        await self._accept_utterance(utterance, force=True)
        return {"ok": True, "text": text}

    async def set_mode(self, mode: str) -> dict[str, Any]:
        mode = mode if mode in ("idle", "auto") else "idle"
        self.mode = mode
        self._next_decision_at = time.time() + (0.5 if mode == "auto" else 0)
        await self.bus.publish("mode", {"mode": mode})
        return {"ok": True, "mode": mode}

    async def manual(self, payload: dict[str, Any]) -> dict[str, Any]:
        """网页 / 键盘直接遥控：仍然要过安全层，只是不经过大模型。"""
        action = normalize(payload, source="manual")
        verdict = self.gate.check(action)
        if not verdict.allowed and not verdict.forced:
            return {"ok": False, "blocked": verdict.reason, "action": verdict.action.to_dict()}
        result = await self._apply(verdict.action, record=False)
        return {
            "ok": bool(result.get("ok")),
            "stopped": verdict.forced,
            "reason": verdict.reason,
            "action": verdict.action.to_dict(),
            "result": result.get("result"),
        }

    async def estop(self, reason: str = "手动急停") -> None:
        self.gate.estop(reason)
        if self.driver:
            try:
                await self.driver.halt(reason)
            except Exception as exc:  # noqa: BLE001
                log.warning("急停时驱动异常：%s", exc)
        await self.bus.publish("estop", {"active": True, "reason": reason})

    async def clear_estop(self) -> None:
        self.gate.clear_estop()
        self.gate.heartbeat()
        await self.bus.publish("estop", {"active": False, "reason": ""})

    # ------------------------------------------------------------ 事件
    async def _accept_utterance(self, utterance: Utterance, *, force: bool = False) -> None:
        if self.settings.wake_required and not force:
            if not wake_word_hit(utterance.text):
                await self.bus.publish("utterance", {**utterance.to_dict(), "ignored": "没有唤醒词"})
                return
            utterance.text = strip_wake_word(utterance.text)
            utterance.woken = True
        item = utterance.to_dict()
        self._utterances.append(item)
        await self.bus.publish("utterance", item)
        self._pending_user = utterance.text
        # 有人说话就立刻插一次思考。注意：如果此刻正好在思考，
        # 不能就这么算了 —— 那句话会一直挂着（以前就丢过指令）。
        # _decide_and_act 收尾时会发现 _pending_user 还在，自己再补一轮。
        if not self._deciding:
            asyncio.create_task(self._decide_and_act())

    def _on_segment_threadsafe(self, seg) -> None:
        """从音频线程回到事件循环。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self._transcribe_segment(seg)))

    async def _transcribe_segment(self, seg) -> None:
        if self.asr is None:
            return
        try:
            text = await self.asr.transcribe(seg.wav(), self.settings.asr_language)
        except Exception as exc:  # noqa: BLE001
            log.warning("识别失败：%s", exc)
            await self.bus.publish("error", {"where": "asr", "error": str(exc)})
            return
        text = (text or "").strip()
        if not text:
            return
        utterance = Utterance(text=text, start=seg.start, end=seg.end, source="mic")
        await self._accept_utterance(utterance)

    # ------------------------------------------------------------ 主循环
    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("tick 异常")
                await self.bus.publish("error", {"where": "tick", "error": str(exc)})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, self.settings.tick_ms / 1000.0))
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = time.time()
        telemetry = self.driver.telemetry() if self.driver else {}
        verdict = self.gate.watchdog(
            now,
            moving=bool(telemetry.get("moving")),
            last_decision_at=self._last_decision_at,
            mode=self.mode,
        )
        if verdict is not None:
            await self.bus.publish("error", {"where": "watchdog", "error": verdict.reason})
            await self._apply(verdict.action, record=True)

        if self.mode == "auto" and now >= self._next_decision_at and not self._deciding:
            self._next_decision_at = now + self.settings.decision_interval_ms / 1000.0
            asyncio.create_task(self._decide_and_act())

    # ------------------------------------------------------------ 思考
    async def _decide_and_act(self) -> None:
        if self._deciding or not self._started:
            return
        self._deciding = True
        try:
            s = self.settings
            frame = self.camera.latest() if self.camera else None
            stale = frame is None or frame.age_ms > s.stale_frame_ms
            if stale and self.mode == "auto":
                reason = "画面过期，不敢瞎走" if frame else "没有画面，不敢走"
                await self.bus.publish("error", {"where": "camera", "error": reason})
                await self._apply(stop_action(reason, say="我看不见了，先停下", source="safety"))
                return

            telemetry = self.driver.telemetry() if self.driver else {}
            user_text = self._pending_user
            self._pending_user = ""
            self._current_frame = frame.jpeg if frame else None

            decision = await self.brain.decide(
                jpeg=frame.jpeg if frame else None,
                mode=self.mode,
                user_text=user_text,
                utterances=list(self._utterances),
                actions=list(self._actions),
                state={
                    "驱动": self.driver.name if self.driver else "无",
                    "电量": f"{telemetry.get('battery'):.0f}%" if isinstance(telemetry.get("battery"), (int, float)) else "未知",
                    "朝向": f"{telemetry.get('heading_deg', 0):.0f}°" if telemetry else "未知",
                },
                blocked=self._last_blocked_reason(),
                front_m=telemetry.get("front_m"),
            )
            await self.bus.publish("decision", {
                **decision.to_dict(),
                "mode": self.mode,
                "user": user_text,
                "front_m": telemetry.get("front_m"),
            })
            self._last_decision_at = time.time()

            if decision.action.say:
                self._say_queue.append({"text": decision.action.say, "mood": decision.action.mood,
                                        "at": time.time()})
                await self.bus.publish("say", {"text": decision.action.say, "mood": decision.action.mood})

            await self._apply(decision.action, decision=decision)
        finally:
            self._deciding = False
        # 思考期间又来了新指令 → 立刻补一轮，别让它烂在队列里
        if self._started and self._pending_user:
            asyncio.create_task(self._decide_and_act())

    def _last_blocked_reason(self) -> str:
        events = self.gate.snapshot().get("events") or []
        for event in reversed(events):
            if event.get("kind") in ("blocked", "forced_stop"):
                return str(event.get("reason") or "")
        return ""

    # ------------------------------------------------------------ 执行
    async def _apply(self, action: Action, *, decision: Decision | None = None, record: bool = True) -> dict[str, Any]:
        s = self.settings
        caps = self.driver.capabilities if self.driver else {"drive": False, "press": False}
        telemetry = self.driver.telemetry() if self.driver else {}

        # 反射层：不管模型怎么说，正前方太近就不许往前
        if s.reflex_enabled and action.action == "forward":
            front = telemetry.get("front_m")
            if isinstance(front, (int, float)) and front < s.reflex_stop_m:
                action = stop_action(
                    f"反射层：正前方 {front:.2f}m 小于 {s.reflex_stop_m:.2f}m",
                    say="前面太近了，我先停一下",
                    source="reflex",
                )

        capability = self.gate.check_capability(action, caps)
        if capability:
            result = {"ok": False, "detail": capability}
            await self.bus.publish("action", {"action": action.to_dict(), "result": result,
                                              "blocked": capability})
            self._actions.append({"action": action.to_dict(), "ok": False, "detail": capability})
            return result

        verdict = self.gate.check(action)
        if not verdict.allowed and not verdict.forced:
            await self.bus.publish("action", {
                "action": verdict.action.to_dict(),
                "result": {"ok": False, "detail": verdict.reason},
                "blocked": verdict.reason,
            })
            self._actions.append({"action": action.to_dict(), "ok": False, "detail": verdict.reason})
            return {"ok": False, "blocked": verdict.reason}

        final = verdict.action
        try:
            exec_result = await self.driver.execute(final) if self.driver else None
        except Exception as exc:  # noqa: BLE001
            log.exception("执行失败")
            await self.bus.publish("error", {"where": "driver", "error": str(exc)})
            exec_result = None

        payload = {
            "action": final.to_dict(),
            "result": exec_result.to_dict() if exec_result else {"ok": False, "detail": "没有身体"},
            "blocked": verdict.reason or "",
        }
        await self.bus.publish("action", payload)
        self._actions.append({"action": final.to_dict(), "ok": bool(exec_result and exec_result.ok),
                              "detail": exec_result.detail if exec_result else "没有身体"})
        self._last_action = payload
        self.gate.heartbeat()

        if record:
            await self._record(decision=decision, action=final, result=payload["result"],
                               frame=self._current_frame)
        return payload

    # ------------------------------------------------------------ 记录
    async def _record(self, *, decision: Decision | None, action: Action, result: dict[str, Any],
                      frame: bytes | None) -> None:
        if self._episode_file is None:
            return
        telemetry = self.driver.telemetry() if self.driver else {}
        entry = {
            "t": round(time.time(), 3),
            "mode": self.mode,
            "decision": decision.to_dict() if decision else None,
            "action": action.to_dict(),
            "result": result,
            "state": telemetry,
            "utterances": list(self._utterances)[-3:],
        }
        try:
            with self._episode_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return
        # 每隔一段时间存一张原图，将来要做 VLA 微调就是现成数据
        if frame is not None and self._frames_saved < 40:
            self._frames_saved += 1
            try:
                (self._episode_dir / "frames").mkdir(parents=True, exist_ok=True)
                (self._episode_dir / "frames" / f"{self._frames_saved:04d}.jpg").write_bytes(frame)
            except OSError:
                pass

    # ------------------------------------------------------------ 查询
    def snapshot(self) -> dict[str, Any]:
        frame = self.camera.latest() if self.camera else None
        return {
            "started": self._started,
            "mode": self.mode,
            "uptime_s": round(time.time() - self.started_at, 1),
            "episode": self._episode_id,
            "camera": self.camera.status() if self.camera else None,
            "audio": self.audio.status() if self.audio else None,
            "driver": self.driver.snapshot() if self.driver else None,
            "safety": self.gate.snapshot(),
            "brain": self.brain.stats(),
            "frame": {
                "seq": frame.seq if frame else 0,
                "age_ms": frame.age_ms if frame else None,
            },
            "utterances": list(self._utterances)[-5:],
            "actions": list(self._actions)[-6:],
            "last_action": self._last_action,
            "sim": self.sim.snapshot() if self.sim else None,
            "events": self.bus.history[-12:],
        }

    async def understand_now(self, text: str = "") -> dict[str, Any]:
        frame = self.camera.latest() if self.camera else None
        if frame is None:
            return {"ok": False, "error": "还没有画面"}
        data = await self.brain.understand(frame.jpeg, text)
        return {"ok": True, "frame_seq": frame.seq, **data}

    def mjpeg_frames(self):
        """给 MJPEG 流用：只在有新帧时产出。"""
        return self.camera
