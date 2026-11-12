"""What a model call cost, estimated from its token counts and a table of prices.

The table is a stored setting (`model_prices`): US dollars for a million tokens, by a pattern
matched against the model's name, with the first match winning. Site admins edit it
(`/api/config/prices`), and an API provider can carry prices of its own (its `prices` option),
which are tried before the server's; it starts as `DEFAULT_PRICES` (the rows of `default_prices.yaml`), which are list
prices as they were known when that file was last checked and so only a starting point: providers change them, and a model with no
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
from pathlib import Path
from typing import Any

import yaml

MILLION = 1_000_000
MAX_PRICES = 500
PATTERN = re.compile(r"[^\s\x00-\x1f]{1,200}")
RATES = ("input", "output", "cache_read", "cache_write")
# The prices the server ships with, kept as data so they can be brought up to date without touching code.
SHIPPED = Path(__file__).with_name("default_prices.yaml")


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


def shipped_prices() -> list[dict[str, Any]]:
    """The table in default_prices.yaml, checked as a saved one is: a mistake there stops the server starting."""
    document = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    return checked_prices(document.get("prices") if isinstance(document, dict) else None)


# US dollars for a million tokens. Patterns are matched in order against the lower-cased model name.
DEFAULT_PRICES: list[dict[str, Any]] = shipped_prices()

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
