"""NEC-2 engine discovery and execution.

The engines shipped with 4NEC2 (nec2dxs*.exe) are interactive: they read the
input file name and the output file name from stdin.  4nec2 itself drives them
the same way (see C:\\4nec2\\exe\\nec2d.tmp which simply contains two paths).

This module wraps that protocol, keeps the card deck byte-for-byte intact
(only non-ASCII characters on comment cards are replaced, because the engines
are ANSI programs) and returns the produced output text.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_NEC4NEC_DIR = Path(os.environ.get("NEC4NEC_DIR", r"C:\4nec2"))

# Ordered preferences.  The first existing file wins unless the caller passes
# an explicit engine name or the NEC2_ENGINE environment variable.
ENGINE_CANDIDATES = (
    r"exe\nec2dxs11k.exe",
    r"NEC引擎\NEC2LP64引擎\nec2dxs11k.exe",
    r"NEC引擎\原版NEC2DXS引擎\nec2dxs11k.exe",
    r"NEC引擎\NEC2MP多线程引擎\nec2dxs11k.exe",
    r"NEC引擎\NEC2LP32引擎\nec2dxs11k.exe",
    r"exe\nec2dxs11k.exe",
    r"exe\nec2dxs8k0.exe",
    r"exe\nec2dxs5k0.exe",
)

COMMENT_LABELS = ("CM", "CE", "CT", "CK")


class NecError(RuntimeError):
    """Raised for unrecoverable engine problems (no engine, empty output)."""


@dataclass
class RunResult:
    """Outcome of one engine invocation."""

    ok: bool
    deck: str
    output: str = ""
    engine_path: str = ""
    duration_s: float = 0.0
    exit_code: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    saved_out_path: str | None = None

    @property
    def engine_name(self) -> str:
        return Path(self.engine_path).name if self.engine_path else ""


def find_nec4nec_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Return the 4NEC2 installation directory, validating that engines exist."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("NEC4NEC_DIR"):
        candidates.append(Path(os.environ["NEC4NEC_DIR"]))
    candidates.extend(
        [
            DEFAULT_NEC4NEC_DIR,
            Path(r"C:\Program Files\4nec2"),
            Path(r"C:\Program Files (x86)\4nec2"),
            Path.home() / "4nec2",
        ]
    )
    for cand in candidates:
        if not cand:
            continue
        if (cand / "exe").is_dir() or any((cand / rel).is_file() for rel in ENGINE_CANDIDATES):
            return cand
    raise NecError(
        "4NEC2 installation not found; looked in: "
        + ", ".join(str(c) for c in candidates)
    )


def list_engines(nec4nec_dir: str | os.PathLike[str] | None = None) -> list[dict]:
    """List every usable engine under the installation, with a preferred flag."""
    base = find_nec4nec_dir(nec4nec_dir)
    preferred = preferred_engine_path(base)
    found: list[dict] = []
    for path in sorted(base.rglob("nec2d*.exe")):
        if path.parent.name.lower() == "exe" and path.name.lower() == "nec2d.exe":
            # nec2d.exe in exe/ is a 16-bit/DOS leftover that does not run.
            continue
        found.append(
            {
                "path": str(path),
                "name": path.name,
                "folder": path.parent.name,
                "size_bytes": path.stat().st_size,
                "preferred": str(path).lower() == str(preferred).lower(),
            }
        )
    return found


def preferred_engine_path(nec4nec_dir: str | os.PathLike[str] | None = None) -> Path:
    """Return the first engine that exists, honouring NEC2_ENGINE override."""
    override = os.environ.get("NEC2_ENGINE")
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = find_nec4nec_dir(nec4nec_dir) / candidate
        if candidate.is_file():
            return candidate
        raise NecError(f"NEC2_ENGINE points to a missing file: {candidate}")

    base = find_nec4nec_dir(nec4nec_dir)
    for rel in ENGINE_CANDIDATES:
        candidate = base / rel
        if candidate.is_file():
            return candidate
    # Last resort: any nec2d*.exe that is not the DOS leftover.
    for candidate in sorted(base.rglob("nec2d*.exe")):
        if candidate.name.lower() != "nec2d.exe":
            return candidate
    raise NecError(f"No NEC-2 engine executable found under {base}")


def resolve_engine(engine: str | os.PathLike[str] | None, nec4nec_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an engine given a path, a bare file name, or None (preferred)."""
    if not engine:
        return preferred_engine_path(nec4nec_dir)
    path = Path(engine)
    if path.is_file():
        return path
    base = find_nec4nec_dir(nec4nec_dir)
    for candidate in base.rglob(str(engine)):
        if candidate.is_file():
            return candidate
    raise NecError(f"NEC engine not found: {engine}")


