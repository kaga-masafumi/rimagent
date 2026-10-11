"""Versioned USD prices per million tokens. Unknown prices are never zero prices."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Price:
    input: str
    cached_input: str
    output: str
    cache_write: str
    verified_on: str
    source: str
    long_context_threshold: int = 272_000
    long_input_multiplier: str = "2"
    long_output_multiplier: str = "1.5"

    def __post_init__(self):
        for name in ("input", "cached_input", "output", "cache_write", "long_input_multiplier", "long_output_multiplier"):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value < 0:
                raise ValueError(f"Invalid price: {name}")
        if self.long_context_threshold < 0:
            raise ValueError("Invalid long_context_threshold")

    def cost(self, usage: dict[str, int]) -> Decimal:
        inp, cached, writes, out = (usage[k] for k in ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens"))
        long = inp > self.long_context_threshold
        im = Decimal(self.long_input_multiplier) if long else Decimal(1)
        om = Decimal(self.long_output_multiplier) if long else Decimal(1)
        return ((Decimal(inp - cached - writes) * Decimal(self.input)
                 + Decimal(cached) * Decimal(self.cached_input)
                 + Decimal(writes) * Decimal(self.cache_write)) * im
                + Decimal(out) * Decimal(self.output) * om) / Decimal(1_000_000)

    def reserve(self, input_tokens: int, output_tokens: int) -> Decimal:
        # No cache discount; reserve the highest input rate (including cache writes).
        rate = max(Decimal(self.input), Decimal(self.cache_write), Decimal(self.cached_input))
        im = Decimal(self.long_input_multiplier) if input_tokens > self.long_context_threshold else Decimal(1)
        om = Decimal(self.long_output_multiplier) if input_tokens > self.long_context_threshold else Decimal(1)
        return (Decimal(input_tokens) * rate * im + Decimal(output_tokens) * Decimal(self.output) * om) / Decimal(1_000_000)

    def to_dict(self):
        return asdict(self)


LUNA = Price("0.20", "0.02", "1.20", "0.25", "2026-10-11",
             "https://developers.openai.com/api/docs/models/gpt-5.6-luna")


def price_for(provider: str, model: str, overrides: dict[str, Any] | None = None) -> Price | None:
    custom = (overrides or {}).get(f"{provider}/{model}")
    if custom is not None:
        return Price(**custom)
    return LUNA if (provider, model) == ("openai", "gpt-5.6-luna") else None


def normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
    details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    output = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
    values = {
        "input_tokens": raw.get("prompt_tokens", raw.get("input_tokens")),
        "cached_tokens": details.get("cached_tokens", 0),
        "cache_write_tokens": details.get("cache_write_tokens", 0),
        "output_tokens": raw.get("completion_tokens", raw.get("output_tokens")),
        "reasoning_tokens": output.get("reasoning_tokens", 0),
    }
    # SDK optional detail fields are serialized as null when the API omits them.
    for key in ("cached_tokens", "cache_write_tokens", "reasoning_tokens"):
        if values[key] is None:
            values[key] = 0
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values.values()):
        raise ValueError("Incomplete or invalid API usage")
    if values["cached_tokens"] + values["cache_write_tokens"] > values["input_tokens"]:
        raise ValueError("Cached tokens exceed input tokens")
    if values["reasoning_tokens"] > values["output_tokens"]:
        raise ValueError("Reasoning tokens exceed output tokens")
    return values
