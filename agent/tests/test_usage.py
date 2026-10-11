from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3
import threading

import pytest

from rimagent.pricing import LUNA, Price, normalize_usage
from rimagent.usage import LLMBlocked, UsageLedger, estimate_input


def usage(inp=10_000, out=1000, cached=0, reasoning=0, writes=0):
    return {"prompt_tokens": inp, "completion_tokens": out,
            "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": writes},
            "completion_tokens_details": {"reasoning_tokens": reasoning}}


def ledger(tmp_path, **kw):
    return UsageLedger(tmp_path / "usage.sqlite3", **kw)


def test_cache_and_reasoning_are_not_double_billed():
    norm = normalize_usage(usage(cached=5000, reasoning=900))
    assert LUNA.cost(norm) == Decimal("0.0023")
    assert LUNA.cost(normalize_usage(usage())) == Decimal("0.0032")
    assert LUNA.cost(normalize_usage(usage(writes=5000))) == Decimal("0.00345")


def test_long_context_boundary_applies_to_whole_request():
    assert LUNA.cost(normalize_usage(usage(inp=272_000))) == Decimal("0.0556")
    assert LUNA.cost(normalize_usage(usage(inp=272_001))) == Decimal("0.1106004")


@pytest.mark.parametrize("raw", [{}, usage(inp=-1), usage(cached=10_001), usage(reasoning=1001),
                                  {"prompt_tokens": True, "completion_tokens": 1}])
def test_invalid_usage_never_becomes_free(raw):
    with pytest.raises(ValueError):
        normalize_usage(raw)


def test_response_usage_and_price_snapshot_survive_restart(tmp_path):
    l = ledger(tmp_path)
    rid = l.reserve("openai", "gpt-5.6-luna", LUNA, 20_000, 4000,
                    {"stream": "manager", "attempt": 1})
    l.complete(rid, usage(reasoning=900), request_id="req_test")
    restored = ledger(tmp_path).snapshot()
    assert restored["daily_usd"] == "0.0032"
    assert restored["reserved_usd"] == "0"
    assert restored["totals"]["reasoning_tokens"] == 900
    assert restored["requests"][0]["request_id"] == "req_test"
    assert restored["requests"][0]["meta"]["stream"] == "manager"
    assert '2026-10-11' in restored["requests"][0]["price"]
    with pytest.raises(LLMBlocked):
        l.complete(rid, usage())


def test_parallel_reservations_cannot_overcommit(tmp_path):
    l = ledger(tmp_path, cfg={"daily_usd": "0.002", "monthly_usd": 10})
    barrier = threading.Barrier(2)
    def reserve():
        barrier.wait()
        try:
            return l.reserve("openai", "gpt-5.6-luna", LUNA, 1000, 1000)
        except LLMBlocked:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(x is not None for x in ids) == 1
    assert l.snapshot()["reserved_usd"] == "0.00145"
    assert "daily_budget" in l.hold_reason
    l.complete(next(x for x in ids if x), usage(inp=1, out=1))
    assert l.hold_reason  # actual lower spend must not silently clear the hold


def test_monthly_limit_is_independent_of_daily_limit(tmp_path):
    l = ledger(tmp_path, cfg={"daily_usd": 10, "monthly_usd": "0.001"})
    with pytest.raises(LLMBlocked, match="monthly_budget"):
        l.reserve("openai", "gpt-5.6-luna", LUNA, 1000, 1000)
    assert not l.snapshot()["requests"]


def test_missing_usage_keeps_reservation_until_operator_reconciles(tmp_path):
    holds = []
    l = ledger(tmp_path, on_hold=holds.append)
    rid = l.reserve("openai", "gpt-5.6-luna", LUNA, 1000, 1000)
    l.complete(rid, None)
    assert l.snapshot()["reserved_usd"] == "0.00145"
    assert holds and "usage_unknown" in holds[-1]
    with pytest.raises(LLMBlocked):
        l.resume()
    l.reconcile(rid)  # conservative full reservation; verified invoice cost is optional
    assert l.snapshot()["daily_usd"] == "0.00145"
    assert l.hold_reason
    l.resume()
    assert not l.hold_reason


