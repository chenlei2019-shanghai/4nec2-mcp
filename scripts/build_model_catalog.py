#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""Scan NEC model directories and build a searchable model catalog.

默认扫描 ``C:\4nec2\G0KSC\models\antennas``（G0KSC 参考模型库，372 个 .nec）
和 ``C:\4nec2\models``（4NEC2 自带示例），为每个模型提取：频率、单元数、
boom 长度、最大单元长度、导线半径、类型（lfa/owa/owl/quad/duo/triband…），
写成一份 Markdown 目录（``docs/模型目录.md``）和一份 JSON 索引
（``data/model_catalog.json``），供设计新天线时先查现有参考模型。

用法::

    python scripts\\build_model_catalog.py                 # 默认目录
    python scripts\\build_model_catalog.py --root <dir>    # 只扫一个目录
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nec2core.model import parse_deck  # noqa: E402

DEFAULT_ROOTS = [
    (r"C:\4nec2\G0KSC\models\antennas", "G0KSC 参考模型库"),
    (r"C:\4nec2\models", "4NEC2 自带示例"),
]

_TYPE_KEYWORDS = [
    ("triband", "三波段"),
    ("dualband", "双波段"),
    ("duo", "双波段"),
    ("quad", "quad"),
    ("lfa", "LFA（折合驱动）"),
    ("owl", "OWL"),
    ("owa", "OWA"),
    ("plus2", "LFA+2"),
]

# 业余/常用频点 → 波段名；用解析出的真实 FR 频率归一，而不是猜文件名。
_BAND_TABLE = [
    (1.9, "160 m"), (3.7, "80 m"), (7.1, "40 m"), (10.1, "30 m"),
    (14.2, "20 m"), (18.1, "17 m"), (21.2, "15 m"), (24.9, "12 m"),
    (28.5, "10 m"), (50.2, "6 m"), (70.2, "4 m"), (73.0, "射电天文"),
    (144.3, "2 m"), (222.0, "1.25 m"), (406.0, "射电天文"), (432.2, "70 cm"),
]


def band_label(freq_mhz: float | None) -> str:
    if not freq_mhz:
        return ""
    best = min(_BAND_TABLE, key=lambda b: abs(b[0] - freq_mhz))
    return best[1] if abs(best[0] - freq_mhz) / best[0] < 0.05 else f"{freq_mhz:.1f} MHz"


def classify(name: str, freq_mhz: float | None) -> tuple[str, str]:
    low = name.lower()
    kind = "yagi"
    for key, label in _TYPE_KEYWORDS:
        if key in low:
            kind = label
            break
    return kind, band_label(freq_mhz)


def describe(path: Path, root: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        model = parse_deck(text)
    except Exception as exc:  # noqa: BLE001
        return {"file": str(path.relative_to(root)), "error": str(exc)}
    if not model.wires:
        return None
    xs = [w.x1 for w in model.wires] + [w.x2 for w in model.wires]
    ys = [abs(w.y1) for w in model.wires] + [abs(w.y2) for w in model.wires]
    zs = [w.z1 for w in model.wires] + [w.z2 for w in model.wires]

    def span(vals: list[float]) -> float:
        return max(vals) - min(vals) if vals else 0.0

    boom = max(span(xs), span(zs))
    longest = max(w.length_m for w in model.wires)
    radii = sorted({round(w.radius, 6) for w in model.wires})
    # LFA 折合驱动 = 同一条 boom 位置上并排的 2 根导线；按 boom 位置去重才是真实单元数
    positions = sorted({round((w.x1 + w.x2) / 2.0 + (w.z1 + w.z2) / 2.0, 2) for w in model.wires})
    freq = model.frequencies_mhz[0] if model.frequencies_mhz else None
    kind, band = classify(path.name, freq)
    return {
        "file": str(path.relative_to(root)),
        "comment": model.comment or "",
        "freq_mhz": freq,
        "elements": len(positions),
        "segments": model.total_segments,
        "boom_m": round(boom, 3),
        "longest_element_m": round(longest, 3),
        "radius_mm": [round(r * 1000, 2) for r in radii],
        "type": kind,
        "band_hint": band,
        "excitations": len(model.excitations),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the NEC model catalog")
    parser.add_argument("--root", action="append", help="directory to scan (repeatable)")
    parser.add_argument("--md", default=str(Path(__file__).resolve().parents[1] / "docs" / "模型目录.md"))
    parser.add_argument("--json", default=str(Path(__file__).resolve().parents[1] / "data" / "model_catalog.json"))
    args = parser.parse_args()

    roots = [(Path(r), label) for r, label in DEFAULT_ROOTS] if not args.root else [(Path(r), Path(r).name) for r in args.root]

    catalog: dict[str, list[dict]] = {}
    errors: list[dict] = []
    for root, label in roots:
        if not root.is_dir():
            continue
        entries: list[dict] = []
        for path in sorted(root.glob("*.nec")):
            entry = describe(path, root)
            if entry is None:
                continue
            if "error" in entry:
                errors.append({"root": label, **entry})
                continue
            entries.append(entry)
        catalog[label] = entries

    total = sum(len(v) for v in catalog.values())
    lines = [
        "# NEC 模型目录（自动生成）",
        "",
        "> 由 `scripts/build_model_catalog.py` 生成。设计新天线前先在这里找接近的参考模型，",
        "> 优先直接采用现有几何再按需微调，而不是从零画。",
        "> ⚠️ G0KSC 库的卡片头部注明 *auto-generated from PDF dimensions; verify before building*，",
        "> 个别文件的单元数/频率与文件名不符（复用前先用 `nec_describe` 核对几何、用 `nec_run` 核对指标）。",
        "",
        f"共 **{total}** 个模型。`boom` 为 x/z 两轴中较大的展布，`最长单元` 为最大振子全长。",
        "",
    ]
    for label, entries in catalog.items():
        lines.append(f"## {label}（{len(entries)} 个）")
        lines.append("")
        lines.append("| 文件 | 类型 | 波段提示 | 频率 (MHz) | 单元 | boom (m) | 最长单元 (m) | 半径 (mm) |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for e in entries:
            freq = f"{e['freq_mhz']:.3f}" if e["freq_mhz"] else "—"
            lines.append(
                f"| `{e['file']}` | {e['type']} | {e['band_hint']} | {freq} | "
                f"{e['elements']} | {e['boom_m']} | {e['longest_element_m']} | {e['radius_mm']} |"
            )
        lines.append("")
    if errors:
        lines.append(f"## 解析失败（{len(errors)} 个）")
        lines.append("")
        for e in errors:
            lines.append(f"- `{e['file']}`：{e['error']}")
        lines.append("")

    md_path = Path(args.md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")

    json_path = Path(args.json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps({"catalog": catalog, "errors": errors}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"cataloged {total} models, {len(errors)} parse failures")
    print(f"md   -> {md_path}")
    print(f"json -> {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
