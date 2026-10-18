"""Thin OpenAI chat-completions wrapper with retries, usage accounting and a
uniform `step()` that returns either tool calls or final text.

Native function calling was verified on the interview endpoint (scripts/smoke_vlm.py).
A JSON action protocol fallback is kept behind `transport="json"` for endpoints
that reject `tools=`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import Settings
from .schemas import ToolCall

RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    latency_s: float = 0.0

    def add(self, resp, dt: float) -> None:
        u = getattr(resp, "usage", None)
        if u:
            self.prompt_tokens += u.prompt_tokens or 0
            self.completion_tokens += u.completion_tokens or 0
        self.calls += 1
        self.latency_s += dt

    def as_dict(self) -> Dict[str, Any]:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "calls": self.calls, "latency_s": round(self.latency_s, 2)}


@dataclass
class StepResult:
    content: Optional[str]
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw_message: Optional[Dict[str, Any]] = None  # assistant message to append to history
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    reasoning_chars: int = 0  # length of hidden reasoning (thinking models); content itself is not stored


def _is_retryable_status(e: BaseException) -> bool:
    return isinstance(e, APIStatusError) and e.status_code in (429, 500, 502, 503, 504)


class LLMClient:
    def __init__(self, settings: Settings, transport: str = "native"):
        settings.require_llm()
        self.settings = settings
        self.model = settings.model
        self.transport = transport
        self._client = OpenAI(base_url=settings.base_url, api_key=settings.api_key, timeout=settings.llm_timeout_s)
        self.usage = Usage()

    # -- raw call with retries -------------------------------------------------
    @retry(reraise=True, stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=30),
           retry=retry_if_exception_type(RETRYABLE) | retry_if_exception_type(APIStatusError))
    def _create(self, **kwargs):
        try:
            return self._client.chat.completions.create(model=self.model, temperature=0, seed=7, **kwargs)
        except APIStatusError as e:
            if _is_retryable_status(e):
                raise
            raise _NonRetryable(e) from e

    def complete(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
                 tool_choice: Union[str, Dict[str, Any], None] = None, response_format: Optional[Dict[str, Any]] = None,
                 max_tokens: Optional[int] = None, thinking: bool = True) -> StepResult:
        """thinking=False disables hidden reasoning (Qwen3-style chat_template_kwargs); use for
        formatting/grading steps where the model otherwise burns the whole completion budget."""
        kwargs: Dict[str, Any] = {"messages": messages}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if not thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        if tools and self.transport == "native":
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if response_format:
            kwargs["response_format"] = response_format
        t0 = time.time()
        try:
            resp = self._create(**kwargs)
        except _NonRetryable as e:
            raise e.__cause__  # type: ignore[misc]
        dt = time.time() - t0
        self.usage.add(resp, dt)
        msg = resp.choices[0].message
        u = resp.usage
        reasoning = getattr(msg, "reasoning_content", None) or (msg.model_extra or {}).get("reasoning_content") or ""
        res = StepResult(content=msg.content, latency_s=dt, reasoning_chars=len(reasoning),
                         prompt_tokens=(u.prompt_tokens if u else 0), completion_tokens=(u.completion_tokens if u else 0))
        raw: Dict[str, Any] = {"role": "assistant", "content": msg.content}
        if getattr(msg, "tool_calls", None):
            raw["tool_calls"] = []
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {"__parse_error__": tc.function.arguments}
                res.tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
                raw["tool_calls"].append({"id": tc.id, "type": "function",
                                          "function": {"name": tc.function.name, "arguments": tc.function.arguments}})
        elif tools and self.transport == "json" and msg.content:
            res.tool_calls = parse_json_actions(msg.content)
        res.raw_message = raw
        return res


class _NonRetryable(Exception):
    pass


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_actions(text: str) -> List[ToolCall]:
    """JSON action protocol fallback: {"action": "<tool>", "args": {...}}."""
    m = _JSON_BLOCK.search(text)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if isinstance(obj, dict) and "action" in obj:
        return [ToolCall(id="json_0", name=str(obj["action"]), arguments=obj.get("args") or {})]
    return []


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of the first JSON object in a model reply (handles ```json fences)."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    cand = fence.group(1) if fence else None
    if cand is None:
        m = _JSON_BLOCK.search(text)
        cand = m.group(0) if m else None
    if cand is None:
        return None
    try:
        obj = json.loads(cand)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
