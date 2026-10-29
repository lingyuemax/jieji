"""HTTP 服务：网页看到的、听到的、点的，全在这里。

只有一条原则：**这个服务默认只听 127.0.0.1**。机器人能动手，把它丢到公网上
等于把家门钥匙挂在门口。要远程看，就上加油站/反代 + 认证，别直连。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config as C
from .bus import EventBus
from .capture import list_audio_devices
from .drivers import DRIVER_KINDS, build_driver, load_codes
from .llm import LLMError
from .loop import AgentLoop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jieji.server")

REBUILD_KEYS = {
    "camera_source", "camera_index", "camera_width", "camera_height", "camera_fps", "camera_file",
    "audio_source", "audio_device", "audio_file", "asr_provider", "asr_model",
    "cloud_asr_base_url", "cloud_asr_api_key", "cloud_asr_model",
    "driver", "driver_bridge_url", "driver_serial_port", "driver_serial_baud",
}

bus = EventBus()
loop: AgentLoop | None = None
START_AT = time.time()
BOOT_ERROR = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop, BOOT_ERROR
    settings = C.get_settings()
    loop = AgentLoop(settings, bus)
    try:
        await loop.start()
    except Exception as exc:  # noqa: BLE001
        BOOT_ERROR = str(exc)
        log.exception("启动失败")
    log.info("jieji %s 就绪：http://%s:%s", __version__, settings.host, settings.port)
    try:
        yield
    finally:
        if loop is not None:
            await loop.stop()


app = FastAPI(title="jieji · 解机", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------- 静态页面
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(C.WEB_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


app.mount("/web", StaticFiles(directory=str(C.WEB_DIR)), name="web")


def need_loop() -> AgentLoop:
    if loop is None:
        raise HTTPException(503, "主循环还没启动")
    return loop


# ---------------------------------------------------------------- 状态
@app.get("/api/health")
async def health():
    s = C.get_settings()
    return {
        "ok": loop is not None and loop._started,
        "version": __version__,
        "uptime_s": round(time.time() - START_AT, 1),
        "boot_error": BOOT_ERROR,
        "mode": loop.mode if loop else "down",
        "driver": loop.driver.name if loop and loop.driver else None,
        "camera": loop.camera.source if loop and loop.camera else None,
        "audio": loop.audio.source if loop and loop.audio else None,
        "asr": s.asr_provider,
        "model": s.llm_model,
        "vision": s.llm_vision,
        "key_set": bool(s.llm_api_key),
        "estop": loop.gate.snapshot()["estop"] if loop else False,
        "sse_clients": bus.subscribers,
    }


@app.get("/api/state")
async def state():
    return need_loop().snapshot()


@app.get("/api/events")
async def events(request: Request):
    queue = await bus.subscribe()

    async def gen():
        try:
            yield ": jieji connected\n\n"
            snapshot = need_loop().snapshot()
            yield f"data: {json.dumps({'kind': 'snapshot', **snapshot}, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=12)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            await bus.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@app.get("/api/stream")
async def stream():
    agent = need_loop()

    async def gen():
        last = -1
        idle = 0
        while True:
            camera = agent.camera
            frame = camera.latest() if camera else None
            if frame is not None and frame.seq != last:
                last = frame.seq
                idle = 0
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(frame.jpeg)).encode() + b"\r\n\r\n" + frame.jpeg + b"\r\n")
            else:
                idle += 1
                if idle > 200:
                    break
                await asyncio.sleep(0.05)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@app.get("/api/snapshot.jpg")
async def snapshot():
    agent = need_loop()
    frame = agent.camera.latest() if agent.camera else None
    if frame is None:
        raise HTTPException(404, "还没有画面")
    return Response(frame.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/map.png")
async def map_png():
    agent = need_loop()
    if agent.sim is None:
        raise HTTPException(404, "当前不是模拟身体")
    return Response(agent.sim.render_map(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- 控制
@app.post("/api/chat")
async def chat(payload: dict[str, Any]):
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "说点什么")
    return await need_loop().submit_text(text, source="text")


@app.post("/api/mode")
async def mode(payload: dict[str, Any]):
    return await need_loop().set_mode(str(payload.get("mode") or "idle"))


@app.post("/api/manual")
async def manual(payload: dict[str, Any]):
    return await need_loop().manual(payload)


@app.post("/api/estop")
async def estop(payload: dict[str, Any] | None = None):
    reason = str((payload or {}).get("reason") or "手动急停")
    await need_loop().estop(reason)
    return {"ok": True, "estop": True, "reason": reason}


@app.post("/api/estop/clear")
async def clear_estop():
    await need_loop().clear_estop()
    return {"ok": True, "estop": False}


@app.post("/api/understand")
async def understand(payload: dict[str, Any] | None = None):
    text = str((payload or {}).get("text") or "")
    return await need_loop().understand_now(text)


# ---------------------------------------------------------------- 设置
@app.get("/api/settings")
async def get_settings():
    s = C.get_settings()
    return {
        "settings": s.public(),
        "drivers": DRIVER_KINDS,
        "audio_devices": [{"name": n, "kind": k} for n, k in list_audio_devices()],
        "asr_providers": ["mock", "local", "cloud"],
        "camera_sources": ["mock", "opencv", "file"],
        "audio_sources": ["mock", "mic", "file"],
    }


@app.post("/api/settings")
async def set_settings(payload: dict[str, Any]):
    s = C.get_settings()
    patch = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
    changed = s.update(patch or {})
    s.clamp()
    rebuilt = None
    if REBUILD_KEYS & set(changed.keys()):
        rebuilt = await need_loop().restart_io()
    return {"ok": True, "changed": changed, "settings": s.public(), "rebuilt": rebuilt}


@app.get("/api/llm/ping")
async def llm_ping():
    agent = need_loop()
    return await agent.llm.ping()


@app.get("/api/asr/status")
async def asr_status():
    agent = need_loop()
    from .asr import LocalASR

    info = agent.asr.info() if isinstance(agent.asr, LocalASR) else {"provider": agent.settings.asr_provider}
    return {"asr": info, "audio": agent.audio.status() if agent.audio else None}


# ---------------------------------------------------------------- 遥控器
def remote_driver():
    agent = need_loop()
    driver = agent.driver
    if driver is None or not hasattr(driver, "learn"):
        raise HTTPException(400, "当前驱动不是遥控器类（切到 ir 或 button）")
    return driver


@app.get("/api/ir/codes")
async def ir_codes():
    return {
        "codes": load_codes(),
        "driver": loop.driver.name if loop and loop.driver else None,
        "bridge": loop.driver.connection() if loop and loop.driver else None,
    }


@app.post("/api/ir/learn")
async def ir_learn(payload: dict[str, Any]):
    slot = str(payload.get("slot") or "").strip()
    if not slot:
        raise HTTPException(400, "缺少 slot（要绑定的动作名，如 forward / ok）")
    driver = remote_driver()
    try:
        return await driver.learn(slot)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.delete("/api/ir/codes/{slot}")
async def ir_forget(slot: str):
    driver = remote_driver()
    driver.forget(slot)
    return {"ok": True, "codes": load_codes()}


# ---------------------------------------------------------------- 数据
@app.get("/api/episodes")
async def episodes():
    items = []
    if C.EPISODE_DIR.exists():
        for path in sorted(C.EPISODE_DIR.iterdir(), reverse=True)[:30]:
            if not path.is_dir():
                continue
            file = path / "episode.jsonl"
            count = 0
            if file.exists():
                try:
                    count = sum(1 for _ in file.open("rb"))
                except OSError:
                    count = 0
            items.append({"id": path.name, "steps": count,
                          "frames": len(list((path / "frames").glob("*.jpg"))) if (path / "frames").exists() else 0})
    return {"episodes": items}


@app.get("/api/episodes/{episode_id}")
async def episode(episode_id: str, limit: int = 60):
    if "/" in episode_id or "\\" in episode_id or ".." in episode_id:
        raise HTTPException(400, "非法 id")
    file = C.EPISODE_DIR / episode_id / "episode.jsonl"
    if not file.exists():
        raise HTTPException(404, "没有这个片段")
    lines = file.read_text(encoding="utf-8").splitlines()[-max(1, min(500, limit)):]
    return {"id": episode_id, "entries": [json.loads(line) for line in lines if line.strip()]}


@app.get("/api/fs/dirs")
async def list_dirs(path: str = ""):
    """给「选视频文件」用的小工具（只看目录，不读内容）。"""
    base = Path(path) if path else Path.home()
    if not base.exists() or not base.is_dir():
        raise HTTPException(404, "目录不存在")
    items = []
    for child in sorted(base.iterdir()):
        try:
            items.append({"name": child.name, "dir": child.is_dir(), "size": child.stat().st_size})
        except OSError:
            continue
    return {"path": str(base), "items": items[:200]}


def main() -> None:
    import uvicorn

    settings = C.get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
