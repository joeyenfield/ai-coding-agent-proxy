"""A session's model calls read back as chats, the way the agent's own window shows them.

Agents resend the whole conversation with every model call, so the newest call in
a conversation holds all of it. Calls are grouped into threads by their messages:
a call continues the thread whose last call its messages start with. The main
chat, sub-agents and one-off calls (titles, classifiers) each end up in their own.

Traces can be large (hundreds of MB for a long session), so each session keeps a
small index (its chat_index row): per thread, the calls in it and the
hashes of its last call's messages. Only traces added since are read on a visit,
and a view reads just the one trace that ends its thread.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from typing import Any

from sqlalchemy import select

from .db import Database, kv, schema
from .sessions import Session, SessionStore


INDEX_VERSION = 3
# Endpoints that carry a conversation: Anthropic Messages, OpenAI Chat and Responses, native Ollama.
MODEL_PATH = re.compile(r"/(?:v1/messages|chat/completions|responses|api/chat|api/generate)$")
# Agents rewrite the tail of the history between calls (Claude Code moves a reminder to the end),
# so a call still continues a thread when it differs from the thread's last call in this many trailing messages.
TAIL_SLACK = 2
# One block's text above this is cut, so a huge file read doesn't make the page unusable.
MAX_TEXT = 200_000
# Pages of turns carry this much of each text (and of each string in a tool call's input);
# the console asks for a whole turn when it's opened.
PREVIEW_TEXT = 2_000
PREVIEW_INPUT = 300
PAGE_SIZE = 60
# Threads whose turns stay parsed in memory, so paging doesn't reread a large trace.
CACHED_THREADS = 8


class Chats:
    def __init__(self, sessions: SessionStore):
        self.sessions = sessions
        self._locks: dict[str, threading.Lock] = {}
        self._indexes: dict[str, dict[str, Any]] = {}
        self._cache: OrderedDict[tuple[str, str], tuple[str, list[dict[str, Any]]]] = OrderedDict()

    def view(
        self, session: Session, records: list[dict[str, Any]], thread: str | None = None,
        before: int | None = None, limit: int = PAGE_SIZE,
    ) -> dict[str, Any]:
        """The session's threads, and a page of one thread's turns (the main chat unless thread names another).

        A page is the limit turns before position before, or the newest ones; long texts in it are previews.
        """
        lock = self._locks.setdefault(session.session_id, threading.Lock())
        with lock:
            index = self._update(session, records)
            threads = _merged(index)
            main = max(threads, key=lambda item: (len(item["members"]), item["count"]), default=None)
            chosen = next((item for item in threads if item["id"] == thread), main)
            turns = self._turns(session, chosen) if chosen else []
        end = len(turns) if before is None else max(0, min(before, len(turns)))
        start = max(0, end - max(1, limit))
        return {
            "threads": [_summary(item, item is main) for item in sorted(threads, key=lambda item: item["members"][0])],
            "thread": chosen["id"] if chosen else None,
            "total": len(turns),
            "start": start,
            "turns": [_preview(turn, position) for position, turn in enumerate(turns[start:end], start)],
            "model_calls": sum(1 for record in records if _is_model_call(record)),
            "traced_calls": sum(len(item["members"]) for item in threads),
        }

    def turn(self, session: Session, records: list[dict[str, Any]], thread: str, position: int) -> dict[str, Any] | None:
        """One turn in full, for a preview that's been opened."""
        lock = self._locks.setdefault(session.session_id, threading.Lock())
        with lock:
            index = self._update(session, records)
            chosen = next((item for item in _merged(index) if item["id"] == thread), None)
            turns = self._turns(session, chosen) if chosen else []
        if not 0 <= position < len(turns):
            return None
        return {**turns[position], "index": position}

    def forget(self, session: Session) -> None:
        self.forget_id(session.session_id)

    def forget_id(self, session_id: str) -> None:
        self._indexes.pop(session_id, None)
        for key in [key for key in self._cache if key[0] == session_id]:
            del self._cache[key]

    def _update(self, session: Session, records: list[dict[str, Any]]) -> dict[str, Any]:
        db = self.sessions.db
        index = self._indexes.get(session.session_id) or _load_index(db, session.session_id)
        seen = set(index["seen"])
        changed = False
        for record in sorted(records, key=lambda item: str(item.get("request_id", ""))):
            request_id = str(record.get("request_id", ""))
            if request_id in seen or not _is_model_call(record):
                continue
            trace = self.sessions.read_trace(session, request_id)
            if trace is None:
                # Tracing was off or the trace isn't written yet; look again next time.
                continue
            seen.add(request_id)
            index["seen"].append(request_id)
            changed = True
            request = trace.get("request")
            if not isinstance(request, dict):
                continue
            messages = _messages(request)
            if not messages:
                continue
            thread = _place(index, request_id, [_hash(message) for message in messages], _leading_system(messages), request, record)
            for turn in request_turns(request):
                # Claude Code sends some calls with only the newest messages, so they start threads of their own;
                # their replies turning up in another thread's history put them in that thread.
                earlier = index["replies"].get(_reply_key(turn["blocks"])) if turn["role"] == "assistant" else None
                if earlier and earlier != request_id:
                    index["owners"][earlier] = thread
            reply = response_turn(trace.get("response"))
            if reply:
                index["replies"][_reply_key(reply["blocks"])] = request_id
        if changed:
            _save_index(db, session.session_id, index)
        self._indexes[session.session_id] = index
        return index

    def _turns(self, session: Session, thread: dict[str, Any]) -> list[dict[str, Any]]:
        last_id = thread["requests"][-1][0]
        key = (session.session_id, thread["id"])
        cached = self._cache.get(key)
        if cached and cached[0] == last_id:
            self._cache.move_to_end(key)
            return cached[1]
        turns = self._read_turns(session, last_id)
        self._cache[key] = (last_id, turns)
        while len(self._cache) > CACHED_THREADS:
            self._cache.popitem(last=False)
        return turns

    def _read_turns(self, session: Session, last_id: str) -> list[dict[str, Any]]:
        trace = self.sessions.read_trace(session, last_id) or {}
        request = trace.get("request") if isinstance(trace.get("request"), dict) else {}
        turns = request_turns(request)
        # Agents send replies back as they got them, so a reply's tool call ids (or text) name the call that wrote it.
        # Calls that weren't recorded leave their replies without one.
        replies = self._indexes[session.session_id]["replies"]
        for turn in turns:
            if turn["role"] == "assistant" and _reply_key(turn["blocks"]) in replies:
                turn["request_id"] = replies[_reply_key(turn["blocks"])]
        reply = response_turn(trace.get("response"))
        if reply:
            reply["request_id"] = last_id
            turns.append(reply)
        return turns


