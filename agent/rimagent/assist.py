"""Human-directed commands. No scheduler, watchers, brain loading or autonomous episodes."""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import Path
import threading

from .bridge import Bridge, BridgeError
from .llm import LLM
from .paths import RUNS
from .registry import coerce_param, to_text
from .usage import LLMBlocked

READ_PREFIXES = ("state.", "defs.")
READ_MAP = {"map.view", "map.detail", "map.find", "map.open_rects", "map.power", "ui.gizmos", "ui.orders_at", "anchor.list", "game.status"}
READ_STEWARD = {"steward.status", "steward.stock.list", "steward.orders", "steward.orders.explain", "steward.explain"}
WRITE_UI = {"ui.build", "ui.build_many", "ui.designate", "ui.zone", "ui.set_work", "ui.set_work_many",
            "ui.set_policies", "ui.bill", "ui.add_bill", "ui.set_research", "ui.set_schedule", "ui.storage", "ui.animal",
            "ui.cancel_job", "ui.select", "ui.draft", "ui.goto", "ui.attack", "ui.order", "ui.job", "ui.letter", "ui.dialog", "ui.press"}
WRITE_OTHER = {"anchor.set", "anchor.delete", "steward.research", "steward.posture", "steward.pawn", "steward.orders.rally"}
ORDERS = {"combat", "rescue", "unforbid", "corpses", "beds", "policies", "blueprints", "fire"}
SYSTEM = """あなたはRimWorldの人間主導の操作補助です。日本語で報告してください。
人間の今回の指示だけをゲーム停止中に設定します。作業の完了を待たず、設定ができたらfinishを呼びます。
建築予定設置は完成ではありません。製造指示受付は製品完成ではありません。キャラの作業待ちを明記してください。
建築前にmap.detailと素材を確認し、対象ID・研究名・方針名は実データで確認します。failedや空のplacedを成功と報告しません。
RPCの引数は提示された仕様を使い、推測しません。不明ならdescribeで確認します。ui.buildの位置はat=[x,z]です。
defs.buildableにはsearchがありません。categoryで絞ります。map.detailにはcenterやradiusではなくx,z,w,hを渡します。
曖昧な指示はask_userで質問します。指示外の拡張・研究・自動処理を始めません。必要な観察だけ行い、長い一覧は絞ります。
自動処理は今回人間が画面で許可したものだけautomationで設定できます。stockを限定できないときは通常のui.bill等を使います。
ゲーム再開はコントローラーが担当します。失敗を隠さず、修正できなければfinish(success=false)で終了します。
"""


def parameter_doc(doc):
    """Keep the original parameter declaration, including nested dictionaries, compact."""
    if not doc.startswith("{"):
        return doc[:240]
    depth = 0
    for i, char in enumerate(doc):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return doc[:i + 1]
    return doc


def normalize_rpc_params(method, params):
    """Known, unambiguous aliases only. Reject ignored keys on common observation RPCs."""
    params = dict(params)
    if method == "ui.build":
        if "pos" in params:
            if "at" in params and params["pos"] != params["at"]:
                raise ValueError("ui.build: posとatが競合しています。at=[x,z]を指定してください。")
            params.setdefault("at", params.pop("pos"))
        if not any(key in params for key in ("at", "line", "rect")):
            raise ValueError("ui.build: at=[x,z]、line、rectのいずれかが必要です。")
        if isinstance(params.get("rot"), str):
            params["rot"] = {"north": "N", "east": "E", "south": "S", "west": "W"}.get(params["rot"].lower(), params["rot"])
    keys = {
        "ui.build": {"def", "at", "line", "rect", "rot", "stuff", "fill", "dry_run"},
        "map.detail": {"x", "z", "around", "w", "h", "roof", "mark"},
        "defs.buildable": {"category"},
        "state.stocks": {"category", "min"},
    }.get(method)
    if keys is not None and set(params) - keys:
        raise ValueError(f"{method}: 未対応の引数 {sorted(set(params) - keys)}。利用可能: {sorted(keys)}。describeで確認してください。")
    return params


def spec(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}}


