#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

assert_contains() {
  local haystack="$1"
  local needle="$2"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "ASSERTION FAILED: expected output to contain: $needle" >&2
    echo "--- output ---" >&2
    echo "$haystack" >&2
    exit 1
  fi
}

export CLUB3090_FAKE_GPUS='0:RTX_3090:24576:8.6,1:RTX_3090:24576:8.6'

GOOD="${TMP_DIR}/estate-good.yml"
cat > "$GOOD" <<'YAML'
schema_version: 1
created: 2026-05-14T00:00:00Z
rig:
  hardware_id: rtx-3090
  gpu_count: 2
  nvlink_active: false
estate:
  - name: qwen-left
    compose: llamacpp/default
    gpus: [0]
    port: 8110
  - name: qwen-right
    compose: llamacpp/default
    gpus: [1]
    port: 8120
YAML

out="$(bash "${ROOT_DIR}/scripts/launch.sh" --validate-estate "$GOOD" 2>&1)"
assert_contains "$out" "Estate validation: PASS"
assert_contains "$out" "qwen-left: llamacpp/default GPUs=[0] port=8110"

out="$(bash "${ROOT_DIR}/scripts/diagnose-estate.sh" "$GOOD" 2>&1)"
assert_contains "$out" "[1/6] Estate file parses + schema_version supported"
assert_contains "$out" "[4/6] Estate cross-checks E1-E4"
assert_contains "$out" "Triage summary: GREEN"

out="$(python3 "${ROOT_DIR}/scripts/lib/profiles/estate_cli.py" report-state --file "$GOOD" 2>&1)"
assert_contains "$out" "## Profile state"
assert_contains "$out" "Active estate"
assert_contains "$out" "qwen-left: llamacpp/default, GPUs [0], port 8110"

GPU_COLLISION="${TMP_DIR}/estate-gpu-collision.yml"
cat > "$GPU_COLLISION" <<'YAML'
schema_version: 1
created: 2026-05-14T00:00:00Z
rig:
  hardware_id: rtx-3090
  gpu_count: 2
  nvlink_active: false
estate:
  - name: qwen-left
    compose: llamacpp/default
    gpus: [0]
    port: 8110
  - name: qwen-right
    compose: llamacpp/default
    gpus: [0]
    port: 8120
YAML

if out="$(bash "${ROOT_DIR}/scripts/launch.sh" --validate-estate "$GPU_COLLISION" 2>&1)"; then
  echo "ASSERTION FAILED: GPU collision estate unexpectedly passed" >&2
  echo "$out" >&2
  exit 1
fi
assert_contains "$out" "E1: GPU 0 claimed by qwen-left, qwen-right"

PORT_COLLISION="${TMP_DIR}/estate-port-collision.yml"
cat > "$PORT_COLLISION" <<'YAML'
schema_version: 1
created: 2026-05-14T00:00:00Z
rig:
  hardware_id: rtx-3090
  gpu_count: 2
  nvlink_active: false
estate:
  - name: qwen-left
    compose: llamacpp/default
    gpus: [0]
    port: 8110
  - name: qwen-right
    compose: llamacpp/default
    gpus: [1]
    port: 8110
YAML

if out="$(bash "${ROOT_DIR}/scripts/diagnose-estate.sh" "$PORT_COLLISION" 2>&1)"; then
  echo "ASSERTION FAILED: port collision estate unexpectedly passed" >&2
  echo "$out" >&2
  exit 1
fi
assert_contains "$out" "E4: port 8110 claimed by qwen-left, qwen-right"

MISSING_COMPOSE="${TMP_DIR}/estate-missing-compose.yml"
cat > "$MISSING_COMPOSE" <<'YAML'
schema_version: 1
created: 2026-05-14T00:00:00Z
rig:
  hardware_id: rtx-3090
  gpu_count: 2
  nvlink_active: false
estate:
  - name: missing
    compose: vllm/not-real
    gpus: [0]
    port: 8110
YAML

if out="$(bash "${ROOT_DIR}/scripts/diagnose-estate.sh" "$MISSING_COMPOSE" 2>&1)"; then
  echo "ASSERTION FAILED: missing-compose estate unexpectedly passed" >&2
  echo "$out" >&2
  exit 1
fi
assert_contains "$out" "vllm/not-real missing from registry"

echo "test-diagnose-estate: ok"
