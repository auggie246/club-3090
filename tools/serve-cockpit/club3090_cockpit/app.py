"""club3090 serve cockpit — main Textual application.

Phase 3 (Wire): the four panes are wired to the real data layer
(``services.CockpitData`` + ``data.py`` shapes), reusing the shared core
(``club3090_tui_core``) for detect / streaming / widgets.

  - Discover / Catalog  : real enriched rows from ``CockpitData.load_catalog``;
                          ``e`` opens ``explain``, ``/`` filters, ``⏎`` → Serve.
  - Discover / BYO       : ``CockpitData.byo_check`` → fit verdict + swap_path.
  - Serve               : plan-confirm modal (§7 #8) — the tears-down line comes
                          from ``reconcile_before_write``; ``⏎`` commits the GATED
                          ``serve(slug)`` (NOT --force) streamed via the core
                          SubprocessRunner; ``F`` surfaces the force override.
  - Estate / Orch        : ``estate_state`` live (GPU cards, Doctor, scenes,
                          services); scene-switch → confirm modal that FIRST
                          calls ``reconcile_before_write`` then ``scene_switch``.
  - Estate / Containers  : ``containers`` real list; drill into Logs/Stats/Config;
                          restart/stop behind the reconcile-gated confirm.

EVERY write path (serve / scene-switch / estate-down / container restart|stop)
goes through ``CockpitData.execute_action``, which re-runs the reconcile gate
first and refuses when unsafe (unless an explicit, reasoned force override).
In this phase the write runner is NEVER executed live — tests inject a fake.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)
from textual import work

from club3090_tui_core.registry import VariantRow
from club3090_tui_core.widgets.live_pane import LivePane

from .data import (
    ActionPlan,
    ByoResult,
    CatalogEntry,
    ContainerInfo,
    EstateState,
    ReconcileResult,
    Scene,
    measurement_from_explain_columns,
)
from .services import CockpitData

# ── Status glyph mapping ──────────────────────────────────────────────────────

_STATUS_GLYPH: dict[str, str] = {
    "production": "✅",
    "caveats": "⚠️",
    "experimental": "🧪",
    "incubating": "🐣",
    "preview": "👁️",
    "upstream-gated": "⏸️",
    "deprecated": "🗑️",
}


def _status_glyph(status: str) -> str:
    return _STATUS_GLYPH.get(status.lower(), status)


# ── Help modal ────────────────────────────────────────────────────────────────


class HelpScreen(ModalScreen):
    """Help overlay showing keybindings and current phase status."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > Vertical {
        width: 76;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    HelpScreen .help-title {
        text-style: bold;
        color: $accent;
        text-align: center;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
    ]

    HELP_TEXT = """\
[bold]Keybindings[/bold]

  [cyan]1[/cyan]  Discover    [cyan]2[/cyan]  Serve    [cyan]3[/cyan]  Estate    [cyan]4[/cyan]  Validate
  [cyan]r[/cyan]  Refresh (re-reads the live data layer for the active mode)
  [cyan]/[/cyan]  Filter catalog (Discover · Catalog)
  [cyan]e[/cyan]  Explain selected slug (Discover · Catalog)
  [cyan]⏎[/cyan]  Primary action (serve / launch / switch scene)
  [cyan]F[/cyan]  Force override (Serve — surfaced, requires a reason)
  [cyan]?[/cyan]  This help        [cyan]q[/cyan]  Quit

[bold]Safety — the reconcile gate[/bold]

  Every write (serve, scene-switch, estate-down, container restart/stop)
  re-runs a FRESH detect immediately before executing and refuses if a
  running container / busy GPU / active estate claim would collide.  The
  confirm modal shows exactly what a write would tear down.  Nothing is
  ever forced silently — F surfaces the override with its reason.

[bold]Status glyphs[/bold]

  ✅ production   ⚠️  caveats   🧪 experimental
  🐣 incubating  👁️  preview   ⏸️  upstream-gated   🗑️  deprecated

[bold]Fit glyphs (local card)[/bold]

  ● fits-clean   ◐ fits-constrained   ○ won't-fit   · skip / unknown
"""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("club3090 serve cockpit — Help", classes="help-title")
            yield Static(self.HELP_TEXT)

    def action_dismiss(self) -> None:
        self.app.pop_screen()


# ── Discover · Catalog ─────────────────────────────────────────────────────────