TOOLS = [
    spec("inspect", "許可された読み取りRPC。対象・範囲を絞る。", {"method": {"type": "string"}, "params": {"type": "object"}}, ("method",)),
    spec("act", "許可されたゲーム操作RPC。操作結果を確認する。", {"method": {"type": "string"}, "params": {"type": "object"}}, ("method",)),
    spec("describe", "RPCの引数仕様を調べる。methodが空なら利用可能な名前を返す。", {"method": {"type": "string"}}),
    spec("look", "必要時だけ座標と物体番号付きのマップ画像を見る。", {"x": {"type": "integer"}, "z": {"type": "integer"}, "w": {"type": "number"}}),
    spec("automation", "人間が許可したSteward処理だけ設定。stockは既存ジョブIDを指定。", {"kind": {"type": "string", "enum": ["scorer", "orders", "stock"]}, "enabled": {"type": "boolean"}, "ids": {"type": "array", "items": {"type": "string"}}}, ("kind", "enabled")),
    spec("ask_user", "質問を表示し、ゲーム停止のまま回答を待つ。", {"question": {"type": "string"}}, ("question",)),
    spec("finish", "設定結果を日本語で報告。未完成の作業を明記。失敗時success=false。", {"report": {"type": "string"}, "success": {"type": "boolean"}}, ("report", "success")),
]


@dataclass
class Command:
    id: str
    text: str
    permissions: list[str] = field(default_factory=list)
    status: str = "executing"
    report: str = ""
    question: str = ""
    api_calls: int = 0
    operations: int = 0
    actions: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    cancelled: threading.Event = field(default_factory=threading.Event)

    def public(self):
        return {k: deepcopy(getattr(self, k)) for k in ("id", "text", "permissions", "status", "report", "question", "api_calls", "operations", "actions")}


