from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace

import httpx
from openai import APITimeoutError, BadRequestError, RateLimitError
from openai.types.chat import ChatCompletion
import pytest

from rimagent.config import _DEFAULTS
from rimagent.llm import LLM, validate_configuration
from rimagent.usage import LLMBlocked, UsageLedger


def response(content="hello", tools=None, *, with_usage=True, reasoning=""):
    message = {"role": "assistant", "content": content, "reasoning_content": reasoning}
    if tools:
        message["tool_calls"] = tools
    raw = {"id": "chat_test", "object": "chat.completion", "created": 1, "model": "model",
           "choices": [{"index": 0, "finish_reason": "tool_calls" if tools else "stop", "message": message}]}
    if with_usage:
        raw["usage"] = {"prompt_tokens": 10_000, "completion_tokens": 1000, "total_tokens": 11_000,
                        "prompt_tokens_details": {"cached_tokens": 0},
                        "completion_tokens_details": {"reasoning_tokens": 900}}
    return ChatCompletion.model_validate(raw)


class Client:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_llm(tmp_path, monkeypatch, client, *, provider="qwen", **opts):
    cfg = deepcopy(_DEFAULTS)
    cfg["llm"].update({"provider": provider, "capture": False, **opts})
    if provider == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-no-network")
        cfg["llm"]["model"] = "gpt-5.6-luna"
    ledger = UsageLedger(tmp_path / "usage.sqlite3", cfg["budget"])
    return LLM(cfg, client=client, ledger=ledger)


def test_legacy_qwen_request_and_history_remain_compatible(tmp_path, monkeypatch):
    tc = {"id": "call_1", "type": "function", "function": {"name": "rw_ui_build", "arguments": '{"at":"[1,2]"}'}}
    client = Client(response("", [tc], reasoning="Qwen thought"))
    llm = make_llm(tmp_path, monkeypatch, client)
    reply = llm.chat([{"role": "user", "content": "build"}], tools=[{"type": "function", "function": {"name": "rw_ui_build"}}])
    kw = client.calls[0]
    assert kw["max_tokens"] == 4000 and kw["temperature"] == .6
    assert kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert "max_completion_tokens" not in kw and "reasoning_effort" not in kw
    assert reply.reasoning == "Qwen thought"
    history = llm.assistant_message(reply)
    assert history["tool_calls"][0]["id"] == "call_1"
    assert json.loads(history["tool_calls"][0]["function"]["arguments"]) == {"at": "[1,2]"}
    assert "reasoning_content" not in history


def test_qwen_empty_retry_records_both_requests_and_capture(tmp_path, monkeypatch):
    client = Client(response(""), response("done"))
    llm = make_llm(tmp_path, monkeypatch, client, capture=True)
    try:
        reply = llm.chat([{"role": "user", "content": "hi"}], meta={"stream": "play", "attempt": 1})
        assert reply.content == "done"
        assert len(client.calls) == 2
        assert client.calls[1]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
        s = llm.ledger.snapshot()
        assert len(s["requests"]) == 2 and s["totals"]["output_tokens"] == 2000
        capture = llm._capture_fh.name
        llm._capture_fh.close()
        from pathlib import Path
        records = [json.loads(line) for line in Path(capture).read_text(encoding="utf-8").splitlines()]
        assert [r["reply"]["content"] for r in records[-2:]] == ["", "done"]
    finally:
        llm._capture_fh.close()


def test_openai_parameters_and_schema_copy(tmp_path, monkeypatch):
    client = Client(response())
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    tools = [{"type": "function", "function": {"name": "end_turn", "parameters": {
        "type": "object", "properties": {"wake_on": {"type": "array"}}, "required": [], "additionalProperties": True}}}]
    original = deepcopy(tools)
    reply = llm.chat([{"role": "user", "content": "hi"}], tools, tool_choice="required")
    kw = client.calls[0]
    assert kw["max_completion_tokens"] == 4000 and kw["reasoning_effort"] == "low"
    assert kw["service_tier"] == "default" and kw["store"] is False
    assert not set(kw) & {"max_tokens", "temperature", "extra_body"}
    assert kw["tools"][0]["function"]["strict"] is False
    assert kw["tools"][0]["function"]["parameters"]["properties"]["wake_on"]["items"] == {}
    assert tools == original
    assert reply.provider == "openai" and reply.finish_reason == "stop"
    assert llm.provider.base_url == "https://api.openai.com/v1"  # inherited localhost must not receive the key
    assert llm.ledger.snapshot()["daily_usd"] == "0.0032"


