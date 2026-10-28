"""Matplotlib figure rendering for NEC-2 results.

Three kinds of figures are produced:

* ``polar_pattern``  – elevation (fixed phi) and azimuth (fixed theta) cuts on
  a polar grid, in dBi.
* ``pattern_3d``     – a 3-D radiation lobe built from a full spherical grid
  (an ``RP`` card with both NTH and NPH > 1), coloured by gain.
* ``sweep_dashboard``– SWR, gain/front-to-back, R/X and a Smith chart versus
  frequency.

The module imports matplotlib lazily so the MCP server still starts (and all
parsing/engine tools still work) on machines without matplotlib installed.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Sequence

from .metrics import reflection_coefficient
from .output import FrequencyBlock, PatternSample

#: Everything below ``peak - DYNAMIC_RANGE_DB`` is clipped so the null of a
#: Yagi does not drag the polar grid to -999 dBi.
DYNAMIC_RANGE_DB = 40.0

_FONT_CANDIDATES = ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans")

_COLUMN_LABELS = {"total": "总增益", "vertical": "垂直极化", "horizontal": "水平极化"}


def _plt():
    """Import matplotlib with a non-interactive backend and CJK-capable font."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import rcParams

    rcParams["font.sans-serif"] = list(_FONT_CANDIDATES)
    rcParams["font.family"] = "sans-serif"
    rcParams["axes.unicode_minus"] = False
    rcParams["figure.autolayout"] = False
    return plt


def matplotlib_available() -> bool:
    """True when the optional plotting dependency can be imported."""
    try:
        import matplotlib  # noqa: F401
    except Exception:  # pragma: no cover - depends on the environment
        return False
    return True


def preload() -> dict:
    """Import matplotlib (and therefore numpy) eagerly, in the calling thread.

    The MCP server must call this from its **main** thread before the transport
    starts.  Importing numpy's C extension for the first time inside an anyio
    worker thread deadlocks on Windows: the import blocks forever on the loader
    lock while the event-loop thread sits in the stdin read, and the tool call
    only unwinds when the client gives up and cancels.  Priming the import from
    the main thread costs a few hundred milliseconds and keeps every
    ``nec_plot_*`` call fast and responsive.

    Returns a small status dict; never raises.
    """
    import time

    start = time.time()
    try:
        plt = _plt()
    except Exception as exc:  # pragma: no cover - depends on the environment
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "seconds": round(time.time() - start, 2),
        "matplotlib": plt.matplotlib.__version__,
        "backend": plt.matplotlib.get_backend(),
    }


def _ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _gain(sample: PatternSample, column: str) -> float | None:
    if column == "total":
        return sample.total_db
    if column == "vertical":
        return sample.vert_db
    if column == "horizontal":
        return sample.hor_db
    raise ValueError(f"unknown gain column {column!r}")


def _cut(
    block: FrequencyBlock, kind: str, fixed_deg: float | None, column: str
) -> tuple[float, list[tuple[float, float]]]:
    """Return ``(actual_fixed_angle, [(angle_deg, gain_db), ...])``.

    ``kind="elevation"`` fixes phi and sweeps theta; the plotted angle is NEC's
    theta itself (0° = along +Z, i.e. the boom axis for a Yagi, 90° = broadside
    to +Z). ``kind="azimuth"`` fixes theta and sweeps phi; the angle is the
    azimuth phi (0° = +X, 90° = +Y).
    """
    if kind not in {"elevation", "azimuth"}:
        raise ValueError("kind must be 'elevation' or 'azimuth'")

    if kind == "elevation":
        groups: dict[float, list[tuple[float, float]]] = {}
        for sample in block.patterns:
            gain = _gain(sample, column)
            if gain is None:
                continue
            groups.setdefault(round(sample.phi_deg, 3), []).append(
                (sample.theta_deg, gain)
            )
        if not groups:
            raise ValueError("no radiation pattern samples in this frequency block")
        target = 90.0 if fixed_deg is None else fixed_deg
        actual = min(groups, key=lambda key: abs(key - target))
        pairs = sorted(groups[actual])
    else:
        groups = {}
        for sample in block.patterns:
            gain = _gain(sample, column)
            if gain is None:
                continue
            groups.setdefault(round(sample.theta_deg, 3), []).append(
                (sample.phi_deg, gain)
            )
        if not groups:
            raise ValueError("no radiation pattern samples in this frequency block")
        target = 90.0 if fixed_deg is None else fixed_deg
        actual = min(groups, key=lambda key: abs(key - target))
        pairs = sorted(groups[actual])

    peak = max(value for _, value in pairs)
    floor = max(peak - DYNAMIC_RANGE_DB, -200.0)
    return actual, [(angle, max(value, floor)) for angle, value in pairs]


