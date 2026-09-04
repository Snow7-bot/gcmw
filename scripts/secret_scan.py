#!/usr/bin/env python3
"""Basic secret scan for common plaintext secret patterns.

This is a lightweight placeholder. It is intentionally conservative and will be
replaced/strengthened when Issue #4 is implemented with a dedicated scanner.
"""
from __future__ import annotations

import os
import re
import sys

EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "out",
    "build",
    ".venv",
    "__pycache__",
}

PATTERNS = [
    re.compile(r"(?i)api[_-]?key\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
    re.compile(r"(?i)secret\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
    re.compile(r"(?i)password\s*=\s*['\"][A-Za-z0-9_\-]{8,}['\"]"),
    re.compile(r"(?i)token\s*=\s*['\"][A-Za-z0-9_\-\.]{16,}['\"]"),
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
]


def walk(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for filename in filenames:
            yield os.path.join(dirpath, filename)


def main() -> int:
    root = os.getcwd()
    errors = []
    for path in walk(root):
        if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".mp4", ".zip", ".pdf", ".bin", ".pcm", ".so", ".a", ".icns", ".ico")):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            for pattern in PATTERNS:
                if pattern.search(line):
                    errors.append(f"{os.path.relpath(path, root)}:{lineno}: {pattern.pattern}")
                    break
    if errors:
        print("Secret scan failed:")
        for error in errors:
            print(" ", error)
        return 1
    print("Basic secret scan passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
