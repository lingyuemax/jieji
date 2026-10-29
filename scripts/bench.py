"""量真实数字：把 README 里的"1.2 秒"和"1000 token"重新测一遍。

    python serve.py              # 另开一个窗口
    python scripts/bench.py      # 默认 6 条指令，各跑一次
    python scripts/bench.py 20   # 想更稳就跑 20 次

需要服务端配好真模型（不是 mock）。会先解除急停，跑完不停机器人。
"""
from __future__ import annotations

import json
import statistics
import sys
import time

import httpx

BASE = "http://127.0.0.1:8790"
DEFAULT_TEXTS = [
    "往前走一小步",
    "左边有什么？转过去看看",
    "前面挡路了，绕一下",
    "停一下，我看看",
    "你好，你能做什么？",
    "后退一点，太近了",
]


def brain_history(client: httpx.Client) -> list[dict]:
    return client.get("/api/state").json()["brain"].get("history") or []


def ask(client: httpx.Client, text: str, timeout: float = 60.0) -> dict:
    """发一句话，等这一条真正落到 history 里（/api/chat 只是入队，立刻返回）。"""
    before = len(brain_history(client))
    t0 = time.time()
    client.post("/api/chat", json={"text": text})
    queued_ms = (time.time() - t0) * 1000

    deadline = time.time() + timeout
    while time.time() < deadline:
        history = brain_history(client)
        if len(history) > before:
            return {"历史": history[-1], "入队ms": round(queued_ms)}
        time.sleep(0.1)
    return {"历史": None, "入队ms": round(queued_ms), "错误": "等不到决策"}


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(DEFAULT_TEXTS)
    texts = (DEFAULT_TEXTS * ((limit // len(DEFAULT_TEXTS)) + 1))[:limit]

    with httpx.Client(base_url=BASE, timeout=120) as client:
        health = client.get("/api/health").json()
        print(f"服务：{health['model']} · 眼睛 {health['camera']} · 身体 {health['driver']}\n")
        client.post("/api/estop/clear")

        rows = []
        for i, text in enumerate(texts, 1):
            result = ask(client, text)
            entry = result.get("历史") or {}
            action = entry.get("action") or {}
            rows.append({
                "指令": text,
                "入队ms": result["入队ms"],
                "模型ms": entry.get("latency_ms"),
                "tokens": entry.get("tokens"),
                "动作": action.get("action"),
                "理由": (action.get("why") or "")[:38],
                "报错": entry.get("error") or "",
            })
            print(f"{i:2d}. {text:<18} → {str(action.get('action')):<10} "
                  f"{str(entry.get('latency_ms')):>5}ms  {str(entry.get('tokens')):>5}tok  "
                  f"{(action.get('why') or '')[:30]}")
            time.sleep(1.0)

        lat = [r["模型ms"] for r in rows if r["模型ms"]]
        tok = [r["tokens"] for r in rows if r["tokens"]]
        queued = [r["入队ms"] for r in rows]
        print("\n" + "-" * 60)
        if lat:
            print(f"模型往返：中位 {statistics.median(lat):.0f}ms  "
                  f"最快 {min(lat)}ms  最慢 {max(lat)}ms  （{len(lat)} 次）")
        if tok:
            print(f"单次 token：中位 {statistics.median(tok):.0f}  "
                  f"最少 {min(tok)}  最多 {max(tok)}")
        if queued:
            print(f"/api/chat 入队：中位 {statistics.median(queued):.0f}ms")
        errors = [r for r in rows if r["报错"]]
        if errors:
            print(f"有 {len(errors)} 次失败：{[r['报错'][:40] for r in errors]}")

        print("\n" + json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
