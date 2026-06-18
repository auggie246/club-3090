"""Cockpit data models — the typed shapes the panes consume.

This module is **pure**: no subprocess, no I/O, no Textual.  It defines the
dataclasses produced by ``services.CockpitData`` and the small parsing helpers
that turn raw contract output (JSON dicts / health.sh text) into those shapes.

Keeping these here (separate from ``services.py``) lets the panes and the tests
import the shapes without dragging in the subprocess machinery, and lets the
service layer be fully dependency-injected against a fake runner.

The enriched catalog row (``CatalogEntry``) wraps the shared-core ``VariantRow``
(never re-implements it) and layers on the join results: the local-card fit
verdict, measured TPS / 8-pack, and provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from club3090_tui_core.registry import VariantRow

# ── Fit verdict ───────────────────────────────────────────────────────────────


@dataclass
class FitVerdict:
    """Result of kv-calc --fit / switch.sh --explain's fit block for one slug."""

    # Real kv-calc --fit verdict enum (verified live):
    #   fits-clean | fits-constrained | wont-fit | unknown
    # plus the cockpit-internal "skip" (ik/llama kvcalc_key=SKIP — no vLLM fit).
    verdict: str = "unknown"          # fits-clean | fits-constrained | wont-fit | unknown | skip
    vram_est_gb: Optional[float] = None
    band_gb: Optional[float] = None
    max_ctx: Optional[int] = None
    card: str = ""
    error: str = ""

    # Compact glyph for the Catalog "fit" column.
    @property
    def glyph(self) -> str:
        return {
            "fits-clean": "●",
            "fits-constrained": "◐",
            "wont-fit": "○",
            "skip": "·",
            "unknown": "·",
        }.get(self.verdict, "·")

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, card: str = "") -> "FitVerdict":
        if not d:
            return cls(card=card)
        return cls(
            verdict=str(d.get("verdict", "unknown")),
            vram_est_gb=_as_float(d.get("vram_est_gb")),
            band_gb=_as_float(d.get("band_gb")),
            max_ctx=_as_int(d.get("max_ctx")),
            card=card,
            error=str(d.get("error", "")),
        )


# ── Measurement (TPS / 8-pack) ──────────────────────────────────────────────────


@dataclass
class Measurement:
    """A measured result for a slug, joined from a structured corpus or parsed
    coarsely from BENCHMARKS.md.  ``source`` records provenance so the UI can
    distinguish a structured record from a best-effort markdown parse."""

    narr_tps: Optional[float] = None
    code_tps: Optional[float] = None
    quality_8pk: Optional[str] = None   # e.g. "107/150"
    max_ctx_label: str = ""
    date: str = ""
    source: str = ""                    # "explain" | "corpus" | "benchmarks.md" | ""

    @property
    def tps_label(self) -> str:
        if self.narr_tps is None and self.code_tps is None:
            return "—"
        n = f"{self.narr_tps:.0f}" if self.narr_tps is not None else "—"
        c = f"{self.code_tps:.0f}" if self.code_tps is not None else "—"
        return f"{n}/{c}"

    @property
    def quality_label(self) -> str:
        return self.quality_8pk or "—"


# ── Enriched catalog entry ──────────────────────────────────────────────────────


@dataclass
class CatalogEntry:
    """A registry VariantRow enriched with fit + measurement + provenance.

    ``row`` is the shared-core dataclass verbatim; the cockpit never mutates it.
    """

    row: VariantRow
    fit: FitVerdict = field(default_factory=FitVerdict)
    measurement: Measurement = field(default_factory=Measurement)

    # Convenience pass-throughs (so panes can read entry.slug, not entry.row.slug)
    @property
    def slug(self) -> str:
        return self.row.slug

    @property
    def engine(self) -> str:
        return self.row.engine

    @property
    def model(self) -> str:
        return self.row.model

    @property
    def status(self) -> str:
        return self.row.status

    @property
    def status_note(self) -> str:
        return self.row.status_note

    @property
    def ctx_label(self) -> str:
        return self.row.ctx_label

    @property
    def port(self) -> int:
        return self.row.port

    @property
    def source(self) -> str:
        """Provenance string for the catalog 'source' column (registry source
        field, e.g. 'curated' / 'community' / 'local')."""
        return getattr(self.row, "source", "") or "·"