def _preview(turn: dict[str, Any], position: int) -> dict[str, Any]:
    """The turn as a page carries it: what the console shows folded is cut short, what people wrote is whole."""
    blocks = []
    for block in turn["blocks"]:
        folded = turn["role"] == "system" or block.get("reminder") or block["type"] in ("tool_result", "thinking", "other")
        if folded and isinstance(block.get("text"), str) and len(block["text"]) > PREVIEW_TEXT:
            block = {**block, "text": block["text"][:PREVIEW_TEXT], "preview": True}
        if block["type"] == "tool_use":
            shrunk = _shrink(block.get("input"))
            if shrunk != block.get("input"):
                block = {**block, "input": shrunk, "preview": True}
        blocks.append(block)
    return {**turn, "blocks": blocks, "index": position}


def _shrink(value: Any) -> Any:
    if isinstance(value, str):
        return value[:PREVIEW_INPUT]
    if isinstance(value, dict):
        return {key: _shrink(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shrink(item) for item in value[:20]]
    return value


def _summary(thread: dict[str, Any], main: bool) -> dict[str, Any]:
    return {
        "id": thread["id"],
        "title": thread["title"],
        "model": thread["model"],
        "requests": thread["members"],
        "messages": thread["count"],
        "started": thread["started"],
        "updated": thread["updated"],
        "main": main,
    }


def _merged(index: dict[str, Any]) -> list[dict[str, Any]]:
    """The threads, with each one whose calls all went into another thread's history folded into that one."""
    by_id = {thread["id"]: {**thread, "members": [rid for rid, _ in thread["requests"]]} for thread in index["threads"]}
    owners = index["owners"]

    def home(thread_id: str) -> str:
        for _ in range(len(by_id)):
            thread = by_id[thread_id]
            targets = {owners.get(rid) for rid, _ in thread["requests"]}
            if len(targets) != 1 or None in targets or thread_id in targets or not targets <= by_id.keys():
                return thread_id
            thread_id = targets.pop()
        return thread_id

    result: dict[str, dict[str, Any]] = {}
    for thread_id in by_id:
        target = home(thread_id)
        if target == thread_id:
            result.setdefault(thread_id, by_id[thread_id])
    for thread_id, thread in by_id.items():
        target = home(thread_id)
        if target != thread_id:
            result[target]["members"] = sorted({*result[target]["members"], *thread["members"]})
    return list(result.values())


def _place(index: dict[str, Any], request_id: str, hashes: list[str], leading: int, request: dict[str, Any], record: dict[str, Any]) -> str:
    best, best_length = None, 0
    for thread in index["threads"]:
        previous = thread["hashes"]
        length = _common_prefix(previous, hashes)
        # The shared part must go past the system prompt (every chat shares that) and cover the old call's history.
        if length - thread["leading"] < 1 or length < len(previous) - TAIL_SLACK:
            continue
        if length > best_length or (length == best_length and best is not None and thread["updated"] >= best["updated"]):
            best, best_length = thread, length
    timestamp = str(record.get("timestamp") or "")
    if best is None:
        index["threads"].append({
            "id": request_id,
            "title": _title(request),
            "model": str(request.get("model") or record.get("model") or ""),
            "requests": [[request_id, len(hashes)]],
            "hashes": hashes,
            "leading": leading,
            "count": len(hashes),
            "started": timestamp,
            "updated": timestamp,
        })
        return request_id
    best["requests"].append([request_id, len(hashes)])
    best["requests"].sort()
    if request_id >= max(rid for rid, _ in best["requests"]):
        best.update(hashes=hashes, leading=leading, count=len(hashes), updated=timestamp)
        best["model"] = str(request.get("model") or best["model"])
    return best["id"]


def _reply_key(blocks: list[dict[str, Any]]) -> str:
    calls = [block["id"] for block in blocks if block["type"] == "tool_use" and block.get("id")]
    if calls:
        return "tools:" + ",".join(calls)
    parts = [block.get("text", "") for block in blocks if block["type"] == "text"]
    parts += [json.dumps([block.get("name"), block.get("input")], sort_keys=True) for block in blocks if block["type"] == "tool_use"]
    return hashlib.blake2b(" ".join(" ".join(parts).split()).encode("utf-8"), digest_size=8).hexdigest()


def _common_prefix(left: list[str], right: list[str]) -> int:
    length = 0
    for a, b in zip(left, right):
        if a != b:
            break
        length += 1
    return length


def _load_index(db: Database, session_id: str) -> dict[str, Any]:
    with db.read() as connection:
        data = connection.execute(select(schema.chat_index.c.data).where(schema.chat_index.c.session_id == session_id)).scalar()
    if isinstance(data, dict) and data.get("version") == INDEX_VERSION:
        return data
    return {"version": INDEX_VERSION, "seen": [], "threads": [], "replies": {}, "owners": {}}


def _save_index(db: Database, session_id: str, index: dict[str, Any]) -> None:
    # A copy: the index in memory goes on changing while the write waits its turn.
    row = {"session_id": session_id, "data": json.loads(json.dumps(index))}
    db.writer.submit(lambda connection: kv.upsert(connection, schema.chat_index, row))


def _is_model_call(record: dict[str, Any]) -> bool:
    path = str(record.get("endpoint") or "").split("?", 1)[0].rstrip("/")
    return bool(MODEL_PATH.search(path))


def _messages(request: dict[str, Any]) -> list[Any]:
    """The conversation a call sends, in order, with the system prompt first."""
    messages: list[Any] = []
    if request.get("system"):
        messages.append({"role": "system", "content": request["system"]})
    if request.get("instructions"):
        messages.append({"role": "system", "content": request["instructions"]})
    if isinstance(request.get("messages"), list):
        messages.extend(request["messages"])
    if isinstance(request.get("input"), list):
        messages.extend(request["input"])
    elif isinstance(request.get("input"), str):
        messages.append({"role": "user", "content": request["input"]})
    if isinstance(request.get("prompt"), str):
        messages.append({"role": "user", "content": request["prompt"]})
    return messages


def _leading_system(messages: list[Any]) -> int:
    count = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
            break
        count += 1
    return count


def _hash(message: Any) -> str:
    # System prompts change from call to call (Claude Code puts a per-call header in its own),
    # and the user's messages tell chats apart anyway.
    if isinstance(message, dict) and message.get("role") in ("system", "developer"):
        return "system"
    # Cache markers move from call to call without changing the conversation.
    text = json.dumps(_without(message, "cache_control"), sort_keys=True, ensure_ascii=False)
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()


def _without(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return {k: _without(v, key) for k, v in value.items() if k != key}
    if isinstance(value, list):
        return [_without(item, key) for item in value]
    return value


def _title(request: dict[str, Any]) -> str:
    """The first thing the user typed, skipping the reminders agents put in front of it."""
    texts = [block for turn in request_turns(request) if turn["role"] == "user" for block in turn["blocks"] if block["type"] == "text"]
    for block in [block for block in texts if not block.get("reminder")] + texts:
        text = " ".join(block["text"].split())
        if text:
            return text[:120]
    return ""


# Turns: {"role": system|user|assistant|tool, "blocks": [...]}; blocks are text, thinking,
# tool_use (id, name, input), tool_result (tool_use_id, text, is_error), image and other.

REMINDER = re.compile(r"^\s*<(system-reminder|local-command-caveat|local-command-stdout|command-name|command-message|command-args|environment_context|user_instructions)\b")


def request_turns(request: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for message in _messages(request):
        turn = _turn(message)
        if not turn:
            continue
        if turns and turn["role"] == turns[-1]["role"] and turn["role"] in ("assistant", "tool"):
            # OpenAI Responses sends a reply as several items (reasoning, text, each tool call).
            turns[-1]["blocks"].extend(turn["blocks"])
        else:
            turns.append(turn)
    for turn in turns:
        if turn["role"] == "system":
            turn["preamble"] = True
        else:
            break
    return turns


def _turn(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return {"role": "user", "blocks": [_text(message)]}
    kind = message.get("type")
    # OpenAI Responses items that aren't messages.
    if kind in ("function_call", "custom_tool_call", "local_shell_call"):
        return {"role": "assistant", "blocks": [_tool_use(message.get("call_id") or message.get("id"), message.get("name") or kind, message.get("arguments", message.get("input", message.get("action"))))]}
    if kind in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
        return {"role": "tool", "blocks": [_tool_result(message.get("call_id"), message.get("output"))]}
    if kind == "reasoning":
        summary = "\n".join(str(part.get("text", "")) for part in message.get("summary") or [] if isinstance(part, dict))
        return {"role": "assistant", "blocks": [{"type": "thinking", "text": summary}]} if summary else None
    role = str(message.get("role") or "user")
    if role == "developer":
        role = "system"
    blocks = _blocks(message.get("content"))
    if message.get("reasoning_content") or message.get("thinking"):
        blocks.insert(0, {"type": "thinking", "text": str(message.get("reasoning_content") or message.get("thinking"))})
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            function = call.get("function") or {}
            blocks.append(_tool_use(call.get("id"), function.get("name", ""), function.get("arguments")))
    if role == "tool":
        text = "\n".join(block.get("text", "") for block in blocks)
        blocks = [_tool_result(message.get("tool_call_id") or message.get("tool_name"), text)]
    if not blocks:
        return None
    if role == "user" and all(block["type"] == "tool_result" for block in blocks):
        role = "tool"
    if role not in ("system", "user", "assistant", "tool"):
        role = "user"
    return {"role": role, "blocks": blocks}


def _blocks(content: Any) -> list[dict[str, Any]]:
    if content is None or content == "":
        return []
    if isinstance(content, str):
        return [_text(content)]
    if not isinstance(content, list):
        return [_other(content)]
    blocks = []
    for part in content:
        if isinstance(part, str):
            blocks.append(_text(part))
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text") or (kind is None and "text" in part):
            if part.get("text"):
                blocks.append(_text(part["text"]))
        elif kind == "thinking":
            blocks.append({"type": "thinking", "text": str(part.get("thinking") or "")})
        elif kind == "redacted_thinking":
            blocks.append({"type": "thinking", "text": ""})
        elif kind in ("tool_use", "server_tool_use"):
            blocks.append(_tool_use(part.get("id"), part.get("name", ""), part.get("input")))
        elif isinstance(kind, str) and (kind == "tool_result" or kind.endswith("_tool_result")):
            inner = part.get("content")
            text = "\n".join(block.get("text", "") for block in _blocks(inner)) if isinstance(inner, list) else inner
            blocks.append(_tool_result(part.get("tool_use_id"), text, bool(part.get("is_error"))))
        elif kind in ("image", "image_url", "input_image"):
            blocks.append({"type": "image"})
        elif kind in ("document", "input_file"):
            blocks.append({"type": "other", "text": f"[{kind}]"})
        elif kind == "refusal":
            blocks.append(_text(str(part.get("refusal", ""))))
        else:
            blocks.append(_other(part))
    return blocks


def _text(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    block = {"type": "text", **_cut(text)}
    if REMINDER.match(text):
        block["reminder"] = True
    return block


def _other(value: Any) -> dict[str, Any]:
    return {"type": "other", **_cut(json.dumps(value, ensure_ascii=False, indent=2))}


def _tool_use(call_id: Any, name: Any, arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            pass
    return {"type": "tool_use", "id": str(call_id or ""), "name": str(name or "tool"), "input": arguments}


def _tool_result(call_id: Any, output: Any, is_error: bool = False) -> dict[str, Any]:
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, indent=2) if output is not None else ""
    return {"type": "tool_result", "tool_use_id": str(call_id or ""), "is_error": is_error, **_cut(text)}


def _cut(text: str) -> dict[str, Any]:
    if len(text) <= MAX_TEXT:
        return {"text": text}
    return {"text": text[:MAX_TEXT], "truncated": len(text)}


def response_turn(response: Any) -> dict[str, Any] | None:
    """The reply a call got, put back together from its streamed events when it was streamed."""
    if isinstance(response, list):
        blocks = _streamed_blocks([event for event in response if isinstance(event, dict)])
    elif isinstance(response, dict):
        blocks = _complete_blocks(response)
    elif isinstance(response, str) and response:
        blocks = [_text(response)]
    else:
        blocks = []
    blocks = [block for block in blocks if block["type"] != "text" or block["text"]]
    return {"role": "assistant", "blocks": blocks} if blocks else None


def _complete_blocks(response: dict[str, Any]) -> list[dict[str, Any]]:
    if response.get("error"):
        error = response["error"]
        return [{"type": "error", "text": error if isinstance(error, str) else json.dumps(error, ensure_ascii=False, indent=2)}]
    if isinstance(response.get("content"), list):
        return _blocks(response["content"])
    if isinstance(response.get("output"), list):
        blocks = []
        for item in response["output"]:
            turn = _turn(item)
            if turn:
                blocks.extend(turn["blocks"])
        return blocks
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        turn = _turn({"role": "assistant", **(choices[0].get("message") or {"content": choices[0].get("text")})})
        return turn["blocks"] if turn else []
    if isinstance(response.get("message"), dict):
        turn = _turn({**response["message"], "role": "assistant"})
        return turn["blocks"] if turn else []
    if isinstance(response.get("response"), str):
        return ([{"type": "thinking", "text": response["thinking"]}] if response.get("thinking") else []) + [_text(response["response"])]
    return [_other(response)]


def _streamed_blocks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # OpenAI Responses sends the whole reply once more at the end.
    for event in events:
        if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
            return _complete_blocks(event["response"])
    blocks: list[dict[str, Any]] = []
    anthropic: dict[int, dict[str, Any]] = {}
    calls: dict[int, dict[str, Any]] = {}
    text = {"thinking": "", "text": ""}

    def flush() -> None:
        for kind in ("thinking", "text"):
            if text[kind]:
                blocks.append({"type": kind, **_cut(text[kind])})
                text[kind] = ""

    for event in events:
        kind = event.get("type")
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            entry = {"type": block.get("type"), "id": block.get("id"), "name": block.get("name"), "text": block.get("text") or block.get("thinking") or "", "json": ""}
            anthropic[int(event.get("index", len(anthropic)))] = entry
            blocks.append(entry)
            continue
        if kind == "content_block_delta":
            entry = anthropic.get(int(event.get("index", 0)))
            delta = event.get("delta") or {}
            if entry is not None:
                entry["text"] += delta.get("text") or delta.get("thinking") or ""
                entry["json"] += delta.get("partial_json") or ""
            continue
        if kind in ("response.output_text.delta", "response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            text["text" if kind == "response.output_text.delta" else "thinking"] += str(event.get("delta") or "")
            continue
        if kind == "error":
            flush()
            blocks.append({"type": "error", "text": json.dumps(event.get("error", event), ensure_ascii=False, indent=2)})
            continue
        message = event.get("message")
        if isinstance(message, dict):
            # Native Ollama chunks.
            text["thinking"] += str(message.get("thinking") or "")
            text["text"] += str(message.get("content") or "")
            for call in message.get("tool_calls") or []:
                flush()
                function = call.get("function") or {}
                blocks.append(_tool_use(call.get("id"), function.get("name"), function.get("arguments")))
        elif isinstance(event.get("response"), str):
            text["thinking"] += str(event.get("thinking") or "")
            text["text"] += event["response"]
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            text["thinking"] += str(delta.get("reasoning_content") or delta.get("reasoning") or "")
            text["text"] += str(delta.get("content") or "")
            for call in delta.get("tool_calls") or []:
                position = int(call.get("index", len(calls)))
                entry = calls.setdefault(position, {"id": "", "name": "", "arguments": ""})
                function = call.get("function") or {}
                entry["id"] = call.get("id") or entry["id"]
                entry["name"] += function.get("name") or ""
                entry["arguments"] += function.get("arguments") or ""
    flush()
    result: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") == "tool_use" and "json" in block:
            result.append(_tool_use(block["id"], block["name"], block["json"]))
        elif block.get("type") in ("thinking", "redacted_thinking") and "json" in block:
            result.append({"type": "thinking", **_cut(block["text"])})
        elif block.get("type") == "text" and "json" in block:
            result.append(_text(block["text"]))
        elif "json" not in block:
            result.append(block)
    for position in sorted(calls):
        entry = calls[position]
        result.append(_tool_use(entry["id"], entry["name"], entry["arguments"]))
    return result
