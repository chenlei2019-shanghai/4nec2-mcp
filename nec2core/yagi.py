"""Yagi-Uda geometry model builder (NEC-2 card decks).

The generated model uses the "classic 4NEC2" convention:

* every element is a straight wire along the **Y** axis, centred on ``x = 0``
  and placed at its boom position ``z`` (the boom runs along Z),
* the first element (tag 1) is the reflector, the driven element is fed at its
  centre by a single voltage source (``EX 0 <tag> <mid segment> 0 1 0``),
* the deck ends with two radiation-pattern requests: an azimuth cut at
  ``theta = 90`` and an elevation cut at ``phi = 90``.

Module: ``nec2core/yagi.py``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .model import ModelError, parse_deck

__all__ = ["YagiElement", "YagiDesign", "LIGHT_SPEED_MHZ_M"]

# 300 / f(MHz) = wavelength in metres (speed of light rounded, as 4NEC2 does).
LIGHT_SPEED_MHZ_M = 300.0

# Classic Yagi length factors, expressed in wavelengths.  The values assume a
# uniform boom with roughly 0.2 lambda element spacing: longer elements detune
# the array badly (the reflector starts to dominate and peak gain drops to
# ~5 dBi), so they are kept close to the self-resonant length.
REFLECTOR_FACTOR = 0.460
DRIVEN_FACTOR = 0.430
FIRST_DIRECTOR_FACTOR = 0.420
LAST_DIRECTOR_FACTOR = 0.370
MIN_ELEMENT_SPACING_LAMBDA = 0.05
# Default element spacing of :meth:`YagiDesign.classic` in wavelengths.
DEFAULT_SPACING_LAMBDA = 0.20

_ASCII_SAFE = str.maketrans({chr(c): "?" for c in range(0x80, 0x10000)})


def _ascii(text: str) -> str:
    """Strip non-ASCII characters (the NEC engines are ANSI programs)."""
    return text.translate(_ASCII_SAFE)


def _fmt(value: float, digits: int = 6) -> str:
    """Format a number compactly but without losing tuning precision."""
    return f"{value:.{digits}g}"


@dataclass
class YagiElement:
    """One parasitic/driven element: a straight wire centred on the boom."""

    length_m: float
    position_m: float

    @property
    def half_length_m(self) -> float:
        return self.length_m / 2.0


@dataclass
class YagiDesign:
    """A complete Yagi-Uda design that can be rendered as a NEC-2 deck."""

    freq_mhz: float
    elements: list[YagiElement] = field(default_factory=list)
    radius_m: float = 0.004
    # 1-based element number that carries the feed point; 1 is the reflector,
    # so 2 (the driven element right after it) is the physically correct default.
    feed_element_index: int = 2
    segments: int = 7
    ground: str = "free"
    comment: str = ""

    # -- derived geometry ------------------------------------------------
    @property
    def wavelength_m(self) -> float:
        """Free-space wavelength in metres."""
        return LIGHT_SPEED_MHZ_M / self.freq_mhz if self.freq_mhz else 0.0

    @property
    def element_count(self) -> int:
        return len(self.elements)

    @property
    def boom_length_m(self) -> float:
        if len(self.elements) < 2:
            return 0.0
        return self.elements[-1].position_m - self.elements[0].position_m

    @property
    def feed_element(self) -> YagiElement | None:
        idx = self.feed_element_index
        if 1 <= idx <= len(self.elements):
            return self.elements[idx - 1]
        return None

    # -- constructors ----------------------------------------------------
    @classmethod
    def classic(
        cls,
        freq_mhz: float,
        n_elements: int,
        boom_length_m: float | None = None,
        radius_m: float = 0.004,
        ground: str = "free",
        feed_element_index: int = 2,
        segments: int = 7,
        comment: str = "",
    ) -> "YagiDesign":
        """Build a classic tapered Yagi for ``n_elements`` on a uniform boom.

        Reflector ~0.46 lambda, driven ~0.43 lambda and directors tapering
        from ~0.42 lambda down to ~0.37 lambda, on a uniform boom with 0.2
        lambda element spacing by default (the taper is softened for tighter
        booms, where nearly equal directors are more appropriate).
        Element positions are evenly spaced along the boom, first element at
        ``z = 0`` (reflector).

        Raises:
            ValueError: when fewer than two elements are requested.
        """
        n_elements = int(n_elements)
        if n_elements < 2:
            raise ValueError("a Yagi needs at least 2 elements (reflector + driven)")
        freq_mhz = float(freq_mhz)
        if freq_mhz <= 0:
            raise ValueError("freq_mhz must be positive")
        lam = LIGHT_SPEED_MHZ_M / freq_mhz
        if boom_length_m is None or float(boom_length_m) <= 0:
            boom_length_m = DEFAULT_SPACING_LAMBDA * (n_elements - 1) * lam
        boom_length_m = float(boom_length_m)

        spacing = boom_length_m / (n_elements - 1)
        # Directors shrink with element spacing: a wide boom tolerates a strong
        # taper while a tight one needs nearly equal directors to stay tuned.
        taper = min(1.0, max(0.0, (spacing / lam) / DEFAULT_SPACING_LAMBDA))
        last_factor = FIRST_DIRECTOR_FACTOR - (FIRST_DIRECTOR_FACTOR - LAST_DIRECTOR_FACTOR) * taper

        n_directors = n_elements - 2
        lengths: list[float] = [REFLECTOR_FACTOR * lam, DRIVEN_FACTOR * lam]
        for i in range(n_directors):
            if n_directors <= 1:
                factor = FIRST_DIRECTOR_FACTOR
            else:
                t = i / (n_directors - 1)
                factor = FIRST_DIRECTOR_FACTOR + (last_factor - FIRST_DIRECTOR_FACTOR) * t
            lengths.append(factor * lam)

        # Keep the full precision here so a uniformly spaced boom whose gaps sit
        # exactly on the minimum spacing still validates; ``to_nec`` rounds the
        # values it writes to the deck.
        elements = [YagiElement(length_m=length, position_m=i * spacing)
                    for i, length in enumerate(lengths)]
        return cls(
            freq_mhz=freq_mhz,
            elements=elements,
            radius_m=float(radius_m),
            feed_element_index=int(feed_element_index),
            segments=int(segments),
            ground=str(ground),
            comment=comment,
        )

    @classmethod
    def from_nec(cls, deck: str) -> "YagiDesign":
        """Rebuild a :class:`YagiDesign` from an existing Yagi-style deck.

        The deck must use one GW card per element, all parallel and ordered by
        tag; the element position is taken from the wire centre along Z.
        """
        model = parse_deck(deck)
        if len(model.wires) < 2:
            raise ModelError("deck does not look like a Yagi: fewer than 2 wires")
        if not model.frequencies_mhz:
            raise ModelError("deck has no FR card; cannot infer the frequency")
        wires = sorted(model.wires, key=lambda w: w.tag)
        ground_code = model.ground.get("code", -1) if model.ground else -1
        ground = "free" if ground_code == -1 else "perfect"
        feed_index = 1
        if model.excitations:
            feed_index = int(model.excitations[0].get("tag") or 1)
        return cls(
            freq_mhz=float(model.frequencies_mhz[0]),
            elements=[YagiElement(length_m=w.length_m, position_m=w.center[2]) for w in wires],
            radius_m=wires[0].radius,
            feed_element_index=feed_index,
            segments=wires[0].segments,
            ground=ground,
            comment="rebuilt from deck",
        )

    # -- validation ------------------------------------------------------
    def validate(self) -> list[str]:
        """Return a list of human-readable geometry problems (empty when OK)."""
        problems: list[str] = []
        if not self.freq_mhz or self.freq_mhz <= 0:
            problems.append("freq_mhz must be positive")
        if len(self.elements) < 2:
            problems.append("at least 2 elements are required (reflector + driven)")
        if self.radius_m <= 0:
            problems.append("radius_m must be positive")
        if self.segments < 3 or self.segments % 2 == 0:
            problems.append("segments must be an odd number >= 3 (centre-fed element)")
        if not (1 <= self.feed_element_index <= len(self.elements)):
            problems.append(
                f"feed_element_index {self.feed_element_index} is outside 1..{len(self.elements)}"
            )
        if self.ground not in ("free", "perfect"):
            problems.append("ground must be 'free' or 'perfect'")

        lam = self.wavelength_m
        for i, element in enumerate(self.elements, start=1):
            if element.length_m <= 0:
                problems.append(f"element {i}: length_m must be positive")
            if element.length_m > 0.75 * lam:
                problems.append(f"element {i}: length {element.length_m:.3f} m is longer than 0.75 lambda")
            if self.radius_m > 0 and element.length_m / self.radius_m < 20:
                problems.append(f"element {i}: length/radius ratio is below 20, the thin-wire kernel is inaccurate")

        positions = [e.position_m for e in self.elements]
        minimum_gap = MIN_ELEMENT_SPACING_LAMBDA * lam if lam else 0.0
        for i, (a, b) in enumerate(zip(positions, positions[1:]), start=2):
            gap = b - a
            if gap <= 0:
                problems.append(
                    f"element {i}: position {b:.4f} m is not beyond element {i - 1} ({a:.4f} m)"
                )
            elif minimum_gap and gap < minimum_gap * (1.0 - 1e-6):
                # Compare with a relative tolerance so a uniformly spaced boom
                # that lands exactly on the minimum (e.g. the 0.35 lambda
                # default) is not rejected by floating-point noise.
                problems.append(
                    f"element {i}: spacing {gap:.4f} m ({gap / lam:.4f} lambda) is below the "
                    f"{MIN_ELEMENT_SPACING_LAMBDA:g} lambda minimum ({minimum_gap:.4f} m)"
                )
        return problems

    # -- rendering -------------------------------------------------------
    def to_nec(self) -> str:
        """Render the design as a standard NEC-2 card deck."""
        if self.freq_mhz <= 0:
            raise ValueError("freq_mhz must be positive")
        if self.segments < 3:
            raise ValueError("segments must be >= 3")
        if not (1 <= self.feed_element_index <= len(self.elements)):
            raise ValueError("feed_element_index is outside the element list")

        # NEC-2 accepts a comment block only at the very start of the deck and
        # terminates it with CE; a CE followed by CM is a hard "card error" in
        # the 4NEC2 engines, so the CM lines come first and CE closes them.
        title = self.comment or f"{len(self.elements)}-element Yagi {self.freq_mhz:g} MHz"
        comments = [
            title,
            f"Yagi-Uda: {len(self.elements)} elements, boom {self.boom_length_m:.4f} m, "
            f"freq {self.freq_mhz:g} MHz",
            f"reflector {self.elements[0].length_m:.4f} m, driven "
            f"{self.elements[self.feed_element_index - 1].length_m:.4f} m, radius {self.radius_m:.4f} m",
            "element lengths (m): " + ", ".join(f"{e.length_m:.4f}" for e in self.elements),
        ]
        lines: list[str] = []
        for text in comments:
            lines.append(f"CM {_ascii(text)[:120]}")
        lines.append("CE")

        for tag, element in enumerate(self.elements, start=1):
            half = element.length_m / 2.0
            z = element.position_m
            lines.append(
                "GW {tag} {segs} 0 {y1} {z} 0 {y2} {z} {rad}".format(
                    tag=tag,
                    segs=self.segments,
                    y1=_fmt(-half),
                    z=_fmt(z),
                    y2=_fmt(half),
                    rad=_fmt(self.radius_m),
                )
            )

        lines.append("GE 0")
        lines.append("GN -1" if self.ground == "free" else "GN 1")
        lines.append("EK")
        mid_segment = (self.segments + 1) // 2
        lines.append(f"EX 0 {self.feed_element_index} {mid_segment} 0 1 0")
        lines.append(f"FR 0 0 0 0 {_fmt(self.freq_mhz)} 0")
        # Azimuth cut at theta = 90 deg, 5 deg steps.
        lines.append("RP 0 1 73 1000 90 0 0 5 0 0")
        # Elevation cut at phi = 90 deg, 5 deg steps.
        lines.append("RP 0 37 1 1000 0 90 5 0 0 0")
        lines.append("EN")
        return "\n".join(lines) + "\n"

    # -- convenience -----------------------------------------------------
    def describe(self) -> dict:
        """JSON-friendly description of the design (for the MCP tools)."""
        return {
            "freq_mhz": self.freq_mhz,
            "wavelength_m": round(self.wavelength_m, 5),
            "boom_length_m": round(self.boom_length_m, 5),
            "radius_m": self.radius_m,
            "segments": self.segments,
            "feed_element_index": self.feed_element_index,
            "ground": self.ground,
            "elements": [
                {
                    "index": i,
                    "length_m": round(e.length_m, 5),
                    "position_m": round(e.position_m, 5),
                    "length_lambda": round(e.length_m / self.wavelength_m, 4) if self.wavelength_m else None,
                }
                for i, e in enumerate(self.elements, start=1)
            ],
        }
