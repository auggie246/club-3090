"""club3090 serve cockpit — main Textual application.

Phase 1: walking skeleton — visual mockups enriched for design review.
  - Full 4-mode nav (Discover / Serve / Estate / Validate)
  - Real Catalog DataTable populated from registry_variant_rows
  - All other panels are static illustrative mockups (hardcoded, clearly labelled)
  - No actions wired (Enter pops a "wired in Phase 3" toast)
"""

from __future__ import annotations

from pathlib import Path

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

from .registry import VariantRow, load_catalog_sync

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
        width: 72;
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
  [cyan]r[/cyan]  Refresh catalog (re-reads registry only)
  [cyan]⏎[/cyan]  Primary action (Phase 3 — shows "wired in Phase 3" notice)
  [cyan]?[/cyan]  This help
  [cyan]q[/cyan]  Quit

[bold]Phase 1 scope[/bold]

  This is the walking skeleton.  All navigation nodes are present and
  render something.  Only the Discover → Catalog tab is wired to real
  data (registry_variant_rows).  Serve / Estate / Validate and all
  action bindings are stubbed — they will be wired in Phase 3.

  All GPU numbers, container rows, bench rows, logs, and statuses in
  the non-Catalog panels are hardcoded illustrative mockup data.

[bold]Stub columns in Catalog[/bold]

  fit · TPS · 8pk · source  are placeholder glyphs (·/—).
  slug · engine · status · ctx  come from the live registry.

[bold]Status glyphs[/bold]

  ✅ production   ⚠️  caveats   🧪 experimental
  🐣 incubating  👁️  preview   ⏸️  upstream-gated   🗑️  deprecated
"""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("club3090 serve cockpit — Help", classes="help-title")
            yield Static(self.HELP_TEXT)

    def action_dismiss(self) -> None:
        self.app.pop_screen()


# ── Placeholder panels ────────────────────────────────────────────────────────


class PlaceholderPanel(Static):
    """A static placeholder panel with a title and description text."""

    def __init__(self, title: str, body: str, **kwargs):
        markup = f"[bold]{title}[/bold]\n\n{body}"
        super().__init__(markup, **kwargs)


# ── Discover tab content ──────────────────────────────────────────────────────


class CatalogPane(Container):
    """Catalog tab: DataTable populated from the live registry."""

    DEFAULT_CSS = """
    CatalogPane {
        height: 1fr;
    }
    CatalogPane #catalog-status {
        height: 1;
        color: $text-muted;
        padding: 0 1;
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
        table: DataTable = DataTable(id="catalog-table", zebra_stripes=True)
        table.cursor_type = "row"
        yield table
        yield Label(
            "[dim]\\[/] filter   \\[⏎] serve   \\[e] explain   \\[b] BYO fit-check[/dim]",
            id="catalog-hint",
        )

    def on_mount(self) -> None:
        table = self.query_one("#catalog-table", DataTable)
        table.add_columns("slug", "engine", "fit", "ctx", "TPS", "8pk", "status", "source")

    def populate(self, rows: list[VariantRow], error: str | None) -> None:
        """Fill the table with catalog rows (called from the worker result)."""
        status_label = self.query_one("#catalog-status", Label)
        table = self.query_one("#catalog-table", DataTable)
        table.clear()

        if error:
            status_label.update(f"[red]Catalog error:[/red] {error}")
            table.add_row("—", "—", "—", "—", "—", "—", "—", "—")
            return

        status_label.update(f"{len(rows)} variants loaded from registry")
        for r in rows:
            table.add_row(
                r.slug,
                r.engine,
                r.fit,           # stub: "·"
                r.ctx_label or "—",
                r.tps,           # stub: "—"
                r.quality_8pk,   # stub: "—"
                _status_glyph(r.status),
                r.source,        # stub: "·"
            )