class CatalogPane(Container):
    """Catalog tab: DataTable populated from the enriched registry catalog."""

    DEFAULT_CSS = """
    CatalogPane {
        height: 1fr;
    }
    CatalogPane #catalog-status {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    CatalogPane #catalog-filter {
        height: 3;
        display: none;
        margin: 0 1;
    }
    CatalogPane #catalog-filter.visible {
        display: block;
    }
    CatalogPane DataTable {
        height: 1fr;
    }
    CatalogPane #catalog-hint {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Loading catalog…", id="catalog-status")
        yield Input(placeholder="filter slug / engine / model / status…", id="catalog-filter")
        table: DataTable = DataTable(id="catalog-table", zebra_stripes=True)
        table.cursor_type = "row"
        yield table
        yield Label(
            "[dim]\\[/] filter   \\[⏎] serve   \\[e] explain   "
            "\\[d] set-default   \\[D] clear-default[/dim]",
            id="catalog-hint",
        )

    def on_mount(self) -> None:
        table = self.query_one("#catalog-table", DataTable)
        table.add_columns("slug", "engine", "fit", "ctx", "TPS", "8pk", "status", "source")
        # Full enriched catalog, and the current filter substring.
        self._entries: list[CatalogEntry] = []
        self._filter: str = ""

    # ── data ────────────────────────────────────────────────────────────────────

    def populate(self, entries: list[CatalogEntry], error: Optional[str]) -> None:
        """Fill the table with enriched catalog entries."""
        status_label = self.query_one("#catalog-status", Label)
        table = self.query_one("#catalog-table", DataTable)

        if error:
            self._entries = []
            table.clear()
            status_label.update(f"[red]Catalog error:[/red] {error}")
            table.add_row("—", "—", "—", "—", "—", "—", "—", "—")
            return

        self._entries = list(entries)
        self._render_rows()

    def _render_rows(self) -> None:
        status_label = self.query_one("#catalog-status", Label)
        table = self.query_one("#catalog-table", DataTable)
        table.clear()

        rows = self._filtered_entries()
        for e in rows:
            # source provenance — flag a coarse markdown scrape so a measurement
            # from BENCHMARKS.md is never mistaken for a structured record.
            meas_src = e.measurement.source
            tps = e.measurement.tps_label
            if meas_src == "benchmarks.md" and tps != "—":
                tps = f"{tps}*"
            table.add_row(
                e.slug,
                e.engine,
                e.fit.glyph,
                e.ctx_label or "—",
                tps,
                e.measurement.quality_label,
                _status_glyph(e.status),
                e.source,
            )

        if self._filter:
            status_label.update(
                f"{len(rows)} / {len(self._entries)} variants  ·  filter: {self._filter!r}"
            )
        else:
            star = "  ([dim]*[/dim] = BENCHMARKS.md scrape)" if self._has_md_scrape() else ""
            status_label.update(f"{len(self._entries)} variants loaded from registry{star}")

    def _has_md_scrape(self) -> bool:
        return any(e.measurement.source == "benchmarks.md" for e in self._entries)

    def _filtered_entries(self) -> list[CatalogEntry]:
        if not self._filter:
            return self._entries
        f = self._filter.lower()
        out: list[CatalogEntry] = []
        for e in self._entries:
            hay = f"{e.slug} {e.engine} {e.model} {e.status} {e.source}".lower()
            if f in hay:
                out.append(e)
        return out

    def set_filter(self, text: str) -> None:
        self._filter = (text or "").strip()
        self._render_rows()

    def selected_entry(self) -> Optional[CatalogEntry]:
        """The CatalogEntry under the table cursor, or None."""
        table = self.query_one("#catalog-table", DataTable)
        rows = self._filtered_entries()
        idx = table.cursor_row
        if 0 <= idx < len(rows):
            return rows[idx]
        return None

    def toggle_filter(self) -> None:
        inp = self.query_one("#catalog-filter", Input)
        if "visible" in inp.classes:
            inp.remove_class("visible")
            self.query_one("#catalog-table", DataTable).focus()
        else:
            inp.add_class("visible")
            inp.focus()


# ── Explain detail modal ────────────────────────────────────────────────────────


class ExplainScreen(ModalScreen):
    """Tier-3 detail overlay for one slug (switch.sh --explain --json)."""

    DEFAULT_CSS = """
    ExplainScreen {
        align: center middle;
    }
    ExplainScreen > Vertical {
        width: 84;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    ExplainScreen .explain-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    ExplainScreen #explain-body {
        height: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("e", "dismiss", "Close"),
    ]

    def __init__(self, slug: str, **kwargs):
        super().__init__(**kwargs)
        self._slug = slug

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Explain · {self._slug}", classes="explain-title")
            yield Static("Loading detail…", id="explain-body")

    def set_detail(self, detail: Optional[dict], error: Optional[str]) -> None:
        body = self.query_one("#explain-body", Static)
        if error or detail is None:
            body.update(f"[red]explain failed:[/red] {error or 'no data'}")
            return
        reg = detail.get("registry", {}) or {}
        fit = detail.get("fit", {}) or {}
        benches = detail.get("benchmarks", []) or []
        lines: list[str] = []
        lines.append(f"  [bold]Model[/bold]   {reg.get('model', '—')}")
        lines.append(f"  [bold]Engine[/bold]  {reg.get('engine', '—')}")
        lines.append(f"  [bold]Status[/bold]  {_status_glyph(str(reg.get('status', '')))} {reg.get('status', '—')}")
        if reg.get("status_note"):
            lines.append(f"  [bold]Caveat[/bold]  [yellow]{reg.get('status_note')}[/yellow]")
        lines.append(f"  [bold]Card[/bold]    {detail.get('card', '—')}")
        verdict = str(fit.get("verdict", "—"))
        vram = fit.get("vram_est_gb")
        band = fit.get("band_gb")
        fit_line = verdict
        if vram is not None:
            fit_line += f"  ~{float(vram):.2f} GiB"
            if band is not None:
                fit_line += f" / {float(band):.1f} GiB band"
        lines.append(f"  [bold]Fit[/bold]     {fit_line}")
        if fit.get("max_ctx"):
            lines.append(f"  [bold]Max ctx[/bold] {fit.get('max_ctx')}")
        if benches:
            lines.append("")
            lines.append("  [bold]Measured[/bold]")
            # Fix 3: the REAL shape is [{"row","columns"}]; TPS lives in
            # columns[4] — NOT invented {"narr_tps":…} keys.  Parse each
            # record via measurement_from_explain_columns so the modal shows
            # real numbers and never literal 'None/None'.
            for b in benches[-3:]:
                if not isinstance(b, dict):
                    continue
                m = measurement_from_explain_columns(b)
                n = f"{m.narr_tps:.0f}" if m.narr_tps is not None else "—"
                c = f"{m.code_tps:.0f}" if m.code_tps is not None else "—"
                q = m.quality_8pk or "—"
                d = m.date or ""
                lines.append(f"    {n}/{c} TPS · 8pk {q}  [dim]{d}[/dim]")
        else:
            lines.append("")
            lines.append("  [dim]no structured benchmarks for this slug[/dim]")
        lines.append("")
        lines.append("  [dim]Esc / e to close[/dim]")
        body.update("\n".join(lines))

    def action_dismiss(self) -> None:
        self.app.pop_screen()


# ── Discover · Bring-your-own ────────────────────────────────────────────────────


