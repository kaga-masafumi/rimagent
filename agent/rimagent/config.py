from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .paths import ROOT

_DEFAULTS: dict[str, Any] = {
    "llm": {"provider": "qwen", "base_url": "http://127.0.0.1:8000/v1", "model": "qwen", "api_key": "not-needed", "max_streams": 4, "thinking": True, "reasoning_effort": "low", "max_tokens": 4000, "timeout_s": 900, "capture": True},
    "budget": {"daily_usd": 1, "monthly_usd": 10},
    "bridge": {"url": "http://127.0.0.1:8765"},
    "play": {"speed": 3, "think_speed": 3, "danger_think_speed": 0, "wake_hours": 6, "max_tool_calls": 30, "max_days": 60, "autosave": True, "seeds": ["rimagent-1"], "scenario": "Crashlanded", "storyteller": "Cassandra", "difficulty": "Rough", "wake_on_kinds": ["dialog", "letter", "incident", "colonist_died", "colonist_downed", "mental_break", "hostile_group", "quest", "building_lost", "steward", "orders"]},
    "steward": {"enabled": True, "scorer": True, "stock": True, "research_queue_default": [], "orders": {"enabled": True, "off": [], "superseded_watchers": None}},
    "watchdog": {"enabled": False, "every_hours": 6, "min_errors": 8, "max_tool_calls": 40},
    "dashboard": {"port": 8770, "open_browser": True},
}


def _merge(cfg: dict[str, Any], user: dict[str, Any]) -> None:
    for k, v in user.items():
        if k == "superseded_watchers":
            cfg[k] = deepcopy(v)  # an explicit empty map disables all supersessions
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
            _merge(cfg[k], v)
        else:
            cfg[k] = deepcopy(v)


def load(path: Path | None = None) -> dict[str, Any]:
    """config.yaml, then config.local.yaml (gitignored, private endpoints) merged on top."""
    path = path or ROOT / "config.yaml"
    cfg = deepcopy(_DEFAULTS)
    for p in (path, path.with_name("config.local.yaml")):
        if p.exists():
            user = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
            if not isinstance(user, dict):
                raise ValueError(f"{p.name} must contain a YAML mapping")
            _merge(cfg, user)
    return cfg


CONFIG = load()