# ── Estate / Scene / Container / Doctor ─────────────────────────────────────────


@dataclass
class Scene:
    """One gpu-mode scene from --list-modes --json."""

    name: str
    group: str = ""
    description: str = ""
    services: list[str] = field(default_factory=list)
    ports: list[str] = field(default_factory=list)
    gpus: str = ""                      # "none" | "0" | "both" | etc.

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Scene":
        return cls(
            name=str(d.get("name", "")),
            group=str(d.get("group", "")),
            description=str(d.get("description", "")),
            services=list(d.get("services", []) or []),
            ports=[str(p) for p in (d.get("ports", []) or [])],
            gpus=str(d.get("gpus", "")),
        )


@dataclass
class ContainerInfo:
    """A running stack container that can hold a GPU, from docker ps.

    ``kind`` is one of:
      - ``"engine"``  — a core inference engine (``vllm-`` / ``llama-cpp-`` /
        ``ik-llama-`` / ``sglang-`` / ``beellama-``); ``slug`` is registry-matched.
      - ``"estate"``  — an estate-planner container (``club3090-<name>``).
      - ``"service"`` — a GPU-holding rig service (ComfyUI / Step-Audio).
    """

    name: str
    kind: str = "service"               # "engine" | "estate" | "service"
    host_port: int = 0
    internal_port: int = 0
    engine: str = ""                    # for engine containers
    slug: str = ""                      # registry slug if matched
    gpus: str = ""                      # "0,1" if known, else ""


@dataclass
class DoctorRead:
    """Parsed runtime-state summary from health.sh (text-only contract).

    health.sh has no --json mode, so this is a deliberately coarse text parse —
    ``raw`` keeps the full output for the pane to render verbatim, and the
    booleans/strings are best-effort signals for the rail/summary line.
    """

    reachable: bool = False
    serving: bool = False
    summary: str = ""                   # one-line condensed status
    kv_pool_pct: Optional[int] = None
    spec_dec: str = ""                  # e.g. "MTP n=2, 73% accept" or ""
    recent_errors: Optional[int] = None
    raw: str = ""
    parse_source: str = "health.sh-text"


@dataclass
class EstateState:
    """Live estate snapshot: detect + doctor + scene catalog + estate-planner."""

    target: Any = None                  # core ServingTarget (or None)
    gpus: list[Any] = field(default_factory=list)   # core GpuInfo list
    containers: list[ContainerInfo] = field(default_factory=list)
    scenes: list[Scene] = field(default_factory=list)
    doctor: DoctorRead = field(default_factory=DoctorRead)
    estate_report: dict[str, Any] = field(default_factory=dict)   # estate_cli report-state
    matched_slug: str = ""              # slug the running engine matched, if any
    error: str = ""


# ── Reconcile gate ──────────────────────────────────────────────────────────────


@dataclass
class GpuConflict:
    """A live GPU user that a pending write would collide with."""

    gpu_index: int
    mem_used_mib: int
    container: str = ""                 # container occupying it, if known
    note: str = ""