def sphere_grid_card(nth: int = 37, nph: int = 73, dth: int = 5, dph: int = 5) -> str:
    """Return an ``RP`` card requesting a full spherical pattern grid."""
    return f"RP 0 {nth} {nph} 1000 0 0 {dth} {dph}"


def with_sphere_grid(
    deck: str, nth: int = 37, nph: int = 73, dth: int = 5, dph: int = 5
) -> str:
    """Replace every ``RP`` card with one full-sphere grid (needed for 3-D)."""
    lines = deck.splitlines()
    kept = [line for line in lines if not line.lstrip().upper().startswith("RP")]
    if not any(line.strip().upper() == "EN" for line in kept):
        kept.append("EN")
    result: list[str] = []
    for line in kept:
        if line.strip().upper() == "EN":
            result.append(sphere_grid_card(nth, nph, dth, dph))
        result.append(line)
    return "\n".join(result) + "\n"


def polar_pattern(
    block: FrequencyBlock,
    out_path: str | Path,
    *,
    planes: Sequence[str] = ("elevation", "azimuth"),
    phi_deg: float | None = 90.0,
    theta_deg: float | None = 90.0,
    column: str = "total",
    title: str | None = None,
    dpi: int = 140,
) -> dict:
    """Render elevation and/or azimuth cuts of one frequency block to a PNG."""
    plt = _plt()

    requested = [plane for plane in planes if plane in {"elevation", "azimuth"}]
    if not requested:
        raise ValueError("planes must contain 'elevation' and/or 'azimuth'")

    path = _ensure_parent(Path(out_path))

    cuts: dict[str, tuple[float, list[tuple[float, float]]]] = {}
    if "elevation" in requested:
        cuts["elevation"] = _cut(block, "elevation", phi_deg, column)
    if "azimuth" in requested:
        cuts["azimuth"] = _cut(block, "azimuth", theta_deg, column)

    peak = max(value for _, pairs in cuts.values() for _, value in pairs)
    floor = max(peak - DYNAMIC_RANGE_DB, -200.0)

    fig = plt.figure(figsize=(5.6 * len(requested) + 0.7, 5.4), layout="constrained")
    fig.suptitle(
        title
        or f"辐射方向图 @ {block.freq_mhz:.3f} MHz（{_COLUMN_LABELS.get(column, column)}，dBi）",
        fontsize=13,
    )

    labels = {
        "elevation": lambda actual: f"仰角面（φ={actual:.0f}°，0°=天顶（+Z），90°=水平面）",
        "azimuth": lambda actual: f"方位面（θ={actual:.0f}°，0°=+X 轴）",
    }
    ring_values = _ring_ticks(floor, peak)
    for index, kind in enumerate(requested):
        actual, pairs = cuts[kind]
        ax = fig.add_subplot(1, len(requested), index + 1, projection="polar")
        angles = [math.radians(angle) for angle, _ in pairs]
        values = [value for _, value in pairs]
        ax.plot(angles + angles[:1], values + values[:1], color="#1f4fd8", linewidth=1.9)
        peak_angle, peak_value = max(pairs, key=lambda pair: pair[1])
        peak_theta = math.radians(peak_angle)
        ax.plot([peak_theta], [peak_value], marker="o", color="#c62828", markersize=5, zorder=5)
        ax.annotate(
            f"{peak_value:.2f} dBi",
            (peak_theta, peak_value),
            textcoords="offset points",
            xytext=(6, 6),
            fontsize=8,
            color="#c62828",
        )
        ax.set_theta_zero_location("N")
        ax.set_theta_direction(-1)
        ax.set_ylim(floor, peak)
        ax.set_yticks(ring_values)
        ax.set_yticklabels([f"{value:.0f}" for value in ring_values])
        ax.set_title(labels[kind](actual), fontsize=10, pad=8)
        ax.grid(True, linestyle=":", alpha=0.6)
        ax.tick_params(axis="y", labelsize=7)
        ax.set_rlabel_position(112.5)

    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    payload = {
        "path": str(path),
        "freq_mhz": block.freq_mhz,
        "peak_gain_db": peak,
        "dpi": dpi,
        "planes": requested,
    }
    if "elevation" in cuts:
        payload["elevation_fixed_phi_deg"] = cuts["elevation"][0]
    if "azimuth" in cuts:
        payload["azimuth_fixed_theta_deg"] = cuts["azimuth"][0]
    return payload