class AssistController:
    def __init__(self, cfg, *, bridge=None, llm=None, history_path: Path | None = None):
        self.cfg = cfg
        for key, default in (("max_api_calls", 20), ("max_operations", 40)):
            value = cfg.get("assist", {}).get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"assist.{key} must be a positive integer")
        self.bridge = bridge or Bridge(cfg["bridge"]["url"])
        self.llm = llm or LLM(cfg)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.phase = "paused"
        self.last_error = ""
        self.game = None
        self.speed = 1
        self.identity = None
        self.last_tick = None
        self.steward = {}
        self.methods = {}
        self.current = None
        self.worker = None
        self.commands = {}
        self.history_path = history_path or RUNS / "assist-commands.jsonl"
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        if self.history_path.exists():
            for line in self.history_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    if row["status"] in ("executing", "question"):
                        row["status"] = "interrupted"
                    self.commands[row["id"]] = row
                except (ValueError, KeyError):
                    continue  # A crashed last append cannot trigger replay.

    def _save(self, c):
        row = c.public()
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
        self.commands[c.id] = row

    def _pause(self):
        self.bridge.call("game.pause", timeout_ms=2000, paused=True)
        st = self.bridge.status(timeout_ms=2000)
        if st.get("state") != "playing" or not st.get("paused"):
            raise BridgeError("ゲームを停止できません。ゲーム画面で停止してください。")
        self.game = st
        return st

    def connect(self):
        with self.lock:
            st = self._pause()
            self.identity = (st.get("seed"), tuple(st.get("map_size") or []))
            self.last_tick = st.get("tick", 0)
            self.methods = {m["method"]: m for m in self.bridge.methods()}
            if "steward.enable" in self.methods:
                self.bridge.call("steward.enable", timeout_ms=2000, scorer=False, stock=False)
                if "steward.orders.set" in self.methods:
                    self.bridge.call("steward.orders.set", timeout_ms=2000, id="all", enabled=False)
                self.steward = self.bridge.call("steward.status", timeout_ms=2000)
            self.phase = "paused"

    def _check(self, c):
        if self.stop_event.is_set() or c.cancelled.is_set():
            raise LLMBlocked("指示は中断されました。")
        self.llm.ensure_active()

    def _check_game(self):
        st = self.bridge.status(timeout_ms=2000)
        identity = (st.get("seed"), tuple(st.get("map_size") or []))
        if st.get("state") != "playing" or identity != self.identity or st.get("tick", 0) < self.last_tick:
            raise BridgeError("ゲームまたはセーブが切り替わりました。assistを再起動してください。")
        self.last_tick = st.get("tick", 0)
        self.game = st
        if not st.get("paused"):
            raise BridgeError("設定中にゲームが再開されました。指示を中断します。")

    def submit(self, text, request_id, permissions=(), *, answer_to=None):
        text = text.strip()
        if not text or len(text) > 10000 or not request_id or len(request_id) > 128:
            raise ValueError("指示と一意のrequest_idが必要です（指示は10000文字以内）。")
        if set(permissions) - {"scorer", "orders", "stock"}:
            raise ValueError("自動処理の許可が不正です。")
        with self.lock:
            if request_id in self.commands:
                return self.commands[request_id]
            if self.stop_event.is_set():
                raise RuntimeError("コントローラーは終了しています。")
            if self.worker and self.worker.is_alive():
                raise RuntimeError("別の指示を実行中です。")
            previous = self.bridge.status(timeout_ms=2000)
            if previous.get("speed", 0) > 0:
                self.speed = min(3, previous["speed"])
            self._pause()
            self._check_game()
            self.llm.ensure_active()
            if answer_to:
                c = self.current
                if not c or c.id != answer_to or c.status != "question":
                    raise ValueError("回答待ちの指示がありません。")
                c.messages.append({"role": "user", "content": text})
                c.permissions = sorted(set(c.permissions) | set(permissions))
                c.messages.append({"role": "user", "content": "今回の自動処理許可: " + str(c.permissions)})
                c.status, c.question = "executing", ""
                # Persist the answer's id too, so retrying it never repeats operations.
                self.commands[request_id] = {"id": request_id, "status": "answer", "command_id": c.id}
                with self.history_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(self.commands[request_id]) + "\n")
            else:
                if self.current and self.current.status == "question":
                    raise RuntimeError("質問に回答するか、今回の指示を中断してください。")
                c = Command(request_id, text, list(permissions))
                self.current = c
            self._save(c)
            self.last_error = ""
            self.phase = "executing"
            self.worker = threading.Thread(target=self._run, args=(c,), name="assist-command", daemon=False)
            self.worker.start()
            return c.public()

    def _request(self, c):
        with self.lock:
            self._check(c)
            self._check_game()
            if c.api_calls >= int(self.cfg.get("assist", {}).get("max_api_calls", 20)):
                raise LLMBlocked("API要求上限に達しました。実行済み内容を確認して追加指示してください。")
            c.api_calls += 1
            self._save(c)

    def _rpc(self, c, method, params, *, write=False):
        with self.lock:
            self._check(c)
            self._check_game()
            weight = max(1, len(params.get("ops", []))) if method == "ui.build_many" else 1
            if c.operations + weight > int(self.cfg.get("assist", {}).get("max_operations", 40)):
                raise LLMBlocked("操作上限に達しました。")
            c.operations += weight
            record = {"method": method, "params": params, "write": write, "ok": False, "result": "結果未確定"}
            c.actions.append(record)
            self._save(c)  # Journal intent before a potentially irreversible game operation.
            try:
                result = self.bridge.call(method, timeout_ms=5000, **params)
            except BridgeError as exc:
                uncertain = any(s in str(exc) for s in ("TimeoutException", "bridge unreachable", "bad response"))
                record.update(result=str(exc), outcome="unknown" if uncertain else "rejected")
                self._save(c)
                raise
            bad = isinstance(result, dict) and (bool(result.get("failed")) or result.get("ok") is False or
                    result.get("accepted") is False or ("placed" in result and not result["placed"]))
            record.update(ok=not bad, result=to_text(result, 2500))
            self._save(c)
            return result, not bad

    def _allowed(self, method, write):
        if method not in self.methods:
            return False
        if write:
            return method in WRITE_UI or method in WRITE_OTHER
        return method.startswith(READ_PREFIXES) or method in READ_MAP or method in READ_STEWARD

    def _automation(self, c, args):
        kind, enabled = args["kind"], args["enabled"]
        ids = args.get("ids") or []
        if not isinstance(enabled, bool) or not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise ValueError("enabledとidsの型が不正です。")
        if kind not in c.permissions:
            raise ValueError("この自動処理は人間の許可がありません。ask_userで許可を求めてください。")
        if kind == "scorer":
            result, ok = self._rpc(c, "steward.enable", {"scorer": enabled}, write=True)
        elif kind == "orders":
            if not ids or set(ids) - ORDERS:
                raise ValueError("個別のstanding order IDが必要です。allは使用できません。")
            result = []
            ok = True
            for i in ids:
                r, good = self._rpc(c, "steward.orders.set", {"id": i, "enabled": enabled}, write=True)
                result.append(r)
                ok &= good
        elif kind == "stock":
            rows, _ = self._rpc(c, "steward.stock.list", {})
            if not isinstance(rows, list) or not ids or not set(ids) <= {r.get("id") for r in rows}:
                raise ValueError("既存の在庫ジョブIDを指定してください。")
            # Prevent the default simple-meal job being added on a later tick outside the selected ids.
            if enabled and not any(r.get("recipe") == "CookMealSimple" for r in rows):
                raise ValueError("将来追加される既定製造ジョブを限定できません。通常のui.billなどを使ってください。")
            for row in rows:
                self._rpc(c, "steward.stock.set", {"id": row["id"], "suspended": not (enabled and row["id"] in ids)}, write=True)
            result, ok = self._rpc(c, "steward.enable", {"stock": enabled}, write=True)
        else:
            raise ValueError("不明な自動処理です。")
        self.steward, _ = self._rpc(c, "steward.status", {})
        return result, ok

    def _tool(self, c, name, args):
        args = coerce_param(args)
        if not isinstance(args, dict):
            raise ValueError("ツール引数はobjectが必要です。")
        if name in ("inspect", "act"):
            method, params = args["method"], args.get("params") or {}
            if not isinstance(method, str) or not isinstance(params, dict) or "timeout_ms" in params:
                raise ValueError("RPC引数が不正です。")
            if not self._allowed(method, name == "act"):
                raise ValueError("このRPCは許可されていません。")
            params = normalize_rpc_params(method, params)
            if method == "ui.build_many":
                params["ops"] = [normalize_rpc_params("ui.build", op) for op in params.get("ops", [])]
            if method == "ui.build_many" and len(params.get("ops", [])) > int(self.cfg.get("assist", {}).get("max_operations", 40)) - c.operations:
                raise ValueError("一括建築の操作数が残り上限を超えています。")
            return self._rpc(c, method, params, write=name == "act")
        if name == "describe":
            method = args.get("method")
            allowed = {m: d for m, d in self.methods.items() if self._allowed(m, False) or self._allowed(m, True)}
            return (allowed.get(method, {"error": "許可されていないRPCです"}) if method else sorted(allowed)), True
        if name == "automation":
            return self._automation(c, args)
        if name == "look":
            from .annotate import annotate
            x, z, w = args.get("x"), args.get("z"), max(10, min(250, float(args.get("w", 60))))
            if x is None or z is None:
                base, _ = self._rpc(c, "state.base", {})
                x, z = base["home_center"]
            detail, _ = self._rpc(c, "map.detail", {"x": x, "z": z, "w": min(60, int(w)), "h": min(60, int(w * .75) + 2)})
            with self.lock:
                self._check(c)
                self._check_game()
                if c.operations >= int(self.cfg.get("assist", {}).get("max_operations", 40)):
                    raise LLMBlocked("操作上限に達しました。")
                c.operations += 1
                png = self.bridge.screenshot(x, z, w)
            png, marks = annotate(png, x, z, w, 1024, 768, detail.get("things", []), detail.get("anchors_in_view", {}))
            return {"marks": marks, "_image_png_b64": base64.b64encode(png).decode()}, True
        raise ValueError("不明なツールです。")

    def _run(self, c):
        try:
            if not c.messages:
                state, _ = self._rpc(c, "state.summary", {})
                c.messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps({
                    "instruction": c.text, "automation_permissions": c.permissions, "state": state,
                    "read_methods": {m: parameter_doc(d.get("doc", "")) for m, d in self.methods.items() if self._allowed(m, False)},
                    "action_methods": {m: parameter_doc(d.get("doc", "")) for m, d in self.methods.items() if self._allowed(m, True)},
                    "steward": self.steward,
                    "recent_instructions": [{"text": r.get("text"), "report": r.get("report", "")[:1500]}
                                            for r in list(self.commands.values())[-4:] if r.get("id") != c.id]}, ensure_ascii=False)}]
            while True:
                self._check(c)
                reply = self.llm.chat(c.messages, TOOLS, thinking=False, meta={"stream": "assist", "command_id": c.id}, request_guard=lambda: self._request(c))
                self._check(c)
                c.messages.append(self.llm.assistant_message(reply))
                if not reply.tool_calls:
                    raise LLMBlocked("終了ツールが呼ばれませんでした。" + reply.content[:1500])
                for tc in reply.tool_calls:
                    self._check(c)
                    name, args = tc["name"], tc["arguments"]
                    if name in ("finish", "ask_user"):
                        if len(reply.tool_calls) != 1:
                            raise LLMBlocked("終了・質問は他の操作と同時に実行できません。")
                        with self.lock:
                            self._check(c)
                            self._check_game()
                            if name == "ask_user":
                                c.question = str(args["question"]).strip()
                                if not c.question:
                                    raise ValueError("質問が空です。")
                                c.status = self.phase = "question"
                                c.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "人間の回答待ち。"})
                            else:
                                c.report = str(args["report"])
                                good = args.get("success") is True and all(a["ok"] for a in c.actions)
                                c.status = "complete" if good else "partial"
                                if not good:
                                    c.report = "設定は未完了・一部失敗です。操作履歴を確認してください。\n" + c.report
                                self.phase = "paused"
                            self._save(c)
                            if name == "finish" and c.status == "complete":
                                self._resume(c)
                        return
                    try:
                        result, ok = self._tool(c, name, args)
                    except (BridgeError, LLMBlocked):
                        raise  # Never replay a timed-out mutation or retry a blocked API.
                    except (ValueError, KeyError, TypeError) as exc:
                        result, ok = {"error": str(exc)}, False
                        with self.lock:
                            c.actions.append({"method": name, "params": args, "ok": False, "write": False, "result": to_text(result)})
                            self._save(c)
                    image = result.pop("_image_png_b64", None) if isinstance(result, dict) else None
                    c.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": to_text({"ok": ok, "result": result})})
                    if image:
                        c.messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}}]})
        except Exception as exc:
            with self.lock:
                c.status = "cancelled" if c.cancelled.is_set() or self.stop_event.is_set() else "partial"
                c.report = str(exc) if isinstance(exc, (LLMBlocked, BridgeError)) else "処理に失敗しました: " + type(exc).__name__
                self.phase = "stopped" if self.stop_event.is_set() else "paused"
                self.last_error = c.report
                try:
                    self._pause()
                except BridgeError:
                    self.last_error += " ゲーム画面で手動停止してください。"
                try:
                    self._save(c)
                except OSError:
                    self.last_error += " 指示履歴の保存に失敗しました。"

    def _resume(self, c=None):
        if c:
            self._check(c)
        self.llm.ensure_active()
        self._check_game()
        self.bridge.call("game.speed", timeout_ms=2000, speed=self.speed)
        self.game = self.bridge.status(timeout_ms=2000)
        if self.game.get("paused"):
            raise BridgeError("ゲームが再開しません。開いているダイアログを確認してください。")
        self.phase = "running"
        self.last_error = ""

    def control(self, action, speed=None):
        # Set cancellation before waiting for an in-flight RPC to release the lock.
        if action in ("cancel", "kill") and self.current:
            self.current.cancelled.set()
        if action == "kill":
            self.stop_event.set()
        with self.lock:
            busy = self.worker and self.worker.is_alive()
            if action == "resume":
                if busy or (self.current and self.current.status == "question"):
                    raise RuntimeError("指示実行中・質問待ちは再開できません。")
                if speed is not None:
                    if speed not in (1, 2, 3):
                        raise ValueError("速度は1・2・3です。")
                    self.speed = speed
                self._resume()
            elif action in ("pause", "cancel", "kill"):
                if action == "pause" and busy:
                    raise RuntimeError("実行中は『今回の指示を中断』を使用してください。")
                try:
                    previous = self.bridge.status(timeout_ms=2000)
                    if action == "pause" and previous.get("speed", 0) > 0:
                        self.speed = min(3, previous["speed"])
                    self._pause()
                except BridgeError:
                    self.last_error = "ゲームを停止できません。ゲーム画面で手動停止してください。"
                    if action == "pause":
                        raise
                if action == "cancel" and self.current and not busy:
                    self.current.status = "cancelled"
                    self._save(self.current)
                self.phase = "stopped" if action == "kill" else "paused"
                if action == "kill":
                    self.llm.ledger.stop()
            else:
                raise ValueError("不明な操作です。")
            return {"ok": True, "phase": self.phase}

    def snapshot(self):
        with self.lock:
            return {"phase": self.phase, "game": deepcopy(self.game), "error": self.last_error,
                    "busy": bool(self.worker and self.worker.is_alive()), "current": self.current.public() if self.current else None,
                    "history": list(self.commands.values())[-20:], "steward": deepcopy(self.steward), "speed": self.speed,
                    "notice": "以前の建築予定・製造指示・作業指定は残ります。結果と次の行動は人間が判断します。"}

    def usage(self):
        snapshot = self.llm.ledger.snapshot()
        with self.llm.ledger._db() as db:
            rows = db.execute("SELECT meta,cost_usd,status,reserved_usd,price FROM requests").fetchall()
        totals = {}
        for row in rows:
            cid = json.loads(row["meta"]).get("command_id")
            if cid:
                t = totals.setdefault(cid, {"cost_usd": Decimal(0), "reserved_usd": Decimal(0), "unpriced": False})
                t["unpriced"] |= row["price"] is None
                t["cost_usd"] += Decimal(row["cost_usd"] or "0")
                if row["status"] in ("reserved", "unknown"):
                    t["reserved_usd"] += Decimal(row["reserved_usd"])
        return {**snapshot, "provider": getattr(getattr(self.llm, "provider", None), "name", "unknown"),
                "priced": getattr(self.llm, "price", None) is not None,
                "commands": {k: {"cost_usd": None if t["unpriced"] else str(t["cost_usd"]),
                                 "reserved_usd": str(t["reserved_usd"])} for k, t in totals.items()}}