def test_crashed_request_is_held_on_restart(tmp_path):
    rid = ledger(tmp_path).reserve("openai", "gpt-5.6-luna", LUNA, 1000, 1000)
    l = ledger(tmp_path)
    assert "usage_unknown" in l.hold_reason
    with pytest.raises(LLMBlocked):
        l.ensure_active()
    l.reconcile(rid, "0")
    l.resume()
    assert l.snapshot()["reserved_usd"] == "0"


def test_jst_day_and_month_reset_but_hold_does_not(tmp_path):
    now = [datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc).timestamp()]
    l = ledger(tmp_path, clock=lambda: now[0])
    rid = l.reserve("openai", "gpt-5.6-luna", LUNA, 1000, 1000)
    l.complete(rid, usage(inp=1000, out=1000))
    l.hold("manual_test")
    now[0] += 120
    s = l.snapshot()
    assert (s["day"], s["month"]) == ("2026-11-01", "2026-11")
    assert s["daily_usd"] == s["monthly_usd"] == "0"
    assert l.hold_reason == "manual_test"
    l.resume()
    assert not l.hold_reason


def test_unresolved_reservation_carries_across_midnight(tmp_path):
    now = [datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc).timestamp()]
    l = ledger(tmp_path, clock=lambda: now[0])
    l.reserve("openai", "gpt-5.6-luna", LUNA, 1000, 1000)
    now[0] += 120
    assert l.snapshot()["reserved_usd"] == "0.00145"


def test_actual_usage_over_reservation_latches_hold(tmp_path):
    l = ledger(tmp_path, cfg={"daily_usd": "0.002", "monthly_usd": 10})
    rid = l.reserve("openai", "gpt-5.6-luna", LUNA, 1, 1)
    l.complete(rid, usage())
    assert "daily_budget" in l.hold_reason
    with pytest.raises(LLMBlocked):
        l.resume()


def test_unpriced_qwen_is_visible_and_remains_compatible(tmp_path):
    l = ledger(tmp_path)
    rid = l.reserve("qwen", "qwen", None, 1000, 1000)
    l.complete(rid, usage())
    assert l.snapshot()["requests"][0]["cost_usd"] is None
    rid = l.reserve("qwen", "qwen", None, 1000, 1000)
    l.failed(rid, uncertain=True, error="transport_error")
    assert not ledger(tmp_path).hold_reason


def test_unknown_openai_price_and_storage_fail_closed(tmp_path, monkeypatch):
    l = ledger(tmp_path)
    with pytest.raises(LLMBlocked, match="price_unknown"):
        l.reserve("openai", "unknown", None, 1, 1)
    assert not l.snapshot()["requests"]
    def broken(*args, **kw):
        raise sqlite3.OperationalError("disk unavailable")
    monkeypatch.setattr(sqlite3, "connect", broken)
    with pytest.raises(LLMBlocked, match="usage_storage_error"):
        l.reserve("openai", "gpt-5.6-luna", LUNA, 1, 1)
    assert "usage_storage_error" in l.hold_reason


def test_estimate_includes_tools_and_images_without_treating_base64_as_text():
    plain = [{"role": "user", "content": "hi"}]
    assert estimate_input(plain, [{"large": "x" * 1000}]) > estimate_input(plain, None)
    image = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "x" * 100_000}}]}]
    assert 65_536 < estimate_input(image, None) < 100_000


def test_thresholds_include_reservations(tmp_path):
    l = ledger(tmp_path, cfg={"daily_usd": "0.0016", "monthly_usd": 10})
    l.reserve("openai", "gpt-5.6-luna", LUNA, 1000, 1000)
    assert l.snapshot()["warning_percent"] == 80