def elevation_comparison_polar(
    curves: Sequence[dict],
    out_path: str | Path,
    *,
    title: str | None = None,
    dpi: int = 140,
    dynamic_range_db: float = 30.0,
) -> dict:
    """Overlay several elevation cuts (0° = horizon, 90° = zenith) on one polar plot.

    ``curves`` is a sequence of dicts with ``label``, optional ``color`` and
    ``pairs`` = ``[(elevation_deg, gain_db), ...]`` (elevation above the ground
    plane, i.e. ``90 - theta`` for a horizontal antenna). Only 0–90° is drawn,
    which is what matters for a horizontal antenna over ground.
    """
    plt = _plt()

    usable = [
        curve
        for curve in curves
        if any(0.0 <= angle <= 90.0 for angle, _ in curve.get("pairs", []))
    ]
    if not usable:
        raise ValueError("no elevation samples between 0° and 90° to plot")

    path = _ensure_parent(Path(out_path))
    peak = max(value for curve in usable for _, value in curve["pairs"])
    floor = max(peak - dynamic_range_db, -200.0)
    ring_values = _ring_ticks(floor, peak, 5.0)

    fig = plt.figure(figsize=(12.6, 5.8), layout="constrained")
    ax = fig.add_subplot(1, 2, 1, projection="polar")
    ax.set_theta_zero_location("E")
    ax.set_theta_direction(1)
    ax.set_thetamin(0.0)
    ax.set_thetamax(90.0)
    ax_cart = fig.add_subplot(1, 2, 2)

    handles: list[object] = []
    legend_labels: list[str] = []
    for curve in usable:
        pairs = sorted(
            (angle, max(value, floor))
            for angle, value in curve["pairs"]
            if 0.0 <= angle <= 90.0
        )
        color = curve.get("color")
        (line,) = ax.plot(
            [math.radians(angle) for angle, _ in pairs],
            [value for _, value in pairs],
            color=color,
            linewidth=1.9,
            label=curve["label"],
        )
        handles.append(line)
        best_angle, best_value = max(
            ((angle, value) for angle, value in curve["pairs"] if 0.0 <= angle <= 90.0),
            key=lambda pair: pair[1],
        )
        ax.plot([math.radians(best_angle)], [best_value], marker="o", color=color, markersize=4.5)
        legend_labels.append(f"{curve['label']}：{best_value:.2f} dBi @ {best_angle:.0f}°")

        clipped = [(angle, max(value, floor)) for angle, value in pairs if angle <= 60.0]
        ax_cart.plot(
            [angle for angle, _ in clipped],
            [value for _, value in clipped],
            color=color,
            linewidth=1.9,
        )
        ax_cart.plot([best_angle], [best_value], marker="o", color=color, markersize=4.5)

    ax.set_ylim(floor, peak)
    ax.set_yticks(ring_values)
    ax.set_yticklabels([f"{value:.0f}" for value in ring_values])
    ax.tick_params(axis="y", labelsize=7)
    ax.set_rlabel_position(22.5)
    ax.set_thetagrids([0, 15, 30, 45, 60, 75, 90])
    ax.set_title("极坐标（0°=水平面，90°=天顶）", fontsize=10, pad=16)
    ax.grid(True, linestyle=":", alpha=0.6)

    ax_cart.set_title("仰角剖面（0–60°）", fontsize=10)
    ax_cart.set_xlabel("仰角 (°)")
    ax_cart.set_ylabel("增益 (dBi)")
    ax_cart.set_xlim(0.0, 60.0)
    ax_cart.set_ylim(floor, peak + 1.0)
    ax_cart.grid(True, linestyle=":", alpha=0.6)
    fig.suptitle(title or "仰角方向图对比", fontsize=13)
    fig.legend(
        handles,
        legend_labels,
        loc="outside lower center",
        ncol=2,
        fontsize=8.5,
        frameon=False,
    )

    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return {
        "path": str(path),
        "peak_gain_db": peak,
        "curves": [curve["label"] for curve in usable],
        "dpi": dpi,
    }


