"""Reviews of a session: what it spent tokens, time and money on, and what its engineer could change.

report() measures a session from its request records and, where payloads were captured, from its
conversations (chat.py) and the main chat's newest call: traffic, a timeline of the context, what
the context is made of, the system prompt's outline, every tool call with what came back, each
sub-agent, and the main chat step by step. It then runs a fixed list of checks (CHECKS). Each
finding says what was seen, why it matters and what to do about it, and where it can, about how
many tokens it cost. No model is involved in any of that.

brief() writes part of the report as the messages a model is asked to review, by one of the
server's prompts (review_prompts.py, a stored setting): each prompt has its own question, the
parts of the report it is sent (the functions in SECTIONS, by the names in review_prompts.DATA)
and the checks whose findings go with it.

The server never calls a model for this. The console's AI review opens a session of its own
(client REVIEW_CLIENT, the server tag review_of naming the session reviewed) and sends the brief
through the agent endpoints like any other model call, so it's translated, limited, priced and
recorded as one. A finished review is that session's last reply (reply()).
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from datetime import datetime
from typing import Any, Callable

from . import api_providers, pricing
from .chat import _is_model_call, _plain, response_turn
from .config import Backend, Settings, serves
from .review_prompts import DATA, METRICS, Prompt
from .sessions import Session, SessionStore


REVIEW_CLIENT = "console-review"
# The server's tag on a review's session: the session it reviews (access.SERVER_TAGS).
REVIEW_TAG = "review_of"
# The console sends the brief as OpenAI Chat, so a reviewer is a backend that takes it.
REVIEW_PROTOCOL = "openai-chat"
SEVERITIES = ("high", "medium", "low", "info")
# What a finding of each severity takes off a session's score of 100.
PENALTIES = {"high": 18, "medium": 9, "low": 3, "info": 0}
GRADES = ((85, "Efficient"), (65, "Some waste"), (40, "Wasteful"), (0, "Very wasteful"))

# Where a check starts to say something, and where it says it louder. Tokens in payloads are
# estimated at four characters each: close enough to tell 2,000 from 20,000.
CHARS_PER_TOKEN = 4
SYSTEM_PROMPT_TOKENS = (8_000, 20_000)
TOOL_DEFINITION_TOKENS = (10_000, 25_000)
UNUSED_TOOLS = (30, 0.25)
PEAK_CONTEXT_TOKENS = (100_000, 180_000)
PEAK_CONTEXT_SHARE = (0.7, 0.9)
# Tokens the context gains with each call of the main chat, on average, over at least this many calls.
CONTEXT_GROWTH = (5_000, 12_000)
CONTEXT_GROWTH_CALLS = 8
MODEL_CALLS = (150, 400)
COMPACTIONS = (1, 3)
SEARCHES = (20, 60)
SEARCH_SHARE = 0.35
REREADS = (3, 6)
LARGE_RESULT_CHARS = 40_000
LARGE_RESULTS = (1, 5)
TOOL_ERRORS = (5, 0.15, 0.3)
REPEATED_CALLS = 3
SUBAGENTS = (6, 15)
SHORT_PROMPT_CHARS = 80
SHORT_PROMPT_CALLS = 30
STEERING_PROMPTS = 20
FAILED_CALLS = (3, 0.05)
INCOMPLETE_CALLS = 2
CACHE_CALLS = 10
CACHE_INPUT_TOKENS = 200_000
CACHE_SHARE = 0.5
SLOW_FIRST_TOKEN_MS = 15_000
# A call this long after the one before it that writes most of its prompt to the cache again: the cache
# ran out while the session sat idle (Anthropic keeps it five minutes by default).
IDLE_SECONDS = 300
IDLE_REWRITE_SHARE = 0.5
IDLE_REWRITES = (2, 6)
IMAGES = (5, 20)
# A model's tier, from its list price for a million input tokens in the server's price table: small below
# the first, frontier from the second, mid-range between. A machine's own model is local; a paid one the
# table doesn't know is unpriced.
TIER_INPUT_USD = (1.5, 4.0)
TIERS = ("frontier", "mid", "small", "local", "unpriced")
TIER_LABELS = {"frontier": "frontier", "mid": "mid-range", "small": "small", "local": "local", "unpriced": "no price"}
# The share of a session's model calls on frontier models that says something, over at least this many calls.
TIER_SHARE = (0.6, 0.9)
TIER_CALLS = 10
# How much of the report a brief carries, so it fits a small local model's context too.
BRIEF_LIMIT = 16_000
QUESTION_LIMIT = 500
PROMPT_EXCERPTS = 6
PROMPT_EXCERPT_CHARS = 300
TIMELINE_POINTS = 60
STEPS = 20

FILE_KEYS = ("file_path", "path", "filePath", "filename", "target_file", "absolute_path", "notebook_path")
COMMAND_KEYS = ("command", "cmd", "script")
SEARCH_COMMANDS = {"rg", "grep", "egrep", "fgrep", "find", "fd", "ag", "ack", "ls", "dir", "tree", "locate", "findstr", "select-string", "get-childitem"}
READ_COMMANDS = {"cat", "head", "tail", "less", "more", "bat", "type", "get-content"}
MCP_TOOL = re.compile(r"^mcp__(.+?)__")
# A line that starts a section of a system prompt: a Markdown heading, or an XML-style tag on its own.
SECTION = re.compile(r"^(?:#{1,4}\s+(\S.*)|<([A-Za-z][\w-]*)>)\s*$")
# In this order: "web_search" is the web's, not a search of the project.
CATEGORIES = (
    ("mcp", re.compile(r"^mcp__")),
    ("web", re.compile(r"web|fetch|browser|http", re.I)),
    ("agent", re.compile(r"^(task|agent|subagent|dispatch_agent|spawn\w*)$", re.I)),
    ("edit", re.compile(r"edit|write|patch|replace|create_file|insert|delete_file", re.I)),
    ("search", re.compile(r"grep|glob|search|find|^ls$|list_?(dir|files|directory)|codebase|toolsearch", re.I)),
    ("read", re.compile(r"read|view|^cat$|open_file", re.I)),
    ("shell", re.compile(r"bash|shell|exec|command|terminal|powershell", re.I)),
)


def report(sessions: SessionStore, chats: Any, session: Session, records: list[dict[str, Any]], prices: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The session measured and checked: traffic and the timeline (from its records, always), and prompt,
    context, conversation and tools (only for model calls whose payloads were captured), then the findings,
    worst first, and the score they leave. prices is the server's price table (Settings.prices()), which
    says each model's tier."""
    calls = sorted((record for record in records if _is_model_call(record)), key=lambda record: str(record.get("request_id") or ""))
    traffic = _traffic(records, calls, prices if prices is not None else pricing.DEFAULT_PRICES)
    threads = chats.conversations(session, records) if calls else []
    main = next((item for item in threads if item["main"]), None)
    request = {}
    if main:
        trace = sessions.read_trace(session, main["requests"][-1]) or {}
        request = trace.get("request") if isinstance(trace.get("request"), dict) else {}
    tools = _tools(threads)
    prompt = _prompt(request, tools) if request else None
    # The main chat's calls where they're known: sub-agents and background calls have contexts of their own.
    mine = set(main["requests"]) if main else None
    measured = {
        "traced_calls": sum(len(item["requests"]) for item in threads),
        "traffic": traffic,
        "timeline": _timeline([record for record in calls if mine is None or str(record.get("request_id")) in mine]),
        "prompt": prompt,
        "context": _composition(main, prompt) if main and prompt else None,
        "conversation": _conversation(threads) if threads else None,
        "tools": tools if threads else None,
    }
    found = [finding for check in CHECKS for finding in [check(measured)] if finding]
    # Worst first, and within a level the one that cost most.
    found.sort(key=lambda item: (SEVERITIES.index(item["severity"]), -(item["impact_tokens"] or 0)))
    score = max(0, 100 - sum(PENALTIES[item["severity"]] for item in found)) if calls else None
    return {
        "session_id": session.session_id, **measured, "findings": found, "checks": len(CHECKS), "score": score,
        "grade": next(label for floor, label in GRADES if score >= floor) if score is not None else None,
    }