@dataclass
class ReconcileResult:
    """Result of reconcile_before_write() — the dual-writer safety gate.

    ``safe`` is True only when no running container / GPU user would collide
    with the pending action.  ``conflicts`` and ``gpu_conflicts`` enumerate
    exactly what's in the way so the UI can show "this will tear down X".
    """

    safe: bool
    action: str = ""                    # "serve:<slug>" | "scene:<mode>" | ...
    pending_gpus: list[int] = field(default_factory=list)   # GPUs the action wants
    conflicts: list[ContainerInfo] = field(default_factory=list)
    gpu_conflicts: list[GpuConflict] = field(default_factory=list)
    estate_claims: list[dict[str, Any]] = field(default_factory=list)  # estate instances in the way
    pending_claim_tokens: list[str] = field(default_factory=list)  # in-flight writes (HARD block, non-forceable)
    note: str = ""

    @property
    def conflict_summary(self) -> str:
        parts: list[str] = []
        for c in self.conflicts:
            g = f" (GPU {c.gpus})" if c.gpus else ""
            parts.append(f"{c.name}{g}")
        for e in self.estate_claims:
            parts.append(f"estate:{e.get('name', '?')}")
        return ", ".join(parts) if parts else "none"


# ── BYO check ────────────────────────────────────────────────────────────────────


@dataclass
class ByoResult:
    """Result of pull.sh --profile-like <repo> --dry-run --json."""

    repo: str
    profile_like: str
    arch: str = ""
    eligible: bool = False
    fit_verdict: str = ""
    note: str = ""
    # swap_path block
    route: Optional[str] = None
    sibling_slug: Optional[str] = None
    quant_match: Optional[str] = None
    drop_spec_config: bool = False
    error: str = ""

    @classmethod
    def from_dict(cls, repo: str, profile_like: str, d: dict[str, Any] | None) -> "ByoResult":
        if not d:
            return cls(repo=repo, profile_like=profile_like, error="no output")
        swap = d.get("swap_path") or {}
        return cls(
            repo=repo,
            profile_like=profile_like,
            arch=str(d.get("arch", "")),
            eligible=bool(d.get("eligible", False)),
            fit_verdict=str(d.get("fit_verdict", "")),
            note=str(d.get("note", "")),
            route=swap.get("route"),
            sibling_slug=swap.get("sibling_slug"),
            quant_match=swap.get("quant_match"),
            drop_spec_config=bool(swap.get("drop_spec_config", False)),
        )


# ── Action plans (wired but execution-gated) ─────────────────────────────────────


@dataclass
class ActionPlan:
    """A constructed-but-not-executed write command.

    Action builders return this; runtime execution (only when actually invoked,
    NEVER in tests / this phase) feeds ``cmd`` to the core SubprocessRunner.
    The reconcile gate is consulted BEFORE execution.
    """

    kind: str                           # "serve" | "set_default" | "clear_default" | "scene" | "estate_down" | "container"
    cmd: list[str]
    description: str = ""
    is_write: bool = True
    requires_reconcile: bool = True
    force: bool = False
    force_reason: str = ""              # required when force=True


# ── Parse helpers (pure) ─────────────────────────────────────────────────────────


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Strip ANSI color codes (health.sh / gpu-mode emit them).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def parse_health_text(text: str) -> DoctorRead:
    """Best-effort parse of health.sh stdout into a DoctorRead.

    health.sh has no --json contract; this scans the human-readable output for
    the load-bearing signals.  It is intentionally tolerant — any line it can't
    recognize is ignored, and ``raw`` always preserves the full text.
    """
    clean = strip_ansi(text or "")
    dr = DoctorRead(raw=text or "", parse_source="health.sh-text")

    lower = clean.lower()
    dr.reachable = "not reachable" not in lower and "✗ api not reachable" not in lower
    # "✓ serving" / "serving" markers
    dr.serving = "serving" in lower and "not serving" not in lower

    # KV pool percent: "KV pool 61%" / "KV cache ... 61%"
    m = re.search(r"kv\s*(?:pool|cache)[^0-9]*([0-9]{1,3})\s*%", clean, re.IGNORECASE)
    if m:
        dr.kv_pool_pct = int(m.group(1))

    # spec-dec firing: "MTP n=2, 73% accept" / "spec-dec firing (DFlash ...)"
    m = re.search(r"(spec[- ]?dec[^\n]*|MTP\s*n=\d+[^\n]*|DFlash[^\n]*)", clean, re.IGNORECASE)
    if m:
        dr.spec_dec = m.group(1).strip()

    # recent errors: "0 recent errors" / "3 errors"
    m = re.search(r"([0-9]+)\s+(?:recent\s+)?errors?", clean, re.IGNORECASE)
    if m:
        dr.recent_errors = int(m.group(1))

    # Condensed one-liner: first non-empty content line after the banner.
    if not dr.reachable:
        dr.summary = "API not reachable"
    else:
        bits: list[str] = []
        if dr.serving:
            bits.append("serving")
        if dr.kv_pool_pct is not None:
            bits.append(f"KV pool {dr.kv_pool_pct}%")
        if dr.spec_dec:
            bits.append(dr.spec_dec)
        if dr.recent_errors is not None:
            bits.append(f"{dr.recent_errors} errors")
        dr.summary = " · ".join(bits) if bits else "reachable"

    return dr