def _ring_ticks(floor: float, peak: float, step_db: float = 10.0) -> list[float]:
    """Round dB rings (every ``step_db``) strictly inside ``[floor, peak]``."""
    start = math.ceil(floor / step_db) * step_db
    stop = math.floor(peak / step_db) * step_db
    if stop <= start:
        return [floor, peak]
    ticks = [float(value) for value in range(int(start), int(stop) + 1, int(step_db))]
    return ticks or [floor, peak]


def _sphere_grid(block: FrequencyBlock) -> tuple[list[float], list[float], list[float]]:
    samples = [sample for sample in block.patterns if sample.total_db is not None]
    if not samples:
        raise ValueError("no radiation pattern samples in this frequency block")
    thetas = sorted({round(sample.theta_deg, 3) for sample in samples})
    phis = sorted({round(sample.phi_deg, 3) for sample in samples})
    if len(thetas) < 3 or len(phis) < 3:
        raise ValueError(
            "3-D 方向图需要完整球面网格：请在 deck 里用 "
            "`RP 0 37 73 1000 0 0 5 5` 这类同时给出 NTH 与 NPH 的 RP 卡"
        )
    table = {
        (round(sample.theta_deg, 3), round(sample.phi_deg, 3)): sample.total_db
        for sample in samples
    }
    theta_grid, phi_grid, gain_grid = [], [], []
    for theta in thetas:
        for phi in phis:
            value = table.get((theta, phi))
            if value is None:
                raise ValueError("球面网格不完整，无法绘制 3-D 方向图")
            theta_grid.append(theta)
            phi_grid.append(phi)
            gain_grid.append(value)
    return theta_grid, phi_grid, gain_grid


def pattern_3d(
    block: FrequencyBlock,
    out_path: str | Path,
    *,
    title: str | None = None,
    dpi: int = 150,
    views: Sequence[tuple[float, float]] = ((22.0, -58.0),),
) -> dict:
    """Render a 3-D lobe from a full spherical grid to a PNG."""
    plt = _plt()
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401  (registers the 3d projection)
    from matplotlib import cm, colors

    thetas, phis, gains = _sphere_grid(block)
    path = _ensure_parent(Path(out_path))

    peak = max(gains)
    floor = max(peak - DYNAMIC_RANGE_DB, -200.0)

    # Normalise radius 0..1 so nulls collapse to the origin and the lobe keeps
    # its shape; colour still carries the true dBi value.
    radii = [(max(gain, floor) - floor) / (peak - floor) if peak > floor else 1.0 for gain in gains]
    norm = colors.Normalize(vmin=floor, vmax=peak)

    points: list[list[tuple[float, float, float]]] = []
    for theta, phi, radius in zip(thetas, phis, radii):
        theta_r, phi_r = math.radians(theta), math.radians(phi)
        points.append(
            [
                radius * math.sin(theta_r) * math.cos(phi_r),
                radius * math.sin(theta_r) * math.sin(phi_r),
                radius * math.cos(theta_r),
            ]
        )

    figure_index = 1
    n_views = max(1, len(views))
    fig = plt.figure(figsize=(6.4 * n_views, 6.2))
    for elev, azim in views:
        ax = fig.add_subplot(1, n_views, figure_index, projection="3d")
        figure_index += 1
        phi_unique = sorted({round(phi, 3) for phi in phis})
        theta_unique = sorted({round(theta, 3) for theta in thetas})
        nx, ny = len(theta_unique), len(phi_unique)
        xs = [points[row * ny + col][0] for row in range(nx) for col in range(ny)]
        ys = [points[row * ny + col][1] for row in range(nx) for col in range(ny)]
        zs = [points[row * ny + col][2] for row in range(nx) for col in range(ny)]

        import numpy as np

        shape = (len(theta_unique), len(phi_unique))
        x = np.array(xs).reshape(shape)
        y = np.array(ys).reshape(shape)
        z = np.array(zs).reshape(shape)
        gain_map = np.array(gains).reshape(shape)
        face = cm.turbo(norm(gain_map))
        ax.plot_surface(x, y, z, facecolors=face, linewidth=0, antialiased=True, shade=False)
        limit = 1.05
        ax.set_xlim(-limit, limit)
        ax.set_ylim(-limit, limit)
        ax.set_zlim(-limit, limit)
        ax.set_box_aspect((1, 1, 1))
        ax.set_xlabel("X")
        ax.set_ylabel("Y（振子方向）")
        ax.set_zlabel("Z（boom 方向）")
        ax.view_init(elev=elev, azim=azim)
        ax.set_title(f"峰值 {peak:.2f} dBi", fontsize=11)

    fig.suptitle(
        title or f"3-D 辐射方向图 @ {block.freq_mhz:.3f} MHz", fontsize=13
    )
    sm = cm.ScalarMappable(norm=norm, cmap=cm.turbo)
    cbar = fig.colorbar(sm, ax=fig.axes, fraction=0.02, pad=0.02)
    cbar.set_label("总增益 (dBi)")

    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return {"path": str(path), "freq_mhz": block.freq_mhz, "peak_gain_db": peak, "dpi": dpi}


