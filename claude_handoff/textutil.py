"""Text helpers: cleaning, truncation, tiny formatters."""
from __future__ import annotations

import re
from datetime import datetime

NOISE_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<command-name>.*?</command-name>"
    r"|<command-message>.*?</command-message>"
    r"|<command-args>.*?</command-args>"
    r"|<local-command-stdout>.*?</local-command-stdout>"
    r"|<local-command-stderr>.*?</local-command-stderr>",
    re.DOTALL,
)
CAVEAT_RE = re.compile(r"^Caveat: The messages below were generated.*?$", re.MULTILINE)


def one_line(text: str, cap: int = 80) -> str:
    text = " ".join(text.split())
    return text[: cap - 1] + "…" if len(text) > cap else text


def clean_text(text: str) -> str:
    """Strip system-reminder / slash-command envelopes and caveats."""
    text = NOISE_RE.sub("", text)
    text = CAVEAT_RE.sub("", text)
    return text.strip()


def user_text(message: dict) -> str:
    """Human-visible text of a user message (string or block-list content)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append("[image attached]")
        return "\n".join(parts)
    return ""


def tool_result_text(block: dict) -> str:
    """Plain text of a tool_result block (string or block-list content)."""
    content = block.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


def truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head, tail = int(cap * 0.7), int(cap * 0.2)
    omitted = len(text) - head - tail
    return (
        f"{text[:head]}\n\n[... {omitted} chars omitted ...]\n\n{text[-tail:]}"
    )


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) \
            .astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


