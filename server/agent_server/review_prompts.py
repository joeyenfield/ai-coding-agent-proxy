"""The prompts a session's AI review can be asked with (review.py): a stored setting, `review_prompts`.

Each prompt names what the model should look at (`focus`), the parts of the session it is sent
(`data`, names from DATA) and the checks whose findings go with it (`metrics`, ids from METRICS).
The set starts as the rows of `default_review_prompts.yaml`; a site admin's own replaces it whole
(Settings.set_review_prompts, /api/config/review-prompts). Nothing here reads a session: review.py
has the function behind every name in DATA, and the check behind every id in METRICS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


MAX_PROMPTS = 40
MAX_FOCUS = 4_000
PROMPT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
SHIPPED = Path(__file__).with_name("default_review_prompts.yaml")
ALL = "all"

# What a prompt can be sent: the name it's listed by, and what the console calls it.
DATA = {
    "session": "The agent, model and length",
    "tokens": "Tokens, cache and the largest call",
    "cost": "Estimated cost, calls and tokens by model",
    "reliability": "Failures and waiting times",
    "timeline": "The context's size call by call",
    "context": "What the context is made of",
    "prompt": "System prompt size and start, tools, MCP servers",
    "system-outline": "The system prompt's sections by size",
    "tool-definitions": "The largest tool definitions, tools never called",
    "conversation": "Sub-agents, compactions and images",
    "threads": "Each sub-agent and side conversation",
    "typed": "The first messages typed",
    "steps": "The main chat step by step",
    "tools": "Tool calls by kind and tool",
    "files": "Files read again, repeated calls, largest results",
    "commands": "Shell commands run",
    "searches": "What the agent searched for",
    "edits": "The files it changed",
    "errors": "Failed tool calls and their errors",
}

# What each check is called. One entry per check in review.CHECKS.
METRICS = {
    "system-prompt": "System prompt size", "tool-definitions": "Tool definition size", "unused-tools": "Tools never used",
    "peak-context": "Largest call", "context-growth": "Context growth", "images": "Images in the conversation",
    "compactions": "Compactions", "long-session": "Session length",
    "searching": "Searching for files", "rereads": "Files read again", "large-results": "Large tool results", "tool-errors": "Failed tool calls",
    "repeated-calls": "Repeated tool calls", "subagents": "Sub-agents", "short-prompt": "First prompt", "steering": "Messages typed",
    "failed-calls": "Failed model calls", "rate-limited": "Rate limits", "incomplete": "Replies cut off", "caching": "Cache use",
    "idle-cache": "Cache lost to idle time", "slow": "Time to first token", "untraced": "Payload capture",
}

PREAMBLE = """You review one recorded session of an AI coding agent, to help the engineer who ran it get more from coding agents: fewer tokens, less waiting, lower cost, better results.

You are given measurements of the session and what automatic checks found, not its transcript. Work only from them, and say so when they can't tell. Don't repeat the numbers back: say what they mean and what to change. Be specific: name the tool, the file, the command, the setting or the habit. Don't invent facts about the project, and don't suggest changing anything the data doesn't mention. Every figure you give must come from the data: where it gives none for a saving, describe the saving in words instead of estimating one. Token counts of payloads are estimates."""

FORM = """Answer in this form and nothing else:

## Summary
Two or three sentences: {summary}

## What to change
Up to {count} numbered suggestions, the most valuable first. Each starts with what to do, in bold, in one sentence; then why, with the evidence from the session; then what it would save, when the data can say.

## What went well
Up to three short points, or leave the section out if nothing stands out."""

DEFAULT_SUMMARY = "what the session shows in this area."


@dataclass(frozen=True)
class Prompt:
    id: str
    title: str
    about: str
    focus: str
    summary: str
    data: tuple[str, ...]
    metrics: tuple[str, ...]
    suggestions: int

    def public(self) -> dict[str, Any]:
        """As the console shows it: the names of its data and checks, and the whole text the model gets."""
        return {
            "id": self.id, "title": self.title, "about": self.about, "instructions": self.instructions(),
            "data": [DATA[name] for name in self.data], "metrics": [METRICS[name] for name in self.metrics],
        }

    def instructions(self) -> str:
        return "\n\n".join([PREAMBLE, self.focus, FORM.format(summary=self.summary, count=self.suggestions)])


def _names(value: Any, known: dict[str, str], what: str, prompt: str) -> list[str]:
    if value == ALL or value is None:
        return list(known)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{prompt}: {what} is a list of names, or \"all\"")
    unknown = [item for item in value if item not in known]
    if unknown:
        raise ValueError(f"{prompt}: no such {what}: {', '.join(unknown)}. Known: {', '.join(known)}")
    return list(dict.fromkeys(value))


def _line(item: dict[str, Any], key: str, limit: int, prompt: str, default: str | None = None) -> str:
    value = item.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{prompt}: {key} is text of up to {limit} characters")
    return " ".join(value.split()) if limit < MAX_FOCUS else value.strip()


def checked_prompts(data: Any) -> list[dict[str, Any]]:
    """The prompts as they're stored: [{id, title, about, focus, summary, data, metrics, suggestions}], with
    "all" written out. Raises ValueError saying which prompt is wrong and why."""
    if not isinstance(data, list) or not 1 <= len(data) <= MAX_PROMPTS:
        raise ValueError(f"review_prompts is a list of 1 to {MAX_PROMPTS} prompts")
    prompts: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each prompt is an object: id, title, about, focus, data, metrics")
        unknown = sorted(set(item) - {"id", "title", "about", "focus", "summary", "data", "metrics", "suggestions"})
        if unknown:
            raise ValueError(f"Prompts have no {', '.join(unknown)}")
        name = item.get("id")
        if not isinstance(name, str) or not PROMPT_ID.fullmatch(name):
            raise ValueError("A prompt's id is lowercase letters, numbers and dashes, up to 40")
        if any(prompt["id"] == name for prompt in prompts):
            raise ValueError(f"Two prompts are called {name}")
        count = item.get("suggestions", 4)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
            raise ValueError(f"{name}: suggestions is a number from 1 to 10")
        prompts.append({
            "id": name, "title": _line(item, "title", 60, name), "about": _line(item, "about", 200, name),
            "focus": _line(item, "focus", MAX_FOCUS, name), "summary": _line(item, "summary", 200, name, DEFAULT_SUMMARY),
            "data": _names(item.get("data"), DATA, "data", name), "metrics": _names(item.get("metrics"), METRICS, "metrics", name),
            "suggestions": count,
        })
    return prompts


def shipped_prompts() -> list[dict[str, Any]]:
    """The prompts in default_review_prompts.yaml, checked as saved ones are: a mistake there stops the server starting."""
    document = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    return checked_prompts(document.get("prompts") if isinstance(document, dict) else None)


DEFAULT_PROMPTS = shipped_prompts()


def prompts(stored: list[dict[str, Any]]) -> list[Prompt]:
    return [Prompt(**{**item, "data": tuple(item["data"]), "metrics": tuple(item["metrics"])}) for item in stored]


def named(stored: list[dict[str, Any]], name: Any) -> Prompt:
    """The prompt of this id (the first when none is named). Raises ValueError for an unknown one."""
    found = prompts(stored)
    if not name:
        return found[0]
    for prompt in found:
        if prompt.id == name:
            return prompt
    raise ValueError(f"No such review: {name}. Choose one of: {', '.join(prompt.id for prompt in found)}")
