"""Durable per-attempt usage and atomic budget reservations, in real-world JST.

Reservations are estimates, not invoices. Unknown outcomes retain their reservation
until an operator reconciles them. A hold never expires without an explicit resume.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable
import uuid

from .pricing import Price, normalize_usage

JST = timezone(timedelta(hours=9), "Asia/Tokyo")
PENDING = ("reserved", "unknown")


class LLMBlocked(RuntimeError):
    """Do not retry, run tools, or automatically resume the game."""


def estimate_input(messages: list[dict], tools: list[dict] | None) -> int:
    """Conservative UTF-8 estimate including schemas; images are not base64 text tokens.

    Reserve 65,536 tokens per image. Actual provider usage remains authoritative.
    This intentionally overestimates many short, English-language prompts.
    """
    images = 0

    def clean(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") == "image_url":
                images += 1
                return {"type": "image_url"}
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    encoded = json.dumps(clean({"messages": messages, "tools": tools}), ensure_ascii=False).encode("utf-8")
    return len(encoded) + 1024 + images * 65_536


class UsageLedger:
    def __init__(self, path: Path, cfg: dict[str, Any] | None = None, *, emit=None,
                 on_hold: Callable[[str], None] | None = None, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.emit = emit or (lambda kind, data: None)
        self.on_hold = on_hold
        self.lock = threading.RLock()
        self._storage_error = ""
        self._stopped = False
        cfg = cfg or {}
        self.daily = self._limit(cfg.get("daily_usd", 1))
        self.monthly = self._limit(cfg.get("monthly_usd", 10))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, sent_at REAL NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, meta TEXT NOT NULL,
                    status TEXT NOT NULL, reserved_usd TEXT NOT NULL, cost_usd TEXT,
                    usage TEXT, raw_usage TEXT, price TEXT, request_id TEXT, error TEXT,
                    finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS requests_day ON requests(day);
                CREATE INDEX IF NOT EXISTS requests_month ON requests(month);
            """)
            # A fresh process must not silently forget an interrupted in-flight request.
            if db.execute("SELECT 1 FROM requests WHERE status IN ('reserved','unknown') AND price IS NOT NULL LIMIT 1").fetchone():
                self._set_hold(db, "usage_unknown: reconcile outstanding requests before resuming")

    @staticmethod
    def _limit(value) -> Decimal:
        limit = Decimal(str(value))
        if not limit.is_finite() or limit < 0:
            raise ValueError("Budget limits must be finite, nonnegative USD amounts")
        return limit

    @contextmanager
    def _db(self):
        with self.lock:
            db = None
            try:
                db = sqlite3.connect(self.path, timeout=15)
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except (sqlite3.Error, OSError) as exc:
                if db is not None:
                    db.rollback()
                self._storage_error = "usage_storage_error: API requests stopped; repair the ledger and restart"
                self._notify_hold(self._storage_error)
                raise LLMBlocked(self._storage_error) from exc
            finally:
                if db is not None:
                    db.close()

    def _period(self):
        dt = datetime.fromtimestamp(self.clock(), JST)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")

    @staticmethod
    def _set_hold(db, reason: str):
        db.execute("INSERT INTO control VALUES ('hold', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (reason,))

    def _held(self, db):
        row = db.execute("SELECT value FROM control WHERE key='hold'").fetchone()
        return self._storage_error or (row[0] if row else "")

    def _notify_hold(self, reason):
        try:
            self.emit("budget", {"held": True, "reason": reason})
        except Exception:
            pass
        if self.on_hold:
            self.on_hold(reason)

    def hold(self, reason: str):
        with self._db() as db:
            self._set_hold(db, reason)
        self._notify_hold(reason)

    @property
    def hold_reason(self) -> str:
        if self._storage_error:
            return self._storage_error
        with self._db() as db:
            return self._held(db)

    def ensure_active(self):
        if self._stopped:
            raise LLMBlocked("operator_stop: agent is shutting down")
        reason = self.hold_reason
        if reason:
            raise LLMBlocked(reason)

    def stop(self):
        """Block new requests/actions locally; keep in-flight accounting intact."""
        with self.lock:
            self._stopped = True

    def _totals(self, db):
        day, month = self._period()
        rows = db.execute("SELECT status, cost_usd, reserved_usd, day, month FROM requests").fetchall()
        daily = sum((Decimal(r["cost_usd"]) for r in rows if r["day"] == day and r["cost_usd"] is not None), Decimal(0))
        monthly = sum((Decimal(r["cost_usd"]) for r in rows if r["month"] == month and r["cost_usd"] is not None), Decimal(0))
        # Carry all unresolved reservations across date boundaries.
        reserved = sum((Decimal(r["reserved_usd"]) for r in rows if r["status"] in PENDING), Decimal(0))
        return daily, monthly, reserved

    def _exceeded(self, db, amount=Decimal(0)) -> str:
        daily, monthly, pending = self._totals(db)
        if daily + pending + amount > self.daily or (amount == 0 and daily + pending >= self.daily):
            return "daily_budget: daily USD limit reached or insufficient reservation headroom"
        if monthly + pending + amount > self.monthly or (amount == 0 and monthly + pending >= self.monthly):
            return "monthly_budget: monthly USD limit reached or insufficient reservation headroom"
        return ""

    def reserve(self, provider: str, model: str, price: Price | None, input_tokens: int,
                output_tokens: int, meta: dict | None = None) -> str:
        reason = ""
        rid = uuid.uuid4().hex
        with self._db() as db:
            if self._stopped:
                raise LLMBlocked("operator_stop: agent is shutting down")
            reason = self._held(db)
            if not reason and provider == "openai" and price is None:
                reason = f"price_unknown: configure prices for {provider}/{model}"
            amount = price.reserve(input_tokens, output_tokens) if price else Decimal(0)
            if not reason and price is not None:
                reason = self._exceeded(db, amount)
            if reason:
                self._set_hold(db, reason)
            else:
                day, month = self._period()
                db.execute("""INSERT INTO requests
                    (id,sent_at,day,month,provider,model,meta,status,reserved_usd,price)
                    VALUES (?,?,?,?,?,?,?,'reserved',?,?)""",
                           (rid, self.clock(), day, month, provider, model, json.dumps(meta or {}),
                            str(amount), json.dumps(price.to_dict()) if price else None))
        if reason:
            self._notify_hold(reason)
            raise LLMBlocked(reason)
        return rid

    def complete(self, rid: str, raw: dict | None, *, request_id: str | None = None):
        reason = ""
        with self._db() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if row is None or row["status"] != "reserved":
                raise LLMBlocked("Request was already completed or reconciled")
            try:
                normalized = normalize_usage(raw or {})
            except (ValueError, TypeError, AttributeError):
                normalized = None
            price = Price(**json.loads(row["price"])) if row["price"] else None
            cost = price.cost(normalized) if price and normalized else None
            status = "complete" if normalized else "unknown" if price else "unpriced_unknown"
            db.execute("""UPDATE requests SET status=?, cost_usd=?, usage=?, raw_usage=?, request_id=?, finished_at=? WHERE id=?""",
                       (status, str(cost) if cost is not None else None, json.dumps(normalized),
                        json.dumps(raw), request_id, self.clock(), rid))
            if price and normalized is None:
                reason = "usage_unknown: response has no valid usage; reconcile its reservation"
            elif price:
                reason = self._exceeded(db)
            if reason:
                self._set_hold(db, reason)
        self.emit("usage", {"id": rid, "provider": row["provider"], "model": row["model"],
                            "meta": json.loads(row["meta"]), "status": status, "usage": normalized,
                            "cost_usd": str(cost) if cost is not None else None, "request_id": request_id})
        if reason:
            self._notify_hold(reason)

    def failed(self, rid: str, *, uncertain: bool, error: str, blocking: bool = False):
        reason = ""
        with self._db() as db:
            row = db.execute("SELECT price FROM requests WHERE id=? AND status='reserved'", (rid,)).fetchone()
            if row is None:
                raise LLMBlocked("Request is no longer pending")
            paid = row["price"] is not None
            db.execute("UPDATE requests SET status=?, error=?, finished_at=? WHERE id=?",
                       ("unknown" if uncertain and paid else "unpriced_unknown" if uncertain else "failed", error, self.clock(), rid))
            if paid and uncertain:
                reason = "usage_unknown: request outcome is uncertain; reconcile its reservation"
            elif blocking:
                reason = "api_blocked: fix API authentication, billing, or request configuration before resuming"
            if reason:
                self._set_hold(db, reason)
        self.emit("usage", {"id": rid, "status": "unknown" if uncertain else "failed", "error": error})
        if reason:
            self._notify_hold(reason)

    def reconcile(self, rid: str, cost_usd=None):
        """Operator-only: stop the runner first; absent cost charges the full reservation."""
        with self._db() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if row is None or row["status"] not in PENDING:
                raise ValueError("Request is not outstanding")
            cost = self._limit(cost_usd if cost_usd is not None else row["reserved_usd"])
            db.execute("UPDATE requests SET status='reconciled',cost_usd=?,finished_at=? WHERE id=?",
                       (str(cost), self.clock(), rid))

    def resume(self):
        if self._storage_error:
            raise LLMBlocked(self._storage_error)
        with self._db() as db:
            if db.execute("SELECT 1 FROM requests WHERE status IN ('reserved','unknown') LIMIT 1").fetchone():
                raise LLMBlocked("Reconcile outstanding requests before resuming")
            reason = self._exceeded(db)
            if reason:
                raise LLMBlocked(reason)
            db.execute("DELETE FROM control WHERE key='hold'")
        self.emit("budget", {"held": False, "reason": "explicit operator resume"})

    def snapshot(self) -> dict:
        with self._db() as db:
            daily, monthly, reserved = self._totals(db)
            rows = db.execute("SELECT * FROM requests ORDER BY sent_at DESC LIMIT 100").fetchall()
            pending = db.execute("SELECT id,status,reserved_usd FROM requests WHERE status IN ('reserved','unknown')").fetchall()
            totals = {k: 0 for k in ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")}
            for r in db.execute("SELECT usage FROM requests WHERE usage IS NOT NULL"):
                normalized = json.loads(r[0])
                if normalized:
                    for k in totals:
                        totals[k] += normalized[k]
            def threshold(spend, limit):
                fraction = (spend + reserved) / limit if limit else Decimal(1)
                return 100 if fraction >= 1 else 95 if fraction >= Decimal(".95") else 80 if fraction >= Decimal(".8") else 0
            return {"timezone": "Asia/Tokyo", "day": self._period()[0], "month": self._period()[1],
                    "daily_usd": str(daily), "monthly_usd": str(monthly), "reserved_usd": str(reserved),
                    "daily_limit_usd": str(self.daily), "monthly_limit_usd": str(self.monthly),
                    "warning_percent": max(threshold(daily, self.daily), threshold(monthly, self.monthly)),
                    "hold_reason": self._held(db), "totals": totals, "outstanding": [dict(r) for r in pending],
                    "requests": [{**dict(r), "meta": json.loads(r["meta"]),
                                  "usage": json.loads(r["usage"]) if r["usage"] else None} for r in rows]}

    @contextmanager
    def action_guard(self, method: str, params: dict):
        """Serialize new mutations against local holds. Read views and pausing remain available."""
        if method.startswith(("state.", "defs.")) or method in (
            "game.status", "game.list_saves", "bridge.methods", "map.detail", "map.find", "map.overview",
        ) or (method == "game.pause" and params.get("paused", True) is True):
            yield
            return
        with self.lock:
            self.ensure_active()
            yield
