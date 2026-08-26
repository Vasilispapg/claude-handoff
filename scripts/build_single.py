#!/usr/bin/env python3
"""Stitch the claude_handoff/ package into single/claude_handoff.py.

The package is the source of truth; the generated single file exists so
`curl -O …/single/claude_handoff.py && python3 claude_handoff.py` keeps
working. Run with --check to verify the committed artifact is fresh and
behaves identically to the package (used in CI).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "claude_handoff"
ARTIFACT = ROOT / "single" / "claude_handoff.py"

# Concatenation order — dependencies first (call-time resolution makes the
# order forgiving, but keep it meaningful for human readers).
ORDER = ["textutil", "redact", "parse", "webexport", "discovery",
         "render", "llm", "brief", "integrations", "cli"]

STDLIB_BLOCK = """\
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
"""


def module_body(name: str) -> str:
    """A module's code minus its docstring and import header."""
    lines = (PKG / f"{name}.py").read_text(encoding="utf-8").splitlines(True)
    out = []
    in_header = True
    open_paren = False
    in_doc = False
    for i, line in enumerate(lines):
        if i == 0 and line.startswith('"""'):
            body = line.rstrip()
            if body == '"""' or not body.endswith('"""'):
                in_doc = True             # multi-line module docstring
            continue
        if in_doc:
            if '"""' in line:
                in_doc = False
            continue
        if in_header:
            s = line.strip()
            if open_paren:
                open_paren = ")" not in s
                continue
            if s == "" or s.startswith(("import ", "from ")):
                if s.startswith("from ") and "(" in s and ")" not in s:
                    open_paren = True
                continue
            in_header = False
        out.append(line)
    return "".join(out).strip("\n") + "\n"


def build() -> str:
    init = (PKG / "__init__.py").read_text(encoding="utf-8")
    docstring = init.split('"""')[1]
    version_line = (PKG / "_version.py").read_text(encoding="utf-8").strip()
    parts = [
        "#!/usr/bin/env python3\n",
        f'"""{docstring}"""\n\n',
        "# GENERATED FILE — do not edit. Source of truth: the claude_handoff/\n",
        "# package in this repo. Rebuild with: python3 scripts/build_single.py\n\n",
        STDLIB_BLOCK, "\n", version_line, "\n",
    ]
    for name in ORDER:
        parts.append("\n\n# " + "-" * 75 + " #\n")
        parts.append(f"#  {name}\n")
        parts.append("# " + "-" * 75 + " #\n\n")
        parts.append(module_body(name))
    return "".join(parts)


def run_out(script: Path, args: list) -> bytes:
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, check=False,
                          cwd=str(ROOT)).stdout


def check(built: str) -> int:
    if not ARTIFACT.is_file() or ARTIFACT.read_text(encoding="utf-8") != built:
        print("STALE: single/claude_handoff.py does not match the package — "
              "run: python3 scripts/build_single.py", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "claude_handoff.py"
        probe.write_text(built, encoding="utf-8")
        fixtures = ROOT / "tests" / "fixtures"
        cases = [
            [str(fixtures / "agent_session.jsonl"), "-o", "-",
             "--include-sidechains", "--include-tools"],
            [str(fixtures / "agent_session.jsonl"), "-o", "-",
             "--format", "json"],
            [str(fixtures / "secret_session.jsonl"), "-o", "-"],
            [str(fixtures / "classic_session.jsonl"), "-o", "-",
             "--fit", "1k"],
            [str(fixtures / "classic_session.jsonl"), "-o", "-", "--full"],
            # exercises the notification path (html import) in the artifact
            [str(fixtures / "notification_session.jsonl"), "-o", "-"],
        ]
        for args in cases:
            pkg_out = subprocess.run(
                [sys.executable, "-m", "claude_handoff", *args],
                capture_output=True, check=False, cwd=str(ROOT)).stdout
            if run_out(probe, args) != pkg_out:
                print(f"MISMATCH: artifact != package for {args}",
                      file=sys.stderr)
                return 1
    print("single/claude_handoff.py is fresh and byte-identical to the package.")
    return 0


def main() -> int:
    built = build()
    if "--check" in sys.argv:
        return check(built)
    ARTIFACT.parent.mkdir(exist_ok=True)
    ARTIFACT.write_text(built, encoding="utf-8")
    print(f"wrote {ARTIFACT} ({len(built.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
