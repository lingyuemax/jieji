"""耳朵：VAD 切句、唤醒词、识别器选择。"""
from __future__ import annotations

import asyncio
import math
import struct
import time

import numpy as np

from app import asr as asr_mod
from app.config import Settings


def tone(seconds: float, amp: int = 12000, rate: int = 16000) -> bytes:
    n = int(rate * seconds)
    t = np.arange(n) / rate
    samples = (np.sin(2 * math.pi * 220 * t) * amp).astype("<i2")
    return samples.tobytes()


def silence(seconds: float, rate: int = 16000) -> bytes:
    return b"\x00\x00" * int(rate * seconds)


def test_rms_separates_speech_from_silence():
    assert asr_mod.EnergyVAD.rms(silence(0.2)) == 0.0
    assert asr_mod.EnergyVAD.rms(tone(0.2)) > 0.1


def test_vad_emits_segment_after_hangover():
    vad = asr_mod.EnergyVAD(threshold=0.02, hangover_ms=300, min_speech_ms=100)
    seg = vad.feed(tone(0.4), now=1000.0)
    assert seg is None, "还没说完不该切"
    seg = vad.feed(silence(0.1), now=1000.2)
    assert seg is None, "拖尾还没到，不该切"
    seg = vad.feed(silence(0.1), now=1000.4)
    assert seg is not None, "闭嘴超过 hangover 就该切句"
    assert seg.duration > 0


def test_vad_ignores_pure_silence():
    vad = asr_mod.EnergyVAD(threshold=0.02)
    for i in range(30):
        assert vad.feed(silence(0.1), now=1000 + i * 0.1) is None


def test_segment_wav_header():
    vad = asr_mod.EnergyVAD(threshold=0.02, hangover_ms=100, min_speech_ms=50)
    vad.feed(tone(0.3), now=1000.0)
    seg = vad.feed(silence(0.2), now=1000.3)
    wav = seg.wav()
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"


def test_wake_words():
    assert asr_mod.wake_word_hit("小解，往前走")
    assert asr_mod.wake_word_hit("机器人你好")
    assert not asr_mod.wake_word_hit("今天天气不错")
    assert asr_mod.strip_wake_word("小解，往前走") == "往前走"


def test_mock_asr_cycles_script():
    mock = asr_mod.MockASR(["第一句", "第二句"])
    assert asyncio.run(mock.transcribe(b"")) == "第一句"
    assert asyncio.run(mock.transcribe(b"")) == "第二句"
    assert asyncio.run(mock.transcribe(b"")) == "第一句"


def test_build_asr_by_settings():
    s = Settings()
    s.asr_provider = "mock"
    assert isinstance(asr_mod.build_asr(s), asr_mod.MockASR)
    s.asr_provider = "cloud"
    s.cloud_asr_base_url = "https://example.com/v1"
    assert isinstance(asr_mod.build_asr(s), asr_mod.CloudASR)
    s.asr_provider = "local"
    assert isinstance(asr_mod.build_asr(s), asr_mod.LocalASR)


def test_utterance_shape():
    u = asr_mod.Utterance(text="你好", start=1.0, end=2.5)
    payload = u.to_dict()
    assert payload["text"] == "你好"
    assert payload["duration"] == 1.5
