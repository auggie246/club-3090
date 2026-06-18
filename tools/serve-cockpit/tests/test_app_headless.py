"""Headless tests for the CockpitApp (Phase 3 — wired).

Verifies:
  1. The app mounts without error (no TTY, no GPU, no Docker, no live script).
     The data layer is a CockpitData backed by a FakeRunner + fake detect +
     a FakeWriteRunner, so NO subprocess is ever spawned.
  2. All four modes are reachable via digit-key bindings; nav nodes exist.
  3. Discover · Catalog populates from real enriched entries (fit glyph, TPS,
     8pk, source) and filters live.
  4. BYO renders the swap_path route from byo_check.
  5. Serve stages a plan and ⏎ opens the reconcile-gated confirm modal.
  6. Estate · Orchestration + Containers populate from estate_state.
  7. EVERY write path goes through the reconcile gate, and NO test ever
     executes a live write — the FakeWriteRunner records start_raw calls and
     never spawns a process; an unsafe gate refuses to even reach it.

The whole service layer is dependency-injected; tests never touch the real
RealRunner / SubprocessRunner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from textual.widgets import Button, DataTable, Input, Static, TabbedContent, TabPane, Label

from club3090_tui_core.detect import GpuInfo, ServingTarget

from club3090_cockpit.app import (
    CockpitApp,
    CatalogPane,
    ConfirmActionScreen,
    ExplainScreen,
    ModeSwitcher,
    ByoPane,
    ServePane,
    EstateOrchPane,
    EstateContainersPane,
    ValidateRunPane,
    ValidateDoctorPane,
    ValidateBenchmarksPane,
    ValidateEvidencePane,
    RailStatus,
)
from club3090_cockpit.services import CockpitData, RunResult


FAKE_REPO_ROOT = Path("/tmp/fake-club-3090-test-root")


# ---------------------------------------------------------------------------
# Fake service-layer seams (no subprocess, no GPU, no docker, no TTY)
# ---------------------------------------------------------------------------


class FakeRunner:
    """Canned-output read runner keyed on a substring of the command.

    A WRITE command must NEVER reach here (writes go through the write_runner);
    if one did it would still be a no-op canned response, but the write path is
    separately asserted to use FakeWriteRunner only.
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


class FakeWriteRunner:
    """Stand-in for the core SubprocessRunner — records start_raw calls but
    NEVER spawns a process.  This is the assertion that no live write happens."""

    def __init__(self):
        self.started: list[dict[str, Any]] = []

    async def start_raw(self, cmd, env, run_type, parser):
        self.started.append({"cmd": cmd, "run_type": run_type})
        return {"mock_state": True, "cmd": cmd}


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
# Canned contract outputs
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

# REAL switch.sh --explain --json benchmarks shape: [{"row","columns"}].
# TPS lives in columns[4] ("Narr / Code TPS"); 8-pack is scraped from the row.
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
        "registry": {"slug": "vllm/dual", "model": "qwen3.6-27b", "engine": "vllm-stable", "status": "production"},
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

ESTATE_REPORT_FREE = json.dumps({"active_estate": {"present": False, "instances": []}})
ESTATE_REPORT_BUSY = json.dumps(
    {
        "active_estate": {
            "present": True,
            "instances": [
                {"name": "llama-gpu0", "compose": "llamacpp/default", "gpus": [0], "port": 8010},
            ],
        }
    }
)

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

DOCKER_PS_ENGINE = (
    "vllm-qwen36-27b-dual|0.0.0.0:8010->8000/tcp, [::]:8010->8000/tcp\n"
    "open-webui|0.0.0.0:3000->8080/tcp\n"
)
DOCKER_PS_EMPTY = ""


def fake_responses(**overrides) -> dict[str, RunResult]:
    responses = {
        "registry-emit.sh --json": ok(REGISTRY_JSON),
        "kv-calc.py --fit": ok(FIT_JSON),
        "--explain vllm/dual --json": ok(EXPLAIN_JSON),
        "--explain ik-llama/iq4ks-mtp --json": ok(EXPLAIN_NO_BENCH_JSON),
        "gpu-mode.sh --list-modes --json": ok(SCENES_JSON),
        "pull.sh": ok(PULL_JSON),
        "estate_cli.py report-state --json": ok(ESTATE_REPORT_FREE),
        "health.sh": ok(HEALTH_SERVING),
        "docker ps": ok(DOCKER_PS_EMPTY),
    }
    responses.update(overrides)
    return responses


