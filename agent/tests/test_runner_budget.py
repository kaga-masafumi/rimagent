from copy import deepcopy
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from rimagent.bridge import Bridge
from rimagent.bus import Bus
from rimagent.config import _DEFAULTS
from rimagent.dashboard.app import create_app
from rimagent.llm import LLM, LLMReply
from rimagent.loop import think
from rimagent.pricing import LUNA
from rimagent.runner import Runner
from rimagent.usage import LLMBlocked, UsageLedger


@pytest.fixture
def runner(tmp_path, monkeypatch, clean_brain):
    from rimagent import runner as module
    cfg = deepcopy(_DEFAULTS)
    cfg["llm"]["capture"] = False
    cfg["play"]["think_speed"] = 0
    bus = Bus(log_file=False)
    bridge = Bridge()
    calls = []
    def handle(req):
        import json
        if req.url.path == "/rpc":
            payload = json.loads(req.content)
            calls.append((payload["method"], payload["params"]))
            return httpx.Response(200, json={"ok": True, "result": {
                "state": "playing", "colonists": 3, "day": 2, "tick": 120_000, "seed": "test"}})
        if req.url.path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"events": []}})
        return httpx.Response(200, json={"ok": True, "mainThreadAlive": True})
    bridge._client.close()
    bridge._client = httpx.Client(base_url=bridge.url, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(module, "Bridge", lambda url: bridge)
    def make(cfg, *, emit, on_hold):
        ledger = UsageLedger(tmp_path / "usage.sqlite3", cfg["budget"], emit=emit, on_hold=on_hold)
        return LLM(cfg, ledger=ledger, client=SimpleNamespace())
    monkeypatch.setattr(module, "LLM", make)
    r = Runner(cfg, bus)
    r.recorded = calls
    yield r
    bridge._client.close()


def test_hold_inside_think_is_not_unpaused_by_finally(runner):
    runner.with_pause(lambda: runner.llm.ledger.hold("daily_budget"))
    assert runner.controls.paused
    assert runner.recorded and all(method == "game.pause" and p["paused"] is True for method, p in runner.recorded)
    assert runner.bus.state["phase"] == "budget_hold"


def test_normal_step_still_restores_game_speed(runner):
    runner.with_pause(lambda: None)
    assert runner.recorded[-2:] == [("game.speed", {"speed": 3}), ("game.pause", {"paused": False})]


def test_hold_guards_rpc_fallback_and_raw_watcher_calls(runner):
    runner.llm.ledger.hold("daily_budget")
    before = len(runner.recorded)
    with pytest.raises(LLMBlocked):
        runner.bridge.call("ui.draft", pawn="one", drafted=True)
    with pytest.raises(LLMBlocked):
        runner.bridge.call("game.pause", paused=False)
    assert runner.bridge.call_raw("ui.draft", {"pawn": "one"})["ok"] is False
    assert len(runner.recorded) == before
    runner.bridge.call("state.summary")
    runner.bridge.call("game.pause", paused=True)
    assert len(runner.recorded) == before + 2


def test_returned_worker_reply_cannot_act_after_other_stream_holds(runner):
    runner.registry.add_bridge_methods([{"method": "ui.draft", "doc": "draft pawn"}])
    def chat(*args, **kw):
        runner.llm.ledger.hold("other stream exhausted the budget")
        return LLMReply(tool_calls=[{"id": "call_1", "name": "rw_ui_draft", "arguments": {"pawn": "one"}}])
    runner.llm.chat = chat
    result = think(runner.ctx, "test", system="test")
    assert result.calls == 0 and result.stop_reason
    assert not any(method == "ui.draft" for method, _ in runner.recorded)


def test_terminal_budget_error_is_not_retried(runner):
    attempts = []
    def chat(*args, **kw):
        attempts.append(kw)
        runner.llm.ledger.hold("daily_budget")
        raise LLMBlocked("daily_budget")
    runner.llm.chat = chat
    result = think(runner.ctx, "test", system="test")
    assert len(attempts) == 1 and result.stop_reason == "daily_budget"


def test_manager_hold_does_not_fall_back_to_workers(runner, monkeypatch):
    from rimagent import runner as module
    runner.cfg["play"]["parallel_manager"] = True
    monkeypatch.setattr(module, "situation_packet", lambda *a, **kw: ("packet", "hint"))
    worker_calls = []
    monkeypatch.setattr(module, "think", lambda *a, **kw: worker_calls.append(kw))
    def chat(*a, **kw):
        runner.llm.ledger.hold("monthly_budget")
        raise LLMBlocked("monthly_budget")
    runner.llm.chat = chat
    with pytest.raises(LLMBlocked):
        runner.with_pause(lambda: runner.play_step_parallel("scheduled", 1000))
    assert not worker_calls
    assert all(method != "game.pause" or p["paused"] is True for method, p in runner.recorded)


def test_hold_prevents_new_game_recovery_and_improvement(runner, monkeypatch):
    from rimagent import runner as module
    def forbidden(*a, **kw):
        pytest.fail("Must not restart or reflect while held")
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(module.reflect, "improve", forbidden)
    runner.llm.ledger.hold("daily_budget")
    runner.new_game()
    runner.recover_game()
    runner.start_improve_thread(3)
    assert not hasattr(runner, "_improve_thread")
    assert not any(method == "game.new_game" for method, _ in runner.recorded)


def test_reflection_hold_prevents_next_episode(runner, monkeypatch):
    from rimagent import runner as module
    def reflect(*a, **kw):
        runner.llm.ledger.hold("daily_budget")
        return "held"
    monkeypatch.setattr(module.reflect, "episode", reflect)
    monkeypatch.setattr(module.braingit, "commit", lambda *a: pytest.fail("No commit/next episode after hold"))
    runner.new_game = lambda: pytest.fail("No next game after hold")
    runner.end_episode("test end")
    assert runner.controls.paused


def test_hold_during_last_recovery_wait_prevents_restart(runner, monkeypatch):
    from rimagent import runner as module
    count = 0
    def sleep(_):
        nonlocal count
        count += 1
        if count == 20:
            runner.llm.ledger.hold("daily_budget")
    monkeypatch.setattr(runner.bridge, "health", lambda: False)
    monkeypatch.setattr(module.time, "sleep", sleep)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: pytest.fail("Recovery may not restart after a hold"))
    runner.recover_game()
    assert count == 20 and runner.controls.paused


