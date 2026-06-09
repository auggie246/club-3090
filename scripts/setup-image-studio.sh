#!/usr/bin/env bash
# One-shot setup for the club-3090 IMAGE-STUDIO bundle:
#   ComfyUI (Ideogram-4 image gen) + OpenWebUI front-end + gemma-4-12b chat,
#   coexisting on a 2-GPU box (ComfyUI→GPU0, chat→GPU1).
#
#   bash scripts/setup-image-studio.sh            # build + download + bring up
#   SKIP_DOWNLOAD=1 bash scripts/setup-image-studio.sh   # skip the 27 GB weight pull
#   SKIP_BUILD=1    bash scripts/setup-image-studio.sh   # skip the ComfyUI image build
#
# Idempotent: re-running re-pulls/rebuilds only what changed. Brings the stack up
# via `gpu-mode image-studio`.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMFYUI_DIR="$REPO_DIR/services/comfyui"
LANIP="${LANIP:-$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^192\.168\.' | head -1)}"
LANIP="${LANIP:-<host-ip>}"

say()  { echo -e "\033[0;36m$*\033[0m"; }
warn() { echo -e "\033[1;33m$*\033[0m"; }
ok()   { echo -e "\033[0;32m$*\033[0m"; }

# --- 0. Preflight -----------------------------------------------------------
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found." >&2; exit 1; }
NGPU=$(nvidia-smi -L 2>/dev/null | wc -l)
say "═══ club-3090 image-studio setup ═══"
echo "  repo:  $REPO_DIR"
echo "  GPUs:  $NGPU"
if [ "${NGPU:-0}" -lt 2 ]; then
    warn "  ⚠ <2 GPUs — image gen and a local chat model can't run at once (GPU-mutex)."
    warn "    The bundle will run ComfyUI image gen; for chat use 'gpu-mode chat' (LiteLLM)"
    warn "    or run gemma-4-12b only while ComfyUI is down. Continuing…"
fi

# --- 1. Build the ComfyUI image (clones ComfyUI HEAD + nodes on first boot) --
if [ -z "${SKIP_BUILD:-}" ]; then
    say "── [1/3] Building ComfyUI image (comfyui-local:latest) ──"
    (cd "$COMFYUI_DIR" && sudo docker compose build)
else
    echo "  (SKIP_BUILD set — skipping image build)"
fi

# --- 2. Download the Ideogram-4 model set (~27 GB) --------------------------
if [ -z "${SKIP_DOWNLOAD:-}" ]; then
    say "── [2/3] Downloading Ideogram-4 model set (~27 GB; skip with SKIP_DOWNLOAD=1) ──"
    bash "$COMFYUI_DIR/download_ideogram4.sh"
else
    echo "  (SKIP_DOWNLOAD set — skipping weight download)"
fi

# --- 3. Bring the stack up via gpu-mode -------------------------------------
say "── [3/3] Starting the bundle (gpu-mode image-studio) ──"
bash "$REPO_DIR/scripts/gpu-mode.sh" image-studio

echo ""
ok "═══ Image-studio ready ═══"
echo "  Open WebUI:  http://$LANIP:8080   ← chat + 🖼️ image button (start here)"
echo "  ComfyUI:     http://$LANIP:8188   ← full node-graph control"
echo ""
warn "First image after a cold ComfyUI is slow (~2 min, loads ~20 GB); warm gens ~70 s."
warn "Image button not showing in OpenWebUI? You're on a PRE-EXISTING data volume — OpenWebUI"
warn "only reads the image-gen env on a FRESH volume. Set it in Admin → Settings → Images"
warn "(Engine=ComfyUI, Base URL=http://host.docker.internal:8188), or recreate the volume."
