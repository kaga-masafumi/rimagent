"""Common chat interface with provider adapters and per-attempt budget accounting."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from openai import APIStatusError, OpenAI

from .config import CONFIG
from .paths import RUNS, SFT_RAW
from .providers import provider_for
from .pricing import price_for
from .usage import LLMBlocked, UsageLedger, estimate_input

_TOOLS_DIR = SFT_RAW / "tools"
_TOOLS_DIR.mkdir(parents=True, exist_ok=True)


def _tools_ref(tools: list[dict[str, Any]] | None) -> str | None:
    """Dedup tool schemas (repeated near-identically on every call) into sft/raw/tools/<hash>.json, return the hash."""
    if not tools:
        return None
    blob = json.dumps(tools, sort_keys=True).encode()
    h = hashlib.sha256(blob).hexdigest()[:16]
    p = _TOOLS_DIR / f"{h}.json"
    if not p.exists():
        p.write_bytes(blob)
    return h


@dataclass
class LLMReply:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0
    finish_reason: str = ""
    provider: str = ""
    request_id: str = ""


def validate_configuration(cfg: dict[str, Any]) -> dict[str, str]:
    """No network calls: used before Windows starts Steam or the agent."""
    c = cfg["llm"]
    provider = provider_for(c)
    price = price_for(provider.name, c["model"], cfg.get("prices"))
    if provider.name == "openai" and price is None:
        raise ValueError(f"No verified price for openai/{c['model']}; configure prices or choose gpt-5.6-luna")
    if int(c.get("max_streams", 4)) <= 0 or int(c.get("max_tokens", 4000)) <= 0:
        raise ValueError("llm.max_streams and llm.max_tokens must be positive")
    budget = cfg.get("budget") or {}
    for key, default in (("daily_usd", 1), ("monthly_usd", 10)):
        UsageLedger._limit(budget.get(key, default))
    return {"provider": provider.name, "model": c["model"], "price": "configured" if price else "unpriced"}


class LLM:
    def __init__(self, cfg: dict[str, Any] | None = None, *, client=None, ledger=None, emit=None, on_hold=None):
        cfg = CONFIG if cfg is None else cfg
        validate_configuration(cfg)
        c = cfg["llm"]
        self.provider = provider_for(c)
        self.model = c["model"]
        self.price = price_for(self.provider.name, self.model, cfg.get("prices"))
        self.ledger = ledger or UsageLedger(RUNS / "usage.sqlite3", cfg.get("budget"), emit=emit, on_hold=on_hold)
        self.client = client or OpenAI(base_url=self.provider.base_url, api_key=self.provider.api_key,
                                       timeout=c.get("timeout_s", 240), max_retries=0)
        self._sem = threading.Semaphore(int(c.get("max_streams", 4)))
        self.default_thinking = bool(c.get("thinking", True))
        self.max_tokens = int(c.get("max_tokens", 4000))
        self._capture_fh = None
        if bool(c.get("capture", True)):
            self._capture_fh = (SFT_RAW / f"capture-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl").open("a", encoding="utf-8")
        self._capture_lock = threading.Lock()

    def _capture(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, reply: "LLMReply", meta: dict[str, Any] | None) -> None:
        if not self._capture_fh:
            return
        rec = {"t": time.time(), "model": self.model, "provider": self.provider.name, "meta": meta or {}, "messages": messages,
               "tools_ref": _tools_ref(tools), "reply": {"content": reply.content, "reasoning": reply.reasoning, "tool_calls": reply.tool_calls},
               "usage": reply.usage, "elapsed": reply.elapsed}
        try:
            with self._capture_lock:
                self._capture_fh.write(json.dumps(rec, default=str) + "\n")
                self._capture_fh.flush()
        except Exception:  # noqa: BLE001 — capture must never break play
            pass

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, thinking: bool | None = None, max_tokens: int | None = None, temperature: float = 0.6, tool_choice: str | None = None, meta: dict[str, Any] | None = None) -> LLMReply:
        thinking = self.default_thinking if thinking is None else thinking
        limit = self.max_tokens if max_tokens is None else max_tokens
        if limit <= 0:
            raise ValueError("max_tokens must be positive")
        kwargs = self.provider.request(messages, tools, model=self.model, thinking=thinking,
                                       max_tokens=limit, temperature=temperature, tool_choice=tool_choice)
        t0 = time.time()
        with self._sem:
            rid = self.ledger.reserve(self.provider.name, self.model, self.price,
                                      estimate_input(messages, kwargs.get("tools")), limit, meta)
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except APIStatusError as exc:
                # 4xx rejection is known; timeouts/server errors can have incurred usage.
                status = exc.status_code
                code = getattr(exc, "code", None)
                diagnostic = f"HTTP {status}: {code or 'api_error'}"
                if status == 400 and isinstance(exc.body, dict):
                    body = exc.body.get("error", exc.body)
                    if isinstance(body, dict):
                        # Record schema diagnostics, never request headers/bodies or SDK reprs.
                        detail = str(body.get("message") or "")
                        key = self.provider.api_key
                        if key:
                            detail = detail.replace(key, "[REDACTED]")
                        detail = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", detail)
                        param = str(body.get("param") or "")
                        if re.fullmatch(r"[A-Za-z0-9_.\[\]]{1,120}", param):
                            diagnostic += f" ({param})"
                        if detail:
                            diagnostic += ": " + detail[:800]
                blocked = self.provider.name == "openai" and (status in (400, 401, 403, 404) or code in (
                    "insufficient_quota", "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
                    "organization_usage_limit_exceeded", "credit_balance_exhausted"))
                self.ledger.failed(rid, uncertain=status >= 500 or status == 408,
                                   error=diagnostic, blocking=blocked)
                self.ledger.ensure_active()
                raise
            except Exception:
                # Do not log request bodies/credentials from SDK exception strings.
                self.ledger.failed(rid, uncertain=True, error="transport_error")
                self.ledger.ensure_active()
                raise
            # Persist usage BEFORE parsing the response, empty-answer retries, or capture.
            try:
                raw_usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
            except Exception:
                self.ledger.failed(rid, uncertain=True, error="usage_decode_error")
                self.ledger.ensure_active()
                raise
            request_id = getattr(resp, "_request_id", None) or getattr(resp, "id", None)
            self.ledger.complete(rid, raw_usage, request_id=request_id)
        msg = resp.choices[0].message
        reply = LLMReply(elapsed=time.time() - t0, provider=self.provider.name,
                         finish_reason=resp.choices[0].finish_reason or "", request_id=request_id or "")
        reply.content = msg.content or ""
        raw = msg.model_dump()
        reply.raw_message = raw
        reply.reasoning = raw.get("reasoning_content") or raw.get("reasoning") or ""
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": tc.function.arguments}
                reply.tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        if raw_usage:
            reply.usage = raw_usage
        self._capture(messages, tools, reply, meta)
        self.ledger.ensure_active()  # a concurrent stream or this response may have latched a hold
        # Qwen sometimes returns nothing after a long think; retry once without thinking.
        if self.provider.retry_empty and thinking and not reply.content.strip() and not reply.tool_calls:
            retry_meta = {**(meta or {}), "retry": "empty_without_thinking"}
            return self.chat(messages, tools, thinking=False, max_tokens=max_tokens, temperature=temperature, tool_choice=tool_choice, meta=retry_meta)
        return reply

    def ensure_active(self):
        self.ledger.ensure_active()

    def assistant_message(self, reply: LLMReply) -> dict[str, Any]:
        """The assistant turn to append to history (tool calls preserved, reasoning dropped)."""
        m: dict[str, Any] = {"role": "assistant", "content": reply.content or ""}
        if reply.tool_calls:
            m["tool_calls"] = [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}} for tc in reply.tool_calls]
        return m