class ByoPane(Container):
    """Bring-your-own tab: illustrative mockup of the HF fit-check flow."""

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
    ByoPane #byo-fit-btn {
        width: 18;
        margin-left: 1;
    }
    ByoPane #byo-example-card {
        border: solid $primary;
        padding: 1 2;
        margin-top: 1;
        height: auto;
    }
    ByoPane #byo-example-label {
        color: $text-muted;
        margin-bottom: 1;
    }
    ByoPane #byo-phase-note {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Bring-your-own HF model", id="byo-heading")
        with Horizontal(id="byo-input-row"):
            yield Input(placeholder="org/Model  (e.g. unsloth/Qwen3-27B-abliterated-GGUF)",
                        id="byo-url-input")
            yield Button("Fit-check", id="byo-fit-btn", variant="primary")
        yield Label("[dim](example result — Phase 3)[/dim]", id="byo-example-label")
        yield Static(
            "[bold]Route C verdict[/bold]  [dim](example)[/dim]\n\n"
            "  arch:  [cyan]Qwen3_5ForConditionalGeneration[/cyan]\n"
            "         → curated [green]'qwen3.6-27b'[/green]\n\n"
            "  [bold]Route C:[/bold] reuse compose + swap weights\n\n"
            "  • match [yellow]--quantization[/yellow] to weight dtype\n"
            "  • drop  [yellow]--speculative-config[/yellow]  (no MTP head in fine-tune)\n"
            "  • fits GPU0+GPU1 (~23.1 / 48.0 GiB) [dim](illustrative)[/dim]\n\n"
            "  [dim]Suggested slug: vllm/dual  ·  swap weights path → Phase 3[/dim]",
            id="byo-example-card",
        )
        yield Label(
            "[dim]Not wired — Phase 3[/dim]",
            id="byo-phase-note",
        )


# ── Serve pane ────────────────────────────────────────────────────────────────