# Coarse BENCHMARKS.md row parse — provenance-flagged so the UI never mistakes
# a markdown scrape for a structured measurement record.
#
# Real BENCHMARKS.md "Narr / Code TPS" column shapes (verified live):
#   bold:      ``**81.21 / 108.20** single-stream`` / ``**59.67 / 68.78** (decode …)``
#   non-bold:  ``50 / 67`` / ``~32 / ~33``
#   absent:    ``TBD`` / ``—``  (must yield no TPS, NOT a bogus pair)
# The leading ``**`` and ``~`` are optional; trailing prose after the pair is
# ignored.  We anchor on the FIRST ``N / M`` pair (the canonical narr/code), so
# the parenthetical ``(decode 60.39 / 72.40)`` doesn't shadow the headline.
_BENCH_TPS_RE = re.compile(
    r"^\s*\*{0,2}\s*~?\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*~?\s*([0-9]+(?:\.[0-9]+)?)\s*\*{0,2}"
)
_BENCH_8PK_RE = re.compile(r"8-pack\s+([0-9]+/150)")
_BENCH_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _tps_from_cell(cell: str) -> tuple[Optional[float], Optional[float]]:
    """Parse a 'Narr / Code TPS' table cell into (narr, code).

    Handles bold (``**X / Y**``), non-bold (``50 / 67``), tilde-prefixed
    (``~32 / ~33``) and trailing prose (``… single-stream`` / ``(decode …)``).
    Returns (None, None) for ``TBD`` / ``—`` / anything without a leading pair.
    """
    m = _BENCH_TPS_RE.match(cell or "")
    if not m:
        return None, None
    return _as_float(m.group(1)), _as_float(m.group(2))


def _bench_row_cells(line: str) -> list[str]:
    """Split a markdown table row into trimmed cell strings (no leading/trailing
    empties from the surrounding pipes)."""
    if "|" not in line:
        return []
    parts = [c.strip() for c in line.split("|")]
    # A '| a | b |' row splits to ['', 'a', 'b', ''] — drop the bookend empties.
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def parse_benchmarks_md_for_slug(md_text: str, slug: str) -> Optional[Measurement]:
    """Best-effort: scan BENCHMARKS.md for the row whose first cell names the
    slug's serving file.

    Returns a Measurement with source='benchmarks.md' (coarse) if a matching
    benchmark row yields a narr/code TPS pair, else None.  The registry keys
    composes by serving file (e.g. ``llamacpp/mtp``); the BENCHMARKS table keys
    by compose filename in the FIRST column, often with a ``.yml`` extension
    (``minimal.yml``).  The match is **anchored to the first cell** and exact on
    the serving-file token — a substring match would let ``dual`` hit
    ``dual-dflash.yml`` and pull the wrong row (the bug this fixes).
    """
    if not md_text or not slug:
        return None
    stem = slug.split("/")[-1]
    # Backtick-quoted tokens that must appear as a standalone word in the first
    # cell: the bare stem, the stem + .yml, or the full slug.  Word-boundary
    # anchored so 'dual' does not match 'dual-dflash'.
    tokens = {stem, f"{stem}.yml", slug}
    cell_token_res = [
        re.compile(r"(?<![\w./-])" + re.escape(t) + r"(?![\w.-])")
        for t in tokens
    ]
    for line in md_text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _bench_row_cells(line)
        if not cells:
            continue
        first = cells[0]
        if not any(r.search(first) for r in cell_token_res):
            continue
        # The benchmark TPS column is "Narr / Code TPS" — index 4 in the canonical
        # 9-col layout (Compose|Rig|KV|Max ctx|TPS|PP|VRAM|Date|Notes).  Stress /
        # soak rows have a different layout and no TPS cell; for robustness we
        # scan cells for the first parseable 'N / M' pair, skipping the header.
        narr = code = None
        for cell in cells[1:]:
            n, c = _tps_from_cell(cell)
            if n is not None:
                narr, code = n, c
                break
        if narr is None:
            # Matched the row but it carries no TPS (TBD / — / a non-TPS row) →
            # honestly report no measurement rather than a bogus pair.
            continue
        m = Measurement(narr_tps=narr, code_tps=code, source="benchmarks.md")
        q = _BENCH_8PK_RE.search(line)
        if q:
            m.quality_8pk = q.group(1)
        d = _BENCH_DATE_RE.search(line)
        if d:
            m.date = d.group(1)
        return m
    return None