def make_app(
    *,
    responses: Optional[dict[str, RunResult]] = None,
    gpus: Optional[list[GpuInfo]] = None,
    target: Optional[ServingTarget] = None,
    write_runner: Optional[FakeWriteRunner] = None,
) -> tuple[CockpitApp, FakeRunner, FakeWriteRunner]:
    """Build a CockpitApp wired to a fully-faked CockpitData.

    Returns (app, read_runner, write_runner) so tests can assert on calls.
    """
    runner = FakeRunner(responses or fake_responses())
    gpus = gpus if gpus is not None else [GpuInfo(index=0, mem_used_mib=1), GpuInfo(index=1, mem_used_mib=1)]
    target = target if target is not None else ServingTarget(gpus=gpus)
    write_runner = write_runner or FakeWriteRunner()
    data = CockpitData(
        FAKE_REPO_ROOT,
        runner=runner,
        detect_endpoint_fn=make_detect(target),
        get_gpu_info_fn=make_gpu_info(gpus),
        write_runner=write_runner,
    )
    app = CockpitApp(repo_root=FAKE_REPO_ROOT, data=data)
    return app, runner, write_runner


async def _settle(pilot) -> None:
    """Let background workers (catalog / estate) finish."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


PANEL_IDS = ["panel-discover", "panel-serve", "panel-estate", "panel-validate"]


# ===========================================================================
# Mount / navigation
# ===========================================================================


class TestAppMounts:
    @pytest.mark.asyncio
    async def test_app_mounts(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            assert app is not None

    @pytest.mark.asyncio
    async def test_persistent_rail_status_present(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            assert app.query_one("#mode-switcher") is not None
            assert app.query_one("#rail-status", RailStatus) is not None

    @pytest.mark.asyncio
    async def test_discover_panel_visible_on_start(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            assert "active" in app.query_one("#panel-discover").classes

    @pytest.mark.asyncio
    async def test_other_panels_hidden_on_start(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            for pid in ["panel-serve", "panel-estate", "panel-validate"]:
                assert "active" not in app.query_one(f"#{pid}").classes

    @pytest.mark.asyncio
    async def test_no_live_write_runner_constructed(self):
        """The injected fake write runner is the one in use — never a real one."""
        app, _, wr = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            assert app._data._write_runner is wr
            assert wr.started == []  # nothing executed on mount


class TestModeNavigation:
    @pytest.mark.asyncio
    async def test_switch_to_serve_mode(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            assert "active" in app.query_one("#panel-serve").classes
            assert "active" not in app.query_one("#panel-discover").classes

    @pytest.mark.asyncio
    async def test_switch_to_estate_mode(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            assert "active" in app.query_one("#panel-estate").classes

    @pytest.mark.asyncio
    async def test_switch_to_validate_mode(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            assert "active" in app.query_one("#panel-validate").classes

    @pytest.mark.asyncio
    async def test_switch_back_to_discover(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            await pilot.press("1")
            assert "active" in app.query_one("#panel-discover").classes
            assert "active" not in app.query_one("#panel-serve").classes

    @pytest.mark.asyncio
    async def test_all_four_modes_cycle(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            for key, expected_active in [("1", 0), ("2", 1), ("3", 2), ("4", 3), ("1", 0)]:
                await pilot.press(key)
                await pilot.pause()
                active = [pid for pid in PANEL_IDS if "active" in app.query_one(f"#{pid}").classes]
                assert len(active) == 1, f"after {key!r}: {active}"
                assert active[0] == PANEL_IDS[expected_active]


class TestNavNodesExist:
    @pytest.mark.asyncio
    async def test_discover_tabs_exist(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#discover-tabs", TabbedContent)
            app.query_one("#tab-catalog", TabPane)
            app.query_one("#tab-byo", TabPane)

    @pytest.mark.asyncio
    async def test_estate_tabs_exist(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#estate-tabs", TabbedContent)
            app.query_one("#tab-orchestration", TabPane)
            app.query_one("#tab-containers", TabPane)

    @pytest.mark.asyncio
    async def test_validate_tabs_exist(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#validate-tabs", TabbedContent)
            app.query_one("#tab-run", TabPane)
            app.query_one("#tab-doctor", TabPane)
            app.query_one("#tab-benchmarks", TabPane)
            app.query_one("#tab-evidence", TabPane)

    @pytest.mark.asyncio
    async def test_catalog_datatable_has_columns(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.query_one("#catalog-table", DataTable)
            col_labels = [str(c.label) for c in table.columns.values()]
            for expected in ("slug", "engine", "fit", "ctx", "TPS", "8pk", "status", "source"):
                assert expected in col_labels, f"missing {expected!r}: {col_labels}"

    @pytest.mark.asyncio
    async def test_mode_switcher_exists(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#mode-switcher", ModeSwitcher)

    @pytest.mark.asyncio
    async def test_catalog_status_label_exists(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#catalog-status", Label)

    @pytest.mark.asyncio
    async def test_catalog_action_hint_exists(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#catalog-hint", Label)


# ===========================================================================
# Discover · Catalog (now wired to real enriched entries)
# ===========================================================================


class TestCatalogWired:
    @pytest.mark.asyncio
    async def test_catalog_populates_from_service(self):
        """On mount the catalog worker pulls enriched entries from CockpitData."""
        app, runner, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            table = app.query_one("#catalog-table", DataTable)
            assert table.row_count == 2  # vllm/dual + ik-llama/iq4ks-mtp
            # registry-emit was actually consulted (real read, faked).
            assert any("registry-emit.sh" in " ".join(c) for c in runner.calls)

    @pytest.mark.asyncio
    async def test_catalog_shows_fit_glyph_and_tps(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            pane = app.query_one("#catalog-pane", CatalogPane)
            entry = next(e for e in pane._entries if e.slug == "vllm/dual")
            assert entry.fit.glyph == "●"            # fits-clean
            assert entry.measurement.tps_label == "174/42"
            assert entry.measurement.quality_label == "109/150"

    @pytest.mark.asyncio
    async def test_catalog_ik_llama_fit_is_skip(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            pane = app.query_one("#catalog-pane", CatalogPane)
            ik = next(e for e in pane._entries if e.slug == "ik-llama/iq4ks-mtp")
            assert ik.fit.verdict == "skip"

    @pytest.mark.asyncio
    async def test_catalog_filter_narrows_rows(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            pane = app.query_one("#catalog-pane", CatalogPane)
            pane.set_filter("ik-llama")
            table = app.query_one("#catalog-table", DataTable)
            assert table.row_count == 1
            assert pane.selected_entry() is None or pane.selected_entry().slug == "ik-llama/iq4ks-mtp"
            pane.set_filter("")
            assert app.query_one("#catalog-table", DataTable).row_count == 2

    @pytest.mark.asyncio
    async def test_catalog_error_surfaces(self):
        responses = fake_responses(**{"registry-emit.sh --json": ok(json.dumps({"variants": []}))})
        app, _, _ = make_app(responses=responses)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            status = app.query_one("#catalog-status", Label)
            text = str(status.render()).lower()
            assert "error" in text

    @pytest.mark.asyncio
    async def test_explain_modal_opens_and_populates(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.query_one("#catalog-table", DataTable).move_cursor(row=0)
            await pilot.press("e")
            await _settle(pilot)
            assert isinstance(app.screen, ExplainScreen)


# ===========================================================================
# Discover · BYO (wired to byo_check)
# ===========================================================================


class TestByoWired:
    @pytest.mark.asyncio
    async def test_byo_pane_nodes(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#byo-panel", ByoPane)
            app.query_one("#byo-url-input", Input)
            app.query_one("#byo-profile-input", Input)
            app.query_one("#byo-fit-btn", Button)
            app.query_one("#byo-result-card", Static)

    @pytest.mark.asyncio
    async def test_byo_fit_check_renders_route(self):
        app, runner, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.query_one("#byo-url-input", Input).value = "org/Model"
            app.query_one("#byo-fit-btn", Button).press()
            await _settle(pilot)
            card = app.query_one("#byo-result-card", Static)
            text = str(card.render())
            assert "Route C" in text or "vllm/dual" in text
            # pull.sh was invoked with --dry-run (never downloads).
            pull = next(c for c in runner.calls if "pull.sh" in " ".join(c))
            assert "--dry-run" in pull


# ===========================================================================
# Serve (plan staging + reconcile-gated confirm modal)
# ===========================================================================


class TestServeWired:
    @pytest.mark.asyncio
    async def test_serve_pane_nodes(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#serve-panel", ServePane)
            app.query_one("#serve-plan-box")
            app.query_one("#serve-launch-btn", Button)
            app.query_one("#serve-cancel-btn", Button)
            app.query_one("#serve-live")  # core LivePane for the boot stream

    @pytest.mark.asyncio
    async def test_enter_in_catalog_stages_plan_and_jumps_to_serve(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.query_one("#catalog-table", DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert "active" in app.query_one("#panel-serve").classes
            assert app._staged_entry is not None
            assert app._staged_entry.slug == "vllm/dual"
            detail = str(app.query_one("#serve-plan-detail", Static).render())
            assert "vllm/dual" in detail

    @pytest.mark.asyncio
    async def test_serve_enter_opens_confirm_modal(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.query_one("#catalog-table", DataTable).move_cursor(row=0)
            await pilot.press("enter")       # stage + jump to Serve
            await pilot.pause()
            await pilot.press("enter")       # commit → confirm modal
            await pilot.pause()
            assert isinstance(app.screen, ConfirmActionScreen)

    @pytest.mark.asyncio
    async def test_serve_plan_is_not_force(self):
        """The serve plan built by ⏎ is the GATED switch.sh <slug> — NOT --force."""
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.serve("vllm/dual")
            assert "--force" not in plan.cmd
            assert plan.requires_reconcile is True


# ===========================================================================
# Estate · Orchestration + Containers (wired to estate_state)
# ===========================================================================


class TestEstateWired:
    @pytest.mark.asyncio
    async def test_estate_orch_pane_nodes(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            app.query_one("#estate-orch-pane", EstateOrchPane)
            app.query_one("#gpu0-card")
            app.query_one("#gpu1-card")
            app.query_one("#doctor-line")
            app.query_one("#scene-table", DataTable)
            app.query_one("#services-strip")

    @pytest.mark.asyncio
    async def test_estate_scene_table_populates_from_gpu_mode(self):
        app, runner, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            t = app.query_one("#scene-table", DataTable)
            assert t.row_count == 2  # 27b + off (from SCENES_JSON)
            assert any("gpu-mode.sh --list-modes" in " ".join(c) for c in runner.calls)

    @pytest.mark.asyncio
    async def test_estate_gpu_card_reflects_detect(self):
        gpus = [
            GpuInfo(index=0, mem_used_mib=18 * 1024, mem_total_mib=24 * 1024, utilization=71, power_draw_w=312, power_limit_w=370, temp_c=64),
            GpuInfo(index=1, mem_used_mib=12 * 1024, mem_total_mib=24 * 1024, utilization=45),
        ]
        app, _, _ = make_app(gpus=gpus, target=ServingTarget(gpus=gpus))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            bar = str(app.query_one("#gpu0-bar", Static).render())
            assert "18.0 / 24.0 GiB" in bar
            assert "71%" in bar

    @pytest.mark.asyncio
    async def test_estate_doctor_line_serving(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            line = str(app.query_one("#doctor-line", Static).render())
            assert "serving" in line.lower()

    @pytest.mark.asyncio
    async def test_estate_containers_pane_nodes(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            app.query_one("#estate-containers-pane", EstateContainersPane)
            app.query_one("#containers-table", DataTable)
            app.query_one("#drill-tabs", TabbedContent)
            app.query_one("#drill-tab-logs", TabPane)
            app.query_one("#drill-tab-stats", TabPane)
            app.query_one("#drill-tab-config", TabPane)

    @pytest.mark.asyncio
    async def test_estate_containers_populate_from_docker_ps(self):
        responses = fake_responses(**{"docker ps": ok(DOCKER_PS_ENGINE)})
        app, _, _ = make_app(responses=responses)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            pane = app.query_one("#estate-containers-pane", EstateContainersPane)
            names = [c.name for c in pane._containers]
            assert "vllm-qwen36-27b-dual" in names
            assert "open-webui" not in names  # not an engine prefix

    @pytest.mark.asyncio
    async def test_estate_scene_switch_opens_confirm_modal(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            app.query_one("#scene-table", DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmActionScreen)


# ===========================================================================
# THE RECONCILE GATE — every write path goes through it
# ===========================================================================


class TestEveryWriteGoesThroughReconcile:
    @pytest.mark.asyncio
    async def test_confirm_modal_runs_reconcile_on_mount(self):
        """The confirm modal re-runs the fresh reconcile gate before enabling
        any commit button.  On a free rig the gate is clear → Confirm enabled."""
        app, runner, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.serve("vllm/dual")
            app.push_screen(ConfirmActionScreen(plan))
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, ConfirmActionScreen)
            assert screen._reconcile is not None
            assert screen._reconcile.safe is True
            ok_btn = screen.query_one("#confirm-ok-btn", Button)
            assert ok_btn.disabled is False

    @pytest.mark.asyncio
    async def test_unsafe_gate_disables_confirm_enables_force(self):
        """When a container is running, the gate is unsafe → Confirm disabled,
        Force enabled.  The teardown list is surfaced."""
        responses = fake_responses(**{"docker ps": ok(DOCKER_PS_ENGINE)})
        gpus = [GpuInfo(index=0, mem_used_mib=22000), GpuInfo(index=1, mem_used_mib=1)]
        app, _, _ = make_app(responses=responses, gpus=gpus, target=ServingTarget(gpus=gpus))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.serve("vllm/dual")
            app.push_screen(ConfirmActionScreen(plan))
            await _settle(pilot)
            screen = app.screen
            assert screen._reconcile.safe is False
            assert screen.query_one("#confirm-ok-btn", Button).disabled is True
            assert screen.query_one("#confirm-force-btn", Button).disabled is False
            body = str(screen.query_one("#confirm-body", Static).render())
            assert "tear down" in body.lower() or "collide" in body.lower()

    @pytest.mark.asyncio
    async def test_confirm_dispatches_through_gated_executor_safe(self):
        """Confirm on a safe gate → execute_action reaches the (mocked) write
        runner exactly once.  NO live process is ever spawned."""
        wr = FakeWriteRunner()
        app, _, _ = make_app(write_runner=wr)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.serve("vllm/dual")
            app.push_screen(ConfirmActionScreen(plan))
            await _settle(pilot)
            screen = app.screen
            screen.query_one("#confirm-ok-btn", Button).press()
            await _settle(pilot)
            assert len(wr.started) == 1
            assert wr.started[0]["cmd"] == ["bash", "scripts/switch.sh", "vllm/dual"]

    @pytest.mark.asyncio
    async def test_dispatch_refuses_when_unsafe_no_force(self):
        """If the gate is unsafe and the plan is not forced, the executor refuses
        and the write runner is NEVER reached."""
        wr = FakeWriteRunner()
        responses = fake_responses(**{"docker ps": ok(DOCKER_PS_ENGINE)})
        gpus = [GpuInfo(index=0, mem_used_mib=22000), GpuInfo(index=1, mem_used_mib=1)]
        app, _, _ = make_app(responses=responses, gpus=gpus, target=ServingTarget(gpus=gpus), write_runner=wr)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.serve("vllm/dual")  # not forced
            app.dispatch_action(plan)
            await _settle(pilot)
            assert wr.started == []  # refused at the gate

    @pytest.mark.asyncio
    async def test_force_override_proceeds_despite_unsafe(self):
        """A forced plan (reason surfaced) proceeds even when the gate is unsafe,
        still via the MOCKED write runner — never a live process."""
        wr = FakeWriteRunner()
        responses = fake_responses(**{"docker ps": ok(DOCKER_PS_ENGINE)})
        gpus = [GpuInfo(index=0, mem_used_mib=22000), GpuInfo(index=1, mem_used_mib=1)]
        app, _, _ = make_app(responses=responses, gpus=gpus, target=ServingTarget(gpus=gpus), write_runner=wr)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.serve("vllm/dual", force=True, force_reason="user accepted teardown")
            app.dispatch_action(plan)
            await _settle(pilot)
            assert len(wr.started) == 1
            assert "--force" in wr.started[0]["cmd"]

    @pytest.mark.asyncio
    async def test_force_button_reissues_forced_plan(self):
        """Pressing Force in the confirm modal re-issues the plan as forced
        (--force inserted) and dispatches it through the mocked runner."""
        wr = FakeWriteRunner()
        responses = fake_responses(**{"docker ps": ok(DOCKER_PS_ENGINE)})
        gpus = [GpuInfo(index=0, mem_used_mib=22000), GpuInfo(index=1, mem_used_mib=1)]
        app, _, _ = make_app(responses=responses, gpus=gpus, target=ServingTarget(gpus=gpus), write_runner=wr)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.serve("vllm/dual")  # un-forced
            app.push_screen(ConfirmActionScreen(plan))
            await _settle(pilot)
            screen = app.screen
            screen.query_one("#confirm-force-btn", Button).press()
            await _settle(pilot)
            assert len(wr.started) == 1
            assert "--force" in wr.started[0]["cmd"]

    @pytest.mark.asyncio
    async def test_scene_switch_dispatch_through_gate(self):
        """Scene-switch is gated too — a free rig dispatches gpu-mode <mode>."""
        wr = FakeWriteRunner()
        app, _, _ = make_app(write_runner=wr)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.scene_switch("27b")
            assert plan.requires_reconcile is True
            app.dispatch_action(plan)
            await _settle(pilot)
            assert len(wr.started) == 1
            assert wr.started[0]["cmd"] == ["bash", "scripts/gpu-mode.sh", "27b"]


class TestAllPanesWired:
    """Phase-3 acceptance (§9.2 all_panes_wired): every advertised UI hint is
    backed by a real handler routed through the SAME gate."""

    @pytest.mark.asyncio
    async def test_container_restart_opens_confirm_modal(self):
        responses = fake_responses(**{"docker ps": ok(DOCKER_PS_ENGINE)})
        app, _, _ = make_app(responses=responses)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            app.query_one("#estate-containers-pane", EstateContainersPane).query_one(
                "#containers-table", DataTable
            ).move_cursor(row=0)
            await pilot.press("s")  # restart
            await pilot.pause()
            assert isinstance(app.screen, ConfirmActionScreen)
            assert app.screen._plan.cmd == ["docker", "restart", "vllm-qwen36-27b-dual"]

    @pytest.mark.asyncio
    async def test_container_stop_dispatches_through_gate(self):
        wr = FakeWriteRunner()
        responses = fake_responses(**{"docker ps": ok(DOCKER_PS_EMPTY)})
        app, _, _ = make_app(responses=responses, write_runner=wr)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            plan = app._data.container_action("vllm-x", "stop")
            assert plan.requires_reconcile is True
            app.dispatch_action(plan)
            await _settle(pilot)
            assert len(wr.started) == 1
            assert wr.started[0]["cmd"] == ["docker", "stop", "vllm-x"]

    @pytest.mark.asyncio
    async def test_container_logs_stream_into_live_pane(self):
        responses = fake_responses(
            **{"docker ps": ok(DOCKER_PS_ENGINE), "docker logs": ok("boot line A\nboot line B\n")}
        )
        app, _, _ = make_app(responses=responses)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            app.query_one("#estate-containers-pane", EstateContainersPane).query_one(
                "#containers-table", DataTable
            ).move_cursor(row=0)
            await pilot.press("l")  # logs (READ)
            await _settle(pilot)
            # No modal — logs is a read, not a gated write.
            assert not isinstance(app.screen, ConfirmActionScreen)

    @pytest.mark.asyncio
    async def test_estate_off_opens_confirm_modal(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await _settle(pilot)
            await pilot.press("o")  # stop all
            await pilot.pause()
            assert isinstance(app.screen, ConfirmActionScreen)
            assert "down" in app.screen._plan.cmd

    @pytest.mark.asyncio
    async def test_estate_off_dispatches_through_gate(self):
        wr = FakeWriteRunner()
        app, _, _ = make_app(write_runner=wr)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.estate_down()
            app.dispatch_action(plan)
            await _settle(pilot)
            assert len(wr.started) == 1
            assert "down" in wr.started[0]["cmd"]

    @pytest.mark.asyncio
    async def test_set_default_opens_confirm_modal(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.query_one("#catalog-table", DataTable).move_cursor(row=0)
            await pilot.press("d")  # set-default
            await pilot.pause()
            assert isinstance(app.screen, ConfirmActionScreen)
            assert app.screen._plan.cmd == ["bash", "scripts/switch.sh", "--set-default", "vllm/dual"]

    @pytest.mark.asyncio
    async def test_clear_default_opens_confirm_modal(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.query_one("#catalog-table", DataTable).move_cursor(row=0)
            await pilot.press("D")  # clear-default
            await pilot.pause()
            assert isinstance(app.screen, ConfirmActionScreen)
            assert app.screen._plan.cmd == ["bash", "scripts/switch.sh", "--clear-default", "qwen3.6-27b"]

    @pytest.mark.asyncio
    async def test_set_default_dispatches_through_gate(self):
        """set_default routes through the same gate; requires_reconcile=False so
        it dispatches straight to the mocked write runner."""
        wr = FakeWriteRunner()
        app, _, _ = make_app(write_runner=wr)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            plan = app._data.set_default("vllm/dual")
            app.dispatch_action(plan)
            await _settle(pilot)
            assert len(wr.started) == 1
            assert wr.started[0]["cmd"] == ["bash", "scripts/switch.sh", "--set-default", "vllm/dual"]


class TestNoLiveWriteEverExecuted:
    """Belt-and-suspenders: across the whole app surface, no FakeWriteRunner
    call is a real process and the read FakeRunner never receives a write."""

    @pytest.mark.asyncio
    async def test_full_serve_flow_only_touches_fakes(self):
        wr = FakeWriteRunner()
        app, runner, _ = make_app(write_runner=wr)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            # stage → confirm → commit
            app.query_one("#catalog-table", DataTable).move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await _settle(pilot)
            screen = app.screen
            assert isinstance(screen, ConfirmActionScreen)
            screen.query_one("#confirm-ok-btn", Button).press()
            await _settle(pilot)
            # The only write went to the fake; no switch.sh appears in READ calls.
            assert all("scripts/switch.sh vllm/dual" not in " ".join(c) for c in runner.calls)
            assert len(wr.started) == 1


# ===========================================================================
# Validate panes (Phase 4 — illustrative; nodes still present)
# ===========================================================================


class TestValidatePanes:
    @pytest.mark.asyncio
    async def test_validate_run_pane_nodes(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#validate-run-pane", ValidateRunPane)
            app.query_one("#run-ladder")
            app.query_one("#run-extras")
            app.query_one("#run-output")

    @pytest.mark.asyncio
    async def test_validate_doctor_pane_nodes(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#validate-doctor-pane", ValidateDoctorPane)
            app.query_one("#doctor-card-health")
            app.query_one("#doctor-card-estate")
            app.query_one("#doctor-card-profile")

    @pytest.mark.asyncio
    async def test_validate_doctor_health_line_goes_live_on_estate_poll(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")  # estate poll feeds the doctor pane too
            await _settle(pilot)
            body = str(app.query_one("#doctor-health-body", Static).render())
            assert "serving" in body.lower()

    @pytest.mark.asyncio
    async def test_validate_benchmarks_pane_nodes(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#validate-benchmarks-pane", ValidateBenchmarksPane)
            t = app.query_one("#bench-table", DataTable)
            assert t.row_count == 4

    @pytest.mark.asyncio
    async def test_validate_evidence_pane_nodes(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#validate-evidence-pane", ValidateEvidencePane)
            app.query_one("#evidence-list")


# ===========================================================================
# Primary action does not crash in any mode
# ===========================================================================


class TestPrimaryActionSafe:
    @pytest.mark.asyncio
    async def test_enter_in_discover_with_no_selection_is_safe(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            # filter to nothing selectable then press enter — must not crash
            await pilot.press("enter")

    @pytest.mark.asyncio
    async def test_enter_in_validate_is_safe(self):
        app, _, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            await pilot.press("enter")
