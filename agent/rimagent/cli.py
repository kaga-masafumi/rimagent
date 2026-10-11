"""rimagent command line."""
from __future__ import annotations

import argparse
import json
import sys

from .config import CONFIG


def cmd_seed(args):
    from .knowledge import wiki
    if not args.skip_wiki:
        wiki.scrape(force=args.force, log=print)
    if args.distill:
        from .knowledge import seed
        from .llm import LLM
        seed.distill_skills(LLM(), print)
    print("seed done")


def cmd_play(args):
    from .bus import BUS
    from .runner import Runner, start_dashboard
    if args.max_days:
        CONFIG["play"]["max_days"] = args.max_days
    if args.seeds:
        CONFIG["play"]["seeds"] = args.seeds.split(",")
    if args.no_pause:
        CONFIG["play"]["pause_to_think"] = False
    r = Runner(CONFIG, BUS)
    if not args.no_dashboard:
        start_dashboard(r)
    if args.verbose:
        def printer(ev):
            k, d = ev["kind"], ev["data"]
            if k in ("tool_call",):
                print(f"→ {d['name']} {json.dumps(d['args'])[:300]}")
            elif k == "tool_result":
                print(f"← {d['name']} {'ok' if d['ok'] else 'ERR'} {d['text'][:300]}")
            elif k in ("assistant", "log", "error", "think_start", "think_end", "episode_start", "episode_end", "watcher", "brain_change"):
                print(f"[{k}] {json.dumps(d, default=str)[:600]}")
            elif k == "reasoning":
                print(f"[thinking] {d['text'][:400].replace(chr(10), ' ')}…")
        BUS.subscribe(printer)
    try:
        r.run()
    except KeyboardInterrupt:
        print("stopping")
    finally:
        r.request_stop()


def cmd_think(args):
    """One play step against the live game, then exit (for debugging)."""
    from .bus import BUS
    from .loop import situation_packet, think
    from .runner import Runner
    r = Runner(CONFIG, BUS)
    r.bridge.wait_alive(30)
    r.registry.add_bridge_methods(r.bridge.methods())
    r.registry.reload_brain()
    BUS.subscribe(lambda ev: print(f"[{ev['kind']}] {json.dumps(ev['data'], default=str)[:800]}"))
    st = r.bridge.status()
    if st.get("state") != "playing":
        print("no game running; use `rimagent play` or start one via the bridge")
        return
    r.bridge.call("game.pause", paused=True)
    events = r.bridge.events(max(0, int(st.get("seq", 0)) - 50)).get("events", [])
    msg, hint = situation_packet(r.ctx, args.trigger, events, [])
    res = think(r.ctx, msg, hint, trigger=args.trigger)
    print("\nNOTES:", res.notes, "\ncalls:", res.calls, "elapsed:", round(res.elapsed, 1))


def cmd_tools(args):
    from .bridge import Bridge
    from .registry import Registry
    from .tools import brain, knowledge, meta
    reg = Registry()
    for m in (knowledge, brain, meta):
        reg.add_module(m)
    b = Bridge()
    if b.health():
        reg.add_bridge_methods(b.methods())
    reg.reload_brain()
    for t in sorted(reg.tools.values(), key=lambda t: (t.source, t.name)):
        print(f"{t.source:8s} {t.group:10s} {t.name:34s} {t.description[:90]}")
    if reg.load_errors:
        print("load errors:", reg.load_errors)


def cmd_export_sft(args):
    from pathlib import Path
    from .export_sft import export
    streams = None if args.streams == "all" else set(args.streams.split(","))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kept, seen = export(out_path, args.min_score, args.include_assisted, streams)
    print(f"wrote {kept}/{seen} captured calls to {out_path}")


def cmd_llm(args):
    from .llm import LLM
    r = LLM(CONFIG).chat([{"role": "user", "content": args.prompt}], thinking=not args.no_thinking, max_tokens=400,
                         meta={"stream": "cli", "attempt": 1})
    print("reasoning:", r.reasoning[:500])
    print("content:", r.content)
    print("usage:", r.usage, "elapsed:", round(r.elapsed, 1))


