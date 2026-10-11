from copy import deepcopy
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from rimagent.assist import AssistController, READ_MAP, WRITE_UI, WRITE_OTHER
from rimagent.assist_app import create_app
from rimagent.bridge import Bridge, BridgeError
from rimagent.config import _DEFAULTS
from rimagent.llm import LLM, LLMReply
from rimagent.usage import UsageLedger


class Game:
    def __init__(self, steward=False):
        self.paused = False
        self.speed = 3
        self.calls = []
        self.result = {"placed": [[1, 2]], "failed": []}
        self.fail = None
        self.tick = 100
        self.seed = "test"
        self.steward = steward
        self.stock = [{"id": "wood"}, {"id": "meals", "recipe": "CookMealSimple"}]

    def status(self, timeout_ms=None):
        return {"state": "playing", "paused": self.paused, "speed": self.speed, "tick": self.tick,
                "seed": self.seed, "map_size": [250, 250], "colonists": 3}

    def methods(self):
        names = set(READ_MAP) | WRITE_UI | WRITE_OTHER | {"state.summary", "state.base", "state.dialogs", "engine.set", "game.new_game", "dev.spawn"}
        if self.steward:
            names |= {"steward.enable", "steward.orders.set", "steward.status", "steward.stock.list", "steward.stock.set"}
        return [{"method": m, "doc": "test operation"} for m in names]

    def call(self, method, /, timeout_ms=None, **params):
        self.calls.append((method, params))
        if method == self.fail:
            raise BridgeError("TimeoutException: result unknown")
        if method == "game.pause":
            self.paused = params["paused"]
        if method == "game.speed":
            self.speed, self.paused = params["speed"], False
        if method == "state.summary":
            return {"food_days": 3, "colonists": 3}
        if method == "steward.stock.list":
            return deepcopy(self.stock)
        if method == "steward.status":
            return {"test": True}
        return deepcopy(self.result)


def reply(name, **args):
    return LLMReply(tool_calls=[{"id": "test_call", "name": name, "arguments": args}])


class Scripted:
    assistant_message = LLM.assistant_message

    def __init__(self, tmp_path, replies):
        self.replies = iter(replies)
        self.calls = 0
        self.ledger = UsageLedger(tmp_path / "usage.sqlite3")

    def ensure_active(self):
        self.ledger.ensure_active()

    def chat(self, messages, tools, *, request_guard, **kwargs):
        request_guard()
        self.calls += 1
        response = next(self.replies)
        return response() if callable(response) else response


def controller(tmp_path, replies, *, game=None, **options):
    cfg = deepcopy(_DEFAULTS)
    cfg["assist"] = options
    llm = Scripted(tmp_path, replies)
    c = AssistController(cfg, llm=llm, bridge=game or Game(), history_path=tmp_path / "commands.jsonl")
    c.connect()
    return c


def wait(c):
    c.worker.join(3)
    assert not c.worker.is_alive()


def test_idle_polling_never_calls_llm_and_basic_command_auto_resumes(tmp_path):
    c = controller(tmp_path, [reply("act", method="ui.build", params={"def": "Bed", "at": "[1,2]"}), reply("finish", success=True, report="ベッドの建築予定を置きました。完成待ちです。")])
    with TestClient(create_app(c)) as client:
        for _ in range(3):
            assert client.get("/api/state").status_code == 200
            assert client.get("/api/usage").status_code == 200
        assert c.llm.calls == 0
        assert client.post("/api/commands", json={"text": "ベッドを作って", "request_id": "one"}).status_code == 200
        wait(c)
        assert c.phase == "running" and c.current.status == "complete"
        assert c.llm.calls == 2
        assert ("ui.build", {"def": "Bed", "at": [1, 2]}) in c.bridge.calls
        assert client.post("/api/commands", json={"text": "ベッドを作って", "request_id": "one"}).status_code == 200
        assert c.llm.calls == 2


def test_request_cap_is_exact_without_final_summary_call(tmp_path):
    c = controller(tmp_path, [reply("inspect", method="state.summary")] * 21)
    c.submit("状況確認", "limit")
    wait(c)
    assert c.llm.calls == c.current.api_calls == 20
    assert c.phase == "paused" and c.bridge.paused
    assert "上限" in c.current.report


@pytest.mark.parametrize("result", [{"placed": [], "failed": []}, {"placed": [[1, 2]], "failed": [{"reason": "blocked"}]}, {"accepted": False}])
def test_partial_results_do_not_resume_even_if_model_says_success(tmp_path, result):
    g = Game()
    g.result = result
    c = controller(tmp_path, [reply("act", method="ui.build", params={"def": "Bed", "at": [1, 2]}), reply("finish", success=True, report="完了")], game=g)
    c.submit("建築", "partial")
    wait(c)
    assert c.current.status == "partial" and g.paused


@pytest.mark.parametrize("method", ["engine.set", "game.new_game", "dev.spawn", "game.speed", "steward.enable"])
def test_forbidden_calls_never_reach_bridge(tmp_path, method):
    c = controller(tmp_path, [reply("act", method=method), reply("finish", success=True, report="設定")])
    c.submit("設定", "forbidden")
    wait(c)
    assert not any(m == method for m, _ in c.bridge.calls)
    assert c.current.status == "partial" and c.bridge.paused


