"""配置：环境变量 → 运行时设置（可热改，落盘 data/settings.json）。

优先级：data/settings.json（网页里改的） > 环境变量/.env > 代码默认值。

设计原则：所有字段都能在网页「设置」里改，改完立刻生效，不用重启；
Key 只在服务端使用，对外只回显掩码。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_DIR = Path(os.environ.get("JIEJI_DATA_DIR") or (ROOT / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
EPISODE_DIR = DATA_DIR / "episodes"
CACHE_DIR = DATA_DIR / "cache"
IR_CODES_FILE = DATA_DIR / "ir_codes.json"
ENV_FILE = ROOT / ".env"


# --------------------------------------------------------------------- .env
def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """极简 .env 解析（和 jiehua 一样，不引第三方）。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        eq = line.find("=")
        if eq < 1:
            continue
        key = line[:eq].strip()
        value = line[eq + 1 :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


_ENV = load_env()


def env(key: str, default: str = "") -> str:
    """环境变量优先，其次 .env，最后默认值。"""
    if key in os.environ and os.environ[key] != "":
        return os.environ[key]
    return _ENV.get(key, default)


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(key: str, default: bool) -> bool:
    raw = env(key, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on", "y", "是")


# ------------------------------------------------------------------ settings
@dataclass
class Settings:
    """全部可调项。分组只是为了好读，落盘时是平铺的。"""

    # --- 服务 ---
    host: str = field(default_factory=lambda: env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: env_int("PORT", 8790))

    # --- 大脑（任意 OpenAI 兼容服务：DeepSeek / OpenAI / 智谱 / 通义 / Ollama）---
    llm_base_url: str = field(default_factory=lambda: env("LLM_BASE_URL", "https://api.deepseek.com"))
    llm_api_key: str = field(default_factory=lambda: env("LLM_API_KEY", env("DEEPSEEK_API_KEY", "")))
    llm_model: str = field(default_factory=lambda: env("LLM_MODEL", "deepseek-flash"))
    # 该模型能不能吃图片（Ollama 的 qwen2.5vl / llava 也能）
    llm_vision: bool = field(default_factory=lambda: env_bool("LLM_VISION", True))
    # DeepSeek 官方参数：关掉思考链，单次决策 ~0.9s；别家服务会自动剥掉
    llm_thinking: str = field(default_factory=lambda: env("LLM_THINKING", "disabled"))
    llm_effort: str = field(default_factory=lambda: env("LLM_EFFORT", "low"))
    llm_max_tokens: int = field(default_factory=lambda: env_int("LLM_MAX_TOKENS", 320))
    llm_temperature: float = field(default_factory=lambda: env_float("LLM_TEMPERATURE", 0.2))
    llm_timeout: float = field(default_factory=lambda: env_float("LLM_TIMEOUT", 45))
    llm_retries: int = field(default_factory=lambda: env_int("LLM_RETRIES", 2))
    # 发给模型的图片：low=512px 便宜快，high=768px 看得清小字
    vision_detail: str = field(default_factory=lambda: env("VISION_DETAIL", "low"))

    # --- 眼睛 / 耳朵 ---
    camera_source: str = field(default_factory=lambda: env("CAMERA_SOURCE", "mock"))  # mock|opencv|file
    camera_index: int = field(default_factory=lambda: env_int("CAMERA_INDEX", 0))
    camera_width: int = field(default_factory=lambda: env_int("CAMERA_WIDTH", 640))
    camera_height: int = field(default_factory=lambda: env_int("CAMERA_HEIGHT", 480))
    camera_fps: int = field(default_factory=lambda: env_int("CAMERA_FPS", 8))
    camera_file: str = field(default_factory=lambda: env("CAMERA_FILE", ""))

    audio_source: str = field(default_factory=lambda: env("AUDIO_SOURCE", "mock"))  # mock|mic|file
    audio_device: str = field(default_factory=lambda: env("AUDIO_DEVICE", ""))
    audio_file: str = field(default_factory=lambda: env("AUDIO_FILE", ""))
    audio_chunk_ms: int = field(default_factory=lambda: env_int("AUDIO_CHUNK_MS", 900))
    vad_threshold: float = field(default_factory=lambda: env_float("VAD_THRESHOLD", 0.012))
    asr_provider: str = field(default_factory=lambda: env("ASR_PROVIDER", "mock"))  # mock|local|cloud
    asr_model: str = field(default_factory=lambda: env("ASR_MODEL", "small"))
    asr_language: str = field(default_factory=lambda: env("ASR_LANGUAGE", "zh"))
    cloud_asr_base_url: str = field(default_factory=lambda: env("CLOUD_ASR_BASE_URL", "https://api.openai.com/v1"))
    cloud_asr_api_key: str = field(default_factory=lambda: env("CLOUD_ASR_API_KEY", ""))
    cloud_asr_model: str = field(default_factory=lambda: env("CLOUD_ASR_MODEL", "whisper-1"))

    # --- 手（身体驱动）---
    driver: str = field(default_factory=lambda: env("DRIVER", "mock"))  # mock|ir|button|bridge|serial
    driver_bridge_url: str = field(default_factory=lambda: env("DRIVER_BRIDGE_URL", "ws://192.168.4.1:81"))
    driver_serial_port: str = field(default_factory=lambda: env("DRIVER_SERIAL_PORT", "COM3"))
    driver_serial_baud: int = field(default_factory=lambda: env_int("DRIVER_SERIAL_BAUD", 115200))
    # 遥控器按键 → 动作名（真实按键名以「教它按键」页面抓到的为准）
    ir_repeat_interval_ms: int = field(default_factory=lambda: env_int("IR_REPEAT_INTERVAL_MS", 120))
    press_hold_ms: int = field(default_factory=lambda: env_int("PRESS_HOLD_MS", 120))

    # --- 主循环 ---
    auto_mode: bool = field(default_factory=lambda: env_bool("AUTO_MODE", False))
    tick_ms: int = field(default_factory=lambda: env_int("TICK_MS", 250))
    decision_interval_ms: int = field(default_factory=lambda: env_int("DECISION_INTERVAL_MS", 1800))
    # 默认关：不碰你的音箱。要出声得自己在网页上勾「朗读」。
    speak_reply: bool = field(default_factory=lambda: env_bool("SPEAK_REPLY", False))
    keep_history: int = field(default_factory=lambda: env_int("KEEP_HISTORY", 6))

    # --- 安全护栏 ---
    max_speed: float = field(default_factory=lambda: env_float("MAX_SPEED", 0.6))
    max_duration_ms: int = field(default_factory=lambda: env_int("MAX_DURATION_MS", 2500))
    min_action_interval_ms: int = field(default_factory=lambda: env_int("MIN_ACTION_INTERVAL_MS", 250))
    watchdog_ms: int = field(default_factory=lambda: env_int("WATCHDOG_MS", 6000))
    stale_frame_ms: int = field(default_factory=lambda: env_int("STALE_FRAME_MS", 3000))
    estop_on_start: bool = field(default_factory=lambda: env_bool("ESTOP_ON_START", False))
    # 反射层：正前方比这个距离近就禁止前进（没装超声波时靠画面估，装了就用传感器）
    reflex_enabled: bool = field(default_factory=lambda: env_bool("REFLEX_ENABLED", True))
    reflex_stop_m: float = field(default_factory=lambda: env_float("REFLEX_STOP_M", 0.25))
    # 唤醒词：开着的话只有听到「小解/解机/机器人」才理人
    wake_required: bool = field(default_factory=lambda: env_bool("WAKE_REQUIRED", False))

    # --- 记录 ---
    record_episodes: bool = field(default_factory=lambda: env_bool("RECORD_EPISODES", True))

    # ------------------------------------------------------------------ 方法
    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public(self) -> dict[str, Any]:
        """给前端看的版本：Key 打码。"""
        data = asdict(self)
        for key in ("llm_api_key", "cloud_asr_api_key"):
            value = data.get(key) or ""
            data[key] = mask(value)
            data[f"{key}_set"] = bool(value)
        return data

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """热更新：只认已知字段，做类型转换与夹紧，返回实际生效的变更。"""
        known = {f.name: f for f in fields(self)}
        changed: dict[str, Any] = {}
        for key, raw in (patch or {}).items():
            if key not in known:
                continue
            if key in ("llm_api_key", "cloud_asr_api_key") and (raw is None or str(raw).startswith("••")):
                continue  # 前端回传的掩码，忽略
            cur = getattr(self, key)
            try:
                value = _coerce(cur, raw)
            except (TypeError, ValueError):
                continue
            if value != cur:
                setattr(self, key, value)
                changed[key] = value
        if changed:
            self.save()
        return changed

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = self.as_dict()
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)

    @classmethod
    def load(cls) -> "Settings":
        """构造：先拿环境变量/默认值，再让 settings.json 覆盖。"""
        inst = cls()
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        known = {f.name: f for f in fields(cls)}
        for key, raw in saved.items():
            if key not in known:
                continue
            try:
                setattr(inst, key, _coerce(getattr(inst, key), raw))
            except (TypeError, ValueError):
                pass
        return inst

    # 夹紧到合理区间，避免网页里填个 999 把舵机烧了
    def clamp(self) -> None:
        self.port = int(min(65535, max(1, self.port)))
        self.camera_index = max(0, self.camera_index)
        self.camera_fps = int(min(30, max(1, self.camera_fps)))
        self.camera_width = int(min(1920, max(160, self.camera_width)))
        self.camera_height = int(min(1080, max(120, self.camera_height)))
        self.llm_max_tokens = int(min(2000, max(64, self.llm_max_tokens)))
        self.llm_timeout = float(min(180, max(3, self.llm_timeout)))
        self.max_speed = float(min(1.0, max(0.05, self.max_speed)))
        self.max_duration_ms = int(min(3000, max(100, self.max_duration_ms)))
        self.min_action_interval_ms = int(min(2000, max(0, self.min_action_interval_ms)))
        self.decision_interval_ms = int(min(20000, max(400, self.decision_interval_ms)))
        self.tick_ms = int(min(2000, max(50, self.tick_ms)))
        self.watchdog_ms = int(min(60000, max(500, self.watchdog_ms)))
        self.reflex_stop_m = float(min(2.0, max(0.05, self.reflex_stop_m)))
        self.keep_history = int(min(50, max(1, self.keep_history)))
        self.audio_chunk_ms = int(min(5000, max(300, self.audio_chunk_ms)))
        if self.llm_effort not in ("low", "high", "max"):
            self.llm_effort = "low"
        if self.llm_thinking not in ("disabled", "enabled"):
            self.llm_thinking = "disabled"
        if self.vision_detail not in ("low", "high", "auto"):
            self.vision_detail = "low"


def _coerce(current: Any, raw: Any) -> Any:
    if isinstance(current, bool):
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on", "y")
        return bool(raw)
    if isinstance(current, int):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    return str(raw)


def mask(value: str) -> str:
    value = value or ""
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:5]}••••{value[-4:]}"


# 全局单例：整个进程共用一份（网页改了立即生效）
_settings: Settings | None = None
_lock = threading.Lock()


def get_settings() -> Settings:
    global _settings
    with _lock:
        if _settings is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            for sub in (EPISODE_DIR, CACHE_DIR):
                sub.mkdir(parents=True, exist_ok=True)
            _settings = Settings.load()
            _settings.clamp()
        return _settings


def reload_settings() -> Settings:
    global _settings
    with _lock:
        _settings = Settings.load()
        _settings.clamp()
        return _settings
