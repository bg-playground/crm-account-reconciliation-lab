#!/usr/bin/env python3
"""Fail if any tracked text file names a private project or uses a non-reserved email/URL domain.

Allowed domains end in .example or .invalid (RFC 2606 reserved names). The
denylist terms are matched case-insensitively. This script and its own test are
the only files exempt from the denylist (they must spell the terms to check
for them); they are still checked for emails and URLs.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


class BoundaryCheck:
    DENYLIST = ("nataegisflow", "studio-offers", "turgon", "penguin")
    ALLOWED_SUFFIXES = (".example", ".invalid")
    DENYLIST_EXEMPT = ("scripts/boundary_check.py", "tests/test_boundary_check.py")
    URL = re.compile(r"\b[a-z][a-z0-9+.-]*://([^\s/'\"<>()\[\]`]+)", re.IGNORECASE)
    EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")

    @staticmethod
    def host_allowed(host: str) -> bool:
        host = host.split("@")[-1].split(":")[0].strip(".").lower()
        return host.endswith(BoundaryCheck.ALLOWED_SUFFIXES)

    @staticmethod
    def scan_text(rel: str, text: str) -> list[str]:
        problems = []
        lowered = text.lower()
        if rel not in BoundaryCheck.DENYLIST_EXEMPT:
            for term in BoundaryCheck.DENYLIST:
                if term in lowered:
                    problems.append(f"{rel}: private name '{term}'")
        for match in BoundaryCheck.URL.finditer(text):
            if not BoundaryCheck.host_allowed(match.group(1)):
                problems.append(f"{rel}: URL host '{match.group(1)}' is not .example/.invalid")
        for match in BoundaryCheck.EMAIL.finditer(text):
            if not BoundaryCheck.host_allowed(match.group(1)):
                problems.append(f"{rel}: email domain '{match.group(1)}' is not .example/.invalid")
        return problems

    @staticmethod
    def tracked_files(root: Path) -> list[str]:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
        return sorted(p for p in out.decode().split("\0") if p)

    @staticmethod
    def scan_paths(root: Path, files: list[str]) -> tuple[list[str], int]:
        problems, scanned = [], 0
        for rel in files:
            path = root / rel
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if b"\0" in raw[:8192]:
                continue
            scanned += 1
            problems += BoundaryCheck.scan_text(rel, raw.decode("utf-8", errors="replace"))
            problems += BoundaryCheck.scan_text(rel, rel) if rel not in BoundaryCheck.DENYLIST_EXEMPT else []
        return problems, scanned

    @staticmethod
    def main(argv: list[str]) -> int:
        root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
        problems, scanned = BoundaryCheck.scan_paths(root, BoundaryCheck.tracked_files(root))
        for problem in problems:
            print(problem)
        print(f"boundary check: {scanned} tracked text files scanned, {len(problems)} problem(s)")
        return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(BoundaryCheck.main(sys.argv))