class ByoPane(Container):
    """Bring-your-own tab: real HF fit-check via pull.sh --dry-run --json."""

    DEFAULT_CSS = """
    ByoPane {
        height: 1fr;
        padding: 1 2;
    }
    ByoPane #byo-heading {
        text-style: bold;
        margin-bottom: 1;
    }
    ByoPane #byo-input-row {
        height: 3;
        margin-bottom: 1;
    }
    ByoPane #byo-url-input {
        width: 1fr;
    }
    ByoPane #byo-profile-input {
        width: 28;
        margin-left: 1;
    }
    ByoPane #byo-fit-btn {
        width: 14;
        margin-left: 1;
    }
    ByoPane #byo-result-card {
        border: solid $primary;
        padding: 1 2;
        margin-top: 1;
        height: auto;
    }
    ByoPane #byo-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Bring-your-own HF model", id="byo-heading")
        with Horizontal(id="byo-input-row"):
            yield Input(
                placeholder="org/Model  (e.g. unsloth/Qwen3-27B-abliterated-GGUF)",
                id="byo-url-input",
            )
            yield Input(
                placeholder="profile-like (vllm/dual)",
                value="vllm/dual",
                id="byo-profile-input",
            )
            yield Button("Fit-check", id="byo-fit-btn", variant="primary")
        yield Static(
            "[dim]Enter an HF repo + a profile-like slug, then Fit-check.\n"
            "Runs pull.sh --dry-run (Path B — evaluates only, never downloads).[/dim]",
            id="byo-result-card",
        )
        yield Label(
            "[dim]Routes:  A = new curated profile   ·   B = serve-locally   ·   "
            "C = reuse a sibling compose + swap weights[/dim]",
            id="byo-hint",
        )

    def set_checking(self, repo: str) -> None:
        self.query_one("#byo-result-card", Static).update(
            f"[dim]Checking[/dim] [cyan]{repo}[/cyan] [dim](pull.sh --dry-run --json)…[/dim]"
        )

    def populate(self, res: ByoResult) -> None:
        card = self.query_one("#byo-result-card", Static)
        if res.error:
            card.update(f"[red]Fit-check failed:[/red] {res.error}")
            return
        lines: list[str] = []
        elig = "[green]eligible[/green]" if res.eligible else "[red]not eligible[/red]"
        lines.append(f"  [bold]{res.repo}[/bold]   {elig}")
        lines.append(f"  [bold]arch[/bold]     [cyan]{res.arch or '—'}[/cyan]")
        fitc = {
            "fits-clean": "[green]● fits-clean[/green]",
            "fits-constrained": "[yellow]◐ fits-constrained[/yellow]",
            "wont-fit": "[red]○ won't-fit[/red]",
        }.get(res.fit_verdict, res.fit_verdict or "—")
        lines.append(f"  [bold]fit[/bold]      {fitc}")
        if res.route:
            route_label = {
                "A": "Route A — author a new curated profile",
                "B": "Route B — serve locally (no catalog entry)",
                "C": "Route C — reuse a sibling compose + swap weights",
            }.get(str(res.route).upper(), f"Route {res.route}")
            lines.append("")
            lines.append(f"  [bold]{route_label}[/bold]")
            if res.sibling_slug:
                lines.append(f"    • reuse compose for [green]{res.sibling_slug}[/green]")
            if res.quant_match:
                lines.append(f"    • match [yellow]--quantization[/yellow] → {res.quant_match}")
            if res.drop_spec_config:
                lines.append("    • drop [yellow]--speculative-config[/yellow] (no MTP head in fine-tune)")
        if res.note:
            lines.append("")
            lines.append(f"  [dim]{res.note}[/dim]")
        card.update("\n".join(lines))


# ── Serve pane ───────────────────────────────────────────────────────────────────


class ServePane(Container):
    """Serve mode: the live plan-confirm box (§7 #8) + boot LivePane."""

    DEFAULT_CSS = """
    ServePane {
        height: 1fr;
        padding: 1 1;
    }
    ServePane #serve-heading {
        text-style: bold;
        margin: 0 1 1 1;
    }
    ServePane #serve-plan-box {
        border: solid $primary;
        padding: 1 2;
        height: auto;
        margin: 0 1 1 1;
    }
    ServePane #serve-plan-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    ServePane #serve-btn-row {
        height: 3;
        margin: 0 1 1 1;
    }
    ServePane #serve-launch-btn {
        width: 14;
    }
    ServePane #serve-force-btn {
        width: 12;
        margin-left: 1;
    }
    ServePane #serve-cancel-btn {
        width: 12;
        margin-left: 1;
    }
    ServePane LivePane {
        height: 1fr;
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Serve", id="serve-heading")
        with Container(id="serve-plan-box"):
            yield Label("Launch plan", id="serve-plan-title")
            yield Static(
                "[dim]No slug selected.  Pick a row in Discover · Catalog and press "
                "⏎ to stage a launch plan here.[/dim]",
                id="serve-plan-detail",
            )
        with Horizontal(id="serve-btn-row"):
            yield Button("⏎ Launch", id="serve-launch-btn", variant="success")
            yield Button("F Force", id="serve-force-btn", variant="warning")
            yield Button("Esc Cancel", id="serve-cancel-btn")
        yield LivePane(id="serve-live")

    def show_plan(self, entry: CatalogEntry) -> None:
        """Render the staged launch plan for a selected catalog entry (pre-gate)."""
        fit = entry.fit
        fit_glyphs = {
            "fits-clean": "[green]● fits-clean[/green]",
            "fits-constrained": "[yellow]◐ fits-constrained[/yellow]",
            "wont-fit": "[red]○ won't-fit[/red]",
            "skip": "[dim]· (ik/llama — kv-calc skipped)[/dim]",
            "unknown": "[dim]· unknown[/dim]",
        }
        fit_line = fit_glyphs.get(fit.verdict, fit.verdict)
        if fit.vram_est_gb is not None:
            fit_line += f"  ~{fit.vram_est_gb:.1f} GiB"
        detail = [
            f"  [bold]Slug[/bold]      {entry.slug}",
            f"  [bold]Engine[/bold]    {entry.engine}",
            f"  [bold]Status[/bold]    {_status_glyph(entry.status)} {entry.status}",
            f"  [bold]Fit[/bold]       {fit_line}",
            f"  [bold]Max ctx[/bold]   {entry.ctx_label or '—'}",
        ]
        if entry.status_note:
            detail.append(f"  [bold]Caveat[/bold]    [yellow]{entry.status_note}[/yellow]")
        detail.append("  [dim]⏎ Launch (gated) · F Force (surfaced override) · Esc Cancel[/dim]")
        self.query_one("#serve-plan-detail", Static).update("\n".join(detail))

    def clear_plan(self) -> None:
        self.query_one("#serve-plan-detail", Static).update(
            "[dim]No slug selected.  Pick a row in Discover · Catalog and press "
            "⏎ to stage a launch plan here.[/dim]"
        )


# ── Confirm modal (used for serve + scene + container writes) ────────────────────


class ConfirmActionScreen(ModalScreen):
    """The reconcile-gated confirm modal (design §7 #8 / §3.2).

    Shows the plan + the FRESH reconcile result (what this write would collide
    with / tear down), and only on confirm dispatches the write through the
    app's gated executor.  When the gate is unsafe, the primary action is
    disabled and the user must surface the explicit Force override (which is
    routed back to the app with a reason).
    """

    DEFAULT_CSS = """
    ConfirmActionScreen {
        align: center middle;
    }
    ConfirmActionScreen > Vertical {
        width: 80;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    ConfirmActionScreen .confirm-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    ConfirmActionScreen #confirm-body {
        height: auto;
        margin-bottom: 1;
    }
    ConfirmActionScreen #confirm-btn-row {
        height: 3;
        align: left middle;
    }
    ConfirmActionScreen Button {
        margin-right: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, plan: ActionPlan, **kwargs):
        super().__init__(**kwargs)
        self._plan = plan
        self._reconcile: Optional[ReconcileResult] = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Confirm · {self._plan.description}", classes="confirm-title")
            yield Static("Re-running reconcile gate (fresh detect)…", id="confirm-body")
            with Horizontal(id="confirm-btn-row"):
                yield Button("⏎ Confirm", id="confirm-ok-btn", variant="success", disabled=True)
                yield Button("F Force", id="confirm-force-btn", variant="warning", disabled=True)
                yield Button("Esc Cancel", id="confirm-cancel-btn")

    def on_mount(self) -> None:
        # Re-run the gate (fresh detect) before enabling any commit button.
        self.app.run_reconcile_for_modal(self, self._plan)  # type: ignore[attr-defined]

    def set_reconcile(self, rec: ReconcileResult) -> None:
        """Render the reconcile verdict + enable the appropriate commit path."""
        self._reconcile = rec
        body = self.query_one("#confirm-body", Static)
        ok_btn = self.query_one("#confirm-ok-btn", Button)
        force_btn = self.query_one("#confirm-force-btn", Button)

        lines: list[str] = [f"  [bold]Command[/bold]  {' '.join(self._plan.cmd)}"]
        wanted = ", ".join(str(g) for g in rec.pending_gpus) if rec.pending_gpus else "—"
        lines.append(f"  [bold]GPUs[/bold]     {wanted}")

        if rec.safe:
            lines.append("")
            lines.append("  [green]● gate clear[/green] — nothing live overlaps the requested GPUs.")
            lines.append("  [dim]⏎ Confirm to launch (streams below) · Esc Cancel[/dim]")
            ok_btn.disabled = False
            force_btn.disabled = True
            ok_btn.focus()
        else:
            lines.append("")
            lines.append("  [yellow]⚠ this write would tear down / collide with:[/yellow]")
            for c in rec.conflicts:
                g = f" (GPU {c.gpus})" if c.gpus else ""
                slug = f"  [{c.slug}]" if c.slug else ""
                lines.append(f"    • container [red]{c.name}[/red]{g}{slug}")
            for gc in rec.gpu_conflicts:
                lines.append(
                    f"    • GPU{gc.gpu_index} busy ([red]{gc.mem_used_mib} MiB[/red])"
                )
            for inst in rec.estate_claims:
                name = inst.get("name", "?")
                gpus = inst.get("gpus", [])
                lines.append(f"    • estate instance [red]{name}[/red] (GPU {gpus})")
            if rec.note:
                lines.append(f"  [dim]{rec.note}[/dim]")
            lines.append("")
            lines.append("  [dim]Confirm is disabled — F to FORCE this teardown (override).[/dim]")
            ok_btn.disabled = True
            force_btn.disabled = False
            force_btn.focus()

        body.update("\n".join(lines))

    # ── button / key handlers ────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-ok-btn":
            self._commit(force=False)
        elif event.button.id == "confirm-force-btn":
            self._commit(force=True)
        elif event.button.id == "confirm-cancel-btn":
            self.action_cancel()

    def on_key(self, event) -> None:
        if event.key == "f":
            force_btn = self.query_one("#confirm-force-btn", Button)
            if not force_btn.disabled:
                event.stop()
                self._commit(force=True)
        elif event.key == "enter":
            ok_btn = self.query_one("#confirm-ok-btn", Button)
            if not ok_btn.disabled:
                event.stop()
                self._commit(force=False)

    def _commit(self, *, force: bool) -> None:
        plan = self._plan
        if force and not plan.force:
            # Re-issue the plan as a forced one (with a surfaced reason) so the
            # executor's force path is taken explicitly — never silently.
            plan = ActionPlan(
                kind=plan.kind,
                cmd=_with_force(plan),
                description=plan.description + " (FORCED)",
                is_write=plan.is_write,
                requires_reconcile=plan.requires_reconcile,
                force=True,
                force_reason="user accepted teardown via Force override",
            )
        self.app.pop_screen()
        # Hand the actual (gated, mocked-in-test) execution back to the app.
        self.app.dispatch_action(plan)  # type: ignore[attr-defined]

    def action_cancel(self) -> None:
        self.app.pop_screen()


def _with_force(plan: ActionPlan) -> list[str]:
    """Insert --force into a serve switch.sh command for the forced re-issue.

    Only the serve (switch.sh) plan supports --force; for other kinds the
    command is unchanged (the force flag just relaxes the gate refusal)."""
    cmd = list(plan.cmd)
    if plan.kind == "serve" and "scripts/switch.sh" in cmd and "--force" not in cmd:
        # switch.sh --force <slug>: insert before the slug (last positional).
        cmd.insert(len(cmd) - 1, "--force")
    return cmd


# ── Estate · Orchestration ───────────────────────────────────────────────────────


class EstateOrchPane(Container):
    """Estate / Orchestration tab: GPU cards, Doctor, scene table, services."""

    DEFAULT_CSS = """
    EstateOrchPane {
        height: 1fr;
    }
    EstateOrchPane #orch-scroll {
        height: 1fr;
    }
    EstateOrchPane .gpu-card {
        border: solid $primary;
        padding: 0 1;
        margin: 0 1 1 1;
        height: auto;
    }
    EstateOrchPane .gpu-card-title {
        text-style: bold;
        color: $accent;
    }
    EstateOrchPane #doctor-line {
        padding: 0 1;
        margin: 0 1 1 1;
        color: $text;
    }
    EstateOrchPane #scene-heading {
        text-style: bold;
        padding: 0 1;
        margin: 0 1 0 1;
    }
    EstateOrchPane DataTable {
        height: auto;
        margin: 0 1 1 1;
    }
    EstateOrchPane #services-heading {
        text-style: bold;
        padding: 0 1;
        margin: 0 1 0 1;
    }
    EstateOrchPane #services-strip {
        padding: 0 1;
        margin: 0 1 1 1;
    }
    EstateOrchPane #orch-hint {
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="orch-scroll"):
            with Container(classes="gpu-card", id="gpu0-card"):
                yield Label("GPU0", classes="gpu-card-title")
                yield Static("[dim]querying nvidia-smi…[/dim]", id="gpu0-bar")
            with Container(classes="gpu-card", id="gpu1-card"):
                yield Label("GPU1", classes="gpu-card-title")
                yield Static("[dim]querying nvidia-smi…[/dim]", id="gpu1-bar")
            yield Static("[dim]reading health.sh…[/dim]", id="doctor-line")
            yield Label("Scenes  [dim](⏎ to switch — gated)[/dim]", id="scene-heading")
            scene_table: DataTable = DataTable(
                id="scene-table", zebra_stripes=True, show_cursor=True
            )
            scene_table.cursor_type = "row"
            yield scene_table
            yield Label("Services", id="services-heading")
            yield Static("[dim]reading estate…[/dim]", id="services-strip")
            yield Label(
                "[dim]\\[⏎] switch scene (reconcile-gated)   \\[o] stop all (gated)[/dim]",
                id="orch-hint",
            )

    def on_mount(self) -> None:
        t = self.query_one("#scene-table", DataTable)
        t.add_columns("Scene", "Group", "GPUs", "Services")
        self._scenes: list[Scene] = []

    # ── data ────────────────────────────────────────────────────────────────────

    def populate(self, state: EstateState) -> None:
        self._populate_gpus(state)
        self._populate_doctor(state)
        self._populate_scenes(state.scenes)
        self._populate_services(state)

    def _populate_gpus(self, state: EstateState) -> None:
        for i, bar_id, title_id in ((0, "#gpu0-bar", "#gpu0-card"), (1, "#gpu1-bar", "#gpu1-card")):
            bar = self.query_one(bar_id, Static)
            gpu = next((g for g in state.gpus if getattr(g, "index", -1) == i), None)
            if gpu is None:
                bar.update("[dim]not present[/dim]")
                continue
            used = getattr(gpu, "mem_used_mib", 0)
            total = getattr(gpu, "mem_total_mib", 0) or 1
            util = getattr(gpu, "utilization", 0)
            pwr = getattr(gpu, "power_draw_w", 0.0)
            pwr_lim = getattr(gpu, "power_limit_w", 0.0)
            temp = getattr(gpu, "temp_c", 0)
            pct = int(used / total * 100) if total else 0
            filled = max(0, min(20, round(pct / 5)))
            color = "green" if pct < 80 else "yellow" if pct < 95 else "red"
            bar_str = f"[{color}]{'█' * filled}[/{color}][dim]{'░' * (20 - filled)}[/dim]"
            bar.update(
                f"  {bar_str}  {used / 1024:.1f} / {total / 1024:.1f} GiB · {pct}%\n"
                f"  {pwr:.0f} / {pwr_lim:.0f} W · {temp}°C · util {util}%"
            )

    def _populate_doctor(self, state: EstateState) -> None:
        dr = state.doctor
        line = self.query_one("#doctor-line", Static)
        if not dr.reachable:
            line.update("[red]○[/red] API not reachable")
            return
        glyph = "[green]●[/green]" if dr.serving else "[yellow]○[/yellow]"
        line.update(f"{glyph} {dr.summary}")

    def _populate_scenes(self, scenes: list[Scene]) -> None:
        self._scenes = list(scenes)
        t = self.query_one("#scene-table", DataTable)
        t.clear()
        for s in scenes:
            svc = ", ".join(s.services[:3]) + ("…" if len(s.services) > 3 else "")
            t.add_row(s.name, s.group, s.gpus or "—", svc or "—")

    def _populate_services(self, state: EstateState) -> None:
        strip = self.query_one("#services-strip", Static)
        # Services come from the running-container view + scene catalog.
        svc_names: list[str] = []
        for c in state.containers:
            if c.kind == "service":
                svc_names.append(c.name)
        if not svc_names:
            ae = (state.estate_report or {}).get("active_estate") or {}
            insts = ae.get("instances") or []
            if insts:
                strip.update(
                    "  "
                    + "   ".join(
                        f"[green]●[/green] {i.get('name', '?')} (GPU {i.get('gpus', [])})"
                        for i in insts
                    )
                )
                return
            strip.update("[dim]no stack services detected[/dim]")
            return
        strip.update("  " + "   ".join(f"[green]●[/green] {n}" for n in svc_names))

    def selected_scene(self) -> Optional[Scene]:
        t = self.query_one("#scene-table", DataTable)
        idx = t.cursor_row
        if 0 <= idx < len(self._scenes):
            return self._scenes[idx]
        return None


# ── Estate · Containers ──────────────────────────────────────────────────────────


class EstateContainersPane(Container):
    """Estate / Containers tab: container list + drill-down area."""

    DEFAULT_CSS = """
    EstateContainersPane {
        height: 1fr;
    }
    EstateContainersPane #containers-heading {
        text-style: bold;
        padding: 0 1;
        margin: 0 1 0 1;
    }
    EstateContainersPane #containers-table {
        height: auto;
        margin: 0 1 0 1;
        max-height: 12;
    }
    EstateContainersPane #drill-tabs {
        height: 1fr;
        margin: 1 1 0 1;
        border: solid $primary;
    }
    EstateContainersPane #drill-logs {
        height: 1fr;
    }
    EstateContainersPane #drill-stats {
        padding: 1;
        color: $text;
    }
    EstateContainersPane #drill-config {
        padding: 1;
        color: $text-muted;
    }
    EstateContainersPane #containers-hint {
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Containers", id="containers-heading")
        ct: DataTable = DataTable(
            id="containers-table", zebra_stripes=True, show_cursor=True
        )
        ct.cursor_type = "row"
        yield ct
        with TabbedContent(id="drill-tabs"):
            with TabPane("Logs", id="drill-tab-logs"):
                yield LivePane(id="drill-logs")
            with TabPane("Stats", id="drill-tab-stats"):
                yield Static("[dim]select a container to read docker stats[/dim]", id="drill-stats")
            with TabPane("Config", id="drill-tab-config"):
                yield Static("[dim]select a container to read its config[/dim]", id="drill-config")
        yield Label(
            "[dim]\\[l] logs   \\[s] restart (gated)   \\[x] stop (gated)[/dim]",
            id="containers-hint",
        )

    def on_mount(self) -> None:
        t = self.query_one("#containers-table", DataTable)
        t.add_columns("Name", "Kind", "Engine", "Port", "Slug")
        self._containers: list[ContainerInfo] = []

    def populate(self, containers: list[ContainerInfo]) -> None:
        self._containers = list(containers)
        t = self.query_one("#containers-table", DataTable)
        t.clear()
        if not containers:
            t.add_row("[dim]no stack containers[/dim]", "—", "—", "—", "—")
            return
        for c in containers:
            t.add_row(
                c.name,
                c.kind,
                c.engine or "—",
                str(c.host_port) if c.host_port else "—",
                c.slug or "—",
            )

    def selected_container(self) -> Optional[ContainerInfo]:
        t = self.query_one("#containers-table", DataTable)
        idx = t.cursor_row
        if 0 <= idx < len(self._containers):
            return self._containers[idx]
        return None


