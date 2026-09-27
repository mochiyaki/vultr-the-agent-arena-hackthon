"""Vultr Serverless Inference client (OpenAI-compatible chat completions with tool calling).

All agent reasoning goes through this module so the mandatory Vultr inference path is
enforced in one place. The interface is small on purpose so tests can inject a fake.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import settings

log = logging.getLogger("brz.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_tool_calls: list[dict] = field(default_factory=list)  # echoed back into history
    usage: dict[str, Any] = field(default_factory=dict)


class LLM(Protocol):
    model: str

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.2, max_tokens: int = 2048) -> LLMReply: ...


class VultrInference:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key or settings.vultr_inference_api_key
        self.base_url = (base_url or settings.vultr_inference_base_url).rstrip("/")
        self.model = model or settings.vultr_inference_model
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))

    async def chat(self, messages, tools=None, temperature=0.2, max_tokens=2048) -> LLMReply:
        if not self.api_key:
            raise RuntimeError("VULTR_INFERENCE_API_KEY is not set")
        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Vultr inference error {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        msg = data["choices"][0]["message"]
        calls: list[ToolCall] = []
        raw_calls = msg.get("tool_calls") or []
        for tc in raw_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(tc.get("id") or fn.get("name", "call"), fn.get("name", ""), args))
        return LLMReply(content=msg.get("content") or "", tool_calls=calls,
                        raw_tool_calls=raw_calls, usage=data.get("usage") or {})

    async def aclose(self) -> None:
        await self._client.aclose()


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Robustly pull a JSON object/array out of an LLM reply."""
    text = text.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # fall back to the outermost braces/brackets
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = text.find(open_c), text.rfind(close_c)
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in model output")