def measurement_from_explain_columns(rec: dict[str, Any]) -> Measurement:
    """Build a Measurement from ONE explain ``benchmarks[]`` record.

    The REAL shape of switch.sh --explain --json ``benchmarks`` (verified live)
    is ``[{"row": "<markdown>", "columns": [<cell>, …]}]`` — the raw scraped
    BENCHMARKS.md row plus its split cells.  This is NOT the invented
    ``{"narr_tps": …}`` corpus shape; the TPS lives in ``columns[]`` and must be
    parsed by position (the canonical "Narr / Code TPS" column is index 4).

    Stress / soak rows have a different column layout and no TPS — those yield an
    empty Measurement (the caller then falls through to no measurement).
    """
    cols = rec.get("columns") or []
    if not isinstance(cols, list) or not cols:
        return Measurement()
    # Canonical bench layout: index 4 is "Narr / Code TPS".  Fall back to a scan
    # of all cells if index 4 isn't a TPS pair (layout drift / non-bench row).
    narr = code = None
    if len(cols) > 4:
        narr, code = _tps_from_cell(str(cols[4]))
    if narr is None:
        for cell in cols[1:]:
            n, c = _tps_from_cell(str(cell))
            if n is not None:
                narr, code = n, c
                break
    if narr is None:
        return Measurement()
    row_text = str(rec.get("row", ""))
    m = Measurement(narr_tps=narr, code_tps=code, source="explain")
    q = _BENCH_8PK_RE.search(row_text)
    if q:
        m.quality_8pk = q.group(1)
    d = _BENCH_DATE_RE.search(row_text)
    if d:
        m.date = d.group(1)
    # Max-ctx is the 4th canonical column ("Max ctx"); keep it if present.
    if len(cols) > 3:
        m.max_ctx_label = str(cols[3])
    return m


def measurement_from_explain_benchmarks(benchmarks: list[dict[str, Any]]) -> Measurement:
    """Build a Measurement from the structured ``benchmarks`` array of
    switch.sh --explain --json.

    The array is ``[{"row": "<md>", "columns": [...]}]``.  We walk it newest-row-
    last and return the FIRST record that yields a real TPS pair (a benchmark
    row), skipping stress / soak rows that carry no TPS.  Returns an empty
    Measurement (tps_label '—') when nothing parseable is present.
    """
    if not benchmarks:
        return Measurement()
    best = Measurement()
    for rec in benchmarks:
        if not isinstance(rec, dict):
            continue
        m = measurement_from_explain_columns(rec)
        if m.narr_tps is not None:
            best = m  # keep walking → newest TPS-bearing row wins
    return best
