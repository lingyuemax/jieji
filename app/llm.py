"""模型调用层：任何 OpenAI 兼容的 /chat/completions 都能接。

实测（DeepSeek 官方，2026-10，deepseek-flash）：
    关掉思考链 + JSON 模式 + 512px 图片，一次「看图决策」约 0.9~1.3 秒。
    开着思考链会先吐 reasoning_content，同样的问题要 2~4 秒，而且容易把
    max_tokens 烧光导致 content 为空 —— 机器人实时闭环等不起，所以默认关。

兼容策略：不认识的参数逐个剥掉重试（不同服务对 response_format / thinking /
effort / temperature 的支持不一样），最后还能从纯文本里把 JSON 抠出来。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("jieji.llm")


class LLMError(RuntimeError):
    """调用失败（网络、鉴权、限流重试后仍失败）。"""


@dataclass
class LLMResult:
    text: str = ""
    model: str = ""
    latency_ms: int = 0
    tokens: int = 0
    cached_tokens: int = 0
    finish_reason: str = ""
    dropped: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text[:400],
            "model": self.model,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "cached_tokens": self.cached_tokens,
            "finish_reason": self.finish_reason,
            "dropped": self.dropped,
        }


# 依次尝试丢弃：不同服务认识的东西不一样
DEGRADE_ORDER = ["response_format", "thinking", "effort", "temperature", "max_tokens"]


def image_part(jpeg: bytes, detail: str = "low") -> dict[str, Any]:
    b64 = base64.b64encode(jpeg).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": detail},
    }


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


class LLMClient:
    """一个够笨但够稳的客户端：不缓存、不并发控制，只负责把话说清楚。"""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self.calls = 0
        self.errors = 0
        self.last: LLMResult | None = None

    # ------------------------------------------------------------ 基础设施
    def _base(self) -> str:
        base = (self.settings.llm_base_url or "").strip().rstrip("/")
        if not base:
            raise LLMError("没有配置 LLM_BASE_URL")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/chat/completions"

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.llm_timeout, connect=8.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = (self.settings.llm_api_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # ------------------------------------------------------------ 一次调用
    async def chat(
        self,
        system: str,
        user: str | list[dict[str, Any]],
        *,
        jpeg: bytes | None = None,
        json_mode: bool = True,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        content: list[dict[str, Any]] | str = user
        if jpeg is not None and self.settings.llm_vision:
            detail = self.settings.vision_detail
            parts = [text_part(user if isinstance(user, str) else json.dumps(user, ensure_ascii=False))]
            parts.append(image_part(jpeg, detail))
            content = parts

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        body: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            "temperature": self.settings.llm_temperature,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        # DeepSeek 官方：thinking + effort；别的服务不认识会 400，下面自动剥掉
        if self.settings.llm_thinking == "disabled":
            body["thinking"] = {"type": "disabled"}
        body["effort"] = self.settings.llm_effort

        dropped: list[str] = []
        attempts = 0
        max_attempts = len(DEGRADE_ORDER) + 1 + max(0, self.settings.llm_retries)
        last_error = ""
        empty_retry = 0

        while attempts < max_attempts:
            attempts += 1
            t0 = time.time()
            try:
                client = await self.client()
                resp = await client.post(
                    self._base(),
                    headers=self._headers(),
                    json=body,
                    timeout=timeout or self.settings.llm_timeout,
                )
            except httpx.HTTPError as exc:
                self.errors += 1
                last_error = f"网络错误：{exc!r}"
                if attempts < max_attempts:
                    await asyncio.sleep(min(2.0, 0.4 * attempts))
                    continue
                raise LLMError(last_error) from exc

            latency = int((time.time() - t0) * 1000)

            if resp.status_code == 200:
                data = resp.json()
                result = self._parse(data, latency)
                result.dropped = dropped
                # 空 content：思考链吃掉了预算，放开 max_tokens 再试一次
                if not result.text.strip() and empty_retry < 1:
                    empty_retry += 1
                    body["max_tokens"] = min(1200, int(body["max_tokens"]) * 3)
                    body["thinking"] = {"type": "disabled"}
                    log.warning("模型返回空 content（finish=%s），放大 max_tokens 重试", result.finish_reason)
                    continue
                self.calls += 1
                self.last = result
                return result

            text = resp.text[:400]
            if resp.status_code in (401, 403):
                self.errors += 1
                raise LLMError(f"鉴权失败（{resp.status_code}）：Key 不对或没权限。{text}")
            if resp.status_code == 429 or resp.status_code >= 500:
                self.errors += 1
                last_error = f"服务端 {resp.status_code}：{text}"
                if attempts < max_attempts:
                    await asyncio.sleep(min(3.0, 0.6 * attempts))
                    continue
                raise LLMError(last_error)
            if resp.status_code in (400, 404, 422):
                param = _which_param_to_drop(text, body, DEGRADE_ORDER)
                if param and param in body:
                    body.pop(param, None)
                    dropped.append(param)
                    log.info("服务不认识参数 %s，剥掉重试（%s）", param, text[:120])
                    continue
                self.errors += 1
                raise LLMError(f"请求被拒（{resp.status_code}）：{text}")
            self.errors += 1
            raise LLMError(f"HTTP {resp.status_code}：{text}")

        self.errors += 1
        raise LLMError(last_error or "重试次数用尽")

    def _parse(self, data: dict[str, Any], latency: int) -> LLMResult:
        choices = data.get("choices") or []
        if not choices:
            return LLMResult(latency_ms=latency, raw=data)
        choice = choices[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        return LLMResult(
            text=(message.get("content") or "").strip(),
            model=data.get("model") or self.settings.llm_model,
            latency_ms=latency,
            tokens=int(usage.get("total_tokens") or 0),
            cached_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
            finish_reason=choice.get("finish_reason") or "",
            raw=data,
        )

    # ------------------------------------------------------------ 体检
    async def ping(self) -> dict[str, Any]:
        t0 = time.time()
        try:
            result = await self.chat(
                "你是连通性探针，只回四个字：连接正常",
                "只回四个字：连接正常",
                json_mode=False,
                max_tokens=16,
                timeout=20,
            )
            return {
                "ok": bool(result.text),
                "ms": int((time.time() - t0) * 1000),
                "model": result.model,
                "reply": result.text[:40],
                "tokens": result.tokens,
                "vision": self.settings.llm_vision,
            }
        except LLMError as exc:
            return {"ok": False, "ms": int((time.time() - t0) * 1000), "error": str(exc)}

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "errors": self.errors,
            "last": self.last.to_dict() if self.last else None,
        }


def _which_param_to_drop(text: str, body: dict[str, Any], order: list[str]) -> str | None:
    """从报错文本里猜是哪个参数不认；猜不出就按顺序剥第一个还在的。"""
    lower = text.lower()
    hints = {
        "response_format": ("response_format", "json_object", "response format"),
        "thinking": ("thinking", "reasoning"),
        "effort": ("effort",),
        "temperature": ("temperature",),
        "max_tokens": ("max_tokens", "max tokens"),
    }
    for param in order:
        if param not in body:
            continue
        if any(h in lower for h in hints.get(param, (param,))):
            return param
    for param in order:
        if param in body:
            return param
    return None