def test_openai_no_thinking_maps_to_none_and_empty_does_not_retry(tmp_path, monkeypatch):
    client = Client(response(""))
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    llm.chat([{"role": "user", "content": "hi"}], thinking=False)
    assert len(client.calls) == 1 and client.calls[0]["reasoning_effort"] == "none"


def test_usage_is_recorded_before_response_parse_failure(tmp_path, monkeypatch):
    malformed = response()
    malformed.choices = []
    client = Client(malformed)
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    with pytest.raises(IndexError):
        llm.chat([{"role": "user", "content": "hi"}])
    assert llm.ledger.snapshot()["daily_usd"] == "0.0032"
    assert len(client.calls) == 1


def test_paid_timeout_stops_retries_and_retains_reservation(tmp_path, monkeypatch):
    timeout = APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
    client = Client(timeout, response())
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    with pytest.raises(LLMBlocked, match="usage_unknown"):
        llm.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(LLMBlocked):
        llm.chat([{"role": "user", "content": "hi"}])
    assert len(client.calls) == 1
    assert Decimal(llm.ledger.snapshot()["reserved_usd"]) > 0


def api_error(cls, status, code):
    res = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
    return cls("test error", response=res, body={"code": code, "message": "test error"})


@pytest.mark.parametrize("error", [api_error(BadRequestError, 400, "invalid_request"),
                                  api_error(RateLimitError, 429, "project_spend_limit_exceeded")])
def test_configuration_and_billing_errors_do_not_retry(tmp_path, monkeypatch, error):
    client = Client(error)
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    with pytest.raises(LLMBlocked, match="api_blocked"):
        llm.chat([{"role": "user", "content": "hi"}])
    assert len(client.calls) == 1 and llm.ledger.snapshot()["reserved_usd"] == "0"


def test_rate_limit_is_known_unbilled_and_can_retry(tmp_path, monkeypatch):
    client = Client(api_error(RateLimitError, 429, "rate_limit_exceeded"), response())
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    with pytest.raises(RateLimitError):
        llm.chat([{"role": "user", "content": "hi"}])
    assert not llm.ledger.hold_reason and llm.ledger.snapshot()["reserved_usd"] == "0"
    llm.chat([{"role": "user", "content": "hi"}], meta={"attempt": 2})
    assert len(client.calls) == 2
    assert llm.ledger.snapshot()["daily_usd"] == "0.0032"


def test_paid_response_without_usage_is_held(tmp_path, monkeypatch):
    client = Client(response(with_usage=False))
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    with pytest.raises(LLMBlocked, match="usage_unknown"):
        llm.chat([{"role": "user", "content": "hi"}])
    assert llm.ledger.snapshot()["requests"][0]["status"] == "unknown"


def test_usage_decode_failure_preserves_reservation_and_blocks_retry(tmp_path, monkeypatch):
    class BrokenUsage:
        def model_dump(self):
            raise ValueError("malformed usage")
    client = Client(SimpleNamespace(usage=BrokenUsage()), response())
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    with pytest.raises(LLMBlocked, match="usage_unknown"):
        llm.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(LLMBlocked):
        llm.chat([{"role": "user", "content": "hi"}])
    snapshot = llm.ledger.snapshot()
    assert len(client.calls) == 1 and Decimal(snapshot["reserved_usd"]) > 0
    assert snapshot["requests"][0]["error"] == "usage_decode_error"


def test_insufficient_budget_sends_no_request(tmp_path, monkeypatch):
    client = Client(response())
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    llm.ledger.daily = Decimal("0.00001")
    with pytest.raises(LLMBlocked, match="daily_budget"):
        llm.chat([{"role": "user", "content": "hi"}])
    assert not client.calls


def test_preflight_rejects_missing_key_unknown_provider_and_unknown_price(monkeypatch):
    cfg = deepcopy(_DEFAULTS)
    cfg["llm"].update(provider="openai", model="gpt-5.6-luna")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        validate_configuration(cfg)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cfg["llm"]["model"] = "unpriced-model"
    with pytest.raises(ValueError, match="No verified price"):
        validate_configuration(cfg)
    cfg["llm"]["provider"] = "unknown"
    with pytest.raises(ValueError, match="Unknown llm.provider"):
        validate_configuration(cfg)
