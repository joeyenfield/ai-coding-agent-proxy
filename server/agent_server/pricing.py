"""What a model call cost, estimated from its token counts and a table of prices.

The table is a stored setting (`model_prices`): US dollars for a million tokens, by a pattern
matched against the model's name, with the first match winning. Site admins edit it
(`/api/config/prices`), and an API provider can carry prices of its own (its `prices` option),
which are tried before the server's; it starts as `DEFAULT_PRICES`, which are list prices as they were known
when this was written and so only a starting point: providers change them, and a model with no
entry gets no estimate rather than a guess.

A cost is worked out once, when the request is recorded, at the prices of that moment, and kept
in its record (`cost`). Only calls that are paid for by the token get one: a hosted API or
gateway the server calls with a key (`basis: "api"`). An account agent's calls go out on a
subscription that isn't billed by the token, so theirs say what the same tokens would cost at
API prices (`basis: "subscription"`), which is not money spent.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Any

MILLION = 1_000_000
MAX_PRICES = 500
PATTERN = re.compile(r"[^\s\x00-\x1f]{1,200}")
RATES = ("input", "output", "cache_read", "cache_write")

# US dollars for a million tokens. Patterns are matched in order against the lower-cased model name.
DEFAULT_PRICES: list[dict[str, Any]] = [
    # Claude 5: a version's own row comes before its family's (5.5 and 5.2 cost less than 5 and 5.1).
    {"match": "claude-fable-5-2*", "input": 8.0, "output": 40.0, "cache_read": 0.2, "cache_write": 10.0},
    {"match": "claude-mythos-5-2*", "input": 8.0, "output": 40.0, "cache_read": 0.2, "cache_write": 10.0},
    {"match": "claude-fable-5*", "input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    {"match": "claude-mythos-5*", "input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    {"match": "claude-opus-5-5*", "input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0},
    {"match": "claude-opus-5*", "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    {"match": "claude-sonnet-5-5*", "input": 2.0, "output": 10.0, "cache_read": 0.1, "cache_write": 2.5},
    {"match": "claude-sonnet-5*", "input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
    # Prompts up to 100K tokens; longer ones cost more, which one price per model can't say.
    {"match": "claude-haiku-5*", "input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125},
    {"match": "claude-opus-4-5*", "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    {"match": "claude-opus-4-[6-8]*", "input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    {"match": "claude-opus-4*", "input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    {"match": "claude-sonnet-4*", "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    {"match": "claude-3-7-sonnet*", "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    {"match": "claude-haiku-4*", "input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    {"match": "claude-3-5-haiku*", "input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    {"match": "gpt-5-mini*", "input": 0.25, "output": 2.0, "cache_read": 0.025},
    {"match": "gpt-5-nano*", "input": 0.05, "output": 0.4, "cache_read": 0.005},
    {"match": "gpt-5*", "input": 1.25, "output": 10.0, "cache_read": 0.125},
    {"match": "gpt-4.1-mini*", "input": 0.4, "output": 1.6, "cache_read": 0.1},
    {"match": "gpt-4.1-nano*", "input": 0.1, "output": 0.4, "cache_read": 0.025},
    {"match": "gpt-4.1*", "input": 2.0, "output": 8.0, "cache_read": 0.5},
    {"match": "gpt-4o-mini*", "input": 0.15, "output": 0.6, "cache_read": 0.075},
    {"match": "gpt-4o*", "input": 2.5, "output": 10.0, "cache_read": 1.25},
    {"match": "o4-mini*", "input": 1.1, "output": 4.4, "cache_read": 0.275},
    {"match": "o3*", "input": 2.0, "output": 8.0, "cache_read": 0.5},
    {"match": "deepseek-*", "input": 0.28, "output": 0.42, "cache_read": 0.028},
]


def checked_prices(data: Any) -> list[dict[str, Any]]:
    """The price table as it's stored: [{match, input, output, cache_read?, cache_write?}], dollars for a
    million tokens. cache_read and cache_write default to the input price when left out."""
    if not isinstance(data, list) or len(data) > MAX_PRICES:
        raise ValueError(f"model_prices is a list of up to {MAX_PRICES} prices")
    prices: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each price is an object: match, input, output, cache_read, cache_write")
        match = item.get("match")
        if not isinstance(match, str) or not PATTERN.fullmatch(match.strip()):
            raise ValueError("match is a model name or pattern, such as claude-sonnet-4*")
        price: dict[str, Any] = {"match": match.strip().lower()}
        for rate in RATES:
            value = item.get(rate)
            if value is None and rate in ("cache_read", "cache_write"):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100_000:
                raise ValueError(f"{match}: {rate} is dollars for a million tokens, a number from 0 up")
            price[rate] = float(value)
        prices.append(price)
    return prices


def price_for(prices: list[dict[str, Any]], model: str | None) -> dict[str, Any] | None:
    """The first price whose pattern matches the model's name. A name with a provider in front
    (anthropic/claude-sonnet-4, as gateways write them) is also tried without it."""
    name = (model or "").strip().lower()
    if not name:
        return None
    names = [name] + ([name.rsplit("/", 1)[-1]] if "/" in name else [])
    for price in prices:
        if any(fnmatchcase(candidate, price["match"]) for candidate in names):
            return price
    return None


def estimate(prices: list[dict[str, Any]], record: dict[str, Any], basis: str) -> dict[str, Any] | None:
    """What the request's tokens cost at the table's prices, or None when it used none or its model has
    no price. Cached tokens are priced at the cache rates: OpenAI-style usage counts them inside the
    input tokens (cached_in_input), Anthropic's counts them beside it."""
    fresh, output = int(record.get("input_tokens") or 0), int(record.get("output_tokens") or 0)
    read, written = int(record.get("cache_read_tokens") or 0), int(record.get("cache_creation_tokens") or 0)
    if fresh + output + read + written <= 0:
        return None
    # The model the provider was asked for is the one it bills.
    price = price_for(prices, record.get("upstream_model") or record.get("model"))
    if price is None:
        return None
    if record.get("cached_in_input"):
        fresh = max(fresh - read, 0)
    parts = {
        "input_usd": fresh * price["input"] / MILLION,
        "output_usd": output * price["output"] / MILLION,
        "cache_read_usd": read * price.get("cache_read", price["input"]) / MILLION,
        "cache_write_usd": written * price.get("cache_write", price["input"]) / MILLION,
    }
    return {
        "usd": round(sum(parts.values()), 6),
        **{name: round(value, 6) for name, value in parts.items()},
        "price": price["match"],
        "basis": basis,
    }


def total(records: list[dict[str, Any]]) -> dict[str, Any]:
    """A session's cost from its records: the total, by model, and how many model calls had no price."""
    models: dict[str, dict[str, Any]] = {}
    spent = at_api_prices = 0.0
    priced = unpriced = 0
    for record in records:
        cost = record.get("cost")
        tokens = int(record.get("input_tokens") or 0) + int(record.get("output_tokens") or 0)
        if not isinstance(cost, dict):
            # A model call the table had no price for: said, so a total isn't read as complete.
            if tokens > 0 and record.get("model") and record.get("unpriced"):
                unpriced += 1
            continue
        priced += 1
        if cost.get("basis") == "api":
            spent += cost["usd"]
        else:
            at_api_prices += cost["usd"]
        entry = models.setdefault(str(record.get("model") or ""), {"model": record.get("model"), "requests": 0, "usd": 0.0, "basis": cost.get("basis")})
        entry["requests"] += 1
        entry["usd"] = round(entry["usd"] + cost["usd"], 6)
    return {
        "currency": "USD", "usd": round(spent, 6), "subscription_usd": round(at_api_prices, 6),
        "priced": priced, "unpriced": unpriced,
        "models": sorted(models.values(), key=lambda item: -item["usd"]),
    }
