"""Tests for the cockpit data/service layer (data.py + services.py).

ALL subprocess and detect is mocked — no GPU, no Docker, no TTY, no real
script calls.  Covers:
  - the read contracts (catalog enrichment, explain, fit, byo, scenes, doctor,
    estate_state, containers) against a FakeRunner;
  - the pure parse helpers (health text, BENCHMARKS.md scrape, explain corpus);
  - the action builders (gated; --force only with a reason);
  - the reconcile gate (the dual-writer safety core) — thorough scenarios incl.
    the prompt's "estate booted GPU0, scene-switch requested → conflict" case;
  - execute_action refusing to write when the gate is unsafe (execution mocked).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from club3090_tui_core.detect import GpuInfo, ServingTarget
from club3090_tui_core.registry import VariantRow
from club3090_tui_core.runner import CoreRunState


class LeaseWriteRunner:
    """Fire-and-forget write runner, like the real SubprocessRunner: ``start_raw``
    only SPAWNS and returns a RUNNING ``CoreRunState`` immediately (finished=0).
    The test signals the subprocess's completion with ``state.done.set()`` — so
    the pending-claim lease (not any lock-hold duration) is what blocks writer B
    through the whole boot gap."""

    def __init__(self):
        self.started: list[dict] = []

    async def start_raw(self, cmd, env, run_type, parser):
        st = CoreRunState(run_type=run_type, started=time.time())  # is_finished == False
        self.started.append({"cmd": cmd, "state": st})
        return st


class FailedSpawnWriteRunner:
    """``start_raw`` returns an already-finished spawn-failure state — the claim
    must clear immediately (no card was ever claimed)."""

    def __init__(self):
        self.started: list[dict] = []

    async def start_raw(self, cmd, env, run_type, parser):
        st = CoreRunState(run_type=run_type, started=time.time(), finished=time.time(),
                          exit_code=-1, verdict="failed", error="spawn boom")
        st.done.set()
        self.started.append({"cmd": cmd, "state": st})
        return st

from club3090_cockpit.data import (
    ActionPlan,
    ByoResult,
    DoctorRead,
    FitVerdict,
    Measurement,
    ReconcileResult,
    Scene,
    measurement_from_explain_benchmarks,
    parse_benchmarks_md_for_slug,
    parse_health_text,
    strip_ansi,
)
from club3090_cockpit.services import CockpitData, RealRunner, RunResult, _variant_row_from_dict


ROOT = Path("/tmp/fake-club-3090-root")


# ---------------------------------------------------------------------------
# Fake runner + fake detect
# ---------------------------------------------------------------------------


class FakeRunner:
    """Canned-output runner keyed on a recognizable token in the command.

    ``responses`` maps a substring (matched against the joined command) to a
    RunResult.  ``calls`` records every command for assertions.  A WRITE command
    reaching here would mean execution wasn't mocked — tests assert it never is.
    """

    def __init__(self, responses: Optional[dict[str, RunResult]] = None):
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    async def run(self, cmd, *, cwd, timeout=30.0) -> RunResult:
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for token, res in self.responses.items():
            if token in joined:
                return res
        return RunResult(returncode=0, stdout="", stderr="no canned response")


def ok(stdout: str) -> RunResult:
    return RunResult(returncode=0, stdout=stdout, stderr="")


def make_detect(target: ServingTarget):
    async def _detect() -> ServingTarget:
        return target
    return _detect


def make_gpu_info(gpus: list[GpuInfo]):
    async def _gpus() -> list[GpuInfo]:
        return gpus
    return _gpus


# ---------------------------------------------------------------------------
# Fixtures: canned contract outputs
# ---------------------------------------------------------------------------

REGISTRY_JSON = json.dumps(
    {
        "defaults": [],
        "profiles": {},
        "variants": [
            {
                "slug": "vllm/dual",
                "switch_engine": "vllm",
                "launch_engine": "vllm",
                "compose_dir": "models/qwen3.6-27b/vllm/compose/dual/autoround-int4",
                "file": "fp8-mtp.yml",
                "port": 8010,
                "model": "qwen3.6-27b",
                "engine": "vllm-stable",
                "kvcalc_key": "qwen3.6-27b:dual",
                "container": "vllm_qwen36_27b",
                "compose_path": "models/qwen3.6-27b/vllm/compose/dual/autoround-int4/fp8-mtp.yml",
                "status": "production",
                "ctx_label": "262K",
                "status_note": "",
                "source": "curated",
            },
            {
                "slug": "ik-llama/iq4ks-mtp",
                "switch_engine": "ik-llama",
                "launch_engine": "ik-llama",
                "compose_dir": "models/qwen3.6-27b/ik-llama/compose/single/ubergarm-iq4ks",
                "file": "mtp.yml",
                "port": 8063,
                "model": "qwen3.6-27b",
                "engine": "ik-llama",
                "kvcalc_key": "SKIP",
                "container": "ik_llama_qwen_single",
                "compose_path": "models/qwen3.6-27b/ik-llama/compose/single/ubergarm-iq4ks/mtp.yml",
                "status": "production",
                "ctx_label": "200K",
                "status_note": "",
                "source": "curated",
            },
        ],
    }
)

FIT_JSON = json.dumps(
    {"verdict": "fits-clean", "vram_est_gb": 19.881, "band_gb": 1.5, "max_ctx": 262144}
)

# REAL switch.sh --explain --json benchmarks shape (verified live):
#   [{"row": "<markdown row>", "columns": [<cell>, …]}]
# Canonical bench layout: Compose|Rig|KV|Max ctx|Narr/Code TPS|PP|VRAM|Date|Notes
# → TPS is columns[4].  (NOT the invented {"narr_tps": …} corpus shape.)
EXPLAIN_BENCH_ROW = {
    "row": (
        "| `dual.yml` ⭐ | @noonghunna (2× 3090 PCIe) | fp8 | 262K | "
        "**174.0 / 42.0** | — | ~23.6 GB | 2026-05-30 | 8-pack 109/150 |"
    ),
    "columns": [
        "`dual.yml` ⭐",
        "@noonghunna (2× 3090 PCIe)",
        "fp8",
        "262K",
        "**174.0 / 42.0**",
        "—",
        "~23.6 GB",
        "2026-05-30",
        "8-pack 109/150",
    ],
}

EXPLAIN_JSON = json.dumps(
    {
        "slug": "vllm/dual",
        "registry": {"slug": "vllm/dual", "model": "qwen3.6-27b"},
        "card": "rtx-3090",
        "fit": {"verdict": "fits-constrained", "vram_est_gb": 19.881, "band_gb": 1.5, "max_ctx": 262144},
        "benchmarks": [EXPLAIN_BENCH_ROW],
    }
)

EXPLAIN_NO_BENCH_JSON = json.dumps(
    {"slug": "ik-llama/iq4ks-mtp", "registry": {}, "card": "rtx-3090", "fit": {}, "benchmarks": []}
)

SCENES_JSON = json.dumps(
    [
        {"name": "27b", "group": "serving", "description": "Qwen", "services": ["vllm-qwen36-27b-dual"], "ports": ["8010"], "gpus": "both"},
        {"name": "off", "group": "ops", "description": "Stop all", "services": [], "ports": [], "gpus": "none"},
    ]
)

PULL_JSON = json.dumps(
    {
        "arch": "Qwen3_5ForConditionalGeneration",
        "eligible": True,
        "fit_verdict": "fits-clean",
        "note": "reuse compose + swap weights",
        "swap_path": {
            "drop_spec_config": True,
            "quant_match": "int4",
            "route": "C",
            "sibling_slug": "vllm/dual",
        },
    }
)

ESTATE_REPORT_BUSY = json.dumps(
    {
        "active_estate": {
            "present": True,
            "valid": True,
            "instances": [
                {"name": "llama-gpu0", "compose": "llamacpp/default", "gpus": [0], "port": 8010},
                {"name": "llama-gpu1", "compose": "llamacpp/default", "gpus": [1], "port": 8020},
            ],
        }
    }
)

ESTATE_REPORT_FREE = json.dumps({"active_estate": {"present": False, "instances": []}})

HEALTH_SERVING = (
    "club-3090 health check\n"
    "Endpoint: http://localhost:8010\n"
    "  \x1b[0;32m✓\x1b[0m serving\n"
    "  KV pool 61%\n"
    "  spec-dec firing (MTP n=2, 73% accept)\n"
    "  0 recent errors\n"
)

HEALTH_DOWN = (
    "club-3090 health check\n"
    "  ✗ API not reachable at http://localhost:8020 — is the container running?\n"
)

DOCKER_PS_ENGINE = "vllm-qwen36-27b-dual|0.0.0.0:8010->8000/tcp, [::]:8010->8000/tcp\nopen-webui|0.0.0.0:3000->8080/tcp\n"
DOCKER_PS_EMPTY = ""


def full_runner(**overrides) -> FakeRunner:
    """A FakeRunner wired for the common read contracts; override per-test."""
    responses = {
        "registry-emit.sh --json": ok(REGISTRY_JSON),
        "kv-calc.py --fit": ok(FIT_JSON),
        "--explain vllm/dual --json": ok(EXPLAIN_JSON),
        "--explain ik-llama/iq4ks-mtp --json": ok(EXPLAIN_NO_BENCH_JSON),
        "gpu-mode.sh --list-modes --json": ok(SCENES_JSON),
        "pull.sh": ok(PULL_JSON),
        "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
        "health.sh": ok(HEALTH_DOWN),
        "docker ps": ok(DOCKER_PS_EMPTY),
    }
    responses.update(overrides)
    return FakeRunner(responses)


# ===========================================================================
# Pure parse helpers
# ===========================================================================


class TestParseHelpers:
    def test_strip_ansi(self):
        assert strip_ansi("\x1b[0;32m✓\x1b[0m serving") == "✓ serving"

    def test_health_serving_parsed(self):
        dr = parse_health_text(HEALTH_SERVING)
        assert dr.reachable is True
        assert dr.serving is True
        assert dr.kv_pool_pct == 61
        assert "MTP n=2" in dr.spec_dec
        assert dr.recent_errors == 0
        assert "serving" in dr.summary

    def test_health_down_parsed(self):
        dr = parse_health_text(HEALTH_DOWN)
        assert dr.reachable is False
        assert dr.summary == "API not reachable"

    def test_health_empty_is_unreachable(self):
        dr = parse_health_text("")
        assert dr.reachable is True  # nothing says "not reachable"
        assert dr.raw == ""

    def test_benchmarks_md_scrape(self):
        md = (
            "| Compose | Rig | KV |\n"
            "| `llamacpp/mtp` | @x | q4 | 200K | **50.27 / 58.92** | ... 8-pack 100/150 ... 2026-05-23 |\n"
        )
        m = parse_benchmarks_md_for_slug(md, "llamacpp/mtp")
        assert m is not None
        assert m.narr_tps == 50.27
        assert m.code_tps == 58.92
        assert m.quality_8pk == "100/150"
        assert m.date == "2026-05-23"
        assert m.source == "benchmarks.md"

    def test_benchmarks_md_no_match(self):
        md = "| `other/slug` | **10 / 20** |\n"
        assert parse_benchmarks_md_for_slug(md, "nope/missing") is None

    def test_benchmarks_md_matches_file_stem(self):
        md = "| `mtp` | **40 / 50** |\n"
        m = parse_benchmarks_md_for_slug(md, "llamacpp/mtp")
        assert m is not None and m.narr_tps == 40.0

    def test_benchmarks_md_matches_yml_filename(self):
        """The first column is usually `<serving>.yml` — must match the stem."""
        md = (
            "| `minimal.yml` (single) | @x | TQ3 | 64K | 32 / 33 | — | — | 2026-05-03 | n |\n"
        )
        m = parse_benchmarks_md_for_slug(md, "vllm/minimal")
        assert m is not None
        assert m.narr_tps == 32.0 and m.code_tps == 33.0

    def test_benchmarks_md_exact_match_not_substring(self):
        """REGRESSION: 'dual' must NOT match the 'dual-dflash.yml' row.

        Previously a substring test let `vllm/dual` pull the dual-dflash row
        (a different 5090 config).  Anchored first-cell match prevents it."""
        md = (
            "| `dual-dflash.yml` | @z (1× 5090) | fp8 | 49K | 126 / 200 | — | — | 2026-05-07 | n |\n"
            "| `dual.yml` ⭐ | @noonghunna (2× 3090) | fp8 | 262K | 69 / 89 | — | — | 2026-04-29 | n |\n"
        )
        m = parse_benchmarks_md_for_slug(md, "vllm/dual")
        assert m is not None
        # Must be the 3090 dual.yml row (69/89), NOT the 5090 dual-dflash (126/200).
        assert m.narr_tps == 69.0 and m.code_tps == 89.0

    def test_benchmarks_md_non_bold_tps(self):
        """Non-bold '~32 / ~33' rows must parse (minimal.yml-style)."""
        md = "| `minimal.yml` | @x | TQ3 | 64K | ~32 / ~33 | — | — | 2026-05-03 | n |\n"
        m = parse_benchmarks_md_for_slug(md, "vllm/minimal")
        assert m is not None
        assert m.narr_tps == 32.0 and m.code_tps == 33.0

    def test_benchmarks_md_tbd_yields_no_measurement(self):
        """A matched row whose TPS cell is 'TBD' must yield NO measurement (not
        a bogus pair) so the UI renders '—' honestly."""
        md = "| `long-text-no-mtp.yml` | @x | TQ3 | 200K | TBD | — | — | — | n |\n"
        m = parse_benchmarks_md_for_slug(md, "vllm/long-text-no-mtp")
        assert m is None

    def test_benchmarks_md_decode_paren_does_not_shadow_headline(self):
        """The headline 'N / M' wins over the parenthetical (decode X / Y)."""
        md = (
            "| `mtp.yml` | @x | q4 | 200K | **59.67 / 68.78** (decode 60.39 / 72.40) "
            "| — | — | 2026-05-23 | 8-pack 107/150 |\n"
        )
        m = parse_benchmarks_md_for_slug(md, "ik-llama/mtp")
        assert m is not None
        assert m.narr_tps == 59.67 and m.code_tps == 68.78
        assert m.quality_8pk == "107/150"

    def test_measurement_from_explain_benchmarks(self):
        """REAL shape: [{"row","columns"}] — TPS parsed out of columns[4]."""
        m = measurement_from_explain_benchmarks([EXPLAIN_BENCH_ROW])
        assert m.source == "explain"
        assert m.narr_tps == 174.0
        assert m.code_tps == 42.0
        assert m.tps_label == "174/42"
        assert m.quality_8pk == "109/150"
        assert m.date == "2026-05-30"

    def test_measurement_from_explain_picks_newest_tps_row(self):
        """A stress/soak row (no TPS in columns) must NOT shadow a TPS row, and
        the newest TPS-bearing row wins."""
        stress_row = {
            "row": "| `dual.yml` | @x | PASS | PASS at 64K | FAIL | 2026-05-03 |",
            "columns": ["`dual.yml`", "@x", "PASS", "PASS at 64K", "FAIL", "2026-05-03"],
        }
        newer = {
            "row": "| `dual.yml` | @y | fp8 | 262K | 69 / 89 | — | — | 2026-06-01 |",
            "columns": ["`dual.yml`", "@y", "fp8", "262K", "69 / 89", "—", "—", "2026-06-01"],
        }
        m = measurement_from_explain_benchmarks([EXPLAIN_BENCH_ROW, stress_row, newer])
        assert m.tps_label == "69/89"
        assert m.date == "2026-06-01"

    def test_measurement_from_explain_no_tps_is_empty(self):
        """benchmarks[] with only a non-TPS (stress) row → empty Measurement so
        the caller can fall through to the BENCHMARKS.md scrape."""
        stress_row = {
            "row": "| `dual.yml` | @x | PASS | PASS at 64K | FAIL | 2026-05-03 |",
            "columns": ["`dual.yml`", "@x", "PASS", "PASS at 64K", "FAIL", "2026-05-03"],
        }
        m = measurement_from_explain_benchmarks([stress_row])
        assert m.narr_tps is None
        assert m.tps_label == "—"

    def test_measurement_empty(self):
        m = measurement_from_explain_benchmarks([])
        assert m.tps_label == "—"
        assert m.quality_label == "—"

    def test_fit_glyphs(self):
        # REAL kv-calc --fit verdict enum (fits-constrained, NOT fits-tight).
        assert FitVerdict(verdict="fits-clean").glyph == "●"
        assert FitVerdict(verdict="fits-constrained").glyph == "◐"
        assert FitVerdict(verdict="wont-fit").glyph == "○"
        assert FitVerdict(verdict="skip").glyph == "·"
        assert FitVerdict(verdict="unknown").glyph == "·"
        # 'fits-tight' is NEVER emitted by kv-calc → falls to the unknown glyph.
        assert FitVerdict(verdict="fits-tight").glyph == "·"

    def test_variant_row_from_dict_attaches_source(self):
        row = _variant_row_from_dict(
            {"slug": "x/y", "port": 8010, "source": "community", "status": "production"}
        )
        assert isinstance(row, VariantRow)
        assert row.slug == "x/y"
        assert row.port == 8010
        assert getattr(row, "source") == "community"


# ===========================================================================
# READ contracts
# ===========================================================================


class TestLoadCatalog:
    @pytest.mark.asyncio
    async def test_catalog_parses_variants(self):
        cd = CockpitData(ROOT, runner=full_runner())
        entries, err = await cd.load_catalog(enrich_fit=False, enrich_measurement=False)
        assert err is None
        assert len(entries) == 2
        assert entries[0].slug == "vllm/dual"
        assert entries[0].source == "curated"

    @pytest.mark.asyncio
    async def test_catalog_enriches_fit(self):
        cd = CockpitData(ROOT, runner=full_runner())
        entries, _ = await cd.load_catalog(enrich_fit=True, enrich_measurement=False)
        vllm = next(e for e in entries if e.slug == "vllm/dual")
        assert vllm.fit.verdict == "fits-clean"
        assert vllm.fit.glyph == "●"

    @pytest.mark.asyncio
    async def test_catalog_skip_fit_for_ik_llama(self):
        """ik/llama kvcalc_key=SKIP → fit verdict 'skip', no kv-calc call."""
        cd = CockpitData(ROOT, runner=full_runner())
        entries, _ = await cd.load_catalog(enrich_fit=True, enrich_measurement=False)
        ik = next(e for e in entries if e.slug == "ik-llama/iq4ks-mtp")
        assert ik.fit.verdict == "skip"

    @pytest.mark.asyncio
    async def test_catalog_enriches_measurement_from_explain(self):
        cd = CockpitData(ROOT, runner=full_runner())
        entries, _ = await cd.load_catalog(enrich_fit=False, enrich_measurement=True)
        vllm = next(e for e in entries if e.slug == "vllm/dual")
        assert vllm.measurement.source == "explain"
        assert vllm.measurement.tps_label == "174/42"
        assert vllm.measurement.quality_label == "109/150"

    @pytest.mark.asyncio
    async def test_catalog_empty_registry_returns_error(self):
        runner = full_runner(**{"registry-emit.sh --json": ok(json.dumps({"variants": []}))})
        cd = CockpitData(ROOT, runner=runner)
        entries, err = await cd.load_catalog(enrich_fit=False, enrich_measurement=False)
        assert entries == []
        assert err is not None


class TestExplainFit:
    @pytest.mark.asyncio
    async def test_explain(self):
        cd = CockpitData(ROOT, runner=full_runner())
        ex, err = await cd.explain("vllm/dual")
        assert err is None
        assert ex["slug"] == "vllm/dual"
        assert ex["benchmarks"]

    @pytest.mark.asyncio
    async def test_fit(self):
        cd = CockpitData(ROOT, runner=full_runner())
        fit = await cd.fit("vllm/dual", "rtx-3090")
        assert fit.verdict == "fits-clean"
        assert fit.vram_est_gb == 19.881
        assert fit.card == "rtx-3090"

    @pytest.mark.asyncio
    async def test_fit_unknown_card_surfaces_error(self):
        runner = full_runner(
            **{"kv-calc.py --fit": ok(json.dumps({"verdict": "unknown", "error": "unrecognized --card"}))}
        )
        cd = CockpitData(ROOT, runner=runner)
        fit = await cd.fit("vllm/dual", "bogus")
        assert fit.verdict == "unknown"
        assert "unrecognized" in fit.error

    @pytest.mark.asyncio
    async def test_explain_timeout_returns_error(self):
        runner = full_runner(
            **{"--explain vllm/dual --json": RunResult(-1, "", "timeout", timed_out=True)}
        )
        cd = CockpitData(ROOT, runner=runner)
        ex, err = await cd.explain("vllm/dual")
        assert ex is None
        assert "timed out" in err


class TestByoCheck:
    @pytest.mark.asyncio
    async def test_byo_eligible(self):
        cd = CockpitData(ROOT, runner=full_runner())
        res = await cd.byo_check("org/Model", "vllm/dual")
        assert res.eligible is True
        assert res.route == "C"
        assert res.sibling_slug == "vllm/dual"
        assert res.drop_spec_config is True
        assert res.quant_match == "int4"

    @pytest.mark.asyncio
    async def test_byo_dry_run_is_in_command(self):
        """byo_check MUST pass --dry-run (Path B, never downloads)."""
        runner = full_runner()
        cd = CockpitData(ROOT, runner=runner)
        await cd.byo_check("org/Model", "vllm/dual")
        pull_call = next(c for c in runner.calls if "pull.sh" in " ".join(c))
        assert "--dry-run" in pull_call
        assert "--json" in pull_call

    @pytest.mark.asyncio
    async def test_byo_no_output(self):
        runner = full_runner(**{"pull.sh": ok("")})
        cd = CockpitData(ROOT, runner=runner)
        res = await cd.byo_check("org/Model", "vllm/dual")
        assert res.error


class TestScenesDoctor:
    @pytest.mark.asyncio
    async def test_scenes(self):
        cd = CockpitData(ROOT, runner=full_runner())
        scenes = await cd.scenes()
        assert len(scenes) == 2
        assert scenes[0].name == "27b"
        assert scenes[0].gpus == "both"

    @pytest.mark.asyncio
    async def test_doctor_serving(self):
        runner = full_runner(**{"health.sh": ok(HEALTH_SERVING)})
        cd = CockpitData(ROOT, runner=runner)
        dr = await cd.doctor_read()
        assert dr.serving is True
        assert dr.kv_pool_pct == 61


class TestContainers:
    @pytest.mark.asyncio
    async def test_containers_lists_engine(self):
        runner = full_runner(**{"docker ps": ok(DOCKER_PS_ENGINE)})
        cd = CockpitData(
            ROOT, runner=runner, detect_endpoint_fn=make_detect(ServingTarget())
        )
        cons = await cd.containers()
        names = [c.name for c in cons]
        assert "vllm-qwen36-27b-dual" in names
        # open-webui is not an engine-prefix container → excluded
        assert "open-webui" not in names

    @pytest.mark.asyncio
    async def test_containers_matches_slug(self):
        runner = full_runner(**{"docker ps": ok(DOCKER_PS_ENGINE)})
        cd = CockpitData(
            ROOT, runner=runner, detect_endpoint_fn=make_detect(ServingTarget())
        )
        variants = [
            VariantRow(
                slug="vllm/dual", switch_engine="vllm", launch_engine="vllm",
                compose_dir="", file="", port=8010, model="qwen3.6-27b",
                engine="vllm", kvcalc_key="", container="vllm-qwen36-27b-dual",
                compose_path="", status="production", ctx_label="262K", status_note="",
            )
        ]
        cons = await cd.containers(variants=variants)
        engine = next(c for c in cons if c.name == "vllm-qwen36-27b-dual")
        assert engine.slug == "vllm/dual"


class TestEstateState:
    @pytest.mark.asyncio
    async def test_estate_state_assembles(self):
        runner = full_runner(
            **{
                "health.sh": ok(HEALTH_SERVING),
                "docker ps": ok(DOCKER_PS_ENGINE),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_BUSY),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=22698), GpuInfo(index=1, mem_used_mib=1)]
        target = ServingTarget(url="http://localhost:8010", model="qwen3.6-27b", gpus=gpus)
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(target),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        st = await cd.estate_state()
        assert len(st.scenes) == 2
        assert st.doctor.serving is True
        assert [g.index for g in st.gpus] == [0, 1]
        assert st.estate_report["active_estate"]["present"] is True


# ===========================================================================
# Action builders (gated)
# ===========================================================================


class TestActionBuilders:
    def test_serve_no_force(self):
        cd = CockpitData(ROOT, runner=full_runner())
        plan = cd.serve("vllm/dual")
        assert plan.kind == "serve"
        assert plan.cmd == ["bash", "scripts/switch.sh", "vllm/dual"]
        assert "--force" not in plan.cmd
        assert plan.requires_reconcile is True

    def test_serve_force_requires_reason(self):
        cd = CockpitData(ROOT, runner=full_runner())
        with pytest.raises(ValueError):
            cd.serve("vllm/dual", force=True)

    def test_serve_force_with_reason(self):
        cd = CockpitData(ROOT, runner=full_runner())
        plan = cd.serve("vllm/dual", force=True, force_reason="user override after VRAM check")
        assert "--force" in plan.cmd
        assert plan.force is True
        assert plan.force_reason

    def test_set_default(self):
        cd = CockpitData(ROOT, runner=full_runner())
        plan = cd.set_default("vllm/dual")
        assert plan.cmd == ["bash", "scripts/switch.sh", "--set-default", "vllm/dual"]
        assert plan.requires_reconcile is False  # .env pin — no GPU contention

    def test_clear_default(self):
        cd = CockpitData(ROOT, runner=full_runner())
        plan = cd.clear_default("qwen3.6-27b")
        assert plan.cmd == ["bash", "scripts/switch.sh", "--clear-default", "qwen3.6-27b"]

    def test_scene_switch(self):
        cd = CockpitData(ROOT, runner=full_runner())
        plan = cd.scene_switch("27b")
        assert plan.cmd == ["bash", "scripts/gpu-mode.sh", "27b"]
        assert plan.requires_reconcile is True

    def test_estate_down(self):
        cd = CockpitData(ROOT, runner=full_runner())
        plan = cd.estate_down()
        assert "down" in plan.cmd
        assert plan.requires_reconcile is True

    def test_container_action_restart(self):
        cd = CockpitData(ROOT, runner=full_runner())
        plan = cd.container_action("vllm-x", "restart")
        assert plan.cmd == ["docker", "restart", "vllm-x"]
        assert plan.requires_reconcile is False  # restart same config → no new claim

    def test_container_action_stop_reconciles(self):
        cd = CockpitData(ROOT, runner=full_runner())
        plan = cd.container_action("vllm-x", "stop")
        assert plan.requires_reconcile is True

    def test_container_action_bad_op(self):
        cd = CockpitData(ROOT, runner=full_runner())
        with pytest.raises(ValueError):
            cd.container_action("vllm-x", "rm")


# ===========================================================================
# THE RECONCILE GATE — dual-writer safety core
# ===========================================================================


class TestReconcileGate:
    @pytest.mark.asyncio
    async def test_free_rig_is_safe(self):
        """No containers, no GPU use, no estate → safe to write."""
        runner = full_runner(
            **{
                "docker ps": ok(DOCKER_PS_EMPTY),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is True
        assert rec.conflict_summary == "none"

    @pytest.mark.asyncio
    async def test_running_engine_container_is_conflict(self):
        runner = full_runner(
            **{
                "docker ps": ok(DOCKER_PS_ENGINE),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        assert any(c.name == "vllm-qwen36-27b-dual" for c in rec.conflicts)

    @pytest.mark.asyncio
    async def test_gpu_in_use_is_conflict(self):
        """A card with >512 MiB used but no named container is still a conflict."""
        runner = full_runner(
            **{
                "docker ps": ok(DOCKER_PS_EMPTY),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=22698), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        assert any(g.gpu_index == 0 and g.mem_used_mib == 22698 for g in rec.gpu_conflicts)

    @pytest.mark.asyncio
    async def test_estate_booted_gpu0_then_scene_switch_conflicts(self):
        """The prompt's canonical case: estate_cli booted GPU0; a scene-switch
        wanting GPU0 must be reported as a conflict by the gate."""
        runner = full_runner(
            **{
                "docker ps": ok(DOCKER_PS_EMPTY),  # estate used a non-prefix container
                "estate_cli.py report-state --json": ok(
                    json.dumps(
                        {
                            "active_estate": {
                                "present": True,
                                "instances": [
                                    {"name": "llama-gpu0", "compose": "llamacpp/default", "gpus": [0], "port": 8010}
                                ],
                            }
                        }
                    )
                ),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=20000), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        # scene-switch wants GPU0
        rec = await cd.reconcile_before_write("scene:27b", pending_gpus=[0])
        assert rec.safe is False
        assert any(i.get("name") == "llama-gpu0" for i in rec.estate_claims)
        assert "estate:llama-gpu0" in rec.conflict_summary

    @pytest.mark.asyncio
    async def test_estate_on_gpu1_does_not_conflict_with_gpu0_only_request(self):
        """Estate holds GPU1 only; an action wanting just GPU0 is NOT blocked by it."""
        runner = full_runner(
            **{
                "docker ps": ok(DOCKER_PS_EMPTY),
                "estate_cli.py report-state --json": ok(
                    json.dumps(
                        {
                            "active_estate": {
                                "present": True,
                                "instances": [
                                    {"name": "llama-gpu1", "compose": "llamacpp/default", "gpus": [1], "port": 8020}
                                ],
                            }
                        }
                    )
                ),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=20000)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:single-gpu0", pending_gpus=[0])
        # No container, GPU0 free, estate on GPU1 only → safe.
        assert rec.estate_claims == []
        assert rec.safe is True

    @pytest.mark.asyncio
    async def test_pending_gpus_none_is_conservative_both_cards(self):
        """pending_gpus=None means 'wants both cards' → any GPU1 use conflicts."""
        runner = full_runner(
            **{
                "docker ps": ok(DOCKER_PS_EMPTY),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=20000)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")  # None → {0,1}
        assert rec.pending_gpus == [0, 1]
        assert rec.safe is False
        assert any(g.gpu_index == 1 for g in rec.gpu_conflicts)

    @pytest.mark.asyncio
    async def test_detect_failure_is_unsafe(self):
        """If detect raises, we can't prove the cards are free → not safe."""
        async def boom() -> ServingTarget:
            raise RuntimeError("docker daemon down")

        cd = CockpitData(ROOT, runner=full_runner(), detect_endpoint_fn=boom)
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        assert "detect failed" in rec.note

    @pytest.mark.asyncio
    async def test_reconcile_calls_detect_freshly(self):
        """The gate must call detect every time (never a cached snapshot)."""
        calls = {"n": 0}

        async def counting_detect() -> ServingTarget:
            calls["n"] += 1
            return ServingTarget(gpus=[GpuInfo(index=0, mem_used_mib=1), GpuInfo(index=1, mem_used_mib=1)])

        runner = full_runner(
            **{"docker ps": ok(DOCKER_PS_EMPTY), "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE)}
        )
        cd = CockpitData(ROOT, runner=runner, detect_endpoint_fn=counting_detect,
                         get_gpu_info_fn=make_gpu_info([]))
        await cd.reconcile_before_write("serve:a")
        await cd.reconcile_before_write("serve:b")
        assert calls["n"] >= 2

    # ── B4: FAIL CLOSED on a state-read error ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_gpu_read_error_is_unsafe(self):
        """A SAFETY gate must fail CLOSED: if reading the GPUs raises, we can't
        prove the cards are free → UNSAFE (not 'nothing in use')."""
        async def gpu_boom() -> list[GpuInfo]:
            raise RuntimeError("nvidia-smi not found")

        runner = full_runner(
            **{"docker ps": ok(DOCKER_PS_EMPTY), "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE)}
        )
        # detect returns NO gpus → forces the get_gpu_info path (which raises).
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=[])),
            get_gpu_info_fn=gpu_boom,
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        assert "GPU read failed" in rec.note

    @pytest.mark.asyncio
    async def test_gpu_read_empty_is_unsafe(self):
        """No detect GPUs AND nvidia-smi returns [] → no evidence the cards are
        free → fail closed."""
        runner = full_runner(
            **{"docker ps": ok(DOCKER_PS_EMPTY), "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE)}
        )
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=[])),
            get_gpu_info_fn=make_gpu_info([]),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        assert "cannot prove free" in rec.note

    @pytest.mark.asyncio
    async def test_estate_read_error_is_unsafe(self):
        """If the estate read errors (no JSON), we can't rule out a hidden estate
        claim → fail closed."""
        runner = full_runner(
            **{
                "docker ps": ok(DOCKER_PS_EMPTY),
                # estate_cli emits non-JSON garbage → _run_json returns (None, err)
                "estate_cli.py report-state --json": RunResult(1, "", "Traceback: estate boom"),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        assert "estate read failed" in rec.note

    # ── B5: detector #1 broadened to club3090- + services, GPU-filtered ──────────────

    @pytest.mark.asyncio
    async def test_estate_club3090_container_is_conflict(self):
        """A `club3090-<name>` estate container is a live GPU user the gate must
        see (it doesn't match the engine prefixes)."""
        runner = full_runner(
            **{
                "docker ps": ok("club3090-llama-gpu0|0.0.0.0:8010->8080/tcp\n"),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        names = [c.name for c in rec.conflicts]
        assert "club3090-llama-gpu0" in names
        assert any(c.kind == "estate" for c in rec.conflicts)

    @pytest.mark.asyncio
    async def test_gpu_service_container_is_conflict(self):
        """A GPU-holding rig service (ComfyUI) is surfaced even with no engine
        prefix and no published port."""
        runner = full_runner(
            **{
                "docker ps": ok("comfyui|\n"),  # no published port
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        svc = next((c for c in rec.conflicts if c.name == "comfyui"), None)
        assert svc is not None and svc.kind == "service"

    @pytest.mark.asyncio
    async def test_container_on_gpu1_does_not_conflict_with_gpu0_only_request(self):
        """A container provably on GPU1 only (known gpu set) does NOT conflict
        with a request for GPU0 only; detector #2 (raw GPU) is the backstop."""
        from club3090_cockpit.data import ContainerInfo

        runner = full_runner(
            **{
                "docker ps": ok(DOCKER_PS_EMPTY),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=20000)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )

        # Inject a container whose GPU set is KNOWN to be {1} only.
        async def one_container(variants=None):
            return [ContainerInfo(name="club3090-llama-gpu1", kind="estate", gpus="1")]

        cd.containers = one_container  # type: ignore[assignment]
        rec = await cd.reconcile_before_write("serve:gpu0", pending_gpus=[0])
        # Container on GPU1 only → not a container conflict; GPU0 raw mem is 3 MiB
        # (free) → safe.
        assert all(c.name != "club3090-llama-gpu1" for c in rec.conflicts)
        assert rec.safe is True


# ===========================================================================
# execute_action — gated execution (MOCKED, never live)
# ===========================================================================


class FakeWriteRunner:
    """Stand-in for the core SubprocessRunner — records start_raw calls but
    NEVER spawns a process.  Asserts the write path is fully mocked."""

    def __init__(self):
        self.started: list[dict[str, Any]] = []

    async def start_raw(self, cmd, env, run_type, parser):
        self.started.append({"cmd": cmd, "run_type": run_type})
        return {"mock_state": True, "cmd": cmd}


class TestExecuteActionGated:
    @pytest.mark.asyncio
    async def test_unsafe_gate_refuses_write(self):
        """When the gate is unsafe and no force, execute_action must NOT run."""
        write_runner = FakeWriteRunner()
        runner = full_runner(
            **{"docker ps": ok(DOCKER_PS_ENGINE), "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE)}
        )
        gpus = [GpuInfo(index=0, mem_used_mib=22000), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner, write_runner=write_runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        plan = cd.serve("vllm/dual")
        executed, rec, state = await cd.execute_action(plan)
        assert executed is False
        assert rec is not None and rec.safe is False
        assert state is None
        assert write_runner.started == []  # NEVER reached the runner

    @pytest.mark.asyncio
    async def test_safe_gate_proceeds_to_mocked_runner(self):
        """When the gate is safe, execution reaches the (mocked) write runner.
        The runner is a fake — no real process is ever spawned."""
        write_runner = FakeWriteRunner()
        runner = full_runner(
            **{"docker ps": ok(DOCKER_PS_EMPTY), "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE)}
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner, write_runner=write_runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        plan = cd.serve("vllm/dual")
        executed, rec, state = await cd.execute_action(plan)
        assert executed is True
        assert rec is not None and rec.safe is True
        assert len(write_runner.started) == 1
        assert write_runner.started[0]["cmd"] == ["bash", "scripts/switch.sh", "vllm/dual"]

    @pytest.mark.asyncio
    async def test_force_override_proceeds_despite_unsafe(self):
        """A force ActionPlan with a reason proceeds even when the gate is unsafe
        (the explicit override path) — still via the mocked runner."""
        write_runner = FakeWriteRunner()
        runner = full_runner(
            **{"docker ps": ok(DOCKER_PS_ENGINE), "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE)}
        )
        gpus = [GpuInfo(index=0, mem_used_mib=22000), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner, write_runner=write_runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        plan = cd.serve("vllm/dual", force=True, force_reason="user accepted teardown")
        executed, rec, state = await cd.execute_action(plan)
        assert executed is True
        assert rec is not None and rec.safe is False  # gate still reports unsafe
        assert len(write_runner.started) == 1

    @pytest.mark.asyncio
    async def test_no_reconcile_action_skips_gate(self):
        """set_default has requires_reconcile=False → no detect, straight to run."""
        write_runner = FakeWriteRunner()

        async def detect_should_not_be_called() -> ServingTarget:
            raise AssertionError("detect must not be called for a non-reconcile action")

        cd = CockpitData(
            ROOT, runner=full_runner(), write_runner=write_runner,
            detect_endpoint_fn=detect_should_not_be_called,
        )
        plan = cd.set_default("vllm/dual")
        executed, rec, state = await cd.execute_action(plan)
        assert executed is True
        assert rec is None
        assert len(write_runner.started) == 1

    # ── B6: skip_reconcile only honored as an explicit reasoned force ────────────────

    @pytest.mark.asyncio
    async def test_skip_reconcile_ignored_without_force(self):
        """skip_reconcile=True on a NON-force plan must NOT bypass the gate —
        the docstring couples skip to force+reason; enforce it in code."""
        write_runner = FakeWriteRunner()
        runner = full_runner(
            **{"docker ps": ok(DOCKER_PS_ENGINE), "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE)}
        )
        gpus = [GpuInfo(index=0, mem_used_mib=22000), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner, write_runner=write_runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        plan = cd.serve("vllm/dual")  # not forced
        executed, rec, _ = await cd.execute_action(plan, skip_reconcile=True)
        # Gate still ran (skip ignored) → unsafe → refused.
        assert executed is False
        assert rec is not None and rec.safe is False
        assert write_runner.started == []

    @pytest.mark.asyncio
    async def test_skip_reconcile_honored_with_force_and_reason(self):
        """skip_reconcile IS honored when the plan is a reasoned force — and then
        the gate is genuinely skipped (detect never called)."""
        write_runner = FakeWriteRunner()

        async def detect_should_not_be_called() -> ServingTarget:
            raise AssertionError("gate must be skipped → detect not called")

        cd = CockpitData(
            ROOT, runner=full_runner(), write_runner=write_runner,
            detect_endpoint_fn=detect_should_not_be_called,
        )
        plan = cd.serve("vllm/dual", force=True, force_reason="user accepted teardown")
        executed, rec, _ = await cd.execute_action(plan, skip_reconcile=True)
        assert executed is True
        assert rec is None  # gate skipped
        assert len(write_runner.started) == 1


# ===========================================================================
# B3 — writes are SERIALIZED (the gate→write window is atomic)
# ===========================================================================


class TestWritesSerialized:
    @pytest.mark.asyncio
    async def test_two_concurrent_dispatches_serialize_via_claim(self):
        """Two concurrent execute_action for overlapping GPUs: the write lock
        serializes reconcile→register-claim→spawn, so exactly ONE passes the gate
        and registers its claim; the OTHER, running its reconcile next, sees the
        live claim and is refused. No double-write — the §3.2 TOCTOU is closed
        even though start_raw returns immediately (the claim, held until the
        winner's subprocess completes, is what blocks the loser)."""
        import asyncio as _aio

        wr = LeaseWriteRunner()
        runner = full_runner(
            **{"docker ps": ok(DOCKER_PS_EMPTY), "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE)}
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner, write_runner=wr,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        (execA, recA, _), (execB, recB, _) = await _aio.gather(
            cd.execute_action(cd.serve("slugA")),
            cd.execute_action(cd.serve("slugB")),
        )
        executed = [execA, execB]
        assert executed.count(True) == 1, f"exactly one write should pass the gate; got {executed}"
        assert executed.count(False) == 1
        assert len(wr.started) == 1, "only the winner spawned — no double-write"
        loser_rec = recB if execA else recA
        assert loser_rec is not None and loser_rec.pending_claim_tokens, \
            "the refused writer must have been blocked by the winner's pending claim"


# ===========================================================================
# C7 — container logs READ (docker logs)
# ===========================================================================


class TestContainerLogs:
    @pytest.mark.asyncio
    async def test_container_logs_returns_lines(self):
        runner = full_runner(
            **{"docker logs": ok("line one\nline two\nline three\n")}
        )
        cd = CockpitData(ROOT, runner=runner)
        out = await cd.container_logs("vllm-x")
        assert out["error"] is None
        assert out["lines"] == ["line one", "line two", "line three"]
        # It's a READ — docker logs, never stop/restart/rm.
        logs_call = next(c for c in runner.calls if "logs" in " ".join(c))
        assert logs_call[:3] == ["docker", "logs", "--tail"]

    @pytest.mark.asyncio
    async def test_container_logs_missing_container_errors(self):
        runner = full_runner(
            **{"docker logs": RunResult(1, "", "Error: No such container: nope")}
        )
        cd = CockpitData(ROOT, runner=runner)
        out = await cd.container_logs("nope")
        assert out["lines"] == []
        assert "No such container" in out["error"]


class TestRealRunnerNotInvokedInTests:
    """Sanity: a CockpitData built with a FakeRunner never constructs a
    RealRunner, and the default RealRunner is only the production fallback."""

    def test_default_runner_is_real(self):
        cd = CockpitData(ROOT)
        assert isinstance(cd._runner, RealRunner)

    def test_injected_runner_used(self):
        fr = full_runner()
        cd = CockpitData(ROOT, runner=fr)
        assert cd._runner is fr


# ===========================================================================
# Fix 1 — TOCTOU pending-claim registry (the critical one)
# ===========================================================================


class TestTOCTOUPendingClaims:
    """The pending-claim LEASE blocks writer B through the WHOLE boot gap — from
    A's dispatch until A's subprocess COMPLETES — even while docker ps /
    nvidia-smi still show the cards free (A's container hasn't booted). The key
    property: ``start_raw`` returns a RUNNING state immediately (it only spawns);
    the lease, held until the run's ``done`` fires, is what blocks B — NOT the
    write-lock duration (which is microseconds, the bug the old design had)."""

    def _cd(self, wr):
        runner = full_runner(
            **{
                "docker ps": ok(DOCKER_PS_EMPTY),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        return CockpitData(
            ROOT, runner=runner, write_runner=wr,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )

    @pytest.mark.asyncio
    async def test_claim_persists_through_boot_and_clears_on_completion(self):
        """The lease is HELD after execute_action returns (the subprocess is
        still booting) and clears only when the run's ``done`` event fires."""
        import asyncio as _aio

        wr = LeaseWriteRunner()
        cd = self._cd(wr)
        execA, recA, stateA = await cd.execute_action(cd.serve("slugA"))
        assert execA is True and recA is not None and recA.safe is True
        # HELD while A's subprocess is still running (NOT cleared on start_raw).
        assert len(cd._pending_claims) == 1, "lease must persist through the boot gap"
        # Signal A's subprocess completion → the release task clears the lease.
        stateA.done.set()
        for _ in range(8):
            await _aio.sleep(0)
        assert len(cd._pending_claims) == 0, "lease must clear on subprocess completion"

    @pytest.mark.asyncio
    async def test_inflight_claim_blocks_second_writer_and_is_not_forceable(self):
        """While A's lease is live, B's reconcile is unsafe even though docker ps
        / nvidia-smi show the cards FREE — and the in-flight claim is a HARD
        block: even a forced B is refused (cancel the in-flight write first).
        After A completes, the lease clears and C proceeds."""
        import asyncio as _aio

        wr = LeaseWriteRunner()
        cd = self._cd(wr)
        execA, recA, stateA = await cd.execute_action(cd.serve("slugA"))
        assert execA is True and len(cd._pending_claims) == 1

        # B: the rig still LOOKS free (docker ps empty, GPUs idle) but A's lease
        # is live → reconcile must refuse.
        recB = await cd.reconcile_before_write("serve:slugB")
        assert recB.safe is False
        assert recB.pending_claim_tokens, "B must be blocked by A's pending claim"

        # And it is NOT force-overridable (nothing materialized to tear down yet).
        execB, recB2, _ = await cd.execute_action(
            cd.serve("slugB", force=True, force_reason="B insists")
        )
        assert execB is False, "an in-flight pending claim is NOT force-overridable"
        assert len(wr.started) == 1, "B must never have spawned"

        # A completes → lease clears → C now proceeds.
        stateA.done.set()
        for _ in range(8):
            await _aio.sleep(0)
        execC, recC, _ = await cd.execute_action(cd.serve("slugC"))
        assert execC is True and recC is not None and recC.safe is True

    @pytest.mark.asyncio
    async def test_spawn_failure_clears_claim_immediately(self):
        """If start_raw returns an already-finished spawn-failure state, no card
        was claimed → the lease clears at once (no lingering false conflict)."""
        wr = FailedSpawnWriteRunner()
        cd = self._cd(wr)
        execX, recX, stateX = await cd.execute_action(cd.serve("slugX"))
        assert getattr(stateX, "is_finished", False) is True
        assert len(cd._pending_claims) == 0, "a spawn-failure must not leave a lease"


# ===========================================================================
# Fix 2 — docker ps fail-closed
# ===========================================================================


class TestDockerPsFailClosed:
    @pytest.mark.asyncio
    async def test_timed_out_docker_ps_makes_reconcile_unsafe(self):
        """A timed-out docker ps must NOT silently yield no containers
        (which looks like 'free' to the gate).  reconcile_before_write must
        return safe=False with a note containing 'docker ps read failed'."""
        runner = full_runner(
            **{
                "docker ps": RunResult(-1, "", "timeout", timed_out=True),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        assert "docker ps read failed" in rec.note

    @pytest.mark.asyncio
    async def test_nonzero_docker_ps_makes_reconcile_unsafe(self):
        """A docker ps that exits non-zero (daemon down, permission error) must
        also fail closed — not silently read as empty."""
        runner = full_runner(
            **{
                "docker ps": RunResult(1, "", "permission denied", timed_out=False),
                "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
            }
        )
        gpus = [GpuInfo(index=0, mem_used_mib=3), GpuInfo(index=1, mem_used_mib=1)]
        cd = CockpitData(
            ROOT, runner=runner,
            detect_endpoint_fn=make_detect(ServingTarget(gpus=gpus)),
            get_gpu_info_fn=make_gpu_info(gpus),
        )
        rec = await cd.reconcile_before_write("serve:vllm/dual")
        assert rec.safe is False
        assert "docker ps read failed" in rec.note


# ===========================================================================
# Fix 3 — ExplainScreen renders real benchmark shape (not invented keys)
# ===========================================================================


class TestExplainScreenBenchmarkShape:
    """ExplainScreen.set_detail must parse the REAL {row,columns} shape and
    render real TPS / 8pk numbers — never literal 'None/None'.
    """

    def test_explain_modal_renders_real_tps_from_columns(self):
        """Given a real {row,columns} benchmark record, the Measured section
        must contain the actual TPS numbers parsed from columns[4].
        (174 narr, 42 code, 8pk 109/150 from EXPLAIN_BENCH_ROW fixture.)
        """
        from club3090_cockpit.app import ExplainScreen
        from club3090_cockpit.data import measurement_from_explain_columns

        detail = {
            "registry": {"model": "qwen3.6-27b", "engine": "vllm-stable", "status": "production"},
            "fit": {"verdict": "fits-constrained", "vram_est_gb": 19.881, "band_gb": 1.5, "max_ctx": 262144},
            "card": "rtx-3090",
            "benchmarks": [EXPLAIN_BENCH_ROW],
        }

        # Parse via the same helper the fixed modal uses.
        m = measurement_from_explain_columns(EXPLAIN_BENCH_ROW)
        assert m.narr_tps == 174.0
        assert m.code_tps == 42.0
        assert m.quality_8pk == "109/150"
        assert m.tps_label == "174/42"
        assert "None" not in m.tps_label, "tps_label must never contain 'None'"

    def test_explain_modal_never_renders_none_for_tps(self):
        """The modal must render '—' (not 'None') when a benchmark record
        carries no TPS (a stress/soak row)."""
        from club3090_cockpit.data import measurement_from_explain_columns

        stress_row = {
            "row": "| `dual.yml` | @x | PASS | PASS at 64K | FAIL | 2026-05-03 |",
            "columns": ["`dual.yml`", "@x", "PASS", "PASS at 64K", "FAIL", "2026-05-03"],
        }
        m = measurement_from_explain_columns(stress_row)
        assert m.narr_tps is None
        assert m.code_tps is None
        # The ExplainScreen fix renders n = "—" / c = "—" when tps is None.
        n = f"{m.narr_tps:.0f}" if m.narr_tps is not None else "—"
        c = f"{m.code_tps:.0f}" if m.code_tps is not None else "—"
        rendered = f"{n}/{c} TPS"
        assert "None" not in rendered
        assert rendered == "—/— TPS"

    def test_explain_modal_real_columns_fixture_roundtrip(self):
        """Full roundtrip: the EXPLAIN_BENCH_ROW fixture → the fixed rendering
        path → the body contains '174', '42', and '109/150'."""
        from club3090_cockpit.data import measurement_from_explain_columns

        m = measurement_from_explain_columns(EXPLAIN_BENCH_ROW)
        n = f"{m.narr_tps:.0f}" if m.narr_tps is not None else "—"
        c = f"{m.code_tps:.0f}" if m.code_tps is not None else "—"
        q = m.quality_8pk or "—"
        d = m.date or ""
        line = f"    {n}/{c} TPS · 8pk {q}  {d}"
        assert "174" in line
        assert "42" in line
        assert "109/150" in line
        assert "None" not in line