def test_question_continues_same_command_and_duplicate_answer_is_ignored(tmp_path):
    c = controller(tmp_path, [reply("ask_user", question="何台ですか？"), reply("finish", success=True, report="設定完了")])
    c.submit("ベッド", "question")
    wait(c)
    assert c.current.status == "question" and c.bridge.paused
    c.submit("3台", "answer", answer_to="question")
    wait(c)
    assert c.current.api_calls == 2 and c.phase == "running"
    c.submit("3台", "answer", answer_to="question")
    assert c.llm.calls == 2


def test_cancel_inflight_response_prevents_operations_and_new_requests(tmp_path):
    started, release = threading.Event(), threading.Event()
    def delayed():
        started.set()
        assert release.wait(2)
        return reply("act", method="ui.build", params={"def": "Bed"})
    c = controller(tmp_path, [delayed])
    c.submit("ベッド", "cancel")
    assert started.wait(2)
    with pytest.raises(RuntimeError):
        c.submit("別の指示", "second")
    c.control("cancel")
    release.set()
    wait(c)
    assert c.current.status == "cancelled" and c.bridge.paused
    assert not any(m == "ui.build" for m, _ in c.bridge.calls)
    assert c.llm.calls == 1


def test_timed_out_mutation_is_not_retried_and_restart_does_not_replay(tmp_path):
    g = Game()
    g.fail = "ui.build"
    c = controller(tmp_path, [reply("act", method="ui.build", params={"def": "Bed", "at": [1, 2]})], game=g)
    c.submit("ベッド", "timeout")
    wait(c)
    assert c.current.status == "partial" and g.paused
    assert sum(m == "ui.build" for m, _ in g.calls) == 1
    restarted = controller(tmp_path, [])
    restarted.submit("ベッド", "timeout")
    assert restarted.llm.calls == 0


def test_operation_cap_keeps_partial_work_paused(tmp_path):
    c = controller(tmp_path, [reply("act", method="ui.build", params={"def": "Bed", "at": [1, 2]})] * 3, max_operations=2)
    c.submit("建築", "op_limit")
    wait(c)
    assert c.current.operations == 2 and c.current.status == "partial" and c.bridge.paused


def test_steward_startup_off_and_only_selected_order_can_be_enabled(tmp_path):
    g = Game(steward=True)
    c = controller(tmp_path, [reply("automation", kind="orders", enabled=True, ids=["fire"]), reply("finish", success=True, report="火災処理を設定")], game=g)
    assert ("steward.enable", {"scorer": False, "stock": False}) in g.calls
    assert ("steward.orders.set", {"id": "all", "enabled": False}) in g.calls
    c.submit("火災を自動消火して", "orders", ["orders"])
    wait(c)
    enabled = [p for m, p in g.calls if m == "steward.orders.set" and p["enabled"]]
    assert enabled == [{"id": "fire", "enabled": True}]


def test_stock_suspends_nonselected_jobs_before_enabling(tmp_path):
    g = Game(steward=True)
    c = controller(tmp_path, [reply("automation", kind="stock", enabled=True, ids=["wood"]), reply("finish", success=True, report="木材在庫処理")], game=g)
    c.submit("木材を自動確保", "stock", ["stock"])
    wait(c)
    assert ("steward.stock.set", {"id": "meals", "suspended": True}) in g.calls
    assert ("steward.stock.set", {"id": "wood", "suspended": False}) in g.calls


def test_stock_without_default_meals_falls_back_without_global_enable(tmp_path):
    g = Game(steward=True)
    g.stock = [{"id": "wood"}]
    c = controller(tmp_path, [reply("automation", kind="stock", enabled=True, ids=["wood"]), reply("finish", success=False, report="通常の作業指定が必要")], game=g)
    c.submit("木材", "unsafe_stock", ["stock"])
    wait(c)
    assert not any(m == "steward.enable" and p.get("stock") is True for m, p in g.calls)
    assert g.paused


def test_game_change_rejected_before_new_command(tmp_path):
    c = controller(tmp_path, [])
    c.bridge.seed = "another"
    with pytest.raises(BridgeError):
        c.submit("建築", "new_save")
    assert c.llm.calls == 0


def test_hold_blocks_request_and_resume(tmp_path):
    c = controller(tmp_path, [])
    c.llm.ledger._storage_error = "budget hold"
    from rimagent.usage import LLMBlocked
    with pytest.raises(LLMBlocked):
        c.submit("建築", "hold")
    with pytest.raises(LLMBlocked):
        c.control("resume")
    assert c.bridge.paused and c.llm.calls == 0


def test_rpc_method_argument_is_forwarded_in_params():
    seen = []
    def respond(request):
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})
    b = Bridge("http://localhost")
    b._client.close()
    b._client = httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(respond))
    b.call("ui.dialog", method="PreClose")
    assert seen == [{"method": "ui.dialog", "params": {"method": "PreClose"}}]
    b._client.close()