def test_watchdog_disabled_does_not_register_or_start(runner):
    assert not any(t.group == "watchdog" for t in runner.registry.tools.values())
    runner.start_watchdog_thread(3)
    runner.maybe_start_watchdog(3)
    assert runner._watchdog_thread is None


def test_resume_requires_reconciliation_and_usage_api_reports_hold(runner):
    rid = runner.llm.ledger.reserve("openai", "gpt-5.6-luna", LUNA, 1000, 1000, {"stream": "cli"})
    runner.llm.ledger.complete(rid, None)
    client = TestClient(create_app(runner.bus, runner.bridge, runner.controls))
    s = client.get("/api/usage").json()
    assert s["available"] and s["hold_reason"] and s["outstanding"][0]["id"] == rid
    assert "api_key" not in str(s)
    rejected = client.post("/api/control", json={"action": "resume"}).json()
    assert rejected["ok"] is False and rejected["paused"] is True
    runner.llm.ledger.reconcile(rid, "0")
    accepted = client.post("/api/control", json={"action": "resume"}).json()
    assert accepted["ok"] is True and not runner.llm.ledger.hold_reason
    assert runner.force_think == "operator resumed after budget hold"


def test_registry_and_watchers_share_execution_gate(runner):
    from rimagent.watchers import run_all
    runner.registry.watchers["danger"] = lambda *a: pytest.fail("Watchers should be stopped by hold")
    runner.llm.ledger.hold("daily_budget")
    assert run_all(runner.ctx, []) == []
    result, ok = runner.registry.execute(runner.ctx, "run_python", {"code": "raise AssertionError('should not execute')"})
    assert not ok and "LLMBlocked" in result["error"]
