"""Cockpit service layer — the data/service API the panes call.

Wraps the Phase-2 ``--json`` contracts + the shared-core detect into a clean,
dependency-injectable Python API.  Panes (and the Wire step) call ``CockpitData``
methods; tests construct it with a ``FakeRunner`` so no subprocess / GPU / docker
is ever touched.

Contracts wrapped (all READ-only, safe to call live):
  - ``scripts/lib/registry-emit.sh --json``           → load_catalog / containers
  - ``scripts/switch.sh --explain <slug> --json``      → explain / fit + measurement join
  - ``tools/kv-calc.py --fit <slug> --card <c> --json``→ fit
  - ``scripts/pull.sh <repo> --profile-like <k> --dry-run --json`` → byo_check
  - ``scripts/lib/profiles/estate_cli.py report-state --json`` → estate_state
  - ``scripts/gpu-mode.sh --list-modes --json``        → estate_state (scene catalog)
  - ``scripts/health.sh`` (text)                       → estate_state (Doctor read)
  - core ``detect_endpoint`` / ``get_gpu_info``        → estate_state / containers / reconcile

WRITES (serve / scene_switch / set_default / clear_default / estate_down /
container_action) are WIRED as ``ActionPlan`` builders + an ``execute_action``
that streams via the core SubprocessRunner.  In this phase they are NEVER run
live; tests mock execution.  ``execute_action`` always re-runs the reconcile
gate first and refuses if unsafe (unless an explicit, reasoned force override
is supplied).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

from club3090_tui_core.detect import (
    GpuInfo,
    ServingTarget,
    detect_endpoint as core_detect_endpoint,
    get_gpu_info as core_get_gpu_info,
    match_target_to_registry,
)
from club3090_tui_core.registry import VariantRow, parse_variant_rows
from club3090_tui_core.runner import SubprocessRunner

from .data import (
    ActionPlan,
    ByoResult,
    CatalogEntry,
    ContainerInfo,
    DoctorRead,
    EstateState,
    FitVerdict,
    GpuConflict,
    Measurement,
    ReconcileResult,
    Scene,
    measurement_from_explain_benchmarks,
    parse_benchmarks_md_for_slug,
    parse_health_text,
)

# ── Local card name (this rig) ──────────────────────────────────────────────────

# The locally-detected per-card name used for fit joins.  RTX 3090 is the rig
# default; ``CockpitData(card=...)`` overrides it.  Detection from nvidia-smi is
# done lazily in ``detect_local_card`` so headless tests never shell out.
DEFAULT_CARD = "rtx-3090"


# ── Subprocess runner protocol (dependency injection seam) ───────────────────────


class RunResult:
    """Result of a read-only subprocess call."""

    def __init__(self, returncode: int, stdout: str, stderr: str, timed_out: bool = False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class Runner(Protocol):
    """Async subprocess seam.  Real impl shells out; fake impl returns canned
    output keyed on the command.  All READ contracts go through ``run``."""

    async def run(
        self, cmd: list[str], *, cwd: str, timeout: float = 30.0
    ) -> RunResult: ...


class RealRunner:
    """Production runner — actually shells out (READ contracts only)."""

    async def run(
        self, cmd: list[str], *, cwd: str, timeout: float = 30.0
    ) -> RunResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return RunResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=out.decode("utf-8", errors="replace"),
                stderr=err.decode("utf-8", errors="replace"),
            )
        except asyncio.TimeoutError:
            return RunResult(returncode=-1, stdout="", stderr="timeout", timed_out=True)
        except FileNotFoundError as exc:
            return RunResult(returncode=127, stdout="", stderr=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return RunResult(returncode=-1, stdout="", stderr=str(exc))


# Detect seam: async callables matching the core signatures.
DetectEndpointFn = Callable[[], Awaitable[ServingTarget]]
GetGpuInfoFn = Callable[[], Awaitable[list[GpuInfo]]]


# ── The service class ───────────────────────────────────────────────────────────


class CockpitData:
    """Clean, dependency-injectable data/service API for the cockpit panes.

    Injectable seams (all default to the real implementations):
      - ``runner``            : Runner  — read-only subprocess calls
      - ``detect_endpoint_fn``: core detect_endpoint (running container probe)
      - ``get_gpu_info_fn``   : core get_gpu_info (nvidia-smi)
      - ``write_runner``      : SubprocessRunner — streams WRITE actions (never
                                executed in tests / this phase)
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        card: str = DEFAULT_CARD,
        runner: Optional[Runner] = None,
        detect_endpoint_fn: Optional[DetectEndpointFn] = None,
        get_gpu_info_fn: Optional[GetGpuInfoFn] = None,
        write_runner: Optional[SubprocessRunner] = None,
    ):
        self.repo_root = Path(repo_root)
        self.card = card
        self._runner: Runner = runner or RealRunner()
        self._detect_endpoint: DetectEndpointFn = detect_endpoint_fn or core_detect_endpoint
        self._get_gpu_info: GetGpuInfoFn = get_gpu_info_fn or core_get_gpu_info
        # Write runner is constructed lazily and NEVER invoked in this phase.
        self._write_runner = write_runner or SubprocessRunner(self.repo_root)
        # Dual-writer serialization (design §3.2): the reconcile→execute window
        # must be ATOMIC.  Without this, two confirmed plans can both reconcile
        # "safe" before either claims VRAM (TOCTOU).  Held across the whole gate
        # + dispatch in execute_action so a second write cannot run its gate
        # until the first has finished claiming the cards.
        self._write_lock = asyncio.Lock()
        # In-process pending-claim registry (§3.2 TOCTOU fix).
        # Maps token -> (gpu_set, expiry_monotonic).  Registered UNDER the lock
        # before start_raw is called; cleared when the write subprocess exits.
        # A TTL (600 s) ensures a leaked claim cannot block the gate forever.
        self._pending_claims: dict[str, tuple[frozenset[int], float]] = {}
        # Leak backstop ONLY — the real lifecycle is "clear on the write
        # subprocess's completion" (see _release_claim_when_done). switch.sh's
        # own READY_TIMEOUT is already 600s, so a TTL near that would prune a
        # still-booting claim; keep it well above a worst-case boot.
        self._claim_ttl = 1800.0  # seconds

    # ── small JSON helper ──────────────────────────────────────────────────────

    async def _run_json(
        self, cmd: list[str], *, timeout: float = 30.0
    ) -> tuple[Any, Optional[str]]:
        """Run a read contract, parse stdout as JSON.  Returns (data, error)."""
        res = await self._runner.run(cmd, cwd=str(self.repo_root), timeout=timeout)
        if res.timed_out:
            return None, f"timed out after {timeout:.0f}s: {' '.join(cmd[:2])}"
        if not res.stdout.strip():
            # Some contracts print diagnostics to stderr only on failure.
            return None, (res.stderr.strip()[:200] or f"empty output (rc={res.returncode})")
        try:
            return json.loads(res.stdout), None
        except json.JSONDecodeError as exc:
            # Contracts may prepend non-JSON banner lines on stderr only, but if
            # stdout itself is dirty, try to recover the first JSON value.
            recovered = _extract_first_json(res.stdout)
            if recovered is not None:
                return recovered, None
            return None, f"JSON parse error: {exc}"

    # ── READ: catalog ────────────────────────────────────────────────────────────

    async def load_catalog(
        self, *, enrich_fit: bool = True, enrich_measurement: bool = True
    ) -> tuple[list[CatalogEntry], Optional[str]]:
        """Enriched variant rows.

        1. registry-emit.sh --json → VariantRow list (variants block).
        2. per-slug fit verdict for the locally-detected card (via kv-calc).
        3. per-slug measurement (TPS / 8pk) from the structured explain corpus,
           fallback to a coarse BENCHMARKS.md parse.

        Fit + measurement enrichment is best-effort: a failed join leaves the
        stub glyph rather than failing the whole catalog load.
        """
        data, err = await self._run_json(
            ["bash", "scripts/lib/registry-emit.sh", "--json"], timeout=30.0
        )
        if err and not data:
            # Fall back to the raw tab emitter (registry_variant_rows) so the
            # catalog still loads even if the --json wrapper regresses.
            rows, ferr = await self._load_catalog_rows_fallback()
            if ferr:
                return [], err
        else:
            rows = [_variant_row_from_dict(d) for d in (data or {}).get("variants", [])]

        entries = [CatalogEntry(row=r) for r in rows]

        if enrich_fit:
            await self._enrich_fits(entries)
        if enrich_measurement:
            await self._enrich_measurements(entries)

        if not entries:
            return [], "No variants returned — registry may be empty"
        return entries, None

    async def _load_catalog_rows_fallback(self) -> tuple[list[VariantRow], Optional[str]]:
        cmd = [
            "bash",
            "-c",
            'source "$1/scripts/lib/registry-emit.sh" && registry_variant_rows "$1"',
            "bash",
            str(self.repo_root),
        ]
        res = await self._runner.run(cmd, cwd=str(self.repo_root), timeout=30.0)
        if not res.stdout.strip():
            return [], res.stderr.strip()[:200] or "no rows"
        return parse_variant_rows(res.stdout), None

    async def _enrich_fits(self, entries: list[CatalogEntry]) -> None:
        for e in entries:
            # ik/llama composes use kvcalc_key "SKIP" → no vLLM kv-calc fit.
            if (e.row.kvcalc_key or "").upper() == "SKIP":
                e.fit = FitVerdict(verdict="skip", card=self.card)
                continue
            fit = await self.fit(e.slug, self.card)
            e.fit = fit

    async def _enrich_measurements(self, entries: list[CatalogEntry]) -> None:
        bench_md: Optional[str] = None
        for e in entries:
            # Preferred: structured benchmarks from the explain contract.  The
            # REAL shape is [{"row","columns"}]; measurement_from_explain_*
            # parses TPS out of columns[].  Only COMMIT the explain result when
            # it actually yields a TPS — otherwise an empty benchmarks[] (or a
            # row that is stress/soak-only) must NOT suppress the markdown
            # fallback (the `continue`-suppresses-fallback bug this fixes).
            explain, _err = await self.explain(e.slug)
            if explain and explain.get("benchmarks"):
                m = measurement_from_explain_benchmarks(explain["benchmarks"])
                if m.narr_tps is not None or m.code_tps is not None:
                    e.measurement = m
                    continue
            # Fallback: coarse BENCHMARKS.md scrape (flagged in source).
            if bench_md is None:
                bench_md = self._read_benchmarks_md()
            m = parse_benchmarks_md_for_slug(bench_md or "", e.slug)
            if m:
                e.measurement = m

    def _read_benchmarks_md(self) -> str:
        path = self.repo_root / "BENCHMARKS.md"
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # ── READ: explain (Tier-3 detail) ────────────────────────────────────────────

    async def explain(self, slug: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """switch.sh --explain <slug> --json — full per-slug story."""
        data, err = await self._run_json(
            ["bash", "scripts/switch.sh", "--explain", slug, "--json"], timeout=45.0
        )
        if data is None:
            return None, err
        return data, None

    # ── READ: fit ──────────────────────────────────────────────────────────────────

    async def fit(self, slug: str, card: Optional[str] = None) -> FitVerdict:
        """kv-calc.py --fit <slug> --card <card> --json."""
        card = card or self.card
        data, err = await self._run_json(
            ["python3", "tools/kv-calc.py", "--fit", slug, "--card", card, "--json"],
            timeout=40.0,
        )
        if data is None:
            return FitVerdict(verdict="unknown", card=card, error=err or "")
        return FitVerdict.from_dict(data, card=card)

    # ── READ: BYO check ──────────────────────────────────────────────────────────────

    async def byo_check(self, repo: str, profile_like: str) -> ByoResult:
        """pull.sh <repo> --profile-like <key> --dry-run --json.

        ``--dry-run`` forces Path B (evaluate only, never download/emit), so this
        is safe to call live.  The structured ``swap_path`` block drives the
        Route-C reuse suggestion in the BYO pane.
        """
        data, err = await self._run_json(
            [
                "bash",
                "scripts/pull.sh",
                repo,
                "--profile-like",
                profile_like,
                "--dry-run",
                "--json",
            ],
            timeout=90.0,
        )
        if data is None:
            return ByoResult(repo=repo, profile_like=profile_like, error=err or "no output")
        return ByoResult.from_dict(repo, profile_like, data)

    # ── READ: containers ────────────────────────────────────────────────────────────

    async def containers(
        self, variants: Optional[list[VariantRow]] = None
    ) -> list[ContainerInfo]:
        """The stack containers that hold GPUs, read-only via docker ps.

        Three classes are surfaced (each is a potential GPU user the reconcile
        gate must see):
          - **engine** containers (``vllm-`` / ``llama-cpp-`` / ``ik-llama-`` /
            ``sglang-`` / ``beellama-``) — slug-matched against the registry;
          - **estate** containers (``club3090-<name>`` — booted by the estate
            planner);
          - **service** containers — rig services that hold a GPU (ComfyUI /
            Step-Audio).

        Engine containers get their slug matched against the registry when
        ``variants`` is supplied."""
        infos: list[ContainerInfo] = []
        for name, host_port, internal_port, engine, kind in await self._docker_ps_stack_containers():
            slug = ""
            if kind == "engine" and variants:
                tmp = ServingTarget(container=name, host_port=host_port)
                tmp = match_target_to_registry(tmp, variants)
                slug = tmp.slug
            infos.append(
                ContainerInfo(
                    name=name,
                    kind=kind,
                    host_port=host_port,
                    internal_port=internal_port,
                    engine=engine,
                    slug=slug,
                )
            )
        return infos

    async def _docker_ps_stack_containers(
        self,
    ) -> list[tuple[str, int, int, str, str]]:
        """Read-only docker ps for every stack container that can hold a GPU.

        Returns ``(name, host_port, internal_port, engine, kind)`` where
        ``kind ∈ {engine, estate, service}``.  Engine prefixes come from the
        core ``ENGINE_PREFIXES``; estate containers carry the ``club3090-``
        prefix; a small set of GPU-holding rig services is matched by name so
        the gate sees ComfyUI / Step-Audio too (they don't share a prefix)."""
        from club3090_tui_core.detect import (
            ENGINE_PREFIXES,
            PORT_MAP_BROAD_RE,
            _classify_engine,
            _classify_engine_from_container,
        )

        res = await self._runner.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"],
            cwd=str(self.repo_root),
            timeout=10.0,
        )
        # Fix 2 (docker ps fail-closed): a timed-out or failed docker ps must
        # NOT yield an empty container list (which looks like "no conflicts").
        # Raise so reconcile_before_write can catch it and set safe=False.
        if not res.ok:
            raise RuntimeError(
                f"docker ps read failed (rc={res.returncode}, "
                f"timed_out={res.timed_out}): {res.stderr.strip()[:120]}"
            )
        out: list[tuple[str, int, int, str, str]] = []
        seen: set[tuple[str, int]] = set()
        for line in res.stdout.splitlines():
            if "|" not in line:
                continue
            name, ports_str = line.split("|", 1)
            kind = _classify_container_kind(name)
            if kind is None:
                continue
            engine = ""
            if kind == "engine":
                engine = _classify_engine_from_container(name)
            matched_port = False
            for match in PORT_MAP_BROAD_RE.finditer(ports_str):
                host_port = int(match.group(1))
                internal_port = int(match.group(2))
                key = (name, host_port)
                if key in seen:
                    continue
                seen.add(key)
                eng = engine
                if kind == "engine" and eng in ("", "unknown"):
                    eng = _classify_engine(str(internal_port))
                out.append((name, host_port, internal_port, eng, kind))
                matched_port = True
            if not matched_port:
                # A GPU-holding container with no published port (common for
                # estate / service containers) is still a conflict.
                key = (name, 0)
                if key not in seen:
                    seen.add(key)
                    out.append((name, 0, 0, engine, kind))
        return out

    # ── READ: container logs ──────────────────────────────────────────────────────

    async def container_logs(
        self, name: str, *, tail: int = 200
    ) -> dict[str, Any]:
        """`docker logs --tail <N> <name>` — a READ (safe to run live).

        Returns ``{"lines": [...], "error": <str|None>}``.  Goes through the
        injected read runner so tests stay subprocess-free.  This is NOT a write
        — it does not touch container state."""
        res = await self._runner.run(
            ["docker", "logs", "--tail", str(tail), name],
            cwd=str(self.repo_root),
            timeout=15.0,
        )
        if res.timed_out:
            return {"lines": [], "error": f"timed out reading logs for {name}"}
        # docker logs writes app stdout to stdout and app stderr to stderr; show
        # both, stdout first.  A non-zero rc with no output is an error.
        text = res.stdout or ""
        if res.stderr and not text:
            # No stdout — surface stderr (it may BE the log, or an error).
            if res.returncode != 0 and "No such container" in res.stderr:
                return {"lines": [], "error": res.stderr.strip()[:200]}
            text = res.stderr
        lines = text.splitlines()
        if not lines and res.returncode != 0:
            return {"lines": [], "error": (res.stderr.strip()[:200] or f"rc={res.returncode}")}
        return {"lines": lines, "error": None}

    # ── READ: estate state ────────────────────────────────────────────────────────

    async def estate_state(
        self, variants: Optional[list[VariantRow]] = None
    ) -> EstateState:
        """Live estate snapshot: detect (GPUs + running engine + matched slug) +
        health.sh Doctor read + gpu-mode scene catalog + estate-planner report."""
        state = EstateState()

        # detect: running engine + GPUs
        try:
            target = await self._detect_endpoint()
        except Exception as exc:  # pragma: no cover - defensive
            state.error = f"detect failed: {exc}"
            target = ServingTarget()
        if variants:
            target = match_target_to_registry(target, variants)
            state.matched_slug = target.slug
        state.target = target
        state.gpus = list(getattr(target, "gpus", []) or [])
        if not state.gpus:
            # detect may not populate GPUs if no engine running; query directly.
            try:
                state.gpus = await self._get_gpu_info()
            except Exception:
                state.gpus = []

        # containers
        state.containers = await self.containers(variants=variants)

        # scenes (gpu-mode --list-modes --json)
        state.scenes = await self.scenes()

        # doctor (health.sh — text)
        state.doctor = await self.doctor_read(url=target.url or None)

        # estate planner (report-state --json)
        report, _ = await self._run_json(
            ["python3", "scripts/lib/profiles/estate_cli.py", "report-state", "--json"],
            timeout=40.0,
        )
        state.estate_report = report or {}

        return state

    async def scenes(self) -> list[Scene]:
        """gpu-mode --list-modes --json → Scene list."""
        data, _ = await self._run_json(
            ["bash", "scripts/gpu-mode.sh", "--list-modes", "--json"], timeout=30.0
        )
        if not isinstance(data, list):
            return []
        return [Scene.from_dict(d) for d in data]

    async def doctor_read(self, url: Optional[str] = None) -> DoctorRead:
        """health.sh (text-only) → parsed DoctorRead."""
        env_prefix: list[str] = []
        cmd = ["bash", "scripts/health.sh"]
        if url:
            # health.sh reads URL from env; pass via a wrapper to keep the
            # Runner protocol env-free.
            cmd = ["bash", "-c", 'URL="$1" bash scripts/health.sh', "bash", url]
        res = await self._runner.run(cmd, cwd=str(self.repo_root), timeout=30.0)
        return parse_health_text(res.stdout or res.stderr)

    # ── THE DUAL-WRITER GATE ─────────────────────────────────────────────────────

    async def reconcile_before_write(
        self,
        action: str,
        *,
        pending_gpus: Optional[list[int]] = None,
        variants: Optional[list[VariantRow]] = None,
    ) -> ReconcileResult:
        """Re-run detect to see what is ACTUALLY on the cards NOW, and report the
        set of running containers / GPU users / estate claims a pending write
        would collide with.  This is the safety core (design §3.2): a pending
        serve / scene-switch must call this immediately before executing, so a
        concurrent writer (e.g. estate_cli already booted GPU0) is caught.

        ``pending_gpus`` = the GPUs the action wants.  ``None`` means "any GPU"
        (treated as wanting both 0 and 1 — the conservative default), so ANY
        live GPU user is a conflict.

        ``safe`` is True only when nothing live overlaps the requested GPUs.
        """
        result = ReconcileResult(safe=True, action=action)

        # Fresh detect — never trust a cached snapshot for the gate.
        try:
            target = await self._detect_endpoint()
        except Exception as exc:  # pragma: no cover - defensive
            result.note = f"detect failed: {exc}"
            # A failed detect is NOT safe — we can't prove the cards are free.
            result.safe = False
            return result

        # GPU read — FAIL CLOSED.  For a SAFETY gate, an error reading the cards
        # must mean "I cannot prove they are free" → UNSAFE, never "nothing in
        # use".  (Previously this swallowed the exception → gpus=[] → safe.)
        gpus = list(getattr(target, "gpus", []) or [])
        if not gpus:
            try:
                gpus = await self._get_gpu_info()
            except Exception as exc:
                result.note = f"GPU read failed: {exc}"
                result.safe = False
                return result
            if not gpus:
                # No detect GPUs AND no nvidia-smi readout → we have no evidence
                # the cards are free.  Fail closed.
                result.note = "GPU read returned no cards — cannot prove free"
                result.safe = False
                return result

        # Determine which GPUs the action wants.
        if pending_gpus is None:
            wanted = {0, 1}            # conservative: assume both cards
        else:
            wanted = set(pending_gpus)
        result.pending_gpus = sorted(wanted)

        # 1. Running stack containers (docker ps) — engine containers, estate
        #    `club3090-<name>` containers, and rig services (ComfyUI / Step-Audio)
        #    are all live GPU users.  When a container's GPU set is KNOWN and is
        #    disjoint from the wanted cards, it does not conflict; when UNKNOWN
        #    (the common case — docker ps doesn't expose the device list) we stay
        #    conservative and treat it as a conflict.  Detector #2 (raw GPU,
        #    fail-closed) is the backstop for any GPU holder this misses.
        #    Fix 2 (fail-closed): _docker_ps_stack_containers raises RuntimeError
        #    on a failed/timed-out docker ps → catch it and fail closed.
        try:
            containers = await self.containers(variants=variants)
        except Exception as exc:
            result.note = f"docker ps read failed: {exc}"
            result.safe = False
            return result
        for c in containers:
            known = _container_gpu_set(c)
            if known is not None and not (known & wanted):
                continue  # container provably on other card(s) only
            result.conflicts.append(c)

        # 2. Raw GPU occupancy — a card with meaningful VRAM in use is occupied
        #    even if we can't name the container (e.g. a bare llama-server).
        #    Threshold: >512 MiB rules out driver/compositor noise.
        for g in gpus:
            idx = getattr(g, "index", -1)
            mem = getattr(g, "mem_used_mib", 0)
            if idx in wanted and mem > 512:
                result.gpu_conflicts.append(
                    GpuConflict(
                        gpu_index=idx,
                        mem_used_mib=mem,
                        note="GPU in use",
                    )
                )

        # 3. Active estate claims — the estate planner may have booted instances
        #    that hold cards even if our engine-prefix detect missed them.
        #    FAIL CLOSED: if the estate read errors, we cannot rule out a hidden
        #    estate claim on the wanted cards → UNSAFE.  (Previously the error
        #    was discarded → report={} → "no claims" → falsely safe.)
        report, estate_err = await self._run_json(
            ["python3", "scripts/lib/profiles/estate_cli.py", "report-state", "--json"],
            timeout=40.0,
        )
        if estate_err and report is None:
            result.note = f"estate read failed: {estate_err}"
            result.safe = False
            return result
        active = (report or {}).get("active_estate") or {}
        if active.get("present") and active.get("instances"):
            for inst in active["instances"]:
                inst_gpus = set(inst.get("gpus", []) or [])
                if inst_gpus & wanted:
                    result.estate_claims.append(inst)

        # 4. In-process pending-claim check (Fix 1 — TOCTOU).
        #    A concurrent write that has passed the gate and started its subprocess
        #    registers a pending claim UNDER the write lock before releasing it.
        #    Because reconcile_before_write is called UNDER the same lock by
        #    execute_action, a second writer will see the first's claim here and
        #    report a conflict even though docker ps / nvidia-smi may still show
        #    the cards as free (the first process hasn't booted yet).
        #    Expired claims (> TTL) are silently pruned.
        now = time.monotonic()
        expired = [tok for tok, (_, exp) in self._pending_claims.items() if now > exp]
        for tok in expired:
            self._pending_claims.pop(tok, None)
        for tok, (claimed_gpus, _exp) in self._pending_claims.items():
            if claimed_gpus & wanted:
                result.pending_claim_tokens.append(tok)
                result.note = (
                    result.note or f"in-flight write already claimed GPUs {sorted(claimed_gpus)}"
                )

        # Safe only if NOTHING overlaps.
        result.safe = not (
            result.conflicts or result.gpu_conflicts or result.estate_claims
            or result.pending_claim_tokens
        )
        if not result.safe and not result.note:
            result.note = f"would collide with: {result.conflict_summary}"
        return result

    # ── WRITE: action builders (wired, execution-gated) ──────────────────────────

    def serve(self, slug: str, *, force: bool = False, force_reason: str = "") -> ActionPlan:
        """Build the GATED switch.sh <slug> action.  ``--force`` is only added
        when explicitly requested WITH a reason (surfaced to the user)."""
        cmd = ["bash", "scripts/switch.sh"]
        if force:
            if not force_reason:
                raise ValueError("force=True requires a force_reason (surfaced to user)")
            cmd.append("--force")
        cmd.append(slug)
        desc = f"switch.sh {'--force ' if force else ''}{slug}"
        return ActionPlan(
            kind="serve",
            cmd=cmd,
            description=desc,
            force=force,
            force_reason=force_reason,
            requires_reconcile=True,
        )

    def set_default(self, slug: str) -> ActionPlan:
        return ActionPlan(
            kind="set_default",
            cmd=["bash", "scripts/switch.sh", "--set-default", slug],
            description=f"switch.sh --set-default {slug}",
            requires_reconcile=False,   # .env pin write — no GPU contention
        )

    def clear_default(self, model: str) -> ActionPlan:
        return ActionPlan(
            kind="clear_default",
            cmd=["bash", "scripts/switch.sh", "--clear-default", model],
            description=f"switch.sh --clear-default {model}",
            requires_reconcile=False,
        )

    def scene_switch(self, mode: str) -> ActionPlan:
        return ActionPlan(
            kind="scene",
            cmd=["bash", "scripts/gpu-mode.sh", mode],
            description=f"gpu-mode {mode}",
            requires_reconcile=True,
        )

    def estate_down(self) -> ActionPlan:
        return ActionPlan(
            kind="estate_down",
            cmd=["python3", "scripts/lib/profiles/estate_cli.py", "down"],
            description="estate_cli down",
            requires_reconcile=True,
        )

    def container_action(self, name: str, op: str) -> ActionPlan:
        """op ∈ {restart, stop}.  Builds a docker write — execution-gated."""
        if op not in ("restart", "stop"):
            raise ValueError(f"container op must be restart|stop, got {op!r}")
        return ActionPlan(
            kind="container",
            cmd=["docker", op, name],
            description=f"docker {op} {name}",
            requires_reconcile=(op == "stop"),
        )

    # ── WRITE: execution (gated — NEVER run in tests / this phase) ────────────────

    async def execute_action(
        self,
        plan: ActionPlan,
        *,
        parser: Any = None,
        run_type: Optional[str] = None,
        variants: Optional[list[VariantRow]] = None,
        skip_reconcile: bool = False,
    ) -> tuple[bool, Optional[ReconcileResult], Any]:
        """Execute a WRITE ActionPlan via the core SubprocessRunner.

        ⚠️  WRITE PATH.  The maintainer validates the first real serve / scene
        switch later; in this phase this is wired but execution is mocked in
        tests and NEVER run live.

        Always re-runs the reconcile gate first (unless the plan opts out or
        ``skip_reconcile`` is set — only honored when ``plan.force`` is True with
        a reason).  Returns ``(executed, reconcile_result, run_state)``.

        If the gate is unsafe and force isn't set, returns
        ``(False, reconcile_result, None)`` — refusing to write.

        SERIALIZED (design §3.2): the gate→write window is held under a single
        write lock so two confirmed plans can't both reconcile "safe" before
        either claims VRAM (the dual-writer TOCTOU).  A second write blocks on
        the lock until the first has finished claiming the cards, then runs its
        OWN fresh gate against the now-updated state.
        """
        # skip_reconcile is ONLY honored as an explicit, reasoned force override
        # (the docstring contract).  Enforce the coupling in code so a caller
        # can't silently bypass the safety gate.
        effective_skip = skip_reconcile and plan.force and bool(plan.force_reason)

        async with self._write_lock:
            reconcile: Optional[ReconcileResult] = None
            if plan.requires_reconcile and not effective_skip:
                reconcile = await self.reconcile_before_write(
                    f"{plan.kind}:{plan.description}", variants=variants
                )
                if not reconcile.safe:
                    # An in-flight write's pending claim is a HARD block — force
                    # CANNOT override it: a not-yet-booted write has nothing
                    # deterministic to tear down, so the user must cancel the
                    # in-flight write first (an explicit cancel path), not force
                    # over it. force only overrides *materialized* conflicts.
                    if reconcile.pending_claim_tokens:
                        return False, reconcile, None
                    if not plan.force:
                        return False, reconcile, None
                    if not plan.force_reason:
                        raise ValueError("force override requires force_reason")

            # Fix 1 (TOCTOU): register a pending claim for the GPUs this write
            # wants BEFORE calling start_raw and BEFORE releasing the lock.  The
            # reconcile above (while still under the lock) already checked
            # _pending_claims; by adding our claim here, the NEXT writer's
            # reconcile (also serialized by the lock) will see it — even if our
            # subprocess hasn't allocated any VRAM yet.
            claim_token = str(uuid.uuid4())
            if plan.requires_reconcile:
                # Infer the GPU set the plan wants.  pending_gpus from the last
                # reconcile is most accurate; fall back to conservative {0, 1}.
                if reconcile is not None and reconcile.pending_gpus:
                    claimed = frozenset(reconcile.pending_gpus)
                else:
                    claimed = frozenset({0, 1})
                expiry = time.monotonic() + self._claim_ttl
                self._pending_claims[claim_token] = (claimed, expiry)

            # Stream via the core runner.  No no-op parser → use a passthrough.
            run_parser = parser or _NullParser()
            import os as _os

            try:
                state = await self._write_runner.start_raw(
                    plan.cmd,
                    env=dict(_os.environ),
                    run_type=run_type or plan.kind,
                    parser=run_parser,
                )
            except Exception:
                # The spawn ITSELF failed — no card was claimed; release now.
                self._pending_claims.pop(claim_token, None)
                raise

        # NOTE: do NOT clear the claim here. start_raw only SPAWNS the write
        # (switch.sh/gpu-mode is still booting; no container/VRAM yet — start_raw
        # returns immediately, runner.py). Holding the claim until the subprocess
        # COMPLETES bridges the materialization gap: by the time the process
        # exits (switch.sh's wait_ready ⇒ container up + /v1/models answering),
        # docker ps / nvidia-smi see it, so the next reconcile is covered with no
        # gap. The write lock is released here (block exit) so a concurrent
        # writer gets an immediate "in-flight conflict" rather than hanging.
        if claim_token in self._pending_claims:
            if getattr(state, "is_finished", False):
                # start_raw returned an already-finished (spawn-failure) state.
                self._pending_claims.pop(claim_token, None)
            else:
                asyncio.create_task(self._release_claim_when_done(claim_token, state))

        return True, reconcile, state

    async def _release_claim_when_done(self, token: str, state: Any) -> None:
        """Hold the pending-GPU claim until the write subprocess COMPLETES, then
        release it. By completion the materialized container/VRAM is visible to
        the next reconcile (the clean handoff). Awaits the run's PER-RUN ``done``
        event (not the runner's shared ``current_run``), so overlapping runs are
        never misattributed. The TTL is only a leak backstop for a state that
        never signals done (a stub runner / a vanished process)."""
        done = getattr(state, "done", None)
        try:
            if done is not None:
                await asyncio.wait_for(done.wait(), timeout=self._claim_ttl)
        except asyncio.TimeoutError:
            pass
        finally:
            self._pending_claims.pop(token, None)