def test_cross_origin_command_is_rejected(tmp_path):
    c = controller(tmp_path, [])
    with TestClient(create_app(c)) as client:
        r = client.post("/api/commands", headers={"Origin": "https://example.com"}, json={"text": "建築", "request_id": "cross"})
        assert r.status_code == 403 and c.llm.calls == 0


def test_physical_qwen_retry_runs_guard_and_stops_at_cap(tmp_path, monkeypatch):
    from test_llm import Client, response, make_llm
    from rimagent.usage import LLMBlocked
    client = Client(response(content=""), response())
    llm = make_llm(tmp_path, monkeypatch, client)
    attempts = []
    def guard():
        if attempts:
            raise LLMBlocked("cap")
        attempts.append(1)
    with pytest.raises(LLMBlocked, match="cap"):
        llm.chat([{"role": "user", "content": "test"}], request_guard=guard)
    assert len(client.calls) == 1
    assert len(llm.ledger.snapshot()["requests"]) == 1


def test_cancelled_real_response_is_accounted_before_discarding_actions(tmp_path, monkeypatch):
    from test_llm import Client, response, make_llm
    import json
    started, release = threading.Event(), threading.Event()
    class Delayed(Client):
        def create(self, **kwargs):
            started.set()
            assert release.wait(2)
            return super().create(**kwargs)
    tc = [{"id": "late", "type": "function", "function": {"name": "act", "arguments": json.dumps({"method": "ui.build", "params": {"def": "Bed"}})}}]
    client = Delayed(response(content="", tools=tc))
    llm = make_llm(tmp_path, monkeypatch, client, provider="openai")
    c = AssistController(deepcopy(_DEFAULTS), llm=llm, bridge=Game(), history_path=tmp_path / "cmd.jsonl")
    c.connect()
    c.submit("ベッド", "paid_cancel")
    assert started.wait(2)
    c.control("cancel")
    release.set()
    wait(c)
    usage = c.usage()
    assert usage["commands"]["paid_cancel"]["cost_usd"] != "0"
    assert usage["reserved_usd"] == "0"
    assert not any(m == "ui.build" for m, _ in c.bridge.calls)


def test_bulk_build_counts_each_operation(tmp_path):
    c = controller(tmp_path, [reply("act", method="ui.build_many", params={"ops": [{"def": "Bed", "at": [1, 2]}] * 3}), reply("finish", success=True, report="設定")], max_operations=3)
    c.submit("ベッド3台", "bulk")
    wait(c)
    # The initial summary uses one slot; the batch cannot fit in the remaining two.
    assert not any(m == "ui.build_many" for m, _ in c.bridge.calls)
    assert c.current.status == "partial" and c.bridge.paused


def test_stop_works_even_when_bridge_is_unreachable(tmp_path):
    c = controller(tmp_path, [])
    c.bridge.fail = "game.pause"
    c.control("kill")
    assert c.stop_event.is_set() and c.phase == "stopped"
    assert "手動停止" in c.last_error


def test_process_lock_refuses_another_controller_and_releases():
    from rimagent.session_lock import session_lock
    with session_lock():
        with pytest.raises(RuntimeError, match="already running"):
            with session_lock():
                pass
    with session_lock():
        pass


def test_sleeping_spot_pos_is_corrected_and_specs_are_in_initial_context(tmp_path):
    c = controller(tmp_path, [reply("act", method="ui.build", params={"def": "SleepingSpot", "pos": [132, 160], "rot": "North"}), reply("finish", success=True, report="寝床を設置しました")])
    c.methods["ui.build"]["doc"] = "{def, at?: [x,z], rot?: N|E|S|W} place blueprints"
    c.submit("寝床つくって", "pos_alias")
    wait(c)
    assert ("ui.build", {"def": "SleepingSpot", "at": [132, 160], "rot": "N"}) in c.bridge.calls
    assert 'at?: [x,z]' in c.current.messages[1]["content"]
    assert c.current.status == "complete"


@pytest.mark.parametrize("method,params", [("ui.build", {"def": "Bed"}), ("map.detail", {"center": [1, 2], "radius": 20}), ("defs.buildable", {"search": "bed"}), ("state.stocks", {"resources": ["WoodLog"]})])
def test_common_invalid_params_rejected_before_bridge(tmp_path, method, params):
    from rimagent.assist import normalize_rpc_params
    with pytest.raises(ValueError):
        normalize_rpc_params(method, params)


def test_rpc_rejection_is_recorded_separately_from_timeout(tmp_path):
    class Rejected(Game):
        def call(self, method, /, **params):
            if method == "ui.build":
                raise BridgeError("requires research")
            return super().call(method, **params)
    c = controller(tmp_path, [reply("act", method="ui.build", params={"def": "Bed", "at": [1, 2]})], game=Rejected())
    c.submit("建築", "rejected")
    wait(c)
    assert c.current.actions[-1]["result"] == "requires research"
    assert c.current.actions[-1]["outcome"] == "rejected"
    assert c.bridge.paused