# ── Validate panes (Phase 4 — left as illustrative; out of scope this step) ──────


class ValidateRunPane(Container):
    """Validate / Run tab: ladder steps + extra tools + output area.

    Phase 4 surface — left as an illustrative mockup; wiring lands with the
    §5.2 evidence corpus.  Clearly labelled so it isn't mistaken for live data.
    """

    DEFAULT_CSS = """
    ValidateRunPane {
        height: 1fr;
    }
    ValidateRunPane #run-scroll {
        height: 1fr;
    }
    ValidateRunPane #run-heading {
        text-style: bold;
        padding: 0 1;
        margin: 0 1 0 1;
    }
    ValidateRunPane #run-ladder {
        border: solid $primary;
        padding: 1 2;
        margin: 0 1 1 1;
        height: auto;
    }
    ValidateRunPane #run-extras {
        border: solid $primary;
        padding: 1 2;
        margin: 0 1 1 1;
        height: auto;
    }
    ValidateRunPane #run-output {
        border: solid $primary;
        padding: 1 2;
        margin: 0 1 1 1;
        height: auto;
        color: $text-muted;
    }
    ValidateRunPane #run-phase-note {
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="run-scroll"):
            yield Label("Run  [dim](illustrative — Phase 4)[/dim]", id="run-heading")
            yield Static(
                "[bold]Validation ladder[/bold]  [dim](example steps)[/dim]\n\n"
                "  [cyan]▷[/cyan] verify-full         [green]✓[/green]   [dim]0m 18s[/dim]\n"
                "  [cyan]▷[/cyan] verify-stress       [green]✓[/green]   [dim]4m 02s[/dim]\n"
                "  [cyan]▷[/cyan] bench               [green]✓[/green]   [dim]1m 35s[/dim]\n"
                "  [cyan]▷[/cyan] quality-test        [dim]–[/dim]   [dim]not run[/dim]\n"
                "  [cyan]▷[/cyan] soak-test           [dim]–[/dim]   [dim]not run[/dim]\n"
                "  [cyan]▷[/cyan] rebench-full        [dim]–[/dim]   [dim]not run[/dim]",
                id="run-ladder",
            )
            yield Static(
                "[bold]Extra tools[/bold]  [dim](illustrative)[/dim]\n\n"
                "  [cyan]▷[/cyan] quality-baseline    [dim]–[/dim]   [dim]regression diff vs baseline[/dim]\n"
                "  [cyan]▷[/cyan] bench-agentic       [dim]–[/dim]   [dim]multi-turn prefill stress[/dim]\n"
                "  [cyan]▷[/cyan] stream-toolcall-probe  [dim]–[/dim]   [dim]silent streaming check[/dim]",
                id="run-extras",
            )
            yield Static(
                "[dim]Output  (select a step above and press ⏎ to run — Phase 4)[/dim]\n\n"
                "  [dim]…[/dim]",
                id="run-output",
            )
            yield Label("[dim]Not wired — Phase 4[/dim]", id="run-phase-note")


class ValidateDoctorPane(Container):
    """Validate / Doctor tab: health/estate/profile cards (live health line)."""

    DEFAULT_CSS = """
    ValidateDoctorPane {
        height: 1fr;
        padding: 1 2;
    }
    ValidateDoctorPane #doctor-heading {
        text-style: bold;
        margin-bottom: 1;
    }
    ValidateDoctorPane .doctor-card {
        border: solid $primary;
        padding: 1 2;
        margin-bottom: 1;
        height: auto;
    }
    ValidateDoctorPane .doctor-card-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    ValidateDoctorPane #doctor-phase-note {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Doctor", id="doctor-heading")
        with Container(classes="doctor-card", id="doctor-card-health"):
            yield Label("health.sh", classes="doctor-card-title")
            yield Static("[dim]reading health.sh…[/dim]", id="doctor-health-body")
        with Container(classes="doctor-card", id="doctor-card-estate"):
            yield Label("diagnose-estate", classes="doctor-card-title")
            yield Static("[dim](illustrative — Phase 4)[/dim]", id="doctor-estate-body")
        with Container(classes="doctor-card", id="doctor-card-profile"):
            yield Label("diagnose-profile", classes="doctor-card-title")
            yield Static("[dim](illustrative — Phase 4)[/dim]", id="doctor-profile-body")
        yield Label(
            "[dim]health line is live; estate / profile cards land in Phase 4[/dim]",
            id="doctor-phase-note",
        )

    def populate(self, state: EstateState) -> None:
        dr = state.doctor
        body = self.query_one("#doctor-health-body", Static)
        if not dr.reachable:
            body.update("[red]✗[/red]  API not reachable")
            return
        glyph = "[green]✓[/green]" if dr.serving else "[yellow]○[/yellow]"
        body.update(f"{glyph}  {dr.summary}")


