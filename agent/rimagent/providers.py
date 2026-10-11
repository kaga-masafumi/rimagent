"""Provider-specific Chat Completions parameters; the runner uses a common reply."""
from __future__ import annotations

from copy import deepcopy
import os
from typing import Any


class QwenProvider:
    name = "qwen"
    retry_empty = True

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.base_url = cfg.get("base_url", "http://127.0.0.1:8000/v1")
        self.api_key = cfg.get("api_key", "not-needed")

    def request(self, messages, tools, *, model, thinking, max_tokens, temperature, tool_choice):
        kw = {"model": model, "messages": messages, "max_tokens": max_tokens,
              "temperature": temperature, "extra_body": {"chat_template_kwargs": {"enable_thinking": thinking}}}
        if tools:
            kw["tools"] = tools
            if tool_choice:
                kw["tool_choice"] = tool_choice
        return kw


def _schema(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "array" or (isinstance(node.get("type"), list) and "array" in node["type"]):
            node.setdefault("items", {})
        for value in node.values():
            _schema(value)
    elif isinstance(node, list):
        for value in node:
            _schema(value)


class OpenAIProvider:
    name = "openai"
    retry_empty = False

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        # Ignore the inherited Qwen base_url/api_key when selecting OpenAI.
        self.base_url = "https://api.openai.com/v1"
        self.api_key = os.environ.get(cfg.get("api_key_env", "OPENAI_API_KEY"), "").strip()
        if not self.api_key:
            raise ValueError("Set the OPENAI_API_KEY environment variable (or llm.api_key_env) before using OpenAI")
        self.effort = cfg.get("reasoning_effort", "low")
        if self.effort not in ("none", "low", "medium", "high", "xhigh", "max", None):
            raise ValueError("Invalid llm.reasoning_effort")

    def request(self, messages, tools, *, model, thinking, max_tokens, temperature, tool_choice):
        kw = {"model": model, "messages": messages, "max_completion_tokens": max_tokens,
              "service_tier": "default", "store": False}
        if self.effort is not None:
            kw["reasoning_effort"] = self.effort if thinking else "none"
        if tools:
            specs = deepcopy(tools)
            for spec in specs:
                spec["function"]["strict"] = False
                _schema(spec["function"].get("parameters", {}))
            kw["tools"] = specs
            if tool_choice:
                kw["tool_choice"] = tool_choice
        return kw


def provider_for(cfg: dict[str, Any]):
    name = cfg.get("provider", "qwen")
    if name == "qwen":
        return QwenProvider(cfg)
    if name == "openai":
        return OpenAIProvider(cfg)
    raise ValueError(f"Unknown llm.provider: {name!r}; choose qwen or openai")
