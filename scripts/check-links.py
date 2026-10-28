"""Check that every relative link/image in the repo's markdown files exists."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#")


def main() -> int:
    broken: list[tuple[Path, str]] = []
    checked = 0
    for doc in sorted(ROOT.rglob("*.md")):
        if any(part in {".venv", ".git", "node_modules"} for part in doc.parts):
            continue
        text = doc.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            if target.startswith(SKIP_PREFIXES):
                continue
            anchorless = target.split("#", 1)[0]
            if not anchorless:
                continue
            resolved = (doc.parent / anchorless).resolve()
            checked += 1
            if not resolved.exists():
                broken.append((doc.relative_to(ROOT), target))

    print(f"checked {checked} relative links in markdown files")
    for doc, target in broken:
        print(f"MISSING {doc}: {target}")
    print("all good" if not broken else f"{len(broken)} broken link(s)")
    return 0 if not broken else 1


if __name__ == "__main__":
    raise SystemExit(main())
