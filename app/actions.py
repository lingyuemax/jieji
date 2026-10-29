"""动作模型：大模型能输出的东西，只有这一种。

模型自由发挥的 JSON 一律先过 normalize()（认别名、夹紧、补默认），
再过 SafetyGate（限速、急停、能力检查），最后才可能落到电机上。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

# 连续运动 + 离散按键 + 纯说话
DRIVE_ACTIONS = ("forward", "backward", "turn_left", "turn_right", "stop")
PRESS_ACTION = "press"
SAY_ACTION = "say"
ACTIONS = DRIVE_ACTIONS + (PRESS_ACTION, SAY_ACTION)

# 大模型偶尔会把枚举翻译成中文，这里兜一层
ALIASES: dict[str, str] = {
    "前进": "forward", "向前": "forward", "走": "forward", "往前走": "forward", "前": "forward",
    "后退": "backward", "向后": "backward", "倒车": "backward", "退": "backward",
    "左转": "turn_left", "向左": "turn_left", "左": "turn_left", "turn left": "turn_left",
    "右转": "turn_right", "向右": "turn_right", "右": "turn_right", "turn right": "turn_right",
    "停": "stop", "停下": "stop", "停止": "stop", "刹车": "stop", "freeze": "stop", "halt": "stop",
    "按键": "press", "按": "press", "按钮": "press", "click": "press", "button": "press",
    "说话": "say", "说": "say", "speak": "say", "talk": "say",
}

MOODS = ("平静", "好奇", "专注", "犹豫", "警告", "高兴", "疑惑")

# ---------------------------------------------------------------- 规则解析
# 大模型有时候会把"后退一点"当成一句闲聊回个 say。这里先用规则捞一遍：
# 不是为了取代模型，是为了在提示词里明确提醒它"这句话是在下命令"。
_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("停", "别动", "不要动", "站住", "刹车", "等等", "等一下"), "stop"),
    (("后退", "向后", "往后", "退一", "退后", "倒车", "退一下"), "backward"),
    (("左转", "往左", "向左", "左边"), "turn_left"),
    (("右转", "往右", "向右", "右边"), "turn_right"),
    (("前进", "向前", "往前", "前走", "往前走", "过来", "靠近", "走近"), "forward"),
)

# 否定词：宁可停，不可猜
_NEGATIONS = ("别", "不要", "不准", "不用", "不许", "莫")


def hint_action(text: str) -> tuple[str, str]:
    """从一句中文里猜出明确的移动指令，返回 (动作, 命中的词)。

    只处理"一句话里最明显的那个动词"，猜不出就返回 ("", "")。
    「别往前走」这种带否定的，一律按停车处理 —— 猜错的代价不对称。
    """
    text = (text or "").strip()
    if not text:
        return "", ""
    negated = any(word in text for word in _NEGATIONS)
    for words, action in _HINTS:
        for word in words:
            if word in text:
                if negated and action != "stop":
                    return "stop", word
                return action, word
    return "", ""

SIGNATURE = """动作枚举（action 只能取这 7 个之一）：
- forward      向前走
- backward     后退
- turn_left    原地左转
- turn_right   原地右转
- stop         立刻停下（默认安全动作）
- press        按一下遥控器上的某个键（此时必须给 button）
- say          原地不动，只说一句话
字段：
  say          要对人说的话，中文，≤20 字
  mood         平静 / 好奇 / 专注 / 犹豫 / 警告 / 高兴 之一
  action       见上
  speed        0.1~1.0，越小越慢；stop/say/press 可省略
  duration_ms  100~3000，动作持续多久；stop/say/press 可省略
  button       仅 press 用：遥控器按键名，如 up / down / left / right / ok
  why          一句中文理由，≤40 字"""


@dataclass
class Action:
    action: str = "stop"
    speed: float = 0.35
    duration_ms: int = 800
    button: str = ""
    say: str = ""
    mood: str = "平静"
    why: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    source: str = "model"  # model | manual | safety | fallback | system

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "speed": round(self.speed, 3),
            "duration_ms": self.duration_ms,
            "button": self.button,
            "say": self.say,
            "mood": self.mood,
            "why": self.why,
            "source": self.source,
        }

    @property
    def is_motion(self) -> bool:
        return self.action in DRIVE_ACTIONS and self.action != "stop"

    @property
    def seconds(self) -> float:
        return self.duration_ms / 1000.0


def stop_action(reason: str = "", say: str = "", source: str = "safety") -> Action:
    return Action(action="stop", speed=0.0, duration_ms=0, say=say, why=reason, source=source)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _clip_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def normalize(data: dict[str, Any], source: str = "model") -> Action:
    """把任意 dict 收敛成合法 Action（不做安全判断，只做格式收敛）。"""
    data = data or {}
    raw_action = data.get("action", data.get("act", data.get("cmd", "stop")))
    name = str(raw_action).strip().lower().replace("-", "_").replace(" ", "_")
    name = ALIASES.get(name, name)
    if name not in ACTIONS:
        # 认不出来就当作"原地说话"，比乱动安全
        name = "say" if (data.get("say") or data.get("text")) else "stop"

    # 有的模型给 0~100 的速度
    speed = _as_float(data.get("speed", data.get("velocity", 0.35)), 0.35)
    if speed > 1.0:
        speed = speed / 100.0
    speed = min(1.0, max(0.0, speed))

    # duration_ms / duration(s) / hold_ms 都认
    if "duration_ms" in data:
        duration = _as_int(data["duration_ms"], 800)
    elif "duration" in data:
        duration = _as_int(_as_float(data["duration"], 0.8) * 1000, 800)
    elif "hold_ms" in data:
        duration = _as_int(data["hold_ms"], 800)
    else:
        duration = 800

    mood = _clip_text(data.get("mood", "平静"), 6) or "平静"
    if mood not in MOODS:
        mood = "平静"

    button = _clip_text(data.get("button", data.get("key", "")), 24).lower()

    return Action(
        action=name,
        speed=round(speed, 3),
        duration_ms=duration,
        button=button,
        say=_clip_text(data.get("say", data.get("speech", "")), 60),
        mood=mood,
        why=_clip_text(data.get("why", data.get("reason", "")), 120),
        raw=dict(data),
        source=source,
    )


def parse_json(text: str) -> dict[str, Any]:
    """模型输出 → dict。容忍 markdown 代码块、前后废话、尾逗号。"""
    if not text:
        raise ValueError("空输出")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    # 从文本里抠第一个平衡的 {...}
    start = cleaned.find("{")
    while start >= 0:
        depth, in_str, escape = 0, False, False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = cleaned[start : i + 1]
                    chunk = re.sub(r",\s*([}\]])", r"\1", chunk)
                    obj = json.loads(chunk)
                    if isinstance(obj, dict):
                        return obj
                    break
        start = cleaned.find("{", start + 1)
    raise ValueError("输出里没有能解析的 JSON 对象")


@dataclass
class Decision:
    """一次思考的完整结果：说什么、做什么、为什么、花了多久。"""

    action: Action
    latency_ms: int = 0
    tokens: int = 0
    model: str = ""
    error: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "model": self.model,
            "error": self.error,
            "at": round(self.at, 3),
        }
