#!/usr/bin/env python3
"""Count lines of code in the repository.

"Code" means non-blank, non-comment source lines. Only git-tracked files are
counted so the working tree (e.g. data/*.db, .venv) is ignored automatically.
Run from anywhere inside the repo: `python scripts/cloc.py`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Map file extensions to a language name and its line-comment prefix.
LANGUAGES = {
    ".py": ("Python", "#"),
    ".yaml": ("YAML", "#"),
    ".yml": ("YAML", "#"),
    ".toml": ("TOML", "#"),
    ".cfg": ("Config", "#"),
    ".ini": ("Config", "#"),
    ".sh": ("Shell", "#"),
}


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip())


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in out.stdout.splitlines() if line]


def count_code_lines(path: Path, comment: str) -> int:
    code = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if comment and line.startswith(comment):
            continue
        code += 1
    return code


def main() -> int:
    root = repo_root()
    totals: dict[str, tuple[int, int]] = {}  # lang -> (files, code_lines)
    grand_files = 0
    grand_code = 0

    for path in tracked_files(root):
        lang_comment = LANGUAGES.get(path.suffix)
        if lang_comment is None:
            continue
        lang, comment = lang_comment
        lines = count_code_lines(path, comment)
        files, code = totals.get(lang, (0, 0))
        totals[lang] = (files + 1, code + lines)
        grand_files += 1
        grand_code += lines

    width = max((len(l) for l in totals), default=8)
    print(f"{'Language':<{width}}  {'Files':>6}  {'Code':>8}")
    print(f"{'-' * width}  {'-' * 6}  {'-' * 8}")
    for lang in sorted(totals, key=lambda l: totals[l][1], reverse=True):
        files, code = totals[lang]
        print(f"{lang:<{width}}  {files:>6}  {code:>8}")
    print(f"{'-' * width}  {'-' * 6}  {'-' * 8}")
    print(f"{'TOTAL':<{width}}  {grand_files:>6}  {grand_code:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