class ValidateBenchmarksPane(Container):
    """Validate / Benchmarks tab — illustrative; real corpus in Phase 4."""

    DEFAULT_CSS = """
    ValidateBenchmarksPane {
        height: 1fr;
    }
    ValidateBenchmarksPane #bench-heading {
        text-style: bold;
        padding: 0 1;
        margin: 0 1 0 1;
    }
    ValidateBenchmarksPane #bench-table {
        height: 1fr;
        margin: 0 1 1 1;
    }
    ValidateBenchmarksPane #bench-phase-note {
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label(
            "Benchmarks  [dim](illustrative — real data via §5.2 corpus · Phase 4)[/dim]",
            id="bench-heading",
        )
        bt: DataTable = DataTable(id="bench-table", zebra_stripes=True, show_cursor=True)
        bt.cursor_type = "row"
        yield bt
        yield Label("[dim]Not wired — Phase 4[/dim]", id="bench-phase-note")

    def on_mount(self) -> None:
        t = self.query_one("#bench-table", DataTable)
        t.add_columns("Model", "Engine", "Topo", "TPS (n/c)", "ctx", "8pk", "src")
        t.add_row("qwen3.6-27b", "vllm", "dual", "[dim]174 / 42[/dim]", "295K", "[dim]109[/dim]", "[dim]mock[/dim]")
        t.add_row("qwen3.6-27b", "beellama", "dual", "[dim]155 / 38[/dim]", "102K", "[dim]107[/dim]", "[dim]mock[/dim]")
        t.add_row("gemma-4-31b", "vllm", "dual", "[dim]112 / 29[/dim]", "192K", "[dim]103[/dim]", "[dim]mock[/dim]")
        t.add_row("qwen3.6-35b-a3b", "vllm", "dual", "[dim]178 / 44[/dim]", "262K", "[dim]90[/dim]", "[dim]mock[/dim]")


class ValidateEvidencePane(Container):
    """Validate / Evidence tab — illustrative; real run tags in Phase 4."""

    DEFAULT_CSS = """
    ValidateEvidencePane {
        height: 1fr;
        padding: 1 2;
    }
    ValidateEvidencePane #evidence-heading {
        text-style: bold;
        margin-bottom: 1;
    }
    ValidateEvidencePane #evidence-list {
        border: solid $primary;
        padding: 1 2;
        height: auto;
    }
    ValidateEvidencePane #evidence-phase-note {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Evidence  [dim](illustrative — Phase 4)[/dim]", id="evidence-heading")
        yield Static(
            "[bold]Past runs[/bold]  [dim](example tags)[/dim]\n\n"
            "  results/rebench/[cyan]vllm-dual-20260618[/cyan]  ·  2026-06-18  ·  [green]PASS[/green]\n"
            "  results/rebench/[cyan]vllm-dual-20260615[/cyan]  ·  2026-06-15  ·  [green]PASS[/green]\n"
            "  [dim](illustrative — Phase 4)[/dim]",
            id="evidence-list",
        )
        yield Label("[dim]Not wired — Phase 4[/dim]", id="evidence-phase-note")