# ── helpers ──────────────────────────────────────────────────────────────────────


class _NullParser:
    """Passthrough parser for the write runner — emits no structured events."""

    def parse_line(self, line: str):  # noqa: D401 - protocol shim
        return None


# Rig services that hold a GPU but share no naming prefix with the engines /
# estate containers — matched by name so the reconcile gate sees them.
_GPU_SERVICE_NAMES = ("comfyui", "step-audio", "step-audio-editx")


def _classify_container_kind(name: str) -> Optional[str]:
    """Classify a docker-ps container name into a GPU-holder kind.

    Returns ``"engine"`` for the core engine prefixes, ``"estate"`` for the
    estate planner's ``club3090-`` containers, ``"service"`` for known
    GPU-holding rig services, else ``None`` (not a GPU holder we gate on — e.g.
    open-webui, redis)."""
    from club3090_tui_core.detect import ENGINE_PREFIXES

    if ENGINE_PREFIXES.match(name):
        return "engine"
    if name.startswith("club3090-"):
        return "estate"
    lname = name.lower()
    if any(svc in lname for svc in _GPU_SERVICE_NAMES):
        return "service"
    return None


def _container_gpu_set(c: ContainerInfo) -> Optional[set[int]]:
    """The set of GPU indices a container provably holds, or None if unknown.

    docker ps does not expose the device list, so this is None unless something
    upstream populated ``ContainerInfo.gpus`` (e.g. ``"0,1"`` / ``"1"``).  None
    means "unknown" → the gate stays conservative and treats it as a conflict."""
    raw = (getattr(c, "gpus", "") or "").strip()
    if not raw:
        return None
    out: set[int] = set()
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
    return out or None


