"""耳朵：把麦克风的连续音频切成「一整句」，再交给 ASR。

继承自 jieyin（解音）：同样是「先判有没有人声 → 再识别 → 按停顿断句」，
只是把「上传的视频」换成了「一直开着的麦克风」，所以多了两个东西：

1. 能量 VAD + 拖尾（hangover）：闭嘴 600ms 才算一句说完；
2. 唤醒词门控（可选）：只有听到「小解 / 解机 / 机器人」才把这句话送进决策，
   免得电视里的声音把机器人指挥跑了。

识别器三种：mock（不识别，只走流程）/ local（本机 faster-whisper）/
cloud（任何 OpenAI 兼容的 /audio/transcriptions）。
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from . import config as C

log = logging.getLogger("jieji.asr")

SAMPLE_RATE = 16000
WAKE_WORDS = ("小解", "解机", "机器人", "小捷", "杰基")

# 语音乐句理解的 9 个键 —— 原封不动来自 jieyin
AUDIO_KEYS = ["speaker", "speech", "summary", "detail", "emotion", "intent", "keywords", "soundscape", "lang"]
AUDIO_LABELS = {
    "speaker": "说话人",
    "speech": "原话",
    "summary": "一句话概括",
    "detail": "详细描述",
    "emotion": "语气/情绪",
    "intent": "意图",
    "keywords": "关键词",
    "soundscape": "声音环境",
    "lang": "语言",
}
AUDIO_EMPTY = {
    "speaker": "未知",
    "speech": "",
    "summary": "",
    "detail": "",
    "emotion": "未知",
    "intent": "未知",
    "keywords": [],
    "soundscape": "无法判断",
    "lang": "汉语",
}

AUDIO_SYSTEM = """你是「解音」的语音理解引擎，负责把机器人听到的**一句话**变成结构化中文。

