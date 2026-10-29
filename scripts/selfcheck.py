"""启动前自检：别让人对着一个空白页面猜哪里错了。

    python scripts/selfcheck.py

只检查，不修改。硬性缺失（Python 版本、核心依赖）返回 1，其余只警告。
"""
from __future__ import annotations

import importlib
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, BAD = "✓", "!", "✗"
problems = 0
warnings = 0


def line(mark: str, text: str, hint: str = "") -> None:
    print(f"  {mark} {text}")
    if hint:
        print(f"      → {hint}")


def need(name: str, why: str) -> bool:
    global problems
    try:
        importlib.import_module(name)
        line(OK, f"{name}")
        return True
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"{name} 缺失（{why}）", f"pip install {name}")
        problems += 1
        return False


def optional(name: str, why: str, install: str) -> None:
    global warnings
    try:
        importlib.import_module(name)
        line(OK, f"{name}（可选）")
    except Exception:  # noqa: BLE001
        line(WARN, f"{name} 没装 —— {why}", install)
        warnings += 1


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    global problems, warnings
    print("\n解机 jieji · 启动自检\n" + "─" * 46)

    print("[环境]")
    v = sys.version_info
    if v >= (3, 10):
        line(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        line(BAD, f"Python {v.major}.{v.minor} 太老", "需要 3.10 或更高")
        problems += 1

    print("[核心依赖]")
    for mod, why in (
        ("fastapi", "HTTP 服务"),
        ("uvicorn", "ASGI 服务器"),
        ("httpx", "调用大模型"),
        ("numpy", "音频处理"),
        ("PIL", "图像处理"),
    ):
        need(mod, why)

    print("[可选依赖]")
    optional("cv2", "接 USB 摄像头 / 视频文件时要用", "pip install opencv-python")
    optional("faster_whisper", "本机语音识别（ASR_PROVIDER=local）", "pip install faster-whisper")
    optional("websockets", "连 ESP32 遥控器桥时要用", "pip install websockets")
    optional("serial", "串口直驱电机时要用", "pip install pyserial")

    print("[配置]")
    try:
        from app.config import get_settings

        s = get_settings()
        line(OK, f"模型 {s.llm_model} @ {s.llm_base_url}")
        if s.llm_api_key:
            line(OK, f"API Key 已配置（{s.llm_api_key[:4]}…{s.llm_api_key[-4:]}）")
        elif "127.0.0.1" in s.llm_base_url or "localhost" in s.llm_base_url:
            line(WARN, "没填 Key —— 本机模型通常不需要，直接跑")
            warnings += 1
        else:
            line(WARN, "没填 LLM_API_KEY，机器人将不会做决策", "写进 .env 或网页 ⚙ 里填")
            warnings += 1

        line(OK, f"眼睛 {s.camera_source} · 耳朵 {s.audio_source}/{s.asr_provider} · 身体 {s.driver}")
        if s.driver in ("ir", "button") and not s.driver_bridge_url:
            line(BAD, "选了遥控器方案但没填桥地址", "DRIVER_BRIDGE_URL=ws://<ESP32 的 IP>:81")
            problems += 1
        if (ROOT / "data" / "settings.json").exists():
            line(OK, "data/settings.json 存在（网页里改过的配置会覆盖 .env）")
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"读配置失败：{exc}")
        problems += 1

    print("[端口]")
    try:
        from app.config import get_settings

        port = get_settings().port
    except Exception:  # noqa: BLE001
        port = 8790
    if port_free(port):
        line(OK, f"{port} 空闲")
    else:
        line(WARN, f"{port} 已被占用 —— 可能已经开着一个了", "改 .env 里的 PORT，或先关掉旧的")
        warnings += 1

    print("\n" + "─" * 46)
    if problems:
        print(f"有 {problems} 个必须解决的问题，先处理再启动。")
        return 1
    print(f"可以启动。{'（' + str(warnings) + ' 个提醒，不影响本地试用）' if warnings else ''}")
    print("  python serve.py   →  http://127.0.0.1:%d\n" % port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