def _variant_row_from_dict(d: dict[str, Any]) -> VariantRow:
    """Build a VariantRow from the registry-emit --json 'variants' dict.

    The --json variants block carries an extra ``source`` field not on the
    tab-row dataclass; it's attached as an attribute for the catalog 'source'
    column without altering the shared-core schema.
    """
    row = VariantRow(
        slug=str(d.get("slug", "")),
        switch_engine=str(d.get("switch_engine", "")),
        launch_engine=str(d.get("launch_engine", "")),
        compose_dir=str(d.get("compose_dir", "")),
        file=str(d.get("file", "")),
        port=int(d["port"]) if str(d.get("port", "")).isdigit() else 0,
        model=str(d.get("model", "")),
        engine=str(d.get("engine", "")),
        kvcalc_key=str(d.get("kvcalc_key", "")),
        container=str(d.get("container", "")),
        compose_path=str(d.get("compose_path", "")),
        status=str(d.get("status", "")),
        ctx_label=str(d.get("ctx_label", "")),
        status_note=str(d.get("status_note", "")),
    )
    # 'source' provenance (curated/community/local) — attach without schema change.
    src = d.get("source")
    if src:
        try:
            object.__setattr__(row, "source", str(src))
        except Exception:
            pass
    return row


def _extract_first_json(text: str) -> Any:
    """Recover the first balanced JSON value (object or array) from dirty stdout.

    Some contracts may interleave a banner line; this finds the first '{' or '['
    and decodes from there using raw_decode."""
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    return None
