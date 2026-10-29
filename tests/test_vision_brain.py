"""眼睛 + 大脑：图片处理、键值对收敛、提示词内容、端到端闭环（用假模型）。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.actions import parse_json
from app.brain import Brain
from app.config import Settings
from app.llm import LLMResult
from app.sim import Sim
from app.vision import EMPTY, VISION_KEYS, coerce_kv, prepare, sha256, to_prompt_text


# ------------------------------------------------------------------ 视觉
def test_coerce_fills_missing_keys():
    kv = coerce_kv({"summary": "走廊"})
    assert set(kv) == set(VISION_KEYS)
    assert kv["summary"] == "走廊"
    assert kv["obstacles"] == "未识别"


def test_coerce_joins_lists():
    kv = coerce_kv({"objects": ["箱子", "杯子"]})
    assert kv["objects"] == "箱子 / 杯子"


def test_coerce_handles_empty_values():
    kv = coerce_kv({"summary": "", "details": None})
    assert kv["summary"] == EMPTY["summary"]
    assert kv["details"] == "未识别"


def test_prepare_small_image_untouched():
    sim = Sim(160, 120)
    jpeg = sim.render()
    assert prepare(jpeg, max_width=4096) == jpeg


def test_prepare_shrinks_large_image():
    from PIL import Image
    import io

    img = Image.new("RGB", (1600, 1200), (20, 30, 40))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    out = prepare(buf.getvalue(), max_width=400)
    reopened = Image.open(io.BytesIO(out))
    assert reopened.width == 400
    assert len(out) < len(buf.getvalue())


def test_sha256_stable():
    assert sha256(b"abc") == sha256(b"abc")
    assert sha256(b"abc") != sha256(b"abd")


def test_prompt_text_has_labels():
    text = to_prompt_text(coerce_kv({"summary": "有人挡路"}))
    assert "一句话概括：有人挡路" in text


# ------------------------------------------------------------------ 大脑
class FakeLLM:
    def __init__(self, payload: dict | None = None):
        self.payload = payload or {
            "say": "前面有人，我停下",
            "mood": "警告",
            "action": "stop",
            "speed": 0.0,
            "duration_ms": 300,
            "why": "人挡住了",
        }
        self.calls = 0
        self.last_prompt = ""

    async def chat(self, system, user, **kwargs):
        self.calls += 1
        self.last_prompt = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
        return LLMResult(text=json.dumps(self.payload, ensure_ascii=False), model="fake", latency_ms=7, tokens=99)

    def stats(self):
        return {"calls": self.calls}

    async def close(self):
        return None


def make_brain(payload=None) -> tuple[Brain, FakeLLM]:
    s = Settings()
    s.llm_vision = True
    fake = FakeLLM(payload)
    brain = Brain(s, fake)
    return brain, fake


def test_brain_parses_decision():
    brain, fake = make_brain({"say": "好", "action": "forward", "speed": 0.3, "duration_ms": 800, "why": "路通"})

    async def run():
        d = await brain.decide(jpeg=b"x", mode="auto", state={"驱动": "mock"}, front_m=1.2)
        assert d.action.action == "forward"
        assert d.action.speed == 0.3
        assert d.latency_ms == 7
        assert fake.calls == 1

    asyncio.run(run())


def test_brain_falls_back_to_stop_on_bad_json():
    brain, _ = make_brain()
    brain.llm.payload = None

    class Broken(FakeLLM):
        async def chat(self, *args, **kwargs):
            return LLMResult(text="我不知道该怎么走", model="fake", latency_ms=5)

    brain.llm = Broken()

    async def run():
        d = await brain.decide(jpeg=b"x", mode="idle")
        assert d.action.action == "stop"
        assert d.error

    asyncio.run(run())


def test_brain_prompt_includes_user_and_blocked():
    brain, fake = make_brain()

    async def run():
        await brain.decide(
            jpeg=b"x",
            mode="idle",
            user_text="往前走",
            utterances=[{"text": "往前走", "at": __import__("time").time(), "source": "text"}],
            actions=[{"action": {"action": "forward", "speed": 0.3, "duration_ms": 500}, "ok": False, "detail": "太密"}],
            blocked="动作太密",
            front_m=0.42,
            state={"驱动": "mock"},
        )

    asyncio.run(run())
    assert "往前走" in fake.last_prompt
    assert "动作太密" in fake.last_prompt
    assert "0.42" in fake.last_prompt
    assert "被安全层拦下" in fake.last_prompt


def test_brain_understand_returns_both_key_sets():
    brain, _ = make_brain({"summary": "走廊"})

    async def run():
        data = await brain.understand(b"fake", text="你好")
        assert set(data["vision"]) == set(VISION_KEYS)
        assert data["audio"] is None or "speech" in data["audio"]

    asyncio.run(run())


def test_decision_json_contract():
    """提示词里承诺的键，代码解析时必须都认。"""
    payload = {"say": "好", "mood": "好奇", "action": "turn_left", "speed": 0.4, "duration_ms": 700, "why": "看看左边"}
    brain, _ = make_brain(payload)

    async def run():
        d = await brain.decide(jpeg=b"x", mode="auto")
        data = d.to_dict()
        assert data["action"]["action"] == "turn_left"
        assert data["action"]["mood"] == "好奇"
        assert data["tokens"] == 99

    asyncio.run(run())


def test_parse_json_used_by_vision():
    obj = parse_json('```json\n{"summary":"x"}\n```')
    assert obj["summary"] == "x"