# ── Mode switcher (left rail) ─────────────────────────────────────────────────────


MODES = [
    ("Discover", "1"),
    ("Serve", "2"),
    ("Estate", "3"),
    ("Validate", "4"),
]

# Per-mode primary action (what ⏎ does), by mode index.
PRIMARY_ACTIONS = ["Serve", "Launch", "Switch scene", "Run"]


class RailStatus(Static):
    """Persistent left-rail status card — mirrors c3t's TargetPane.

    Wired in Phase 3: shows the live detect / doctor read.  Until the first
    estate poll completes it shows a 'detecting…' placeholder."""

    PLACEHOLDER = (
        "[bold]Estate[/bold]\n"
        "\n"
        "[dim]detecting…[/dim]\n"
        "\n"
        "[dim]press 3 (Estate) to poll[/dim]"
    )

    def __init__(self, **kwargs):
        super().__init__(self.PLACEHOLDER, **kwargs)

    def update_from_state(self, state: EstateState) -> None:
        lines: list[str] = ["[bold]Estate[/bold]", ""]
        for i in (0, 1):
            gpu = next((g for g in state.gpus if getattr(g, "index", -1) == i), None)
            if gpu is None:
                continue
            used = getattr(gpu, "mem_used_mib", 0) / 1024
            total = (getattr(gpu, "mem_total_mib", 0) or 1) / 1024
            pct = int(used / total * 100) if total else 0
            filled = max(0, min(10, round(pct / 10)))
            color = "green" if pct < 80 else "yellow" if pct < 95 else "red"
            bar = f"[{color}]{'█' * filled}[/{color}][dim]{'░' * (10 - filled)}[/dim]"
            lines.append(f"{bar} GPU{i} {used:.0f}/{total:.0f}G")
        lines.append("")
        if state.matched_slug:
            lines.append(f"model   {state.matched_slug}")
        elif state.target is not None and getattr(state.target, "model", ""):
            lines.append(f"model   {state.target.model}")
        dr = state.doctor
        if dr.reachable:
            glyph = "[green]●[/green]" if dr.serving else "[yellow]○[/yellow]"
            lines.append(f"{glyph} {dr.summary}")
        else:
            lines.append("[red]○[/red] not reachable")
        self.update("\n".join(lines))