class ServePane(Container):
    """Serve mode: illustrative plan-confirm box mockup (§7 #8)."""

    DEFAULT_CSS = """
    ServePane {
        height: 1fr;
        padding: 1 2;
    }
    ServePane #serve-heading {
        text-style: bold;
        margin-bottom: 1;
    }
    ServePane #serve-plan-box {
        border: solid $primary;
        padding: 1 2;
        height: auto;
        margin-bottom: 1;
    }
    ServePane #serve-plan-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    ServePane #serve-btn-row {
        height: 3;
        margin-top: 1;
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
    ServePane #serve-phase-note {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Serve", id="serve-heading")
        with Container(id="serve-plan-box"):
            yield Label("Launch plan  [dim](example)[/dim]", id="serve-plan-title")
            yield Static(
                "  [bold]Slug[/bold]      llamacpp/qwen27b-pi-reasoning-single\n"
                "  [bold]Engine[/bold]    llama.cpp\n"
                "  [bold]KV[/bold]        q4_0 / q4_0\n"
                "  [bold]Fit[/bold]       [green]● fits GPU0[/green]  ~22.7 / 24.0 GiB  [dim](illustrative)[/dim]\n"
                "  [bold]Max ctx[/bold]   188K\n"
                "  [bold]Tears down[/bold] gemma-int8  (GPU0)  [dim](illustrative)[/dim]",
                id="serve-plan-detail",
            )
        with Horizontal(id="serve-btn-row"):
            yield Button("⏎ Launch", id="serve-launch-btn", variant="success")
            yield Button("F Force", id="serve-force-btn", variant="warning")
            yield Button("Esc Cancel", id="serve-cancel-btn")
        yield Label(
            "[dim]Not wired — Phase 3  ·  ⏎ Launch / F Force / Esc Cancel[/dim]",
            id="serve-phase-note",
        )


# ── Estate pane content ───────────────────────────────────────────────────────


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
    EstateOrchPane #orch-phase-note {
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="orch-scroll"):
            # GPU 0 card
            with Container(classes="gpu-card", id="gpu0-card"):
                yield Label("GPU0  RTX 3090  [dim](mock — Phase 3)[/dim]", classes="gpu-card-title")
                yield Static(
                    "  [green]███████████████[/green][dim]░░░░░[/dim]  18.3 / 24.0 GiB · 71%\n"
                    "  312 / 370 W · 64°C",
                    id="gpu0-bar",
                )
            # GPU 1 card
            with Container(classes="gpu-card", id="gpu1-card"):
                yield Label("GPU1  RTX 3090  [dim](mock — Phase 3)[/dim]", classes="gpu-card-title")
                yield Static(
                    "  [yellow]██████████[/yellow][dim]██████████[/dim]  12.1 / 24.0 GiB · 45%\n"
                    "  198 / 370 W · 58°C",
                    id="gpu1-bar",
                )
            # Doctor
            yield Static(
                "[green]●[/green] serving · KV pool 61% · spec-dec firing (MTP n=2, 73% accept) · "
                "0 recent errors  [dim](mock — Phase 3)[/dim]",
                id="doctor-line",
            )
            # Scene table
            yield Label("Scenes  [dim](illustrative)[/dim]", id="scene-heading")
            scene_table: DataTable = DataTable(
                id="scene-table", zebra_stripes=True, show_cursor=False
            )
            yield scene_table
            # Services strip
            yield Label("Services  [dim](illustrative)[/dim]", id="services-heading")
            yield Static(
                "  OpenWebUI [green]●[/green]   LiteLLM [green]●[/green]   Qdrant [green]●[/green]"
                "   SearXNG [yellow]○[/yellow]   ComfyUI [dim]○[/dim]",
                id="services-strip",
            )
            yield Label(
                "[dim]Not wired — Phase 3[/dim]",
                id="orch-phase-note",
            )

    def on_mount(self) -> None:
        t = self.query_one("#scene-table", DataTable)
        t.add_columns("Scene", "Group", "GPUs", "Services")
        t.add_row("[bold cyan]27b[/bold cyan]", "serving", "0,1", "vllm-qwen36-27b")
        t.add_row("gemma-int8", "serving", "0,1", "vllm-gemma4-31b")
        t.add_row("deckard", "serving", "0", "llamacpp-deckard")
        t.add_row("image-studio", "studio", "0,1", "comfyui · webui · gemma-12b")
        t.add_row("video-studio", "studio", "0,1", "comfyui · director · gallery")


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
        padding: 1;
        color: $text;
    }
    EstateContainersPane #drill-stats {
        padding: 1;
        color: $text;
    }
    EstateContainersPane #drill-config {
        padding: 1;
        color: $text-muted;
    }
    EstateContainersPane #containers-phase-note {
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label(
            "Containers  [dim](mock — Phase 3)[/dim]",
            id="containers-heading",
        )
        ct: DataTable = DataTable(
            id="containers-table", zebra_stripes=True, show_cursor=True
        )
        ct.cursor_type = "row"
        yield ct
        with TabbedContent(id="drill-tabs"):
            with TabPane("Logs", id="drill-tab-logs"):
                yield Static(
                    "[dim]2026-06-18 14:23:01[/dim] INFO  Starting vllm worker on GPU0\n"
                    "[dim]2026-06-18 14:23:04[/dim] INFO  Model weights loaded (22.7 GiB)\n"
                    "[dim]2026-06-18 14:23:07[/dim] INFO  KV cache allocated 61% (int8_per_token_head)\n"
                    "[dim]2026-06-18 14:23:08[/dim] INFO  MTP drafter attached (n=2)\n"
                    "[dim]2026-06-18 14:23:09[/dim] INFO  Server started on :8010\n"
                    "[dim]2026-06-18 14:23:11[/dim] INFO  First request served (TTFT 0.21s)\n"
                    "[dim](illustrative log lines — Phase 3)[/dim]",
                    id="drill-logs",
                )
            with TabPane("Stats", id="drill-tab-stats"):
                yield Static(
                    "  CPU    [green]███[/green][dim]░░░░░░░░░░░░░░░░░[/dim]  14 %\n"
                    "  MEM    [green]████████████[/green][dim]████████[/dim]  61 %\n"
                    "  GPU0   [green]███████████████[/green][dim]░░░░░[/dim]  71 %  18.3 GiB\n"
                    "  GPU1   [yellow]██████████[/yellow][dim]██████████[/dim]  45 %  12.1 GiB\n"
                    "  [dim](illustrative — Phase 3)[/dim]",
                    id="drill-stats",
                )
            with TabPane("Config", id="drill-tab-config"):
                yield Static(
                    "  image=vllm/vllm-openai:v0.22.0\n"
                    "  --model /mnt/models/huggingface/qwen3.6-27b/\n"
                    "  --tensor-parallel-size 2\n"
                    "  --max-model-len 295000\n"
                    "  --kv-cache-dtype int8_per_token_head\n"
                    "  --speculative-config '{\"method\":\"mtp\",\"num_speculative_tokens\":2}'\n"
                    "  [dim](illustrative — Phase 3)[/dim]",
                    id="drill-config",
                )
        yield Label(
            "[dim]Not wired — Phase 3[/dim]",
            id="containers-phase-note",
        )

    def on_mount(self) -> None:
        t = self.query_one("#containers-table", DataTable)
        t.add_columns("Name", "Kind", "Status", "Uptime", "Restarts", "Ports", "GPU", "Slug")
        t.add_row(
            "[bold]vllm-qwen36-27b[/bold]", "engine",
            "[green]running[/green]", "2h14m", "0", "8010", "0,1", "vllm/dual",
        )
        t.add_row(
            "open-webui", "service",
            "[green]running[/green]", "6d", "1", "3000", "—", "—",
        )
        t.add_row(
            "litellm-proxy", "service",
            "[green]running[/green]", "6d", "0", "4000", "—", "—",
        )
        t.add_row(
            "qdrant", "service",
            "[green]running[/green]", "6d", "0", "6333", "—", "—",
        )


# ── Validate pane content ─────────────────────────────────────────────────────


class ValidateRunPane(Container):
    """Validate / Run tab: ladder steps + extra tools + output area."""

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
            yield Label(
                "Run  [dim](illustrative — Phase 4)[/dim]",
                id="run-heading",
            )
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
            yield Label(
                "[dim]Not wired — Phase 4[/dim]",
                id="run-phase-note",
            )


class ValidateDoctorPane(Container):
    """Validate / Doctor tab: health/estate/profile cards."""

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
        yield Label(
            "Doctor  [dim](illustrative — Phase 4)[/dim]",
            id="doctor-heading",
        )
        with Container(classes="doctor-card", id="doctor-card-health"):
            yield Label("health.sh", classes="doctor-card-title")
            yield Static(
                "[green]✓[/green]  serving  ·  KV pool 61%  ·  spec-dec firing (MTP n=2, 73% accept)\n"
                "[green]✓[/green]  0 recent errors  ·  TTFT p50 0.22s  ·  decode p50 178 TPS\n"
                "[dim](mock — Phase 3)[/dim]",
            )
        with Container(classes="doctor-card", id="doctor-card-estate"):
            yield Label("diagnose-estate", classes="doctor-card-title")
            yield Static(
                "[green]✓[/green]  estate plan coherent  ·  no VRAM over-commit\n"
                "[yellow]⚠[/yellow]  gemma-int8 on GPU0 not in active scene — orphan risk\n"
                "[dim](mock — Phase 3)[/dim]",
            )
        with Container(classes="doctor-card", id="doctor-card-profile"):
            yield Label("diagnose-profile  [dim]vllm/dual[/dim]", classes="doctor-card-title")
            yield Static(
                "[green]✓[/green]  engine pin valid (v0.22.0)  ·  patches apply clean\n"
                "[green]✓[/green]  kv-calc fits (22.7 / 24.0 GiB)  ·  compose mounts resolve\n"
                "[dim](mock — Phase 3)[/dim]",
            )
        yield Label(
            "[dim]Not wired — Phase 4[/dim]",
            id="doctor-phase-note",
        )


class ValidateBenchmarksPane(Container):
    """Validate / Benchmarks tab: stub DataTable of measured results."""

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
        bt: DataTable = DataTable(
            id="bench-table", zebra_stripes=True, show_cursor=True
        )
        bt.cursor_type = "row"
        yield bt
        yield Label(
            "[dim]Not wired — Phase 4[/dim]",
            id="bench-phase-note",
        )

    def on_mount(self) -> None:
        t = self.query_one("#bench-table", DataTable)
        t.add_columns("Model", "Engine", "Topo", "TPS (n/c)", "ctx", "8pk")
        t.add_row(
            "qwen3.6-27b", "vllm", "dual",
            "[dim]174 / 42[/dim]", "295K", "[dim]109[/dim]",
        )
        t.add_row(
            "qwen3.6-27b", "beellama", "dual",
            "[dim]155 / 38[/dim]", "102K", "[dim]107[/dim]",
        )
        t.add_row(
            "gemma-4-31b", "vllm", "dual",
            "[dim]112 / 29[/dim]", "192K", "[dim]103[/dim]",
        )
        t.add_row(
            "qwen3.6-35b-a3b", "vllm", "dual",
            "[dim]178 / 44[/dim]", "262K", "[dim]90[/dim]",
        )


class ValidateEvidencePane(Container):
    """Validate / Evidence tab: list of past-run tags."""

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
        yield Label(
            "Evidence  [dim](illustrative — Phase 4)[/dim]",
            id="evidence-heading",
        )
        yield Static(
            "[bold]Past runs[/bold]  [dim](example tags)[/dim]\n\n"
            "  results/rebench/[cyan]vllm-dual-20260618[/cyan]  ·  2026-06-18  ·  [green]PASS[/green]  ·  [dim][view report][/dim]\n"
            "  results/rebench/[cyan]vllm-dual-20260615[/cyan]  ·  2026-06-15  ·  [green]PASS[/green]  ·  [dim][view report][/dim]\n"
            "  results/rebench/[cyan]gemma-int8-20260614[/cyan] ·  2026-06-14  ·  [green]PASS[/green]  ·  [dim][view report][/dim]\n"
            "  results/rebench/[cyan]qwen35b-a3b-20260612[/cyan] · 2026-06-12  ·  [yellow]WARN[/yellow]  ·  [dim][view report][/dim]\n"
            "  [dim](illustrative — Phase 4)[/dim]",
            id="evidence-list",
        )
        yield Label(
            "[dim]Not wired — Phase 4[/dim]",
            id="evidence-phase-note",
        )


# ── Mode switcher (left rail) ─────────────────────────────────────────────────


MODES = [
    ("Discover", "1"),
    ("Serve", "2"),
    ("Estate", "3"),
    ("Validate", "4"),
]

# Per-mode primary action (what ⏎ will do in Phase 3), by mode index.
PRIMARY_ACTIONS = ["Serve", "Launch", "Switch scene", "Run"]
PRIMARY_ACTION_TOASTS = [
    "Serve the selected slug",
    "Launch via switch.sh <slug>",
    "Switch scene / inspect container",
    "Run the selected check",
]


class RailStatus(Static):
    """Persistent left-rail status card — always visible under the mode list,
    mirroring c3t's TargetPane. Phase-1 content is illustrative mock data
    (wired to detect.py + health.sh in Phase 3)."""

    STATUS_MARKUP = (
        "[bold]Estate[/bold]  [dim](mock)[/dim]\n"
        "\n"
        "[green]███████[/green][dim]░░░[/dim] GPU0  18/24G\n"
        "[yellow]████[/yellow][dim]██████[/dim] GPU1  12/24G\n"
        "\n"
        "scene   [cyan]27b[/cyan] [green]●[/green]\n"
        "model   qwen3.6-27b\n"
        "power   312 / 370 W\n"
        "\n"
        "[green]●[/green] serving · MTP n=2\n"
        "KV pool 61% · 0 errors\n"
        "\n"
        "[dim]live in Phase 3[/dim]"
    )

    def __init__(self, **kwargs):
        super().__init__(self.STATUS_MARKUP, **kwargs)


class ModeSwitcher(Static):
    """Left-rail mode selector.  Purely cosmetic in Phase 1 — navigation is
    driven by CockpitApp._active_mode and the 1–4 digit bindings."""

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
        # Per-mode primary-action hint — always shows what ⏎ does on this screen.
        yield Label(f"⏎ {PRIMARY_ACTIONS[0]}", id="mode-action-hint",
                    classes="mode-action-hint")

    def set_active(self, index: int) -> None:
        """Update the visual highlight for the active mode."""
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


# ── Main application ──────────────────────────────────────────────────────────


class CockpitApp(App):
    """club3090 serve cockpit — Phase 1 walking skeleton."""

    TITLE = "club3090 cockpit"
    SUB_TITLE = "Phase 1 · walking skeleton"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("question_mark", "help", "Help", show=True),
        Binding("r", "refresh_catalog", "Refresh", show=True),
        Binding("1", "mode_discover", "Discover", show=True),
        Binding("2", "mode_serve", "Serve", show=True),
        Binding("3", "mode_estate", "Estate", show=True),
        Binding("4", "mode_validate", "Validate", show=True),
        Binding("enter", "primary_action", "Select", show=True),
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
    #rail-status .rail-status-title {
        text-style: bold;
        color: $accent;
    }
    #content-area {
        width: 1fr;
        height: 1fr;
    }
    /* Mode content panels — only one visible at a time */
    .mode-panel {
        width: 1fr;
        height: 1fr;
        display: none;
    }
    .mode-panel.active {
        display: block;
    }
    /* Placeholder panel padding */
    PlaceholderPanel {
        padding: 1 2;
        color: $text-muted;
    }
    """

    def __init__(self, repo_root: Path, **kwargs):
        super().__init__(**kwargs)
        self._repo_root = repo_root
        self._active_mode = 0  # 0=Discover 1=Serve 2=Estate 3=Validate

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

    # ── Mount / startup ───────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._load_catalog()

    @work(thread=True)
    def _load_catalog(self) -> None:
        """Load the catalog in a background thread (the only script call)."""
        rows, error = load_catalog_sync(self._repo_root)
        self.call_from_thread(self._apply_catalog, rows, error)

    def _apply_catalog(self, rows: list[VariantRow], error: str | None) -> None:
        try:
            pane = self.query_one("#catalog-pane", CatalogPane)
            pane.populate(rows, error)
        except Exception:
            pass

    # ── Mode switching ────────────────────────────────────────────────────────

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

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_mode_discover(self) -> None:
        self._switch_mode(0)

    def action_mode_serve(self) -> None:
        self._switch_mode(1)

    def action_mode_estate(self) -> None:
        self._switch_mode(2)

    def action_mode_validate(self) -> None:
        self._switch_mode(3)

    def action_refresh_catalog(self) -> None:
        """Re-read the catalog (the only real data source in Phase 1)."""
        try:
            pane = self.query_one("#catalog-pane", CatalogPane)
            pane.query_one("#catalog-status", Label).update("Refreshing catalog…")
        except Exception:
            pass
        self._load_catalog()

    def action_primary_action(self) -> None:
        """⏎ primary action — context-specific per mode; no-op in Phase 1
        (every row/scene/container action is wired in Phase 3)."""
        idx = self._active_mode if 0 <= self._active_mode < len(PRIMARY_ACTION_TOASTS) else 0
        self.notify(
            f"⏎ would {PRIMARY_ACTION_TOASTS[idx]} — wired in Phase 3.",
            title=f"Phase 3 · {PRIMARY_ACTIONS[idx]}",
            severity="information",
            timeout=4,
        )

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