def _compact_card(line: str) -> str:
    """Re-emit an over-long numeric card compactly (single spaces, %g floats).

    Only applied when every field after the label parses as a number — cards
    with symbolic fields (SY expressions) are returned unchanged.
    """
    tokens = line.split()
    if len(tokens) < 2:
        return line
    label, fields = tokens[0], tokens[1:]
    try:
        values = [float(f) for f in fields]
    except ValueError:
        return line
    compact = [label]
    compact.extend(f"{v:.10g}" for v in values)
    return " ".join(compact)


def sanitize_deck(text: str) -> str:
    """Make a card deck safe for the ANSI engines without changing card order.

    Non-ASCII characters are only stripped on comment cards (CM/CE/CT/CK);
    everything else is preserved, including any numeric symbols and comments.
    A trailing EN card is appended when missing.  Data cards longer than the
    80-column card limit (the G0KSC library writes 95-char GW lines whose
    radius starts at column 86) are re-tokenized compactly — otherwise the
    engine truncates the line, reads a zero radius and dies with
    "GEOMETRY DATA CARD ERROR".

    Comment handling (verified against the bundled NEC2/MP engine):
    the engines only accept comment cards at the very top of the deck, as
    ``CM`` lines terminated by exactly ONE ``CE``.  A second CE, a CE before
    a CM, or any comment card between data cards is parsed as a data card and
    kills the run ("CARD ERROR - INVALID NUMBER AT INTEGER POSITION 1" /
    "TOO MANY FIELDS IN RECORD"), a deck with no comment card at all dies
    with "INCORRECT LABEL FOR A COMMENT CARD", and the 4NEC2-specific CT/CK
    cards are not accepted by the engine at all.  sanitize_deck therefore
    rebuilds the comment block: CT/CK become CM, surplus CEs become CM,
    mid-deck comment cards are relocated into the top block, the last comment
    line is emitted as CE, and a bare CE is prepended when the deck has no
    comment card.  (The whole G0KSC model library ships CM cards with no CE
    and needs exactly this repair.)
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned: list[str] = []
    for line in text.split("\n"):
        label = line[:2].upper()
        if label in COMMENT_LABELS or any(ord(ch) > 127 for ch in line):
            # Defensive: keep every card ANSI; engines abort on non-ASCII.
            line = "".join(ch if ord(ch) < 128 else "?" for ch in line)
        line = line.rstrip()
        if len(line) > 80 and label not in COMMENT_LABELS:
            line = _compact_card(line)
        cleaned.append(line)

    comments: list[str] = []
    body: list[str] = []
    head = True
    for line in cleaned:
        label = line[:2].upper()
        if head and label in COMMENT_LABELS:
            comments.append(line)
            continue
        if line.strip():
            head = False
        if label in COMMENT_LABELS and line.strip():
            # Relocate a mid-deck comment card into the top block (as CM).
            comments.append(line if label == "CM" else "CM" + line[2:])
        else:
            body.append(line)
    if comments:
        # CE is a terminator, not a repeatable card: exactly one, at the end.
        norm = [c if c[:2].upper() == "CM" else "CM" + c[2:] for c in comments]
        norm[-1] = "CE" + norm[-1][2:]
        comments = norm
    else:
        comments = ["CE"]
    lines = comments + body
    if not any(ln[:2].upper() == "EN" for ln in lines if ln.strip()):
        lines.append("EN")
    return "\n".join(lines) + "\n"


_ERROR_RE = re.compile(r"(error|incorrect|fatal|abort|not supported|unrecognized|invalid)", re.I)


def _scan_messages(output: str) -> tuple[list[str], list[str]]:
    """Extract engine error/warning lines from the output text."""
    errors: list[str] = []
    warnings: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("WARNING"):
            warnings.append(line)
        elif _ERROR_RE.search(line) and "ERROR" in upper or "INCORRECT" in upper or "FATAL" in upper:
            if "DATA CARD" in upper or "ERROR" in upper or "INCORRECT" in upper:
                errors.append(line)
    # De-duplicate while preserving order.
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


class NecEngine:
    """Thin wrapper around one NEC-2 executable."""

    def __init__(
        self,
        nec4nec_dir: str | os.PathLike[str] | None = None,
        engine: str | os.PathLike[str] | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.nec4nec_dir = find_nec4nec_dir(nec4nec_dir)
        self.engine_path = resolve_engine(engine, self.nec4nec_dir)
        self.timeout_s = float(timeout_s)

    # -- public API -----------------------------------------------------
    def run(
        self,
        deck: str,
        timeout_s: float | None = None,
        save_out_path: str | os.PathLike[str] | None = None,
    ) -> RunResult:
        """Run one NEC-2 card deck and return the produced output text."""
        deck = sanitize_deck(deck)
        timeout = float(timeout_s if timeout_s is not None else self.timeout_s)
        started = time.perf_counter()
        stdout = ""
        stderr = ""
        exit_code: int | None = None
        output = ""
        errors: list[str] = []
        warnings: list[str] = []
        tmpdir = Path(tempfile.mkdtemp(prefix="nec2_"))
        try:
            (tmpdir / "input.inp").write_text(deck, encoding="ascii", errors="replace")
            try:
                proc = subprocess.run(
                    [str(self.engine_path)],
                    cwd=str(tmpdir),
                    # The Fortran runtime strips only LF; a CR would become part
                    # of the file name and the engine would fail to open it.
                    input="input.inp\noutput.out\n",
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                exit_code = proc.returncode
            except subprocess.TimeoutExpired as exc:
                stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
                stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
                errors.append(f"engine timed out after {timeout:.0f} s")

            out_file = tmpdir / "output.out"
            if out_file.is_file() and out_file.stat().st_size > 0:
                raw = out_file.read_bytes()
                output = raw.decode("ascii", errors="replace")
            elif "FREQUENCY=" in stdout:
                output = stdout
            else:
                errors.append("engine produced no output file")
                output = stdout

            if save_out_path:
                target = Path(save_out_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                # Byte-for-byte copy: text mode would translate the engine's
                # CRLF line endings to CRCRLF and parse_output would then see
                # blank lines (truncating radiation-pattern tables to zero).
                target.write_bytes(output.encode("ascii", errors="replace"))
                saved = str(target)
            else:
                saved = None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        msg_errors, msg_warnings = _scan_messages(output)
        errors.extend(msg_errors)
        warnings.extend(msg_warnings)
        if (
            exit_code == 0
            and not errors
            and "FREQUENCY=" not in output
            and ("RUN TIME" in output or "DATA CARD" in output)
        ):
            # The engine parsed every card and then stopped without solving —
            # observed with GN 2 (Sommerfeld) decks that carry no RP card.
            errors.append(
                "engine stopped without producing a solution (output ends after "
                "the data-card listing; with GN 2 Sommerfeld ground, add an RP card)"
            )
        duration = time.perf_counter() - started
        ok = exit_code == 0 and not errors and "FREQUENCY=" in output
        return RunResult(
            ok=ok,
            deck=deck,
            output=output,
            engine_path=str(self.engine_path),
            duration_s=duration,
            exit_code=exit_code,
            errors=errors,
            warnings=warnings,
            stdout=stdout,
            stderr=stderr,
            saved_out_path=saved,
        )