class ModeSwitcher(Static):
    """Left-rail mode selector — navigation is driven by CockpitApp via the
    1–4 digit bindings; this is the visual highlight."""

    DEFAULT_CSS = """
    ModeSwitcher {
        width: 1fr;
        height: auto;
        border: solid $primary;
        padding: 0 1;
    }
    ModeSwitcher .mode-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    ModeSwitcher .mode-item {
        color: $text;
    }
    ModeSwitcher .mode-item-active {
        color: $accent;
        text-style: bold;
    }
    ModeSwitcher .mode-action-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._active = 0

    def compose(self) -> ComposeResult:
        yield Label("Modes", classes="mode-title")
        for i, (name, digit) in enumerate(MODES):
            classes = "mode-item-active" if i == 0 else "mode-item"
            yield Label(f"▸ {name} [{digit}]" if i == 0 else f"  {name} [{digit}]",
                        id=f"mode-{i}", classes=classes)
        yield Label(f"⏎ {PRIMARY_ACTIONS[0]}", id="mode-action-hint",
                    classes="mode-action-hint")

    def set_active(self, index: int) -> None:
        self._active = index
        for i, (name, digit) in enumerate(MODES):
            try:
                lbl = self.query_one(f"#mode-{i}", Label)
                lbl.remove_class("mode-item-active")
                lbl.add_class("mode-item")
                if i == index:
                    lbl.remove_class("mode-item")
                    lbl.add_class("mode-item-active")
                    lbl.update(f"▸ {name} [{digit}]")
                else:
                    lbl.update(f"  {name} [{digit}]")
            except Exception:
                pass
        try:
            self.query_one("#mode-action-hint", Label).update(
                f"⏎ {PRIMARY_ACTIONS[index]}"
            )
        except Exception:
            pass


# ── Main application ──────────────────────────────────────────────────────────────


class CockpitApp(App):
    """club3090 serve cockpit — Phase 3 (wired)."""

    TITLE = "club3090 cockpit"
    SUB_TITLE = "wired"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("question_mark", "help", "Help", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("slash", "filter_catalog", "Filter", show=True),
        Binding("e", "explain", "Explain", show=True),
        Binding("1", "mode_discover", "Discover", show=True),
        Binding("2", "mode_serve", "Serve", show=True),
        Binding("3", "mode_estate", "Estate", show=True),
        Binding("4", "mode_validate", "Validate", show=True),
        Binding("enter", "primary_action", "Select", show=True),
        # Catalog (Discover) — default pin management (.env write, gated=no GPU).
        Binding("d", "set_default", "Set default", show=False),
        Binding("D", "clear_default", "Clear default", show=False),
        # Estate · Containers — logs (read) + restart/stop (gated writes).
        Binding("l", "container_logs", "Logs", show=False),
        Binding("s", "container_restart", "Restart", show=False),
        Binding("x", "container_stop", "Stop", show=False),
        # Estate · Orchestration — stop all (estate down, gated write).
        Binding("o", "estate_off", "Stop all", show=False),
    ]

    CSS = """
    #main-layout {
        height: 1fr;
    }
    #left-rail {
        width: 32;
        height: 1fr;
        padding: 0 0;
    }
    #rail-status {
        width: 1fr;
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
        margin-top: 1;
        color: $text;
    }
    #content-area {
        width: 1fr;
        height: 1fr;
    }
    .mode-panel {
        width: 1fr;
        height: 1fr;
        display: none;
    }
    .mode-panel.active {
        display: block;
    }
    """

    def __init__(self, repo_root: Path, *, data: Optional[CockpitData] = None, **kwargs):
        super().__init__(**kwargs)
        self._repo_root = repo_root
        # Injectable service layer — defaults to the real (live-read) impl.
        self._data: CockpitData = data or CockpitData(repo_root)
        self._active_mode = 0  # 0=Discover 1=Serve 2=Estate 3=Validate
        # Cache the last-loaded variants so detect/match + containers can match
        # running engines back to registry slugs.
        self._variants: list[VariantRow] = []
        # The slug staged for serve (selected from the catalog).
        self._staged_entry: Optional[CatalogEntry] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-layout"):
            with Vertical(id="left-rail"):
                yield ModeSwitcher(id="mode-switcher")
                yield RailStatus(id="rail-status")
            with Container(id="content-area"):
                # Mode 0 — Discover
                with Container(id="panel-discover", classes="mode-panel active"):
                    with TabbedContent(id="discover-tabs"):
                        with TabPane("Catalog", id="tab-catalog"):
                            yield CatalogPane(id="catalog-pane")
                        with TabPane("Bring-your-own", id="tab-byo"):
                            yield ByoPane(id="byo-panel")

                # Mode 1 — Serve
                with Container(id="panel-serve", classes="mode-panel"):
                    yield ServePane(id="serve-panel")

                # Mode 2 — Estate
                with Container(id="panel-estate", classes="mode-panel"):
                    with TabbedContent(id="estate-tabs"):
                        with TabPane("Orchestration", id="tab-orchestration"):
                            yield EstateOrchPane(id="estate-orch-pane")
                        with TabPane("Containers", id="tab-containers"):
                            yield EstateContainersPane(id="estate-containers-pane")

                # Mode 3 — Validate
                with Container(id="panel-validate", classes="mode-panel"):
                    with TabbedContent(id="validate-tabs"):
                        with TabPane("Run", id="tab-run"):
                            yield ValidateRunPane(id="validate-run-pane")
                        with TabPane("Doctor", id="tab-doctor"):
                            yield ValidateDoctorPane(id="validate-doctor-pane")
                        with TabPane("Benchmarks", id="tab-benchmarks"):
                            yield ValidateBenchmarksPane(id="validate-benchmarks-pane")
                        with TabPane("Evidence", id="tab-evidence"):
                            yield ValidateEvidencePane(id="validate-evidence-pane")
        yield Footer()

    # ── Mount / startup ────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.load_catalog()

    # ── Catalog loading ──────────────────────────────────────────────────────────────

    @work(exclusive=True, group="catalog")
    async def load_catalog(self) -> None:
        """Load the enriched catalog from the service layer (real read)."""
        entries, error = await self._data.load_catalog()
        # Cache variants for detect/match + container slug-matching.
        self._variants = [e.row for e in entries]
        try:
            self.query_one("#catalog-pane", CatalogPane).populate(entries, error)
        except Exception:
            pass

    # ── Estate polling ───────────────────────────────────────────────────────────────

    @work(exclusive=True, group="estate")
    async def load_estate(self) -> None:
        """Poll the live estate snapshot + push into the orch/doctor panes + rail."""
        state = await self._data.estate_state(variants=self._variants or None)
        try:
            self.query_one("#estate-orch-pane", EstateOrchPane).populate(state)
        except Exception:
            pass
        try:
            self.query_one("#estate-containers-pane", EstateContainersPane).populate(
                state.containers
            )
        except Exception:
            pass
        try:
            self.query_one("#validate-doctor-pane", ValidateDoctorPane).populate(state)
        except Exception:
            pass
        try:
            self.query_one("#rail-status", RailStatus).update_from_state(state)
        except Exception:
            pass

    # ── BYO fit-check ────────────────────────────────────────────────────────────────

    @work(exclusive=True, group="byo")
    async def run_byo_check(self, repo: str, profile_like: str) -> None:
        pane = self.query_one("#byo-panel", ByoPane)
        pane.set_checking(repo)
        res = await self._data.byo_check(repo, profile_like)
        pane.populate(res)

    # ── Explain ──────────────────────────────────────────────────────────────────────

    @work(group="explain")
    async def run_explain(self, screen: ExplainScreen, slug: str) -> None:
        detail, err = await self._data.explain(slug)
        try:
            screen.set_detail(detail, err)
        except Exception:
            pass

    # ── The reconcile gate (called by the confirm modal on mount) ────────────────────

    @work(group="reconcile")
    async def run_reconcile_for_modal(self, screen: ConfirmActionScreen, plan: ActionPlan) -> None:
        """Re-run the FRESH reconcile gate for a pending write, push verdict back
        into the confirm modal.  Pending GPUs are inferred from the plan kind
        (None = conservative both-cards for a serve/scene)."""
        pending = self._pending_gpus_for(plan)
        rec = await self._data.reconcile_before_write(
            f"{plan.kind}:{plan.description}",
            pending_gpus=pending,
            variants=self._variants or None,
        )
        try:
            screen.set_reconcile(rec)
        except Exception:
            pass

    def _pending_gpus_for(self, plan: ActionPlan) -> Optional[list[int]]:
        """Best-effort GPUs a write wants.  Conservative None → both cards for
        serve / scene; container ops target whatever the named container holds
        (unknown → conservative None)."""
        return None

    # ── Write dispatch (GATED · execution mocked in tests, NEVER live this phase) ────

    @work(exclusive=True, group="dispatch")
    async def dispatch_action(self, plan: ActionPlan) -> None:
        """Execute a confirmed write ActionPlan through the gated executor.

        ⚠️  WRITE PATH.  ``execute_action`` re-runs the reconcile gate itself and
        refuses if unsafe (unless the plan carries an explicit force + reason).
        The actual command is streamed via the core SubprocessRunner — in this
        phase that runner is NEVER executed live; tests inject a fake.

        Serialized two ways: ``exclusive=True`` on this worker group means a
        second dispatch cancels/queues rather than racing; and
        ``execute_action`` holds ``CockpitData._write_lock`` across the
        gate→write window so even direct concurrent calls can't TOCTOU the gate.
        """
        live = self._serve_live_pane()
        executed, rec, _state = await self._data.execute_action(
            plan, variants=self._variants or None
        )
        if not executed:
            summary = rec.conflict_summary if rec else "unknown"
            self.notify(
                f"Refused — gate unsafe (collides with: {summary}). Use Force to override.",
                title="Reconcile gate",
                severity="warning",
                timeout=6,
            )
            if live is not None:
                live.append_line(f"[red]✗ refused[/red] — {plan.description} (gate unsafe: {summary})")
            return
        self.notify(
            f"{plan.description} dispatched.",
            title="Action",
            severity="information",
            timeout=4,
        )
        if live is not None and plan.kind == "serve":
            live.append_line(f"[green]▶ launching[/green] {plan.description}")
            live.append_line("[dim](boot log streams here)[/dim]")

    def _serve_live_pane(self) -> Optional[LivePane]:
        try:
            return self.query_one("#serve-live", LivePane)
        except Exception:
            return None

    # ── Mode switching ───────────────────────────────────────────────────────────────

    def _switch_mode(self, index: int) -> None:
        panel_ids = ["panel-discover", "panel-serve", "panel-estate", "panel-validate"]
        for i, pid in enumerate(panel_ids):
            try:
                panel = self.query_one(f"#{pid}")
                if i == index:
                    panel.add_class("active")
                else:
                    panel.remove_class("active")
            except Exception:
                pass
        try:
            self.query_one("#mode-switcher", ModeSwitcher).set_active(index)
        except Exception:
            pass
        self._active_mode = index
        # Estate is live — poll on entry.
        if index == 2:
            self.load_estate()

    # ── Actions ──────────────────────────────────────────────────────────────────────

    def action_mode_discover(self) -> None:
        self._switch_mode(0)

    def action_mode_serve(self) -> None:
        self._switch_mode(1)

    def action_mode_estate(self) -> None:
        self._switch_mode(2)

    def action_mode_validate(self) -> None:
        self._switch_mode(3)

    def action_refresh(self) -> None:
        """Re-read the live data layer for the active mode."""
        if self._active_mode == 2:
            self.load_estate()
        else:
            try:
                self.query_one("#catalog-pane", CatalogPane).query_one(
                    "#catalog-status", Label
                ).update("Refreshing catalog…")
            except Exception:
                pass
            self.load_catalog()

    def action_filter_catalog(self) -> None:
        if self._active_mode == 0:
            try:
                self.query_one("#catalog-pane", CatalogPane).toggle_filter()
            except Exception:
                pass

    def action_explain(self) -> None:
        """Open the explain detail modal for the selected catalog slug."""
        if self._active_mode != 0:
            return
        try:
            entry = self.query_one("#catalog-pane", CatalogPane).selected_entry()
        except Exception:
            entry = None
        if entry is None:
            return
        screen = ExplainScreen(entry.slug)
        self.push_screen(screen)
        self.run_explain(screen, entry.slug)

    def action_primary_action(self) -> None:
        """⏎ — context-specific per mode."""
        if self._active_mode == 0:
            self._discover_primary()
        elif self._active_mode == 1:
            self._serve_primary()
        elif self._active_mode == 2:
            self._estate_primary()
        else:
            self.notify("Run actions land in Phase 4.", title="Validate", severity="information", timeout=3)

    def _discover_primary(self) -> None:
        """⏎ in Discover · Catalog: stage the selected slug and jump to Serve."""
        try:
            entry = self.query_one("#catalog-pane", CatalogPane).selected_entry()
        except Exception:
            entry = None
        if entry is None:
            return
        self._staged_entry = entry
        try:
            self.query_one("#serve-panel", ServePane).show_plan(entry)
        except Exception:
            pass
        self._switch_mode(1)

    def _serve_primary(self) -> None:
        """⏎ in Serve: open the reconcile-gated confirm modal for the staged slug."""
        if self._staged_entry is None:
            self.notify(
                "No slug staged — pick one in Discover · Catalog (⏎).",
                title="Serve",
                severity="warning",
                timeout=4,
            )
            return
        plan = self._data.serve(self._staged_entry.slug)  # gated, NOT --force
        self.push_screen(ConfirmActionScreen(plan))

    def _estate_primary(self) -> None:
        """⏎ in Estate · Orchestration: confirm-gated scene switch."""
        try:
            scene = self.query_one("#estate-orch-pane", EstateOrchPane).selected_scene()
        except Exception:
            scene = None
        if scene is None:
            return
        plan = self._data.scene_switch(scene.name)
        self.push_screen(ConfirmActionScreen(plan))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # ── Default-pin management (Discover · Catalog) ──────────────────────────────────

    def action_set_default(self) -> None:
        """[d] in Discover · Catalog: pin the selected slug as its model default.

        A ``.env`` write — no GPU contention — but still routed through the same
        ConfirmActionScreen → dispatch_action → execute_action gate so every
        write has one path.  The plan's ``requires_reconcile=False`` makes the
        gate report clear immediately."""
        if self._active_mode != 0:
            return
        entry = self._selected_catalog_entry()
        if entry is None:
            return
        plan = self._data.set_default(entry.slug)
        self.push_screen(ConfirmActionScreen(plan))

    def action_clear_default(self) -> None:
        """[D] in Discover · Catalog: clear the model default pin for the
        selected slug's model (gated path, .env write)."""
        if self._active_mode != 0:
            return
        entry = self._selected_catalog_entry()
        if entry is None:
            return
        plan = self._data.clear_default(entry.model)
        self.push_screen(ConfirmActionScreen(plan))

    def _selected_catalog_entry(self) -> Optional[CatalogEntry]:
        try:
            return self.query_one("#catalog-pane", CatalogPane).selected_entry()
        except Exception:
            return None

    # ── Containers (Estate · Containers) ──────────────────────────────────────────────

    def action_container_logs(self) -> None:
        """[l] in Estate · Containers: stream `docker logs` for the selected
        container into the drill Logs LivePane.  This is a READ — safe to run
        live (the conftest blocks an accidental write, not this read)."""
        if self._active_mode != 2:
            return
        con = self._selected_container()
        if con is None:
            self.notify("No container selected.", title="Logs", severity="warning", timeout=3)
            return
        try:
            tabs = self.query_one("#drill-tabs", TabbedContent)
            tabs.active = "drill-tab-logs"
        except Exception:
            pass
        self.stream_container_logs(con.name)

    def action_container_restart(self) -> None:
        """[s] in Estate · Containers: gated `docker restart <name>`."""
        self._container_write("restart")

    def action_container_stop(self) -> None:
        """[x] in Estate · Containers: gated `docker stop <name>`."""
        self._container_write("stop")

    def _container_write(self, op: str) -> None:
        if self._active_mode != 2:
            return
        con = self._selected_container()
        if con is None:
            self.notify(
                f"No container selected to {op}.", title="Containers", severity="warning", timeout=3
            )
            return
        plan = self._data.container_action(con.name, op)
        self.push_screen(ConfirmActionScreen(plan))

    def _selected_container(self) -> Optional[ContainerInfo]:
        try:
            return self.query_one(
                "#estate-containers-pane", EstateContainersPane
            ).selected_container()
        except Exception:
            return None

    @work(group="container-logs")
    async def stream_container_logs(self, name: str) -> None:
        """Read `docker logs --tail <N> <name>` and push lines into the drill
        Logs LivePane.  READ-only; goes through the injected read runner so
        tests stay subprocess-free."""
        try:
            live = self.query_one("#drill-logs", LivePane)
        except Exception:
            live = None
        if live is not None:
            live.clear_log()
            live.append_line(f"[dim]$ docker logs --tail 200 {name}[/dim]")
        res = await self._data.container_logs(name)
        if live is None:
            return
        if res.get("error"):
            live.append_line(f"[red]logs unavailable:[/red] {res['error']}")
            return
        for ln in res.get("lines", []):
            live.append_line(ln)

    # ── Estate stop-all (Estate · Orchestration) ──────────────────────────────────────

    def action_estate_off(self) -> None:
        """[o] in Estate · Orchestration: gated estate-down (stop all)."""
        if self._active_mode != 2:
            return
        plan = self._data.estate_down()
        self.push_screen(ConfirmActionScreen(plan))

    # ── Widget event handlers ─────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "byo-fit-btn":
            self._trigger_byo()
        elif bid == "serve-launch-btn":
            self._serve_primary()
        elif bid == "serve-force-btn":
            self._serve_force()
        elif bid == "serve-cancel-btn":
            self._staged_entry = None
            try:
                self.query_one("#serve-panel", ServePane).clear_plan()
            except Exception:
                pass

    def _serve_force(self) -> None:
        """Surfaced force override — opens the confirm modal with a forced plan
        (reason surfaced).  Still goes through the gate; the gate reports unsafe
        but a forced plan is permitted to proceed."""
        if self._staged_entry is None:
            self.notify("No slug staged.", title="Serve", severity="warning", timeout=3)
            return
        plan = self._data.serve(
            self._staged_entry.slug,
            force=True,
            force_reason="user override from Serve pane (F)",
        )
        self.push_screen(ConfirmActionScreen(plan))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "catalog-filter":
            try:
                self.query_one("#catalog-pane", CatalogPane).set_filter(event.value)
                self.query_one("#catalog-pane", CatalogPane).query_one(
                    "#catalog-table", DataTable
                ).focus()
            except Exception:
                pass
        elif event.input.id in ("byo-url-input", "byo-profile-input"):
            self._trigger_byo()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "catalog-filter":
            try:
                self.query_one("#catalog-pane", CatalogPane).set_filter(event.value)
            except Exception:
                pass

    def _trigger_byo(self) -> None:
        try:
            repo = self.query_one("#byo-url-input", Input).value.strip()
            profile = self.query_one("#byo-profile-input", Input).value.strip() or "vllm/dual"
        except Exception:
            return
        if not repo:
            self.notify("Enter an HF repo (org/Model).", title="BYO", severity="warning", timeout=3)
            return
        self.run_byo_check(repo, profile)
