"""claude.ai and ChatGPT data exports (conversations.json) as input."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .parse import _new_parse_state, _parse_ts
from .textutil import one_line


def is_web_export(path: Path) -> bool:
    """True for a claude.ai data export (conversations.json): a .json file
    whose first non-whitespace byte opens a JSON array."""
    if path.suffix.lower() != ".json":
        return False
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            head = fh.read(64).lstrip()
        return head.startswith("[")
    except OSError:
        return False


def _web_message_text(msg: dict) -> str:
    text = msg.get("text")
    if not text:
        content = msg.get("content")
        if isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
    text = (text or "").strip()
    if msg.get("attachments") or msg.get("files"):
        text = (text + "\n\n[attachment]").strip()
    return text


def _load_web_conversations(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"Cannot read claude.ai export {path}: {e}") from e
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) \
        else []


def _epoch_iso(t) -> str | None:
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _convo_title(convo: dict) -> str:
    return str(convo.get("name") or convo.get("title") or "")


def _convo_updated(convo: dict) -> datetime:
    """Comparable freshness of a claude.ai (ISO) or ChatGPT (epoch) convo."""
    ts = (_parse_ts(convo.get("updated_at") or convo.get("created_at"))
          or _parse_ts(_epoch_iso(convo.get("update_time")
                                  or convo.get("create_time"))))
    return ts or datetime.min.replace(tzinfo=timezone.utc)


def _chatgpt_messages(convo: dict) -> list[tuple[str, str, str | None]]:
    """(role, text, iso_ts) triples from a ChatGPT export conversation —
    walks the canonical thread backward from current_node."""
    mapping = convo.get("mapping")
    if not isinstance(mapping, dict):
        return []
    chain = []
    node = mapping.get(convo.get("current_node"))
    hops = 0
    while isinstance(node, dict) and hops < 100_000:
        chain.append(node)
        node = mapping.get(node.get("parent"))
        hops += 1
    out = []
    for node in reversed(chain):
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        role = (msg.get("author") or {}).get("role")
        if role not in ("user", "assistant"):
            continue  # system prompts and tool traffic are noise here
        content = msg.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        text = "\n".join(p for p in parts
                         if isinstance(p, str)).strip() if parts else ""
        if text:
            out.append((role, text, _epoch_iso(msg.get("create_time"))))
    return out


def _claude_web_messages(convo: dict) -> list[tuple[str, str, str | None]]:
    out = []
    for msg in convo.get("chat_messages") or []:
        if not isinstance(msg, dict):
            continue
        text = _web_message_text(msg)
        if text:
            role = "user" if msg.get("sender") == "human" else "assistant"
            out.append((role, text, msg.get("created_at")))
    return out


def parse_web_export(path: Path, name_filter: str | None = None) -> dict:
    """Parse a claude.ai or ChatGPT data export (conversations.json) into
    the same shape as parse_session. Picks the newest conversation, or the
    newest whose title matches `name_filter` (case-insensitive)."""
    convos = _load_web_conversations(path)
    if name_filter:
        q = name_filter.lower()
        convos = [c for c in convos if q in _convo_title(c).lower()]
    if not convos:
        raise SystemExit(
            f"No conversation{f' matching {name_filter!r}' if name_filter else ''} "
            f"in {path}. Run with --list to see the conversations it holds.")
    convo = max(convos, key=_convo_updated)

    state = _new_parse_state()
    meta = state["meta"]
    meta["session_id"] = convo.get("uuid") or convo.get("id")
    if _convo_title(convo):
        meta["summaries"].append(_convo_title(convo))
    messages = (_claude_web_messages(convo) if "chat_messages" in convo
                else _chatgpt_messages(convo))
    for role, text, ts in messages:
        meta["n_user" if role == "user" else "n_assistant"] += 1
        meta["first_ts"] = meta["first_ts"] or ts
        meta["last_ts"] = ts or meta["last_ts"]
        state["turns"].append({"role": role, "text_parts": [text],
                               "tools": [], "ts": ts})
    for key in [k for k in state if k.startswith("_")]:
        state.pop(key)
    state.pop("sidechains", None)
    return state


def list_export_conversations(path: Path) -> None:
    for c in sorted(_load_web_conversations(path), key=_convo_updated,
                    reverse=True):
        when = _convo_updated(c).astimezone().strftime("%Y-%m-%d %H:%M")
        n = len(c.get("chat_messages") or c.get("mapping") or [])
        cid = str(c.get("uuid") or c.get("id") or "?")[:8]
        print(f"{when}  {n:>4} msgs  {cid}  "
              f"{one_line(_convo_title(c) or '(untitled)', 70)}")