def _tier(record: dict[str, Any], prices: list[dict[str, Any]]) -> str:
    """The tier of the model a call went to (TIERS), by its list price for input."""
    cost = record.get("cost")
    if not isinstance(cost, dict):
        # No cost: a machine's own model server, or a paid model the table has no price for.
        return "unpriced" if record.get("unpriced") else "local"
    price = pricing.price_for(prices, record.get("upstream_model") or record.get("model"))
    if price is not None:
        rate = float(price["input"])
    else:
        # Priced by its provider's own table: the rate it was charged for new input, where it read any.
        fresh = int(record.get("input_tokens") or 0) - (int(record.get("cache_read_tokens") or 0) if record.get("cached_in_input") else 0)
        if fresh <= 0 or not cost.get("input_usd"):
            return "unpriced"
        rate = float(cost["input_usd"]) / fresh * pricing.MILLION
    return "frontier" if rate >= TIER_INPUT_USD[1] else "small" if rate < TIER_INPUT_USD[0] else "mid"


def _traffic(records: list[dict[str, Any]], calls: list[dict[str, Any]], prices: list[dict[str, Any]]) -> dict[str, Any]:
    contexts = [_context(record) for record in calls]
    cache_read = sum(int(record.get("cache_read_tokens") or 0) for record in calls)
    cache_write = sum(int(record.get("cache_creation_tokens") or 0) for record in calls)
    first_tokens = [float(record["ttft_ms"]) for record in calls if isinstance(record.get("ttft_ms"), (int, float))]
    windows = [int(record["context_size"]) for record in calls if isinstance(record.get("context_size"), (int, float)) and record["context_size"] > 0]
    models: dict[str, dict[str, Any]] = {}
    tiers: dict[str, dict[str, Any]] = {}
    for record, context in zip(calls, contexts):
        # A call that used no tokens (refused, failed before the model) has no cost to sort it by: it's in no tier.
        used = context + int(record.get("output_tokens") or 0) > 0
        tier = _tier(record, prices) if used else None
        entry = models.setdefault(str(record.get("model") or ""), {"model": record.get("model") or None, "tier": tier, "calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0})
        entry["tier"] = entry["tier"] or tier
        level = tiers.setdefault(tier, {"tier": tier, "calls": 0, "share": 0.0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0, "models": []}) if tier else None
        if level is not None and entry["model"] and entry["model"] not in level["models"]:
            level["models"].append(entry["model"])
        for item in (entry, level):
            if item is None:
                continue
            item["calls"] += 1
            item["input_tokens"] += context
            item["output_tokens"] += int(record.get("output_tokens") or 0)
            item["usd"] = round(item["usd"] + float((record.get("cost") or {}).get("usd") or 0), 6)
    sorted_calls = sum(level["calls"] for level in tiers.values())
    for level in tiers.values():
        level["share"] = round(level["calls"] / sorted_calls, 3)
    stamps = [stamp for stamp in (_time(record.get("timestamp")) for record in records) if stamp is not None]
    rewrites = rewritten = 0
    for before, record, context in zip(calls, calls[1:], contexts[1:]):
        gap = (_time(record.get("timestamp")) or 0) - (_time(before.get("timestamp")) or 0)
        written = int(record.get("cache_creation_tokens") or 0)
        if gap >= IDLE_SECONDS and context and written / context >= IDLE_REWRITE_SHARE:
            rewrites += 1
            rewritten += written
    cost = pricing.total(records)
    return {
        "requests": len(records),
        "model_calls": len(calls),
        # Everything each call read: what was cached counts too, since it fills the context all the same.
        "input_tokens": sum(contexts),
        "output_tokens": sum(int(record.get("output_tokens") or 0) for record in calls),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        # None where the provider reports no caching at all (most local model servers).
        "cached_share": round(cache_read / sum(contexts), 3) if (cache_read or cache_write) and sum(contexts) else None,
        "idle_rewrites": rewrites,
        "idle_rewrite_tokens": rewritten,
        "peak_context_tokens": max(contexts, default=0),
        "context_window": max(windows, default=None),
        "failed_calls": sum(1 for record in calls if int(record.get("status") or 0) >= 400 and int(record.get("status") or 0) != 429),
        "rate_limited_calls": sum(1 for record in calls if int(record.get("status") or 0) == 429),
        "incomplete_calls": sum(1 for record in calls if record.get("error_type") == "incomplete_stream"),
        "median_first_token_ms": round(statistics.median(first_tokens)) if first_tokens else None,
        "duration_seconds": round(max(stamps) - min(stamps)) if stamps else 0,
        "usd": cost["usd"],
        "subscription_usd": cost["subscription_usd"],
        "models": sorted(models.values(), key=lambda item: -item["calls"]),
        # The model calls by tier, most expensive tier first: how much of the session ran on which kind of model.
        "tiers": sorted(tiers.values(), key=lambda item: TIERS.index(item["tier"])),
    }


def _context(record: dict[str, Any]) -> int:
    """The tokens a call's model read. Anthropic counts cached tokens beside the input, OpenAI inside it."""
    tokens = int(record.get("input_tokens") or 0)
    if record.get("cached_in_input"):
        return tokens
    return tokens + int(record.get("cache_read_tokens") or 0) + int(record.get("cache_creation_tokens") or 0)


def _time(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _timeline(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """The context's size call by call (calls that read something), thinned to TIMELINE_POINTS, and how fast it grew
    up to its largest: the tokens each call added on average."""
    sizes = [(str(record.get("request_id") or ""), _context(record), int(record.get("output_tokens") or 0)) for record in calls]
    sizes = [item for item in sizes if item[1] > 0]
    if not sizes:
        return {"calls": 0, "points": [], "growth_per_call": 0}
    peak = max(range(len(sizes)), key=lambda index: sizes[index][1])
    step = max(1, -(-len(sizes) // TIMELINE_POINTS))
    kept = sorted({*range(0, len(sizes), step), peak, len(sizes) - 1})
    return {
        "calls": len(sizes),
        "points": [{"call": index + 1, "request_id": sizes[index][0], "tokens": sizes[index][1], "output_tokens": sizes[index][2]} for index in kept],
        "growth_per_call": round((sizes[peak][1] - sizes[0][1]) / peak) if peak else 0,
    }


def _prompt(request: dict[str, Any], tools: dict[str, Any]) -> dict[str, Any]:
    """What the main chat's newest call sends before the conversation: its system prompt and tool definitions."""
    parts = [_plain(request.get("system")), _plain(request.get("instructions"))]
    sent = request.get("messages") or request.get("input")
    for message in sent if isinstance(sent, list) else []:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
            break
        parts.append(_plain(message.get("content")))
    system = "\n".join(part for part in parts if part)
    defined = [item for item in request.get("tools") or [] if isinstance(item, dict)]
    sizes = [
        (str(item.get("name") or (item.get("function") or {}).get("name") or item.get("type") or ""), len(json.dumps(item, ensure_ascii=False)) // CHARS_PER_TOKEN)
        for item in defined
    ]
    used = {item["name"] for item in tools["by_name"]}
    servers: dict[str, dict[str, Any]] = {}
    for name, tokens in sizes:
        match = MCP_TOOL.match(name)
        if match:
            entry = servers.setdefault(match[1], {"name": match[1], "tools": 0, "calls": 0, "tokens": 0})
            entry["tools"] += 1
            entry["tokens"] += tokens
            entry["calls"] += next((item["calls"] for item in tools["by_name"] if item["name"] == name), 0)
    return {
        "system_chars": len(system),
        "system_tokens": len(system) // CHARS_PER_TOKEN,
        "system_start": system[:1200],
        "sections": _sections(system),
        "tools_defined": len(defined),
        "tool_definition_tokens": sum(tokens for _, tokens in sizes),
        "largest_tools": [{"name": name, "tokens": tokens} for name, tokens in sorted(sizes, key=lambda item: -item[1])[:8]],
        "tools_unused": sorted(name for name, _ in sizes if name and name not in used),
        "unused_tool_tokens": sum(tokens for name, tokens in sizes if name not in used),
        "mcp_servers": sorted(servers.values(), key=lambda item: -item["tools"]),
    }


def _sections(system: str) -> list[dict[str, Any]]:
    """The system prompt's sections, largest first: each Markdown heading or XML-style tag starts one."""
    found: list[list[Any]] = [["(before the first heading)", 0]]
    for line in system.split("\n"):
        match = SECTION.match(line)
        if match:
            found.append([(match[1] or f"<{match[2]}>")[:80], 0])
        found[-1][1] += len(line) + 1
    ranked = sorted((item for item in found if item[1] >= CHARS_PER_TOKEN), key=lambda item: -item[1])
    return [{"title": title, "tokens": size // CHARS_PER_TOKEN} for title, size in ranked[:12]]


def _composition(main: dict[str, Any], prompt: dict[str, Any]) -> list[dict[str, Any]]:
    """What the main chat's context holds, by kind, largest first: the system prompt and tool definitions of its
    newest call, and its conversation's text (estimates; an image counts as nothing here)."""
    sizes = Counter({"System prompt": prompt["system_chars"], "Tool definitions": prompt["tool_definition_tokens"] * CHARS_PER_TOKEN})
    for turn in main["turns"] or []:
        if turn["role"] == "system":
            continue
        for block in turn["blocks"]:
            text = len(block.get("text") or "")
            if block["type"] == "tool_result":
                sizes["Tool results"] += int(block.get("truncated") or text)
            elif block["type"] == "tool_use":
                sizes["Tool calls"] += len(json.dumps(block.get("input"), ensure_ascii=False, default=str))
            elif block["type"] == "thinking":
                sizes["Thinking"] += text
            elif block["type"] == "text" and turn["role"] == "assistant":
                sizes["The agent's replies"] += text
            elif block["type"] == "text":
                sizes["Messages typed" if block.get("author") == "person" else "Notes the agent adds"] += text
    total = sum(sizes.values()) or 1
    return [{"part": part, "tokens": size // CHARS_PER_TOKEN, "share": round(size / total, 3)} for part, size in sizes.most_common() if size]


def _conversation(threads: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = Counter(item["kind"] for item in threads)
    prompts: list[str] = []
    steps: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    compactions = images = 0
    for item in threads:
        typed_here = item["kind"] in ("main", "conversation")
        calls = 0
        for turn in item["turns"] or []:
            if turn.get("event") == "compacted":
                compactions += 1
            if turn.get("inherited"):
                continue
            images += sum(1 for block in turn["blocks"] if block["type"] == "image")
            calls += sum(1 for block in turn["blocks"] if block["type"] == "tool_use")
            if turn["role"] == "user" and typed_here:
                typed = [block for block in turn["blocks"] if block["type"] == "text" and block.get("author") == "person"]
                prompts += [block["text"] for block in typed if not block.get("command")]
                if item["main"] and typed:
                    said = " ".join(" ".join(block["text"] for block in typed).split())[:200]
                    steps.append({"typed": said, "tool_calls": 0, "by_kind": Counter(), "result_tokens": 0})
            if item["main"] and steps:
                for block in turn["blocks"]:
                    if block["type"] == "tool_use":
                        steps[-1]["tool_calls"] += 1
                        steps[-1]["by_kind"][_call(block)["category"]] += 1
                    elif block["type"] == "tool_result":
                        steps[-1]["result_tokens"] += int(block.get("truncated") or len(block.get("text") or "")) // CHARS_PER_TOKEN
        if not item["main"] and item["kind"] != "background":
            others.append({"kind": item["kind"], "title": item["title"][:120], "model": item["model"], "calls": len(item["requests"]), "tool_calls": calls})
    return {
        "conversations": kinds["main"] + kinds["conversation"],
        "subagents": kinds["subagent"],
        "background_calls": kinds["background"],
        "compactions": compactions,
        "images": images,
        "prompts": len(prompts),
        "first_prompt_chars": len(prompts[0]) if prompts else None,
        "prompt_excerpts": [" ".join(text.split())[:PROMPT_EXCERPT_CHARS] for text in prompts[:PROMPT_EXCERPTS]],
        "threads": sorted(others, key=lambda item: -item["calls"])[:15],
        "step_count": len(steps),
        "steps": [{**step, "by_kind": dict(step["by_kind"].most_common())} for step in steps[:STEPS]],
    }


def _tools(threads: list[dict[str, Any]]) -> dict[str, Any]:
    """Every tool call the conversations hold, with what came back: by tool, by kind, by file and command, and the ones worth a look."""
    calls: list[dict[str, Any]] = []
    for item in threads:
        by_id: dict[str, dict[str, Any]] = {}
        for turn in item["turns"] or []:
            if turn.get("inherited"):
                # A sub-agent's copy of its parent's conversation: counted there.
                continue
            for block in turn["blocks"]:
                if block["type"] == "tool_use":
                    call = _call(block)
                    calls.append(call)
                    by_id[block.get("id") or ""] = call
                elif block["type"] == "tool_result" and block.get("tool_use_id") in by_id:
                    call = by_id[block["tool_use_id"]]
                    call["result_chars"] = int(block.get("truncated") or len(block.get("text") or ""))
                    call["error"] = bool(block.get("is_error"))
                    if call["error"]:
                        call["error_text"] = " ".join((block.get("text") or "").split())[:240]
    names: dict[str, dict[str, Any]] = {}
    commands: dict[str, dict[str, Any]] = {}
    for call in calls:
        entry = names.setdefault(call["name"], {"name": call["name"], "category": call["category"], "calls": 0, "errors": 0, "result_chars": 0})
        entry["calls"] += 1
        entry["errors"] += call["error"]
        entry["result_chars"] += call["result_chars"]
        if call["command"]:
            # A command by what it runs: its first two words ("pytest tests/", "npm run").
            run = commands.setdefault(" ".join(call["command"].split()[:2])[:60], {"command": "", "calls": 0, "errors": 0, "result_chars": 0, "example": call["command"][:160]})
            run["calls"] += 1
            run["errors"] += call["error"]
            run["result_chars"] += call["result_chars"]
    reads = Counter(call["target"] for call in calls if call["category"] == "read" and call["target"])
    edits = Counter(call["target"] for call in calls if call["category"] == "edit" and call["target"])
    repeats = Counter(call["key"] for call in calls)
    sample = {call["key"]: call for call in calls}
    large = sorted((call for call in calls if call["result_chars"] >= LARGE_RESULT_CHARS), key=lambda call: -call["result_chars"])
    failed: dict[tuple[str, str], dict[str, Any]] = {}
    for call in calls:
        if call["error"]:
            entry = failed.setdefault((call["name"], call.get("error_text", "")[:80]), {"name": call["name"], "target": call["target"], "error": call.get("error_text", ""), "times": 0})
            entry["times"] += 1
    return {
        "calls": len(calls),
        "errors": sum(call["error"] for call in calls),
        "result_tokens": sum(call["result_chars"] for call in calls) // CHARS_PER_TOKEN,
        "by_category": dict(Counter(call["category"] for call in calls).most_common()),
        "by_name": sorted(names.values(), key=lambda item: -item["calls"])[:20],
        "reread_files": [{"file": name, "reads": count} for name, count in reads.most_common(8) if count >= REREADS[0]],
        "edited_files": [{"file": name, "edits": count} for name, count in edits.most_common(10)],
        "repeated_calls": [
            {"name": sample[key]["name"], "target": sample[key]["target"], "times": count}
            for key, count in repeats.most_common(16) if count >= REPEATED_CALLS and sample[key]["category"] != "read"
        ][:8],
        "large_results": [{"name": call["name"], "target": call["target"], "tokens": call["result_chars"] // CHARS_PER_TOKEN} for call in large[:8]],
        "large_result_count": len(large),
        "large_result_tokens": sum(call["result_chars"] for call in large) // CHARS_PER_TOKEN,
        "commands": [
            {"command": name, "calls": run["calls"], "errors": run["errors"], "result_tokens": run["result_chars"] // CHARS_PER_TOKEN, "example": run["example"]}
            for name, run in sorted(commands.items(), key=lambda item: -item[1]["result_chars"])[:12]
        ],
        "searches": list(dict.fromkeys(call["target"] for call in calls if call["category"] == "search" and call["target"]))[:15],
        "error_samples": sorted(failed.values(), key=lambda item: -item["times"])[:6],
    }


def _call(block: dict[str, Any]) -> dict[str, Any]:
    name = str(block.get("name") or "tool")
    given = block.get("input") if isinstance(block.get("input"), dict) else {}
    command = next((given[key] for key in COMMAND_KEYS if given.get(key)), None)
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    category = next((kind for kind, pattern in CATEGORIES if pattern.search(name)), "other")
    target = next((str(given[key]) for key in FILE_KEYS if isinstance(given.get(key), str) and given[key]), "")
    shell = category == "shell" and isinstance(command, str)
    if shell:
        # What a shell command is for: agents without search tools of their own grep and cat through the shell.
        words = command.split()
        first = words[0].rsplit("/", 1)[-1].lower() if words else ""
        category = "search" if first in SEARCH_COMMANDS else "read" if first in READ_COMMANDS else "shell"
        target = (words[-1] if category == "read" and len(words) > 1 else " ".join(command.split()))[:160]
    if not target:
        target = next((str(given[key])[:160] for key in ("pattern", "query", "url", "description") if isinstance(given.get(key), str)), "")
    return {
        "name": name, "category": category, "target": target, "result_chars": 0, "error": False, "command": " ".join(command.split()) if shell else "",
        "key": name + " " + json.dumps(block.get("input"), sort_keys=True, ensure_ascii=False, default=str),
    }


def _finding(check: str, area: str, severity: str, title: str, detail: str, suggestion: str, impact: int | None = None) -> dict[str, Any]:
    """impact: about how many tokens it cost over the session, where that can be said; None where it can't."""
    return {"id": check, "area": area, "severity": severity, "title": title, "detail": detail, "suggestion": suggestion,
            "impact_tokens": int(impact) if impact and impact > 0 else None}


def _level(value: float, steps: tuple[float, float], low: str = "medium") -> str | None:
    return "high" if value >= steps[1] else low if value >= steps[0] else None


def _n(value: float) -> str:
    return f"{value:,.0f}"


def _count(value: int, one: str, many: str | None = None) -> str:
    return f"{_n(value)} {one if value == 1 else many or one + 's'}"


def _system_prompt(m: dict[str, Any]) -> dict[str, str] | None:
    prompt = m["prompt"]
    level = _level(prompt["system_tokens"], SYSTEM_PROMPT_TOKENS) if prompt else None
    if not level:
        return None
    return _finding(
        "system-prompt", "context", level, "Large system prompt",
        f"Every model call starts with about {_n(prompt['system_tokens'])} tokens of instructions, before any of the conversation.",
        "Trim the agent's instruction files (CLAUDE.md, AGENTS.md, rules, memory): keep what applies to every task, and move the rest "
        "into docs the agent reads only when it needs them.",
        (prompt["system_tokens"] - SYSTEM_PROMPT_TOKENS[0]) * m["traffic"]["model_calls"],
    )


def _tool_definitions(m: dict[str, Any]) -> dict[str, str] | None:
    prompt = m["prompt"]
    level = _level(prompt["tool_definition_tokens"], TOOL_DEFINITION_TOKENS) if prompt else None
    if not level:
        return None
    servers = ", ".join(f"{item['name']} ({item['tools']})" for item in prompt["mcp_servers"][:5])
    return _finding(
        "tool-definitions", "context", level, "Many tool definitions",
        f"{_n(prompt['tools_defined'])} tools are described to the model on every call, about {_n(prompt['tool_definition_tokens'])} tokens."
        + (f" MCP servers: {servers}." if servers else ""),
        "Switch off the MCP servers and tools this kind of work doesn't use. Each one costs tokens on every call and gives the model more to choose wrongly from.",
    )


def _unused_tools(m: dict[str, Any]) -> dict[str, str] | None:
    prompt, tools = m["prompt"], m["tools"]
    if not prompt or not tools or prompt["tools_defined"] < UNUSED_TOOLS[0] or not tools["calls"]:
        return None
    used = prompt["tools_defined"] - len(prompt["tools_unused"])
    if used / prompt["tools_defined"] >= UNUSED_TOOLS[1]:
        return None
    idle = [item["name"] for item in prompt["mcp_servers"] if not item["calls"]]
    return _finding(
        "unused-tools", "context", "low", "Most tools were never used",
        f"The session used {used} of the {prompt['tools_defined']} tools it was given."
        + (f" No tool of these MCP servers was called: {', '.join(idle[:6])}." if idle else ""),
        "Give the agent only the tools the task needs: remove idle MCP servers from this project, or start it with a smaller set.",
        prompt["unused_tool_tokens"] * m["traffic"]["model_calls"],
    )


def _peak_context(m: dict[str, Any]) -> dict[str, str] | None:
    traffic = m["traffic"]
    peak, window = traffic["peak_context_tokens"], traffic["context_window"]
    level = _level(peak / window, PEAK_CONTEXT_SHARE) if window else _level(peak, PEAK_CONTEXT_TOKENS)
    if not level:
        return None
    size = f"{_n(peak)} tokens" + (f", {peak / window:.0%} of the model's {_n(window)}" if window else "")
    return _finding(
        "peak-context", "context", level, "The context grew very large",
        f"The largest call read {size}. Models answer slower, cost more and lose track of earlier instructions as the context fills.",
        "Start a new session for each task instead of carrying one on, clear or compact between tasks, and hand large side jobs to a sub-agent so their output stays out of the main chat.",
    )


def _compactions(m: dict[str, Any]) -> dict[str, str] | None:
    chat = m["conversation"]
    level = _level(chat["compactions"], COMPACTIONS) if chat else None
    if not level:
        return None
    return _finding(
        "compactions", "flow", level, "The conversation was compacted",
        f"The agent ran out of room and replaced the conversation with a summary {_count(chat['compactions'], 'time')}. Detail from before each one is gone, and the agent often reads the same files again after it.",
        "Split the work into smaller sessions, and have the agent write its plan and progress to a file so a new session can pick it up.",
    )


def _long_session(m: dict[str, Any]) -> dict[str, str] | None:
    level = _level(m["traffic"]["model_calls"], MODEL_CALLS)
    if not level:
        return None
    return _finding(
        "long-session", "flow", level, "A very long session",
        f"{_n(m['traffic']['model_calls'])} model calls in one session. Each call sends the conversation so far again, so the cost of a session grows faster than its length.",
        "Give the agent one task a session. When it finishes or drifts, start again with a short summary of where things stand.",
    )


def _searching(m: dict[str, Any]) -> dict[str, str] | None:
    tools = m["tools"]
    if not tools or not tools["calls"]:
        return None
    searches = tools["by_category"].get("search", 0)
    level = _level(searches, SEARCHES)
    if not level or searches / tools["calls"] < SEARCH_SHARE:
        return None
    return _finding(
        "searching", "tools", level, "A lot of searching for files",
        f"{_n(searches)} of {_n(tools['calls'])} tool calls ({searches / tools['calls']:.0%}) were searches and listings. The agent spent much of the session finding where things are.",
        "Name the files or folders in the prompt, and keep a short map of the project (what lives where, how to build and test) in its instruction file so it doesn't rediscover it every session.",
    )


def _rereads(m: dict[str, Any]) -> dict[str, str] | None:
    files = (m["tools"] or {}).get("reread_files") or []
    if not files:
        return None
    worst = ", ".join(f"{item['file']} ({item['reads']} times)" for item in files[:4])
    return _finding(
        "rereads", "tools", "medium" if files[0]["reads"] >= REREADS[1] else "low", "The same files were read again and again",
        f"Read repeatedly: {worst}. Every read puts the file into the context once more.",
        "Ask for the change in one go rather than in small steps, and split very large files, which agents read in pieces. A read after every edit is often the agent checking its work: tests do that cheaper.",
    )


def _large_results(m: dict[str, Any]) -> dict[str, str] | None:
    tools = m["tools"]
    level = _level(tools["large_result_count"], LARGE_RESULTS, "low") if tools else None
    if not level:
        return None
    seen = Counter(f"{item['name']} {item['target']}".strip() + f" (~{_n(item['tokens'])} tokens)" for item in tools["large_results"])
    worst = ", ".join(text + (f", {times} times" if times > 1 else "") for text, times in list(seen.items())[:3])
    return _finding(
        "large-results", "tools", "medium" if level == "high" else "low", "Very large tool results",
        f"{_count(tools['large_result_count'], 'tool call')} returned more than {_n(LARGE_RESULT_CHARS // CHARS_PER_TOKEN)} tokens each: {worst}. A result stays in the context for the rest of the session.",
        "Have the agent read ranges of a file, filter command output (head, tail, grep, quiet flags) and keep build and test logs short. Generated and vendored files are best left out of its reach.",
        tools["large_result_tokens"],
    )


def _tool_errors(m: dict[str, Any]) -> dict[str, str] | None:
    tools = m["tools"]
    if not tools or tools["errors"] < TOOL_ERRORS[0] or tools["errors"] / tools["calls"] < TOOL_ERRORS[1]:
        return None
    failing = ", ".join(f"{item['name']} ({item['errors']})" for item in sorted(tools["by_name"], key=lambda item: -item["errors"])[:4] if item["errors"])
    return _finding(
        "tool-errors", "tools", "high" if tools["errors"] / tools["calls"] >= TOOL_ERRORS[2] else "medium", "Many tool calls failed",
        f"{_n(tools['errors'])} of {_n(tools['calls'])} tool calls ({tools['errors'] / tools['calls']:.0%}) came back as errors: {failing}. Each one is a wasted call, and the retry after it another.",
        "Look at what failed: a wrong path or command the instructions could state, a missing tool or permission on the machine, or edits that don't apply because the file changed.",
    )


def _repeated_calls(m: dict[str, Any]) -> dict[str, str] | None:
    repeats = (m["tools"] or {}).get("repeated_calls") or []
    # Reading a file again is the rereads check's; this one is for the same call with the same input.
    if not repeats:
        return None
    worst = ", ".join(f"{item['name']} {item['target']}".strip() + f" ({item['times']} times)" for item in repeats[:3])
    return _finding(
        "repeated-calls", "tools", "medium" if repeats[0]["times"] >= 2 * REPEATED_CALLS else "low", "The same tool call was repeated",
        f"Identical calls: {worst}. An agent repeating itself is usually stuck, or has lost the earlier result from its context.",
        "Stop the agent when it loops and tell it what it's missing. If it repeats a build or test command, give it a faster or narrower one.",
    )


def _subagents(m: dict[str, Any]) -> dict[str, str] | None:
    chat = m["conversation"]
    level = _level(chat["subagents"], SUBAGENTS, "low") if chat else None
    if not level:
        return None
    return _finding(
        "subagents", "flow", "medium" if level == "high" else "low", "Many sub-agents",
        f"The agent started {_n(chat['subagents'])} sub-agents. Each starts from nothing, reads the project for itself and reports back a summary.",
        "Sub-agents pay off for wide, independent searches. For small lookups, tell the agent to do the work itself, or name the files so there's nothing to delegate.",
    )


def _short_prompt(m: dict[str, Any]) -> dict[str, str] | None:
    chat = m["conversation"]
    if not chat or chat["first_prompt_chars"] is None or chat["first_prompt_chars"] >= SHORT_PROMPT_CHARS or m["traffic"]["model_calls"] < SHORT_PROMPT_CALLS:
        return None
    return _finding(
        "short-prompt", "flow", "low", "A short first prompt for a long task",
        f"The session started from {chat['first_prompt_chars']} characters and ran for {_n(m['traffic']['model_calls'])} model calls. The agent had to work out the rest by exploring.",
        "Say what done looks like, which files matter, what not to touch and how to check the result. A few more sentences up front save many calls.",
    )


def _steering(m: dict[str, Any]) -> dict[str, str] | None:
    chat = m["conversation"]
    if not chat or chat["prompts"] < STEERING_PROMPTS:
        return None
    return _finding(
        "steering", "flow", "low", "A lot of steering",
        f"{_n(chat['prompts'])} messages were typed during the session. Many corrections often mean the task was too big or left too open.",
        "Ask for a plan first and correct the plan, not the code. Put corrections you make more than once into the project's instruction file.",
    )


def _failed_calls(m: dict[str, Any]) -> dict[str, str] | None:
    traffic = m["traffic"]
    if traffic["failed_calls"] < FAILED_CALLS[0] or traffic["failed_calls"] / traffic["model_calls"] < FAILED_CALLS[1]:
        return None
    return _finding(
        "failed-calls", "reliability", "medium", "Model calls failed",
        f"{_n(traffic['failed_calls'])} of {_n(traffic['model_calls'])} model calls ended in an error. Agents retry them, so the session waits and may pay twice.",
        "Open the failed requests on the Requests tab to see why: a provider that was down, a model that isn't there, or a request the provider refused as too large.",
    )


def _rate_limited(m: dict[str, Any]) -> dict[str, str] | None:
    limited = m["traffic"]["rate_limited_calls"]
    if not limited:
        return None
    return _finding(
        "rate-limited", "reliability", "medium", "Calls were rate limited",
        f"{_count(limited, 'model call was', 'model calls were')} refused with 429: the provider's limit, a subscription's, or a usage limit your organization set.",
        "Run fewer agents and sub-agents at once on this provider, use a smaller model for routine work, or move some of it to another provider.",
    )


def _incomplete(m: dict[str, Any]) -> dict[str, str] | None:
    incomplete = m["traffic"]["incomplete_calls"]
    if incomplete < INCOMPLETE_CALLS:
        return None
    return _finding(
        "incomplete", "reliability", "low", "Replies were cut off",
        f"{_n(incomplete)} replies stopped before the model finished: interrupted by hand, or a connection that dropped.",
        "An interrupted reply is paid for and thrown away. If you stop the agent often, a plan agreed first saves those calls.",
    )


def _model_tiers(m: dict[str, Any]) -> dict[str, str] | None:
    traffic = m["traffic"]
    tiers = {item["tier"]: item for item in traffic.get("tiers") or []}
    frontier = tiers.get("frontier")
    # Of the calls that used tokens: one refused before the model is in no tier.
    calls = sum(item["calls"] for item in tiers.values())
    level = _level(frontier["share"], TIER_SHARE) if frontier and calls >= TIER_CALLS else None
    if not level:
        return None
    rest = ", ".join(f"{TIER_LABELS[name]} {_n(item['calls'])}" for name, item in tiers.items() if name != "frontier")
    total = traffic["usd"] + traffic["subscription_usd"]
    return _finding(
        "model-tiers", "cost", level, "Almost every call went to a frontier model" if level == "high" else "Most calls went to a frontier model",
        f"{_n(frontier['calls'])} of {_n(calls)} model calls ({frontier['share']:.0%}) used a frontier model, the most expensive tier "
        f"({', '.join(frontier['models'][:4])})"
        + (f": about ${frontier['usd']:,.2f} of the session's ${total:,.2f} at API prices." if total else ".")
        + (f" Other tiers: {rest}." if rest else " No call used a cheaper model.")
        + " Routine steps (searches, small edits, test runs, summaries) cost the same there as the hard ones.",
        "Keep the frontier model for planning, hard debugging and the final check. Set the agent's everyday model a tier down and switch up only when a task "
        "needs it, and give sub-agents and background work a mid-range or small model. On a subscription this is what uses up the plan's limits.",
    )


def _caching(m: dict[str, Any]) -> dict[str, str] | None:
    traffic = m["traffic"]
    share = traffic["cached_share"]
    if share is None or share >= CACHE_SHARE or traffic["model_calls"] < CACHE_CALLS or traffic["input_tokens"] < CACHE_INPUT_TOKENS:
        return None
    return _finding(
        "caching", "cost", "medium", "Little of the prompt came from the cache",
        f"Only {share:.0%} of the {_n(traffic['input_tokens'])} tokens read were cached. Agents resend the same conversation on every call, and cached tokens cost a fraction of new ones.",
        "The cache is lost when the start of the prompt changes or the session sits idle for minutes. Avoid switching models or tool sets mid-session, and finish a task in one sitting.",
    )


def _idle_cache(m: dict[str, Any]) -> dict[str, str] | None:
    traffic = m["traffic"]
    level = _level(traffic["idle_rewrites"], IDLE_REWRITES, "low")
    if not level:
        return None
    return _finding(
        "idle-cache", "cost", "medium" if level == "high" else "low", "The cache ran out while the session sat idle",
        f"{_count(traffic['idle_rewrites'], 'time')} a call came more than {IDLE_SECONDS // 60} minutes after the one before and wrote its prompt to the cache again: "
        f"{_n(traffic['idle_rewrite_tokens'])} tokens paid for at the cache-write price instead of the far lower cache-read one.",
        "Come back to a waiting agent within a few minutes, or let it finish and start a new session later. A long pause in a large session costs its whole context again.",
        traffic["idle_rewrite_tokens"],
    )


def _context_growth(m: dict[str, Any]) -> dict[str, str] | None:
    timeline = m["timeline"]
    level = _level(timeline["growth_per_call"], CONTEXT_GROWTH) if timeline["calls"] >= CONTEXT_GROWTH_CALLS else None
    if not level:
        return None
    return _finding(
        "context-growth", "context", level, "The context grew fast",
        f"Over the main chat's {_n(timeline['calls'])} calls the context gained about {_n(timeline['growth_per_call'])} tokens a call on the way to its largest. "
        "What a call adds is read again by every call after it.",
        "Find the steps that added most (the largest tool results, files read whole, logs) and keep them out: read ranges, quieter commands, or a sub-agent that reports back a summary.",
    )


def _images(m: dict[str, Any]) -> dict[str, str] | None:
    chat = m["conversation"]
    level = _level(chat["images"], IMAGES, "low") if chat else None
    if not level:
        return None
    return _finding(
        "images", "context", "medium" if level == "high" else "low", "Many images in the conversation",
        f"{_n(chat['images'])} images (screenshots, pasted pictures) are in the conversation. Each costs hundreds to thousands of tokens and is sent again on every call after it.",
        "Paste text instead of a screenshot of text, crop to what matters, and start a new session after a round of visual checks.",
    )


def _slow(m: dict[str, Any]) -> dict[str, str] | None:
    wait = m["traffic"]["median_first_token_ms"]
    if wait is None or wait < SLOW_FIRST_TOKEN_MS or m["traffic"]["model_calls"] < 5:
        return None
    return _finding(
        "slow", "reliability", "low", "Slow to start answering",
        f"Half the calls waited more than {wait / 1000:.0f} seconds for the first token. On a local model that's the prompt being read; on a hosted one, load.",
        "A smaller context is the cure on a local model: fewer tools, a shorter system prompt, shorter sessions. Check the model's context size and that it stays loaded.",
    )


def _untraced(m: dict[str, Any]) -> dict[str, str] | None:
    calls = m["traffic"]["model_calls"]
    if not calls or m["traced_calls"] >= calls:
        return None
    none = m["traced_calls"] == 0
    return _finding(
        "untraced", "context", "info", "Payloads weren't captured" if none else "Only some payloads were captured",
        ("None" if none else f"Only {_n(m['traced_calls'])}") + f" of the {_n(calls)} model calls {'has' if m['traced_calls'] == 1 else 'have'} payloads, so the prompt, the conversation and the tools "
        + ("couldn't be checked: only the numbers of each request." if none else "were checked for those calls only."),
        "Turn on Capture full request and response payloads under Details (or start the agent with --trace) to review what was sent.",
    )


# The list, in the order the areas read: what fills the context, how the tools were used, how the session
# went, what failed, what it cost. Each takes the measurements and returns a finding or None.
CHECKS: tuple[Callable[[dict[str, Any]], dict[str, str] | None], ...] = (
    _system_prompt, _tool_definitions, _unused_tools, _peak_context, _context_growth, _images, _compactions, _long_session,
    _searching, _rereads, _large_results, _tool_errors, _repeated_calls,
    _subagents, _short_prompt, _steering,
    _failed_calls, _rate_limited, _incomplete, _model_tiers, _caching, _idle_cache, _slow, _untraced,
)

def _session_lines(session: Session, found: dict[str, Any]) -> list[str]:
    traffic = found["traffic"]
    return [
        f"Agent: {session.client}. Model: {session.model or 'chosen by the agent'}. " + ("Ended." if session.ended_at else "Still open."),
        f"Lasted {traffic['duration_seconds'] // 60} minutes. {traffic['requests']} requests, {traffic['model_calls']} of them model calls.",
        f"Score from the automatic checks: {found['score']} of 100 ({found['grade']})." if found.get("score") is not None else "",
    ]


def _token_lines(session: Session, found: dict[str, Any]) -> list[str]:
    traffic = found["traffic"]
    return [
        f"Tokens read by the model: {traffic['input_tokens']:,} (cached: {traffic['cache_read_tokens']:,}; written to the cache: {traffic['cache_write_tokens']:,}). Tokens written: {traffic['output_tokens']:,}.",
        "Share of tokens read that came from the cache: " + (f"{traffic['cached_share']:.0%}." if traffic["cached_share"] is not None else "the provider reports no caching."),
        f"Largest single call: {traffic['peak_context_tokens']:,} tokens" + (f" of a {traffic['context_window']:,} token context." if traffic["context_window"] else "."),
        f"Calls after an idle gap that wrote the prompt to the cache again: {traffic['idle_rewrites']} ({traffic['idle_rewrite_tokens']:,} tokens).",
    ]


def _cost_lines(session: Session, found: dict[str, Any]) -> list[str]:
    traffic = found["traffic"]
    tiers = traffic.get("tiers") or []
    return [
        f"Estimated cost: ${traffic['usd']:.2f} paid by the token, ${traffic['subscription_usd']:.2f} at API prices on a subscription.",
        "By model (calls, tokens read, tokens written, estimated dollars): "
        + "; ".join(f"{item['model'] or 'unknown'} ({item['calls']}, {item['input_tokens']:,}, {item['output_tokens']:,}, ${item['usd']:.2f})" for item in traffic["models"][:8]),
        "By model tier, from list prices (calls, share of calls, estimated dollars): "
        + "; ".join(f"{TIER_LABELS[item['tier']]} ({item['calls']}, {item['share']:.0%}, ${item['usd']:.2f})" for item in tiers) if tiers else "",
    ]


def _reliability_lines(session: Session, found: dict[str, Any]) -> list[str]:
    traffic = found["traffic"]
    wait = traffic["median_first_token_ms"]
    return [
        f"Failed model calls: {traffic['failed_calls']}. Rate limited: {traffic['rate_limited_calls']}. Replies cut off: {traffic['incomplete_calls']}.",
        f"Median wait for the first token: {wait} ms." if wait is not None else "Time to first token wasn't measured.",
    ]


def _timeline_lines(session: Session, found: dict[str, Any]) -> list[str]:
    timeline = found["timeline"]
    if not timeline["points"]:
        return ["No call's size was recorded."]
    # At most 30 of the points: enough to see where it jumped.
    points = timeline["points"][:: max(1, -(-len(timeline["points"]) // 30))]
    return [
        f"The context's size over the main chat's {timeline['calls']} calls (call number: tokens read): " + ", ".join(f"{point['call']}: {point['tokens']:,}" for point in points),
        f"It grew by about {timeline['growth_per_call']:,} tokens a call up to its largest.",
    ]


def _composition_lines(session: Session, found: dict[str, Any]) -> list[str]:
    if not found["context"]:
        return ["What the context is made of wasn't captured."]
    return ["What the main chat's context holds (estimated tokens, share): " + "; ".join(f"{item['part']} {item['tokens']:,} ({item['share']:.0%})" for item in found["context"])]


def _prompt_lines(session: Session, found: dict[str, Any]) -> list[str]:
    prompt = found["prompt"]
    if not prompt:
        return ["The system prompt and tool definitions weren't captured."]
    return [
        f"System prompt: about {prompt['system_tokens']:,} tokens. Tools defined: {prompt['tools_defined']} (about {prompt['tool_definition_tokens']:,} tokens), {len(prompt['tools_unused'])} never called.",
        "MCP servers (tools, tokens, calls): " + ("; ".join(f"{item['name']} ({item['tools']}, {item['tokens']:,}, {item['calls']})" for item in prompt["mcp_servers"][:10]) or "none"),
        "The system prompt starts:", prompt["system_start"],
    ]


def _outline_lines(session: Session, found: dict[str, Any]) -> list[str]:
    prompt = found["prompt"]
    if not prompt:
        return ["The system prompt wasn't captured."]
    return [f"The system prompt's largest sections, of about {prompt['system_tokens']:,} tokens in all (title: tokens):",
            *(f"- {item['title']}: {item['tokens']:,}" for item in prompt["sections"])]


def _definition_lines(session: Session, found: dict[str, Any]) -> list[str]:
    prompt = found["prompt"]
    if not prompt:
        return ["Tool definitions weren't captured."]
    return [
        "Largest tool definitions (tokens): " + ("; ".join(f"{item['name']} {item['tokens']:,}" for item in prompt["largest_tools"]) or "none"),
        f"Tools never called: {len(prompt['tools_unused'])} of {prompt['tools_defined']}, about {prompt['unused_tool_tokens']:,} tokens on every call: " + (", ".join(prompt["tools_unused"][:30]) or "none"),
    ]


def _conversation_lines(session: Session, found: dict[str, Any]) -> list[str]:
    chat = found["conversation"]
    if not chat:
        return ["The conversation wasn't captured."]
    return [f"Conversations: {chat['conversations']}. Sub-agents: {chat['subagents']}. Background calls (titles, summaries): {chat['background_calls']}. "
            f"Compactions: {chat['compactions']}. Images: {chat['images']}."]


def _thread_lines(session: Session, found: dict[str, Any]) -> list[str]:
    chat = found["conversation"]
    if not chat:
        return []
    if not chat["threads"]:
        return ["No sub-agents or side conversations."]
    return ["Sub-agents and side conversations (kind, model, model calls, tool calls, what it was asked):",
            *(f"- {item['kind']}, {item['model'] or 'unknown'}, {item['calls']}, {item['tool_calls']}: {item['title']}" for item in chat["threads"])]


def _typed_lines(session: Session, found: dict[str, Any]) -> list[str]:
    chat = found["conversation"]
    if not chat:
        return ["What the person typed wasn't captured."]
    return [f"Messages the person typed: {chat['prompts']}. The first ones:", *(f"- {text}" for text in chat["prompt_excerpts"])]


def _step_lines(session: Session, found: dict[str, Any]) -> list[str]:
    chat = found["conversation"]
    if not chat or not chat["steps"]:
        return []
    lines = [f"The main chat step by step, {len(chat['steps'])} of {chat['step_count']} (what was typed, then what the agent did before the next message):"]
    for number, step in enumerate(chat["steps"], 1):
        kinds = ", ".join(f"{kind} {count}" for kind, count in step["by_kind"].items()) or "no tools"
        lines.append(f"{number}. \"{step['typed']}\": {step['tool_calls']} tool calls ({kinds}), about {step['result_tokens']:,} tokens of results")
    return lines


def _tool_lines(session: Session, found: dict[str, Any]) -> list[str]:
    tools = found["tools"]
    if not tools:
        return ["Tool calls weren't captured."]
    return [
        f"Tool calls: {tools['calls']}, {tools['errors']} failed, results about {tools['result_tokens']:,} tokens in all.",
        "By kind: " + (", ".join(f"{kind} {count}" for kind, count in tools["by_category"].items()) or "none"),
        "By tool (calls, errors, result tokens): " + ("; ".join(f"{item['name']} ({item['calls']}, {item['errors']}, {item['result_chars'] // CHARS_PER_TOKEN:,})" for item in tools["by_name"][:15]) or "none"),
    ]


def _file_lines(session: Session, found: dict[str, Any]) -> list[str]:
    tools = found["tools"]
    if not tools:
        return []
    return [
        "Files read three times or more: " + ("; ".join(f"{item['file']} ({item['reads']})" for item in tools["reread_files"]) or "none"),
        "Identical calls repeated: " + ("; ".join(f"{item['name']} {item['target']} ({item['times']})" for item in tools["repeated_calls"]) or "none"),
        "Largest results: " + ("; ".join(f"{item['name']} {item['target']} (~{item['tokens']:,} tokens)" for item in tools["large_results"]) or "none"),
    ]


def _command_lines(session: Session, found: dict[str, Any]) -> list[str]:
    tools = found["tools"]
    if not tools:
        return []
    if not tools["commands"]:
        return ["No shell commands were run."]
    return ["Shell commands by what they run (times, failed, result tokens, an example):",
            *(f"- {item['command']} ({item['calls']}, {item['errors']}, {item['result_tokens']:,}): {item['example']}" for item in tools["commands"])]


def _search_lines(session: Session, found: dict[str, Any]) -> list[str]:
    tools = found["tools"]
    if not tools:
        return []
    return [f"Searches and listings: {tools['by_category'].get('search', 0)}. What was searched for: " + ("; ".join(tools["searches"]) or "nothing")]


def _edit_lines(session: Session, found: dict[str, Any]) -> list[str]:
    tools = found["tools"]
    if not tools:
        return []
    return ["Files changed (edits): " + ("; ".join(f"{item['file']} ({item['edits']})" for item in tools["edited_files"]) or "none")]


def _error_lines(session: Session, found: dict[str, Any]) -> list[str]:
    tools = found["tools"]
    if not tools:
        return []
    if not tools["error_samples"]:
        return ["No tool call failed."]
    return ["Failed tool calls (tool, what it was given, times, how the error starts):",
            *(f"- {item['name']} {item['target']} ({item['times']}): {item['error']}" for item in tools["error_samples"])]


# The lines behind each name a prompt can list (review_prompts.DATA has the names and what they're called).
SECTIONS: dict[str, Callable[[Session, dict[str, Any]], list[str]]] = {
    "session": _session_lines, "tokens": _token_lines, "cost": _cost_lines, "reliability": _reliability_lines,
    "timeline": _timeline_lines, "context": _composition_lines, "prompt": _prompt_lines, "system-outline": _outline_lines,
    "tool-definitions": _definition_lines, "conversation": _conversation_lines, "threads": _thread_lines, "typed": _typed_lines,
    "steps": _step_lines, "tools": _tool_lines, "files": _file_lines, "commands": _command_lines, "searches": _search_lines,
    "edits": _edit_lines, "errors": _error_lines,
}
assert set(SECTIONS) == set(DATA)


def brief(session: Session, found: dict[str, Any], prompt: Prompt, question: str | None = None) -> list[dict[str, str]]:
    """The messages a model is asked to review the session with: the prompt's instructions, then the parts of
    the report it lists as text, what its checks found, and anything the person asked on top."""
    lines: list[str] = []
    for name in prompt.data:
        part = [line for line in SECTIONS[name](session, found) if line]
        if part:
            lines += [*part, ""]
    # The untraced note goes with every prompt: it says how much the rest can be trusted.
    findings = [item for item in found["findings"] if item["id"] in prompt.metrics or item["id"] == "untraced"]
    lines.append("Automatic checks found:")
    for item in findings:
        cost = f" About {item['impact_tokens']:,} tokens over the session." if item.get("impact_tokens") else ""
        lines.append(f"- [{item['severity']}] {item['title']}: {item['detail']}{cost}")
    if not findings:
        lines.append("- nothing in this area")
    text = "\n".join(lines)[:BRIEF_LIMIT]
    asked = " ".join(str(question or "").split())[:QUESTION_LIMIT]
    if asked:
        text += f"\n\nThe engineer also asks: {asked}"
    return [{"role": "system", "content": prompt.instructions()}, {"role": "user", "content": text}]


def reviewers(settings: Settings, usable: Callable[[Backend], bool]) -> list[str]:
    """The backends a review can be sent to from the console: the caller's own that take OpenAI Chat with
    the server's sign-in. One that needs the agent's own credentials is out: the console has none."""
    names = []
    for name, backend in {**settings.backends, **settings.remote_backends}.items():
        if not usable(backend) or not serves(backend, "ollama", REVIEW_PROTOCOL):
            continue
        if backend.type == "api" and api_providers.options(backend)["auth"] == "agent":
            continue
        names.append(name)
    return names


def is_review(candidate: Session, session_id: str) -> bool:
    return candidate.client == REVIEW_CLIENT and candidate.tags.get(REVIEW_TAG) == session_id


def described(candidate: Session) -> dict[str, Any]:
    """A review as the list of a session's reviews carries it. prompt is the title it was asked with, kept in
    its session's tags: the prompts can change afterwards."""
    return {
        "session_id": candidate.session_id, "started_at": candidate.started_at, "ended_at": candidate.ended_at,
        "model": candidate.model, "backend": candidate.backend, "by": candidate.tags.get("owner"),
        "prompt": str(candidate.tags.get("review_prompt") or ""),
    }


def reply(sessions: SessionStore, candidate: Session) -> str | None:
    """What the model answered in a review's session: its newest model call's reply, None when it wasn't captured."""
    calls = sorted((str(record.get("request_id") or "") for record in sessions.records(candidate) if _is_model_call(record)), reverse=True)
    for request_id in calls:
        turn = response_turn((sessions.read_trace(candidate, request_id) or {}).get("response"))
        text = "\n\n".join(block["text"] for block in (turn or {}).get("blocks", []) if block["type"] == "text" and block.get("text"))
        if text:
            return text
    return None
