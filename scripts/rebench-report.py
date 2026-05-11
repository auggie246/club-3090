#!/usr/bin/env python3
"""rebench-report.py — synthesize REPORT.md from a results/rebench/<tag>/ dir.

Reads the raw logs and JSON artifacts that scripts/rebench-full.sh writes,
extracts the structured numbers, and produces a single REPORT.md at the top
of the tag dir. Re-runnable standalone — just pass the tag-dir path.

Usage:
    python3 scripts/rebench-report.py results/rebench/<tag>/
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


# --- file readers -----------------------------------------------------------

def read_file(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(errors="replace")
    except Exception:
        return ""


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(errors="replace"))
    except Exception:
        return None


# --- section: meta + config -------------------------------------------------

def parse_container_config(blob: dict | None) -> dict:
    """Pull the relevant bits from `docker inspect <container>`."""
    if not blob:
        return {}
    # docker inspect returns a list with one element
    if isinstance(blob, list) and blob:
        blob = blob[0]
    cfg = blob.get("Config", {}) or {}
    state = blob.get("State", {}) or {}
    mounts = blob.get("Mounts", [])
    cmd = cfg.get("Cmd") or []
    env = cfg.get("Env") or []

    # extract flag values from Cmd list
    def get_flag(name: str) -> str | None:
        try:
            i = cmd.index(name)
            return cmd[i + 1]
        except (ValueError, IndexError):
            return None

    # mounts that look like vendored patches
    patches = []
    for m in mounts:
        src = m.get("Source", "")
        if "/patches/" in src and m.get("Type") == "bind":
            # take the patch dir name
            parts = src.split("/patches/")
            if len(parts) > 1:
                name = parts[1].split("/")[0]
                if name not in patches:
                    patches.append(name)

    return {
        "image": cfg.get("Image"),
        "name": blob.get("Name", "").lstrip("/"),
        "status": state.get("Status"),
        "model": get_flag("--model") or "?",
        "served_model_name": get_flag("--served-model-name") or "?",
        "quantization": get_flag("--quantization") or "?",
        "dtype": get_flag("--dtype") or "?",
        "tensor_parallel_size": get_flag("--tensor-parallel-size") or "?",
        "max_model_len": get_flag("--max-model-len") or "?",
        "gpu_memory_utilization": get_flag("--gpu-memory-utilization") or "?",
        "max_num_seqs": get_flag("--max-num-seqs") or "?",
        "max_num_batched_tokens": get_flag("--max-num-batched-tokens") or "?",
        "kv_cache_dtype": get_flag("--kv-cache-dtype") or "?",
        "speculative_config": get_flag("--speculative-config") or "?",
        "patches": patches,
        "env": env,
    }


def parse_model_config_json(model_dir: Path) -> dict:
    """Read config.json from the served model dir to get quant metadata."""
    cfg_path = model_dir / "config.json"
    if not cfg_path.is_file():
        return {}
    blob = read_json(cfg_path)
    if not blob:
        return {}
    q = blob.get("quantization_config") or {}
    return {
        "model_type": blob.get("model_type"),
        "architectures": blob.get("architectures") or [],
        "quant_method": q.get("quant_method"),
        "bits": q.get("bits"),
        "group_size": q.get("group_size"),
    }


# --- section: vLLM boot log (KV pool + max concurrency) ---------------------

def parse_vllm_boot(boot_log: str) -> dict:
    """Pull KV cache size and Max concurrency lines."""
    out = {}
    m = re.search(r"GPU KV cache size: ([\d,]+) tokens", boot_log)
    if m:
        out["kv_cache_tokens"] = int(m.group(1).replace(",", ""))
    m = re.search(r"Maximum concurrency for ([\d,]+) tokens per request: ([\d.]+)x", boot_log)
    if m:
        out["max_concurrency_request_size"] = int(m.group(1).replace(",", ""))
        out["max_concurrency"] = float(m.group(2))
    m = re.search(r"Available KV cache memory: ([\d.]+) GiB", boot_log)
    if m:
        out["available_kv_cache_gib"] = float(m.group(1))
    m = re.search(r"Model loading took ([\d.]+) GiB memory", boot_log)
    if m:
        out["model_load_gib"] = float(m.group(1))
    return out


# --- section: bench.sh ------------------------------------------------------

def parse_bench(log: str) -> dict:
    """Extract narrative + code TPS summaries from bench.sh output."""
    out: dict[str, Any] = {}
    # narrative summary block
    for kind in ("narrative", "code"):
        m = re.search(
            rf"=== summary \[{kind}\] \(n=\d+\) ===\s*\n"
            rf"\s+wall_TPS\s+mean=\s*([\d.]+)\s+std=\s*([\d.]+)\s+CV=\s*([\d.]+)%.*\n"
            rf"\s+decode_TPS\s+mean=\s*([\d.]+)\s+std=\s*([\d.]+)\s+CV=\s*([\d.]+)%.*\n"
            rf"\s+TTFT\s+mean=\s*([\d.]+)ms",
            log,
        )
        if m:
            out[kind] = {
                "wall_tps_mean": float(m.group(1)),
                "wall_tps_cv": float(m.group(3)),
                "decode_tps_mean": float(m.group(4)),
                "decode_tps_cv": float(m.group(6)),
                "ttft_ms_mean": float(m.group(7)),
            }
    # GPU state at end
    gpu_block = re.search(r"=== GPU state ===\s*\n((?:\d.+\n){1,4})", log)
    if gpu_block:
        gpu_lines = gpu_block.group(1).strip().split("\n")
        out["gpu_state"] = []
        for line in gpu_lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                out["gpu_state"].append({
                    "idx": parts[0],
                    "util_pct": parts[1],
                    "mem_used": parts[2],
                    "mem_total": parts[3],
                    "power_w": parts[4],
                    "temp_c": parts[5],
                })
    # MTP last metrics line
    mtp_lines = re.findall(
        r"Mean acceptance length: ([\d.]+), .* "
        r"Per-position acceptance rate: ([^,]+), .* "
        r"Avg Draft acceptance rate: ([\d.]+)%",
        log,
    )
    if mtp_lines:
        last = mtp_lines[-1]
        out["mtp"] = {
            "mean_accept_length": float(last[0]),
            "per_position": last[1].strip(),
            "avg_accept_rate": float(last[2]),
        }
    return out


# --- section: verify-stress -------------------------------------------------

def parse_verify_stress(log: str) -> dict:
    """Extract the 7-check verdict table."""
    checks = []
    # match lines like "[1/7] description ..." followed by ✓ or ✗
    for m in re.finditer(r"\[(\d+/\d+)\] (.+?) \.\.\.\n(.*?)(?=\n\[\d+/\d+\]|\nAll stress|\Z)", log, re.DOTALL):
        idx = m.group(1)
        desc = m.group(2).strip()
        body = m.group(3)
        verdict = "PASS" if "✓" in body else ("FAIL" if "✗" in body else "?")
        checks.append({"idx": idx, "desc": desc, "verdict": verdict})
    overall = "PASS" if "All stress / boundary checks passed" in log else (
        "FAIL" if any(c["verdict"] == "FAIL" for c in checks) else "?"
    )
    return {"checks": checks, "overall": overall}


# --- section: quality-full --------------------------------------------------

def parse_quality(blob: dict | None) -> dict:
    if not blob:
        return {}
    packs = []
    failure_examples: dict[str, list[str]] = defaultdict(list)
    for p in blob.get("packs", []):
        if p.get("pack_id") == "aider-polyglot-30":
            continue  # rendered separately
        packs.append({
            "pack_id": p.get("pack_id"),
            "passed": p.get("passed"),
            "total": p.get("total"),
            "score_pct": round(100 * (p.get("score") or 0)),
            "p50_latency_s": p.get("p50_latency"),
            "p95_latency_s": p.get("p95_latency"),
            "status": p.get("status"),
        })
        # gather top-3 failures per pack for the report appendix
        for s in (p.get("scenarios") or []):
            if not s.get("passed", True):
                pack = p.get("pack_id", "?")
                sid = s.get("id") or (s.get("raw_scenario") or {}).get("id", "?")
                fail = s.get("failure_mode") or "?"
                detail = (s.get("detail") or "")[:140]
                if len(failure_examples[pack]) < 3:
                    failure_examples[pack].append(f"{sid}: {fail} — {detail}")
    total_passed = sum(p["passed"] or 0 for p in packs)
    total_total = sum(p["total"] or 0 for p in packs)
    return {
        "packs": packs,
        "total_passed": total_passed,
        "total_total": total_total,
        "total_pct": round(100 * total_passed / max(total_total, 1)),
        "failure_examples": dict(failure_examples),
    }


# --- section: soak ----------------------------------------------------------

def parse_soak(log: str) -> dict:
    out = {}
    for key in ("verdict", "max_growth_mib", "errors", "silent_empty",
                "p50_decode_tps", "p95_ttft_ms", "tps_retention", "boot_vram_mib"):
        m = re.search(rf"\b{key}\b\s+(\S.+)$", log, re.MULTILINE)
        if m:
            out[key] = m.group(1).strip()
    return out


# --- section: aider-polyglot ------------------------------------------------

def parse_aider(blob: dict | None) -> dict:
    if not blob:
        return {}
    for p in blob.get("packs", []):
        if p.get("pack_id") != "aider-polyglot-30":
            continue
        s = (p.get("scenarios") or [{}])[0]
        per_ex = s.get("verifier_trace", {}).get("upstream_per_exercise") or \
                 s.get("verifier_trace", {}).get("per_exercise") or []
        per_lang = defaultdict(lambda: [0, 0])
        for e in per_ex:
            lang = (e.get("language") or "?").lower()
            per_lang[lang][1] += 1
            if e.get("passed") or e.get("status") == "pass":
                per_lang[lang][0] += 1
        return {
            "pass_rate": s.get("pass_rate"),
            "passed_count": s.get("passed_count"),
            "total_count": s.get("total_count"),
            "wall_seconds": s.get("latency_seconds"),
            "per_language": dict(per_lang),
        }
    return {}


# --- markdown rendering -----------------------------------------------------

def render(report: dict) -> str:
    lines = []
    lines.append(f"# Rebench report — {report.get('tag', '?')}")
    lines.append("")
    lines.append(f"_Generated by `scripts/rebench-report.py` from `{report.get('tag_dir')}`_")
    lines.append("")

    # --- meta ---
    meta = report.get("meta", {})
    lines.append("## Meta")
    lines.append("")
    lines.append(f"- **Tag:** `{report.get('tag', '?')}`")
    lines.append(f"- **Date:** {report.get('date', '?')}")
    lines.append(f"- **Repo commit:** `{report.get('commit_sha', '?')}`")
    if meta.get("model_config"):
        mc = meta["model_config"]
        lines.append(f"- **Model arch:** {mc.get('model_type', '?')} ({(mc.get('architectures') or ['?'])[0]})")
        lines.append(f"- **Quant:** {mc.get('quant_method', '?')} {mc.get('bits', '?')}-bit, group_size {mc.get('group_size', '?')}")
    if meta.get("container"):
        c = meta["container"]
        lines.append(f"- **Served as:** `{c.get('served_model_name')}` from `{c.get('model')}`")
        lines.append(f"- **vLLM image:** `{c.get('image')}`")
        lines.append(f"- **Container:** `{c.get('name')}`")
    lines.append("")

    # --- config ---
    lines.append("## Config")
    lines.append("")
    if meta.get("container"):
        c = meta["container"]
        lines.append("| Setting | Value |")
        lines.append("|---|---|")
        lines.append(f"| `--tensor-parallel-size` | {c.get('tensor_parallel_size')} |")
        lines.append(f"| `--max-model-len` | {c.get('max_model_len')} |")
        lines.append(f"| `--gpu-memory-utilization` | {c.get('gpu_memory_utilization')} |")
        lines.append(f"| `--max-num-seqs` | {c.get('max_num_seqs')} |")
        lines.append(f"| `--max-num-batched-tokens` | {c.get('max_num_batched_tokens')} |")
        lines.append(f"| `--kv-cache-dtype` | `{c.get('kv_cache_dtype')}` |")
        lines.append(f"| `--dtype` | `{c.get('dtype')}` |")
        lines.append(f"| `--quantization` | `{c.get('quantization')}` |")
        lines.append(f"| `--speculative-config` | `{c.get('speculative_config')}` |")
        patches = c.get("patches") or []
        lines.append(f"| Patches mounted | {', '.join(f'`{p}`' for p in patches) if patches else 'none'} |")
        # Genesis detection
        genesis_envs = [e for e in (c.get("env") or []) if e.startswith("GENESIS_")]
        lines.append(f"| Genesis | {'on (' + str(len(genesis_envs)) + ' GENESIS_* env vars)' if genesis_envs else 'none'} |")
    lines.append("")

    # --- bench performance ---
    bench = report.get("bench", {})
    lines.append("## Performance — `bench.sh`")
    lines.append("")
    if bench.get("narrative") or bench.get("code"):
        lines.append("| Bench | wall TPS | decode TPS | TTFT | CV (wall/decode) |")
        lines.append("|---|---:|---:|---:|---:|")
        for kind in ("narrative", "code"):
            b = bench.get(kind)
            if b:
                lines.append(
                    f"| {kind} | {b['wall_tps_mean']:.2f} | **{b['decode_tps_mean']:.2f}** | "
                    f"{b['ttft_ms_mean']:.0f} ms | {b['wall_tps_cv']:.1f}% / {b['decode_tps_cv']:.1f}% |"
                )
        if bench.get("mtp"):
            m = bench["mtp"]
            lines.append("")
            lines.append(f"**MTP (warm, last metric):** mean accept length {m['mean_accept_length']:.2f}, "
                         f"avg accept rate {m['avg_accept_rate']:.1f}%, per-position {m['per_position']}")
        if bench.get("gpu_state"):
            lines.append("")
            lines.append("**GPU state at bench end:**")
            lines.append("")
            lines.append("| GPU | Util | Mem used / total | Power | Temp |")
            lines.append("|---|---|---|---|---|")
            for g in bench["gpu_state"]:
                lines.append(f"| {g['idx']} | {g['util_pct']} | {g['mem_used']} / {g['mem_total']} | {g['power_w']} | {g['temp_c']} |")
    else:
        lines.append("_(bench artifacts missing)_")
    lines.append("")

    # --- concurrency + VRAM ---
    boot = report.get("vllm_boot", {})
    lines.append("## Concurrency + VRAM")
    lines.append("")
    if boot:
        kv_tokens = boot.get("kv_cache_tokens")
        max_conc = boot.get("max_concurrency")
        req_size = boot.get("max_concurrency_request_size")
        avail = boot.get("available_kv_cache_gib")
        model_load = boot.get("model_load_gib")
        lines.append("| Metric | Value |")
        lines.append("|---|---:|")
        if model_load:
            lines.append(f"| Model load footprint | {model_load:.2f} GiB |")
        if avail:
            lines.append(f"| Available KV cache memory (per card, post-profiling) | {avail:.2f} GiB |")
        if kv_tokens:
            lines.append(f"| GPU KV cache size | **{kv_tokens:,} tokens** |")
        if max_conc and req_size:
            lines.append(f"| Max concurrency @ {req_size:,} tokens/req | **{max_conc:.2f}×** |")
            if req_size > 1:
                for ctx in (100_000, 32_000):
                    practical = max_conc * (req_size / ctx)
                    lines.append(f"| Practical concurrency @ {ctx:,} tokens/req | ~{practical:.1f}× |")
    else:
        lines.append("_(no vLLM boot log captured)_")
    lines.append("")

    # --- verify-stress ---
    stress = report.get("verify_stress", {})
    lines.append("## Verify-stress — 7-check boundary matrix")
    lines.append("")
    if stress.get("checks"):
        lines.append(f"**Overall:** {stress.get('overall')}")
        lines.append("")
        lines.append("| # | Check | Verdict |")
        lines.append("|---|---|---|")
        for c in stress["checks"]:
            lines.append(f"| {c['idx']} | {c['desc']} | {c['verdict']} |")
    else:
        lines.append("_(verify-stress log missing or unparsed)_")
    lines.append("")

    # --- quality ---
    quality = report.get("quality", {})
    lines.append("## Quality — `quality-test.sh --full`")
    lines.append("")
    if quality.get("packs"):
        lines.append("| Pack | Pass / Total | Score | p50 latency | p95 latency |")
        lines.append("|---|---:|---:|---:|---:|")
        for p in quality["packs"]:
            p50 = f"{p['p50_latency_s']:.2f}s" if p.get("p50_latency_s") else "—"
            p95 = f"{p['p95_latency_s']:.2f}s" if p.get("p95_latency_s") else "—"
            lines.append(f"| {p['pack_id']} | {p['passed']} / {p['total']} | {p['score_pct']}% | {p50} | {p95} |")
        lines.append(f"| **TOTAL** | **{quality['total_passed']} / {quality['total_total']}** | **{quality['total_pct']}%** | | |")
        if quality.get("failure_examples"):
            lines.append("")
            lines.append("**Failure examples (top 3 per pack):**")
            for pack, examples in sorted(quality["failure_examples"].items()):
                lines.append(f"- `{pack}`:")
                for ex in examples:
                    lines.append(f"  - {ex}")
    else:
        lines.append("_(quality JSON missing or unparsed)_")
    lines.append("")

    # --- soak ---
    soak = report.get("soak", {})
    lines.append("## Soak — `soak-test.sh`")
    lines.append("")
    if soak:
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        for k in ("verdict", "silent_empty", "p50_decode_tps", "p95_ttft_ms",
                  "tps_retention", "max_growth_mib", "errors", "boot_vram_mib"):
            if k in soak:
                lines.append(f"| `{k}` | {soak[k]} |")
    else:
        lines.append("_(soak log missing or unparsed)_")
    lines.append("")

    # --- aider-polyglot ---
    aider = report.get("aider", {})
    lines.append("## Aider Polyglot 30 — per-language breakdown")
    lines.append("")
    if aider.get("per_language"):
        lines.append(f"**Total:** {aider.get('passed_count', '?')} / {aider.get('total_count', '?')} "
                     f"({100 * (aider.get('pass_rate') or 0):.1f}%) · "
                     f"wall {aider.get('wall_seconds', 0):.0f}s")
        lines.append("")
        lines.append("| Language | Pass / Total | Score |")
        lines.append("|---|---:|---:|")
        for lang, (passed, total) in sorted(aider["per_language"].items()):
            pct = round(100 * passed / total) if total else 0
            lines.append(f"| {lang} | {passed} / {total} | {pct}% |")
    else:
        lines.append("_(aider-polyglot artifacts missing or no per-exercise trace)_")
    lines.append("")

    # --- phase timings ---
    timings = report.get("timings", {})
    if timings:
        lines.append("## Phase timings")
        lines.append("")
        lines.append("| Phase | Duration |")
        lines.append("|---|---:|")
        total_s = 0
        for phase, secs in timings.items():
            if isinstance(secs, (int, float)) and secs > 0:
                m, s = divmod(int(secs), 60)
                lines.append(f"| {phase} | {m}m {s}s |")
                total_s += secs
        if total_s:
            tm, ts = divmod(int(total_s), 60)
            lines.append(f"| **Total** | **{tm}m {ts}s** |")
        lines.append("")

    return "\n".join(lines)


# --- main -------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("tag_dir", help="results/rebench/<tag>/ directory")
    args = p.parse_args(argv)

    tag_dir = Path(args.tag_dir).resolve()
    if not tag_dir.is_dir():
        print(f"✗ not a directory: {tag_dir}", file=sys.stderr)
        return 2

    bench_log = read_file(tag_dir / "bench.log")
    stress_log = read_file(tag_dir / "verify-stress.log")
    quality_blob = read_json(tag_dir / "quality-full.json")
    soak_log = read_file(tag_dir / "soak.log")
    aider_blob = read_json(tag_dir / "aider-polyglot.json")
    container_blob = read_json(tag_dir / "container-config.json")
    boot_log = read_file(tag_dir / "vllm-boot.log")
    timings_blob = read_json(tag_dir / "timings.json") or {}

    # try to find model dir for quant config
    model_config = {}
    if container_blob:
        cinfo = parse_container_config(container_blob)
        model_path = cinfo.get("model", "")
        if model_path and model_path.startswith("/root/.cache/huggingface/"):
            # rewrite to host path under /mnt/models/huggingface
            host_path = Path("/mnt/models/huggingface") / Path(model_path).name
            model_config = parse_model_config_json(host_path)

    report = {
        "tag": tag_dir.name,
        "tag_dir": str(tag_dir),
        "date": tag_dir.name.split("-", 1)[-1] if "-" in tag_dir.name else "?",
        "commit_sha": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=tag_dir.parent.parent.parent,
            capture_output=True, text=True,
        ).stdout.strip() or "?",
        "meta": {
            "container": parse_container_config(container_blob),
            "model_config": model_config,
        },
        "vllm_boot": parse_vllm_boot(boot_log),
        "bench": parse_bench(bench_log),
        "verify_stress": parse_verify_stress(stress_log),
        "quality": parse_quality(quality_blob),
        "soak": parse_soak(soak_log),
        "aider": parse_aider(aider_blob),
        "timings": timings_blob,
    }

    out_path = tag_dir / "REPORT.md"
    out_path.write_text(render(report))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
