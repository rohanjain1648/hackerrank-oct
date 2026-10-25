"""Token / call accounting for every model the agent uses (remote and local)."""

from __future__ import annotations

from collections import defaultdict

# USD per million tokens (Anthropic first-party list prices).
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class UsageTracker:
    def __init__(self):
        self.models = defaultdict(lambda: {"provider": "", "calls": 0, "cached_calls": 0,
                                           "input_tokens": 0, "output_tokens": 0, "units": 0, "purpose": set()})

    def record_llm(self, model: str, input_tokens: int, output_tokens: int, purpose: str, cached=False):
        m = self.models[model]
        m["provider"] = "Anthropic"
        if cached:
            m["cached_calls"] += 1
        else:
            m["calls"] += 1
            m["input_tokens"] += int(input_tokens or 0)
            m["output_tokens"] += int(output_tokens or 0)
        m["purpose"].add(purpose)

    def record_local(self, model: str, label: str, units: int = 1):
        m = self.models[model]
        m["provider"] = f"local ({label})"
        m["calls"] += units
        m["units"] += units
        m["purpose"].add("screenshot OCR")

    def summary(self, requests: int, seconds: float, split: str, media_engines: dict) -> dict:
        rows = []
        tot_in = tot_out = tot_calls = 0
        tot_cost = 0.0
        for name, m in sorted(self.models.items()):
            pin, pout = PRICES.get(name, (0.0, 0.0))
            cost = m["input_tokens"] / 1e6 * pin + m["output_tokens"] / 1e6 * pout
            rows.append({"model": name, "provider": m["provider"], "calls": m["calls"],
                         "cache_hits": m["cached_calls"], "input_tokens": m["input_tokens"],
                         "output_tokens": m["output_tokens"], "cost_usd": round(cost, 6),
                         "purpose": sorted(m["purpose"])})
            tot_in += m["input_tokens"]
            tot_out += m["output_tokens"]
            tot_calls += m["calls"]
            tot_cost += cost
        n = max(requests, 1)
        return {
            "split": split, "requests": requests, "wall_seconds": round(seconds, 1), "models": rows,
            "total": {"calls": tot_calls, "input_tokens": tot_in, "output_tokens": tot_out,
                      "tokens": tot_in + tot_out, "avg_tokens_per_request": round((tot_in + tot_out) / n, 2),
                      "cost_usd": round(tot_cost, 6), "avg_cost_per_request_usd": round(tot_cost / n, 8)},
            "media_engines": media_engines,
        }