def sweep_dashboard(
    rows: Iterable[dict],
    out_path: str | Path,
    *,
    z0: float = 50.0,
    title: str | None = None,
    dpi: int = 150,
) -> dict:
    """Render SWR / gain / impedance / Smith panels plus an efficiency curve.

    The bottom row is a full-width efficiency panel fed by the optional
    ``efficiency_percent`` key on each row; when fewer than two rows carry a
    usable value it degrades to a placeholder instead of raising.
    """
    plt = _plt()

    rows = [row for row in rows if row.get("swr") is not None or row.get("gain_max_db") is not None]
    if not rows:
        raise ValueError("no sweep rows to plot")
    path = _ensure_parent(Path(out_path))

    freqs = [row.get("freq_mhz") for row in rows]
    swr_values = [row.get("swr") for row in rows]
    gain_values = [row.get("gain_max_db") for row in rows]
    fb_values = [row.get("fb_db") for row in rows]
    r_values = [row.get("z_real") for row in rows]
    x_values = [row.get("z_imag") for row in rows]

    fig = plt.figure(figsize=(12.4, 11.4), layout="constrained")
    grid = fig.add_gridspec(3, 2, height_ratios=[1, 1, 0.8])
    axes = [
        [fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[0, 1])],
        [fig.add_subplot(grid[1, 0]), fig.add_subplot(grid[1, 1])],
    ]
    ax_efficiency = fig.add_subplot(grid[2, :])
    fig.suptitle(title or "扫频曲线", fontsize=14)

    ax = axes[0][0]
    ax.plot(freqs, swr_values, marker="o", color="#c62828", label="SWR")
    ax.axhline(2.0, color="#9e9e9e", linestyle="--", linewidth=1.0, label="SWR=2")
    ax.axhline(1.5, color="#bdbdbd", linestyle=":", linewidth=1.0, label="SWR=1.5")
    ax.set_title(f"驻波比 SWR（Z0 = {z0:g} Ω）")
    ax.set_xlabel("频率 (MHz)")
    ax.set_ylabel("SWR")
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="upper right", fontsize=9)

    ax = axes[0][1]
    ax.plot(freqs, gain_values, marker="s", color="#1565c0", label="最大增益 (dBi)")
    ax.set_title("增益")
    ax.set_xlabel("频率 (MHz)")
    ax.set_ylabel("增益 (dBi)")
    ax.grid(True, linestyle=":", alpha=0.6)
    if any(value is not None for value in fb_values):
        twin = ax.twinx()
        twin.plot(freqs, fb_values, marker="^", linestyle="--", color="#2e7d32", label="前后比 (dB)")
        twin.set_ylabel("前后比 (dB)", color="#2e7d32")
        twin.tick_params(axis="y", labelcolor="#2e7d32")
        lines, labels = ax.get_legend_handles_labels()
        twin_lines, twin_labels = twin.get_legend_handles_labels()
        ax.legend(lines + twin_lines, labels + twin_labels, loc="lower right", fontsize=9)
    else:
        ax.legend(loc="lower right", fontsize=9)

    ax = axes[1][0]
    ax.plot(freqs, r_values, marker="o", color="#6a1b9a", label="电阻 R")
    ax.plot(freqs, x_values, marker="x", color="#ef6c00", label="电抗 X")
    ax.axhline(0.0, color="#9e9e9e", linewidth=1.0)
    ax.set_title("输入阻抗")
    ax.set_xlabel("频率 (MHz)")
    ax.set_ylabel("阻抗 (Ω)")
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="upper left", fontsize=9)

    ax = axes[1][1]
    ax.set_title("Smith 圆图（Z0 = %g Ω）" % z0)
    ax.set_xlabel("反射系数 Γ 实部")
    ax.set_ylabel("反射系数 Γ 虚部")
    ax.set_aspect("equal")
    ax.set_xlim(-1.15, 1.15)
    ax.set_ylim(-1.15, 1.15)
    ax.axhline(0.0, color="#bdbdbd", linewidth=0.8)
    ax.axvline(0.0, color="#bdbdbd", linewidth=0.8)
    for radius, label in ((0.0, "SWR=1"), (1 / 3, "SWR=2"), (0.5, "SWR=3")):
        circle = plt.Circle((0, 0), radius, fill=False, color="#9e9e9e", linestyle=":", linewidth=0.9)
        ax.add_patch(circle)
        ax.annotate(
            label,
            (radius * math.cos(math.radians(60)), radius * math.sin(math.radians(60))),
            fontsize=8,
            color="#757575",
            ha="left",
            va="bottom",
        )
    ax.plot([-1, -1], [-0.6, 0.6], color="#616161", linewidth=1.2, linestyle="--")
    gamma_points = []
    for row in rows:
        if row.get("z_real") is None or row.get("z_imag") is None:
            continue
        gamma = reflection_coefficient(complex(row["z_real"], row["z_imag"]), z0)
        gamma_points.append((gamma.real, gamma.imag, row.get("freq_mhz")))
    if gamma_points:
        gx = [point[0] for point in gamma_points]
        gy = [point[1] for point in gamma_points]
        ax.plot(gx, gy, marker="o", color="#1565c0", linewidth=1.4)
        stride = max(1, len(gamma_points) // 8)
        for index, (gx_value, gy_value, freq) in enumerate(gamma_points):
            if index % stride and index != len(gamma_points) - 1:
                continue
            ax.annotate(f"{freq:g}", (gx_value, gy_value), fontsize=7, alpha=0.75)

    # --- efficiency row: spans both columns, degrades to a placeholder ---------
    ax_efficiency.set_title("效率 (导体损耗)")
    ax_efficiency.set_xlabel("频率 (MHz)")
    ax_efficiency.set_ylabel("效率 (%)")
    ax_efficiency.grid(True, linestyle=":", alpha=0.6)

    efficiency_points: list[tuple[float, float]] = []
    for row in rows:
        freq = row.get("freq_mhz")
        value = row.get("efficiency_percent")
        if freq is None or value is None:
            continue
        try:
            efficiency_points.append((float(freq), float(value)))
        except (TypeError, ValueError):
            continue

    efficiency_payload: dict[str, float | None] = {"min": None, "max": None}
    if len(efficiency_points) < 2:
        ax_efficiency.set_ylim(80.0, 100.0)
        ax_efficiency.set_yticks([80, 85, 90, 95, 100])
        known_freqs = [float(freq) for freq in freqs if freq is not None]
        if len(known_freqs) >= 2 and max(known_freqs) > min(known_freqs):
            ax_efficiency.set_xlim(min(known_freqs), max(known_freqs))
        ax_efficiency.text(
            0.5,
            0.5,
            "效率数据不可用",
            transform=ax_efficiency.transAxes,
            ha="center",
            va="center",
            fontsize=12,
            color="#9e9e9e",
        )
    else:
        eff_freqs = [point[0] for point in efficiency_points]
        eff_values = [point[1] for point in efficiency_points]
        ax_efficiency.plot(
            eff_freqs,
            eff_values,
            marker="o",
            markersize=4,
            linewidth=1.8,
            color="#f9a825",
            label="效率",
        )
        ax_efficiency.axhline(100.0, color="#f9a825", linewidth=1.2, alpha=0.35)
        eff_min, eff_max = min(eff_values), max(eff_values)
        ax_efficiency.set_ylim(max(80.0, eff_min - 0.4), 100.0)
        ax_efficiency.legend(loc="lower left", fontsize=9)
        min_index = min(range(len(eff_values)), key=lambda index: eff_values[index])
        near_top = eff_values[min_index] > 99.4
        at_right_edge = min_index == len(eff_values) - 1
        ax_efficiency.annotate(
            f"{eff_values[min_index]:.2f}%",
            (eff_freqs[min_index], eff_values[min_index]),
            textcoords="offset points",
            xytext=(-10, 10) if at_right_edge else ((8, -14) if near_top else (8, 8)),
            ha="right" if at_right_edge else "left",
            va="bottom",
            fontsize=8,
            color="#8d6e00",
        )
        efficiency_payload = {"min": float(eff_min), "max": float(eff_max)}

    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return {
        "path": str(path),
        "points": len(rows),
        "freq_range_mhz": [min(freqs), max(freqs)],
        "dpi": dpi,
        "efficiency_percent": efficiency_payload,
    }