def cmd_doctor(args):
    """Configuration-only preflight: no game launch, RPC mutation, or paid API request."""
    from .llm import validate_configuration
    from .paths import ROOT, RUNS
    from .usage import UsageLedger
    summary = validate_configuration(CONFIG)
    summary["watchdog"] = "enabled" if CONFIG.get("watchdog", {}).get("enabled", False) else "disabled"
    if args.require_watchdog_off and summary["watchdog"] != "disabled":
        raise ValueError("Windows startup requires watchdog.enabled: false")
    summary["bridge_source"] = "present" if (ROOT / "mod/Source/RimBridge.csproj").exists() else "missing: git submodule update --init"
    summary["budget"] = UsageLedger(RUNS / "usage.sqlite3", CONFIG.get("budget")).snapshot()
    summary["bridge_url"] = CONFIG["bridge"]["url"]
    summary["dashboard_url"] = f"http://127.0.0.1:{CONFIG.get('dashboard', {}).get('port', 8770)}"
    if args.require_budget_active and summary["budget"]["hold_reason"]:
        raise ValueError(summary["budget"]["hold_reason"])
    if args.require_budget_active and summary["price"] == "configured" and summary["budget"]["warning_percent"] == 100:
        raise ValueError("Budget has no remaining headroom; repair limits or outstanding requests before starting")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_usage(args):
    from .paths import RUNS
    from .usage import UsageLedger
    ledger = UsageLedger(RUNS / "usage.sqlite3", CONFIG.get("budget"))
    if args.resolve:
        ledger.reconcile(args.resolve, args.cost_usd)
    if args.resume:
        ledger.resume()
    print(json.dumps(ledger.snapshot(), ensure_ascii=False, indent=2))


def main(argv=None):
    import faulthandler, signal
    if hasattr(faulthandler, "register") and hasattr(signal, "SIGUSR1"):  # POSIX only; Windows has neither
        faulthandler.register(signal.SIGUSR1, all_threads=True)  # kill -USR1 <pid> dumps every thread's stack to stderr
    ap = argparse.ArgumentParser(prog="rimagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="scrape wiki, build indexes, distill starter skills")
    s.add_argument("--skip-wiki", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument("--distill", action="store_true", help="ask the LLM to write starter skills from wiki pages")
    s.set_defaults(fn=cmd_seed)
    p = sub.add_parser("play", help="run episodes unattended")
    p.add_argument("--max-days", type=int)
    p.add_argument("--seeds")
    p.add_argument("--no-pause", action="store_true")
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_play)
    t = sub.add_parser("think", help="run one think step against the live game")
    t.add_argument("--trigger", default="manual")
    t.set_defaults(fn=cmd_think)
    sub.add_parser("tools", help="list tools").set_defaults(fn=cmd_tools)
    e = sub.add_parser("export-sft", help="export captured LLM calls from good episodes as an SFT dataset")
    e.add_argument("--out", default="sft/dataset.jsonl")
    e.add_argument("--min-score", type=float, default=0.0)
    e.add_argument("--include-assisted", action="store_true")
    e.add_argument("--streams", default="play", help="comma-separated stream names, or 'all'")
    e.set_defaults(fn=cmd_export_sft)
    l = sub.add_parser("llm", help="test the LLM endpoint")
    l.add_argument("prompt")
    l.add_argument("--no-thinking", action="store_true")
    l.set_defaults(fn=cmd_llm)
    d = sub.add_parser("doctor", help="check local configuration without calling the LLM or launching the game")
    d.add_argument("--require-watchdog-off", action="store_true")
    d.add_argument("--require-budget-active", action="store_true")
    d.set_defaults(fn=cmd_doctor)
    u = sub.add_parser("usage", help="inspect the usage ledger; stop the runner before reconciling requests")
    u.add_argument("--resolve", metavar="REQUEST_ID", help="reconcile an outstanding request; defaults to charging its full reservation")
    u.add_argument("--cost-usd", help="verified cost for --resolve, e.g. 0 for a confirmed unbilled request")
    u.add_argument("--resume", action="store_true", help="explicitly clear a repaired hold without launching the game")
    u.set_defaults(fn=cmd_usage)
    args = ap.parse_args(argv)
    if getattr(args, "cost_usd", None) is not None and not getattr(args, "resolve", None):
        ap.error("--cost-usd requires --resolve")
    try:
        args.fn(args)
    except (ValueError, RuntimeError) as exc:
        ap.exit(1, f"rimagent: {exc}\n")


if __name__ == "__main__":
    main()
