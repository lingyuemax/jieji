"""动作模型的收敛与解析：模型输出再怎么歪，也得变成能执行的东西。"""
from __future__ import annotations

import pytest

from app.actions import ACTIONS, Action, hint_action, normalize, parse_json, stop_action


def test_normalize_basic():
    a = normalize({"action": "forward", "speed": 0.45, "duration_ms": 1200, "say": "走", "why": "空"})
    assert a.action == "forward"
    assert a.speed == 0.45
    assert a.duration_ms == 1200
    assert a.is_motion


def test_normalize_clamps_speed_and_duration():
    a = normalize({"action": "forward", "speed": 100, "duration_ms": 99999})
    assert a.speed == 1.0
    a = normalize({"action": "forward", "speed": -3, "duration_ms": 0})
    assert a.speed == 0.0
    assert a.duration_ms == 0


def test_normalize_accepts_chinese_and_aliases():
    assert normalize({"action": "前进"}).action == "forward"
    assert normalize({"action": "停"}).action == "stop"
    assert normalize({"action": "turn left"}).action == "turn_left"
    assert normalize({"action": "按键", "button": "OK"}).action == "press"
    assert normalize({"action": "按键", "button": "OK"}).button == "ok"


def test_normalize_unknown_action_becomes_say_or_stop():
    assert normalize({"action": "飞到月球", "say": "我不会飞"}).action == "say"
    assert normalize({"action": "飞到月球"}).action == "stop"


def test_normalize_duration_seconds():
    assert normalize({"action": "forward", "duration": 1.5}).duration_ms == 1500


def test_normalize_mood_whitelist():
    assert normalize({"action": "stop", "mood": "平静"}).mood == "平静"
    assert normalize({"action": "stop", "mood": "非常非常生气"}).mood == "平静"


def test_parse_json_plain():
    assert parse_json('{"a": 1}')["a"] == 1


def test_parse_json_fenced():
    assert parse_json('```json\n{"action":"stop"}\n```')["action"] == "stop"


def test_parse_json_with_chatter_and_trailing_comma():
    text = '好的，这是结果：\n{"action":"forward","speed":0.3,}\n希望有帮助'
    assert parse_json(text)["action"] == "forward"


def test_parse_json_nested_braces_in_string():
    text = '{"say":"他写了 {大括号} 进去","action":"stop"}'
    assert parse_json(text)["say"] == "他写了 {大括号} 进去"


def test_parse_json_rejects_empty():
    with pytest.raises(ValueError):
        parse_json("")
    with pytest.raises(ValueError):
        parse_json("完全不是 JSON")


def test_stop_action_is_safe():
    a = stop_action("测试", say="好的")
    assert a.action == "stop"
    assert a.speed == 0.0
    assert a.duration_ms == 0
    assert not a.is_motion


def test_all_actions_roundtrip():
    for name in ACTIONS:
        a = normalize({"action": name, "speed": 0.3, "duration_ms": 500})
        assert a.action == name
        assert isinstance(a.to_dict()["action"], str)


def test_action_to_dict_shape():
    data = normalize({"action": "press", "button": "up"}).to_dict()
    assert set(data) == {"action", "speed", "duration_ms", "button", "say", "mood", "why", "source"}


# ------------------------------------------------------------------ 规则解析
def test_hint_action_basic_commands():
    assert hint_action("往前走一点")[0] == "forward"
    assert hint_action("后退，太近了")[0] == "backward"
    assert hint_action("向左转看看")[0] == "turn_left"
    assert hint_action("右转")[0] == "turn_right"
    assert hint_action("停下")[0] == "stop"
    assert hint_action("先别动")[0] == "stop"


def test_hint_action_negation_is_safe():
    """「别往前走」猜错的方向必须偏安全 —— 宁可停。"""
    assert hint_action("别往前走")[0] == "stop"
    assert hint_action("不要后退")[0] == "stop"


def test_hint_action_none_for_chat():
    assert hint_action("今天天气不错") == ("", "")
    assert hint_action("") == ("", "")
    assert hint_action("你能做什么？") == ("", "")
