"""Guard the documentation: every relative link/image must point at a real file."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#")
SKIP_PARTS = {".venv", ".git", "node_modules", ".pytest_cache", "__pycache__"}


def _markdown_files() -> list[Path]:
    return [
        doc
        for doc in sorted(ROOT.rglob("*.md"))
        if not SKIP_PARTS.intersection(doc.relative_to(ROOT).parts)
    ]


def test_markdown_files_are_discovered():
    names = {doc.name for doc in _markdown_files()}
    assert "README.md" in names
    assert "showcase-triband.md" in names


def test_relative_links_resolve_to_existing_files():
    missing: list[str] = []
    for doc in _markdown_files():
        text = doc.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            if target.startswith(SKIP_PREFIXES):
                continue
            anchorless = target.split("#", 1)[0]
            if not anchorless:
                continue
            if not (doc.parent / anchorless).resolve().exists():
                missing.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert not missing, "broken relative link(s):\n" + "\n".join(missing)


def test_showcase_page_embeds_every_triband_figure():
    showcase = (ROOT / "docs" / "showcase-triband.md").read_text(encoding="utf-8")
    for name in (
        "geometry.png",
        "triband_10m_sweep.png",
        "triband_15m_sweep.png",
        "triband_20m_sweep.png",
        "triband_10m_polar.png",
        "triband_15m_polar.png",
        "triband_20m_polar.png",
        "triband_10m_3d.png",
        "triband_15m_3d.png",
        "triband_20m_3d.png",
        "triband_10m_elevation_vs_height.png",
    ):
        assert f"images/triband/{name}" in showcase, f"{name} missing from showcase"
