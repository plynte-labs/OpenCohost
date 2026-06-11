"""check_abs_paths.py — pre-commit hook: block drive-letter absolute paths.

Scans staged files line-by-line for Windows-style absolute paths
(pattern: [A-Za-z]:[/\\]).  Lines containing the pragma token ``path-ok``
are skipped so doc examples can opt out.

Usage (invoked by pre-commit with staged file paths as arguments):
    python tools/check_abs_paths.py file1.py file2.yaml ...

Exit 0: no violations found.
Exit 1: one or more violations found (details printed to stdout).
"""

import re
import sys
from pathlib import Path

# Matches Windows drive-letter absolute paths (single letter, colon, slash/backslash).
# Negative lookbehind ensures the letter is not preceded by another letter,
# which prevents false positives on URL schemes like https:// or http://.
_DRIVE_LETTER_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[/\\]")
_PRAGMA_TOKEN = "path-ok"


def check_file(path: Path) -> list[str]:
    """Return violation messages for *path*, or an empty list if clean."""
    violations: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Unreadable file — report as a warning but don't block.
        return [f"{path}: could not read file: {exc}"]

    for lineno, line in enumerate(text.splitlines(), start=1):
        if _PRAGMA_TOKEN in line:
            continue
        if _DRIVE_LETTER_RE.search(line):
            # Trim line for display
            display = line.strip()[:120]
            violations.append(f"{path}:{lineno}: {display}")

    return violations


def main(argv: list[str]) -> int:
    if not argv:
        return 0

    all_violations: list[str] = []
    for arg in argv:
        all_violations.extend(check_file(Path(arg)))

    if all_violations:
        for msg in all_violations:
            print(msg)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