严格只输出一个 JSON 对象，键固定为这 9 个，顺序不要变：
{
  "speaker": "说话人推断，如 青年女性/中年男性/儿童；判断不了写 未知",
  "speech": "整理后的中文原话：纠错别字、补标点、去口水词；不改原意、不扩写、不翻译",
  "summary": "不超过 20 字的一句话概括",
  "detail": "2~3 句：说了什么、什么语气、想让机器人做什么",
  "emotion": "平静/高兴/着急/生气/犹豫/温柔/惊讶/无奈/紧张 中选 1~2 个",
  "intent": "提问/请求/告知/劝说/寒暄/抱怨/确认/解释 中选 1~2 个",
  "keywords": ["2~4 个短关键词"],
  "soundscape": "推断的声音环境，如 室内安静/街道嘈杂；没线索写 无法判断，禁止编造",
  "lang": "汉语/英语/中英混杂"
}
只依据给出的文本，不许编造。输出必须能被 json.loads 直接解析。"""


# ------------------------------------------------------------------ VAD
@dataclass
class Segment:
    """一句人话：起止时间 + pcm。"""

    pcm: bytes
    start: float
    end: float
    peak: float = 0.0

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(self.pcm)
        return buf.getvalue()


class EnergyVAD:
    """够用的能量 VAD：算 RMS，超阈值算说话，闭嘴超过 hangover 就切句。

    不上 Silero 的原因：机器人要的是「一句一句往下走」，不是逐帧精确标注；
    能量 VAD 零依赖、零显存，跟着主循环跑不吃资源。
    """

    def __init__(self, threshold: float = 0.012, hangover_ms: int = 600,
                 min_speech_ms: int = 350, max_speech_ms: int = 12000) -> None:
        self.threshold = threshold
        self.hangover = hangover_ms / 1000.0
        self.min_speech = min_speech_ms / 1000.0
        self.max_speech = max_speech_ms / 1000.0
        self._buf = bytearray()
        self._speaking = False
        self._start = 0.0
        self._last_voice = 0.0
        self._peak = 0.0

    @staticmethod
    def rms(pcm: bytes) -> float:
        import numpy as np

        if len(pcm) < 2:
            return 0.0
        data = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype("float32") / 32768.0
        if data.size == 0:
            return 0.0
        return float(np.sqrt((data * data).mean()))

    def feed(self, pcm: bytes, now: float | None = None) -> Segment | None:
        now = time.time() if now is None else now
        level = self.rms(pcm)
        voice = level >= self.threshold
        if voice:
            if not self._speaking:
                self._speaking = True
                self._start = now
                self._buf = bytearray()
                self._peak = 0.0
            self._last_voice = now
            self._peak = max(self._peak, level)
            self._buf.extend(pcm)
            if now - self._start >= self.max_speech:
                return self._close(now)
            return None
        if not self._speaking:
            return None
        # 拖尾阶段：把静音也带上，别把最后一个字切掉
        self._buf.extend(pcm)
        if now - self._last_voice >= self.hangover:
            if now - self._start >= self.min_speech:
                return self._close(now)
            self._reset()
        return None

    def flush(self) -> Segment | None:
        if self._speaking and time.time() - self._start >= self.min_speech:
            return self._close(time.time())
        self._reset()
        return None

    def _close(self, now: float) -> Segment:
        seg = Segment(pcm=bytes(self._buf), start=self._start, end=now, peak=self._peak)
        self._reset()
        return seg

    def _reset(self) -> None:
        self._buf = bytearray()
        self._speaking = False
        self._start = 0.0
        self._last_voice = 0.0
        self._peak = 0.0


def wake_word_hit(text: str) -> bool:
    return any(word in text for word in WAKE_WORDS)


def strip_wake_word(text: str) -> str:
    out = text
    for word in WAKE_WORDS:
        out = out.replace(word, "")
    return out.strip("，,。.！!？? ")


# ------------------------------------------------------------------ 识别器
class BaseASR:
    name = "base"

    async def transcribe(self, wav: bytes, language: str = "zh") -> str:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class MockASR(BaseASR):
    """不识别，只把流程跑通。演示 / 单测用。"""

    name = "mock"

    def __init__(self, script: list[str] | None = None) -> None:
        self.script = list(script or [])
        self._i = 0

    async def transcribe(self, wav: bytes, language: str = "zh") -> str:
        if not self.script:
            return ""
        text = self.script[self._i % len(self.script)]
        self._i += 1
        return text


class LocalASR(BaseASR):
    """本机 faster-whisper。第一次会下载模型（small ≈ 460MB）。"""

    name = "local"

    def __init__(self, model: str = "small", language: str = "zh") -> None:
        self.model_name = model
        self.language = language
        self._model = None
        self._device = "?"
        self._lock = asyncio.Lock()

    def _load(self):
        from faster_whisper import WhisperModel

        device, compute = "cpu", "int8"
        try:
            import torch  # noqa: F401

            if torch.cuda.is_available():
                device, compute = "cuda", "float16"
        except Exception:  # noqa: BLE001
            pass
        log.info("加载 faster-whisper：%s on %s/%s", self.model_name, device, compute)
        try:
            model = WhisperModel(self.model_name, device=device, compute_type=compute)
        except Exception as exc:  # noqa: BLE001
            log.warning("GPU 加载失败（%s），退回 CPU", exc)
            device, compute = "cpu", "int8"
            model = WhisperModel(self.model_name, device=device, compute_type=compute)
        self._device = f"{device}/{compute}"
        return model

    async def transcribe(self, wav: bytes, language: str = "zh") -> str:
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load)
            model = self._model

        def run() -> str:
            segments, _info = model.transcribe(
                io.BytesIO(wav),
                language=language or self.language,
                beam_size=5,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            return "".join(seg.text for seg in segments).strip()

        return await asyncio.to_thread(run)

    def info(self) -> dict[str, Any]:
        return {"provider": "local", "model": self.model_name, "device": self._device}


class CloudASR(BaseASR):
    """任何 OpenAI 兼容的 POST /audio/transcriptions。"""

    name = "cloud"

    def __init__(self, settings: C.Settings) -> None:
        self.settings = settings

    async def transcribe(self, wav: bytes, language: str = "zh") -> str:
        base = (self.settings.cloud_asr_base_url or "").rstrip("/")
        if not base.endswith("/v1"):
            url = base + "/audio/transcriptions"
        else:
            url = base + "/audio/transcriptions"
        headers = {}
        key = (self.settings.cloud_asr_api_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        files = {"file": ("speech.wav", wav, "audio/wav")}
        data = {"model": self.settings.cloud_asr_model, "response_format": "json"}
        if language:
            data["language"] = language
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, files=files, data=data)
        if resp.status_code != 200:
            raise RuntimeError(f"云端识别失败 {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        return (payload.get("text") or "").strip()


def build_asr(settings: C.Settings) -> BaseASR:
    provider = (settings.asr_provider or "mock").lower()
    if provider == "local":
        return LocalASR(settings.asr_model, settings.asr_language)
    if provider == "cloud":
        return CloudASR(settings)
    return MockASR()


# ------------------------------------------------------------------ 语音理解
async def understand_audio(llm, text: str) -> dict[str, Any]:
    """一句话 → jieyin 的 9 键。用于「复盘这一句」，不进实时闭环。"""
    from .actions import parse_json

    if not text.strip():
        return dict(AUDIO_EMPTY)
    result = await llm.chat(AUDIO_SYSTEM, f"识别文本：{text}", json_mode=True, max_tokens=480)
    try:
        obj = parse_json(result.text)
    except ValueError:
        out = dict(AUDIO_EMPTY)
        out["speech"] = text
        return out
    out: dict[str, Any] = {}
    for key in AUDIO_KEYS:
        value = obj.get(key, AUDIO_EMPTY[key])
        if key == "keywords":
            if isinstance(value, str):
                value = [v.strip() for v in value.replace("，", ",").split(",") if v.strip()]
            elif not isinstance(value, list):
                value = []
        else:
            value = str(value or AUDIO_EMPTY[key]).strip() or str(AUDIO_EMPTY[key])
        out[key] = value
    return out


@dataclass
class Utterance:
    text: str
    start: float
    end: float
    at: float = field(default_factory=time.time)
    source: str = "mic"  # mic | text | file
    woken: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "at": round(self.at, 3),
            "source": self.source,
            "woken": self.woken,
            "duration": round(self.end - self.start, 2),
        }


OnUtterance = Callable[[Utterance], Awaitable[None]]
