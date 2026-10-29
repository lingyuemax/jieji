"""采集层：一直开着的摄像头 + 一直开着的麦克风。

三种画面来源：
  mock   —— 模拟器渲染（没硬件也能跑，默认）
  opencv —— 本机 USB 摄像头（cv2.VideoCapture）
  file   —— 循环播放一段视频（现场演示 / 复现问题）

三种声音来源：
  mock —— 不采集，指令从网页文本框进来
  mic  —— ffmpeg dshow 抓麦克风 → 16k 单声道 → 能量 VAD 切句
  file —— 循环播放一段音频

摄像头永远只保留「最新一帧」，旧帧直接丢：机器人要的是现在，不是历史。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from . import config as C
from .asr import SAMPLE_RATE, EnergyVAD, Segment

log = logging.getLogger("jieji.capture")
IS_WIN = os.name == "nt"


@dataclass
class Frame:
    jpeg: bytes
    seq: int
    at: float
    width: int = 0
    height: int = 0

    @property
    def age_ms(self) -> int:
        return int((time.time() - self.at) * 1000)


# ------------------------------------------------------------------ 摄像头
class Camera:
    def __init__(self, settings: C.Settings, provider: Callable[[], bytes] | None = None) -> None:
        self.settings = settings
        self.provider = provider
        self._frame: Frame | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.source = (settings.camera_source or "mock").lower()
        self.error = ""
        self.frames = 0
        self._times: deque[float] = deque(maxlen=40)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frame

    def status(self) -> dict[str, Any]:
        frame = self.latest()
        with self._lock:
            times = list(self._times)
        fps = 0.0
        if len(times) >= 2 and times[-1] > times[0]:
            fps = (len(times) - 1) / (times[-1] - times[0])
        return {
            "source": self.source,
            "running": bool(self._thread and self._thread.is_alive()),
            "frames": self.frames,
            "fps": round(fps, 1),
            "age_ms": frame.age_ms if frame else None,
            "size": f"{frame.width}x{frame.height}" if frame else "",
            "error": self.error,
        }

    # -------------------------------------------------------------- 内部
    def _run(self) -> None:
        cap = None
        try:
            if self.source == "opencv":
                import cv2

                backend = cv2.CAP_DSHOW if IS_WIN else cv2.CAP_ANY
                cap = cv2.VideoCapture(self.settings.camera_index, backend)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.settings.camera_width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.settings.camera_height)
                if not cap.isOpened():
                    raise RuntimeError(f"打不开摄像头 #{self.settings.camera_index}")
            elif self.source == "file":
                import cv2

                path = self.settings.camera_file
                if not path:
                    raise RuntimeError("没配置 CAMERA_FILE")
                cap = cv2.VideoCapture(path)
                if not cap.isOpened():
                    raise RuntimeError(f"打不开视频文件 {path}")
            elif self.source == "mock":
                if self.provider is None:
                    raise RuntimeError("mock 摄像头需要一个渲染函数")
            else:
                raise RuntimeError(f"未知摄像头来源 {self.source}")
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            log.error("摄像头启动失败：%s", exc)
            return

        interval = 1.0 / max(1, self.settings.camera_fps)
        next_at = time.time()
        while not self._stop.is_set():
            try:
                jpeg, w, h = self._grab(cap)
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                log.warning("取帧失败：%s", exc)
                time.sleep(0.5)
                continue
            if jpeg:
                with self._lock:
                    self.frames += 1
                    self._frame = Frame(jpeg=jpeg, seq=self.frames, at=time.time(), width=w, height=h)
                    self._times.append(self._frame.at)
                self.error = ""
            next_at += interval
            sleep = next_at - time.time()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_at = time.time()
        if cap is not None:
            cap.release()

    def _grab(self, cap) -> tuple[bytes, int, int]:
        if self.source == "mock":
            jpeg = self.provider()
            return jpeg, self.settings.camera_width, self.settings.camera_height
        import cv2

        ok, frame = cap.read()
        if not ok or frame is None:
            if self.source == "file":
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # 循环播放
                ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError("读不到画面")
        h, w = frame.shape[:2]
        if w > self.settings.camera_width:
            frame = cv2.resize(frame, (self.settings.camera_width, int(h * self.settings.camera_width / w)))
            h, w = frame.shape[:2]
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            raise RuntimeError("JPEG 编码失败")
        return buf.tobytes(), w, h


# ------------------------------------------------------------------ 麦克风
class AudioCapture:
    """ffmpeg 抓音频 → 16k 单声道 PCM → 能量 VAD 切句 → 回调。"""

    def __init__(
        self,
        settings: C.Settings,
        on_segment: Callable[[Segment], None],
        asr_enabled: bool = True,
    ) -> None:
        self.settings = settings
        self.on_segment = on_segment
        self.asr_enabled = asr_enabled
        self.source = (settings.audio_source or "mock").lower()
        self.vad = EnergyVAD(threshold=settings.vad_threshold)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self.error = ""
        self.segments = 0
        self.level = 0.0

    def start(self) -> None:
        if self.source == "mock":
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "running": bool(self._thread and self._thread.is_alive()),
            "segments": self.segments,
            "level": round(self.level, 4),
            "threshold": self.settings.vad_threshold,
            "device": self.settings.audio_device,
            "error": self.error,
        }

    # -------------------------------------------------------------- 内部
    def _ffmpeg(self) -> str:
        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError("PATH 里找不到 ffmpeg")
        return exe

    def _cmd(self) -> list[str]:
        exe = self._ffmpeg()
        base = [exe, "-hide_banner", "-loglevel", "error"]
        if self.source == "mic":
            device = self.settings.audio_device.strip() or pick_audio_device()
            if not device:
                raise RuntimeError("找不到可用的录音设备，先到设置页选一个")
            if IS_WIN:
                return base + ["-f", "dshow", "-i", f"audio={device}",
                               "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
            return base + ["-f", "pulse", "-i", "default",
                           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
        if self.source == "file":
            path = self.settings.audio_file
            if not path:
                raise RuntimeError("没配置 AUDIO_FILE")
            return base + ["-stream_loop", "-1", "-re", "-i", path,
                           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
        raise RuntimeError(f"未知声音来源 {self.source}")

    def _run(self) -> None:
        try:
            cmd = self._cmd()
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            log.error("麦克风启动失败：%s", exc)
            return
        creation = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, creationflags=creation,
            )
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            log.error("ffmpeg 启动失败：%s", exc)
            return

        chunk_bytes = int(SAMPLE_RATE * 2 * self.settings.audio_chunk_ms / 1000)
        proc = self._proc
        assert proc.stdout is not None
        while not self._stop.is_set():
            data = proc.stdout.read(chunk_bytes)
            if not data:
                break
            self.level = EnergyVAD.rms(data)
            if not self.asr_enabled:
                continue
            seg = self.vad.feed(data)
            if seg:
                self.segments += 1
                try:
                    self.on_segment(seg)
                except Exception as exc:  # noqa: BLE001
                    log.warning("音频回调失败：%s", exc)
        if proc.poll() is None:
            proc.terminate()
        if self.error == "" and proc.returncode not in (0, None):
            err = b""
            if proc.stderr is not None:
                try:
                    err = proc.stderr.read(400)
                except Exception:  # noqa: BLE001
                    pass
            self.error = err.decode("utf-8", "ignore").strip() or f"ffmpeg 退出码 {proc.returncode}"
            log.warning("ffmpeg 退出：%s", self.error)


def pick_audio_device() -> str:
    """从 ffmpeg 的设备列表里挑一个像麦克风的。"""
    names = [name for name, kind in list_audio_devices() if kind == "audio"]
    if not names:
        return ""
    for name in names:
        low = name.lower()
        if any(k in low for k in ("microphone", "麦克风", "mic", "阵列")):
            return name
    return names[0]


def list_audio_devices() -> list[tuple[str, str]]:
    """Windows: ffmpeg -list_devices；其他平台返回空（直接用默认设备）。"""
    if not IS_WIN:
        return []
    exe = shutil.which("ffmpeg")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, timeout=15, creationflags=0x08000000,
        )
    except Exception:  # noqa: BLE001
        return []
    text = (proc.stderr or b"").decode("utf-8", "ignore")
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = re.search(r'"([^"]+)"\s*\((audio|video)\)', line)
        if match:
            out.append((match.group(1), match.group(2)))
    return out


def schedule_segment(on_segment, loop: asyncio.AbstractEventLoop, seg: Segment, text: str | None = None):
    """线程 → 事件循环的安全投递。"""
    if text is None:
        loop.call_soon_threadsafe(on_segment, seg)
    else:
        loop.call_soon_threadsafe(on_segment, seg, text)
