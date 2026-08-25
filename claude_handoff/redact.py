"""Secret redaction — applied before any egress (LLM or document)."""
from __future__ import annotations

import re
import sys

# Secret-shaped strings are stripped from transcripts before any --llm call
# (zero-trust: session logs routinely contain keys pasted into commands).
SECRET_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),                    # OpenAI/Anthropic
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),  # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),             # Slack
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),                       # AWS key id
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),                   # Google
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9._-]{20,}"),  # JWT
    re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/-]{20,}=*"),
    re.compile(r"((?:api[_-]?key|access[_-]?token|secret|password|passwd"
               r"|authorization)\s*[=:]\s*[\"']?)[^\s\"'\[]{8,}",
               re.IGNORECASE),
]

def redact_secrets(text: str) -> tuple[str, int]:
    """Replace secret-shaped strings with [REDACTED]; returns (text, count).

    Precision-first: known key prefixes and KEY=value assignments only —
    git hashes, URLs and normal prose are left alone.
    """
    total = 0
    for pattern in SECRET_RES:
        def _sub(m: re.Match[str]) -> str:
            keep = m.group(1) if m.groups() else ""
            return keep + "[REDACTED]"
        text, n = pattern.subn(_sub, text)
        total += n
    return text, total


def redact_doc(doc: str, hint: bool = True) -> str:
    """Redact the final document — a written or pasted handoff is egress
    too, not just what goes to an LLM."""
    doc, n = redact_secrets(doc)
    if n:
        note = " (--no-redact to disable)" if hint else ""
        print(f"Redacted {n} secret-looking string(s) in the "
              f"output{note}.", file=sys.stderr)
    return doc


