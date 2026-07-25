#!/usr/bin/env python3
"""
workflow_tester.py — ComfyUI Workflow Testing Framework

A systematic testing tool for ComfyUI workflows:
  1. plan   — Analyze a workflow, generate a smart test plan
  2. run    — Execute the plan against a ComfyUI server
  3. report — Generate HTML + CSV reports from results

Usage:
  python3 workflow_tester.py plan --workflow my_workflow.json
  python3 workflow_tester.py run --plan test_plan.json
  python3 workflow_tester.py report --results results.json
"""

from __future__ import annotations

import argparse
import copy
import csv
import itertools
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# ── ComfyUI skill scripts ──────────────────────────────────────────────────
_COMFY_SKILL_DIR = Path.home() / ".hermes/skills/creative/comfyui/scripts"
if str(_COMFY_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_COMFY_SKILL_DIR))

from _common import (  # noqa: E402
    DEFAULT_LOCAL_HOST, coerce_seed, emit_json, log, unwrap_workflow,
    looks_like_video_workflow, resolve_api_key,
)
from extract_schema import extract_schema  # noqa: E402
from run_workflow import ComfyRunner, download_outputs, inject_params  # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = PROJECT_DIR / "test_plan.schema.json"

# Parameters classified by their primary effect
QUALITY_PARAMS = {
    "cfg", "sampler_name", "scheduler", "denoise",
    "lora_strength", "lora_strength_clip", "controlnet_strength",
    "controlnet_start", "controlnet_end", "ipadapter_weight",
    "ipadapter_start", "ipadapter_end", "max_shift", "base_shift",
    "guidance", "shift", "motion_scale", "cfg_negative",
}
SPEED_PARAMS = {
    "steps", "width", "height", "batch_size", "start_at_step", "end_at_step",
    "upscale_width", "upscale_height", "scale_by",
}
BOTH_PARAMS = {"steps"}  # affects both quality and speed

# Smart defaults for common parameters
SMART_DEFAULTS: dict[str, list[Any]] = {
    "steps": [20, 30, 40],
    "cfg": [3.5, 5.0, 7.0, 9.0],
    "sampler_name": ["euler", "euler_ancestral", "dpmpp_2m", "dpmpp_sde"],
    "scheduler": ["normal", "karras", "sgm_uniform"],
    "denoise": [0.4, 0.6, 0.8, 1.0],
    "lora_strength": [0.5, 0.75, 1.0],
    "lora_strength_clip": [0.5, 0.75, 1.0],
    "controlnet_strength": [0.5, 0.75, 1.0],
}

# Parameters that should NOT be swept (keep fixed)
NEVER_SWEEP = {
    "prompt", "negative_prompt", "prompt_l", "refiner_prompt",
    "seed",  # fixed seed for reproducible comparisons
    "image", "mask_image", "ckpt_name", "vae_name", "clip_name",
    "unet_name", "diffusion_model_name", "controlnet_name",
    "lora_name", "upscale_model_name", "filename_prefix",
    "width", "height", "batch_size",  # keep dimensions fixed
}


# ═══════════════════════════════════════════════════════════════════════════
# PLAN phase
# ═══════════════════════════════════════════════════════════════════════════

def classify_metric(param_name: str) -> str:
    """Classify a parameter's primary effect."""
    if param_name in BOTH_PARAMS:
        return "both"
    if param_name in QUALITY_PARAMS:
        return "quality"
    if param_name in SPEED_PARAMS:
        return "speed"
    return "both"  # default: could be either


def suggest_values(param_name: str, param_type: str, current_value: Any) -> list[Any] | None:
    """Generate smart test values for a parameter. Returns None if shouldn't sweep."""
    # Skip params that should never be swept
    if param_name in NEVER_SWEEP:
        return None
    if param_name.startswith("clip_name") or param_name.endswith("_name"):
        return None

    # Use known smart defaults
    if param_name in SMART_DEFAULTS:
        return list(SMART_DEFAULTS[param_name])

    # Generic suggestions by type
    if param_type == "int":
        v = int(current_value) if current_value is not None else 0
        if v <= 0:
            return None
        if v <= 10:
            return [v, v * 2, v * 3]
        return [v // 2, v, v * 2]
    if param_type == "float":
        v = float(current_value) if current_value is not None else 0.0
        if v == 0:
            return None
        return [round(v * 0.5, 2), v, round(v * 1.5, 2)]
    if param_type == "string":
        return None  # can't guess valid strings
    if param_type == "bool":
        return [True, False]

    return None


def cmd_plan(args: argparse.Namespace) -> int:
    """Generate a test plan from a workflow JSON file."""
    wf_path = Path(args.workflow).expanduser()
    if not wf_path.exists():
        log(f"Error: workflow file not found: {args.workflow}")
        return 1

    # Load and analyze
    try:
        with wf_path.open() as f:
            payload = json.load(f)
        workflow = unwrap_workflow(payload)
    except (ValueError, json.JSONDecodeError) as e:
        log(f"Error: {e}")
        return 1

    schema = extract_schema(workflow)
    params = schema.get("parameters", {})

    if not params:
        log("Warning: no controllable parameters found in this workflow.")

    # Build plan
    plan: dict[str, Any] = {
        "$schema": str(SCHEMA_PATH),
        "workflow": str(wf_path.resolve()),
        "output_dir": args.output_dir or f"./test_outputs/{wf_path.stem}",
        "seed": args.seed,
        "host": args.host,
        "timeout": args.timeout,
        "prompt": args.prompt or "",
        "negative_prompt": "",
        "parameters": {},
    }

    sweep_count = 0
    total_runs = 1

    for pname, pinfo in sorted(params.items()):
        ptype = pinfo.get("type", "string")
        pval = pinfo.get("value")
        metric = classify_metric(pname)
        suggestions = suggest_values(pname, ptype, pval)

        param_entry: dict[str, Any] = {
            "test": suggestions is not None,
            "type": ptype,
            "default": pval,
            "metric": metric,
        }

        if suggestions is not None:
            param_entry["values"] = suggestions
            sweep_count += 1
            total_runs *= len(suggestions)
        else:
            param_entry["reason"] = (
                "fixed parameter (model name / prompt / filename)"
                if pname in NEVER_SWEEP or pname.endswith("_name")
                else "no auto-suggestions available; add values manually to test"
            )

        plan["parameters"][pname] = param_entry

    plan["total_runs"] = total_runs if sweep_count > 0 else 1

    # Write plan
    plan_path = Path(args.output or f"{wf_path.stem}_test_plan.json")
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")

    # Summary
    print()
    print(f"  Workflow    : {wf_path.name}")
    print(f"  Parameters  : {len(params)} total")
    print(f"  To test     : {sweep_count} parameters")
    print(f"  Test matrix : {total_runs} runs" if sweep_count > 0 else "  Test matrix : 1 run (nothing to sweep)")
    print(f"  Plan saved  : {plan_path}")
    print()

    if sweep_count == 0:
        print("  ⚠  No parameters were auto-selected for testing.")
        print("     Edit the plan file and set 'test: true' + add 'values' manually.")
    else:
        for pname, pinfo in plan["parameters"].items():
            if pinfo.get("test"):
                vals = pinfo.get("values", [])
                print(f"  ✓ {pname:25s} → {vals}  [{pinfo['metric']}]")

    print()
    print(f"  Next: edit {plan_path.name} to adjust, then run:")
    print(f"  python3 workflow_tester.py run --plan {plan_path}")
    print()

    # Open in VS Code
    if not args.no_code:
        try:
            subprocess.run(["code", str(plan_path)], check=False)
        except FileNotFoundError:
            pass

    return 0


# ═══════════════════════════════════════════════════════════════════════════
# RUN phase
# ═══════════════════════════════════════════════════════════════════════════

def expand_runs(plan: dict) -> list[dict]:
    """Expand test parameters into individual run configs (cartesian product)."""
    sweep_params: dict[str, list] = {}
    fixed_values: dict[str, Any] = {}

    for pname, pinfo in plan.get("parameters", {}).items():
        if pinfo.get("test") and "values" in pinfo:
            sweep_params[pname] = list(pinfo["values"])
        else:
            if "default" in pinfo and pinfo["default"] is not None:
                fixed_values[pname] = pinfo["default"]

    if not sweep_params:
        return [dict(fixed_values)]

    keys = list(sweep_params.keys())
    values = list(sweep_params.values())
    runs = []
    for combo in itertools.product(*values):
        run_args = dict(fixed_values)
        for k, v in zip(keys, combo):
            run_args[k] = v
        runs.append(run_args)
    return runs


def cmd_run(args: argparse.Namespace) -> int:
    """Execute a test plan against a ComfyUI server."""
    plan_path = Path(args.plan).expanduser()
    if not plan_path.exists():
        log(f"Error: plan file not found: {args.plan}")
        return 1

    try:
        plan = json.loads(plan_path.read_text())
    except json.JSONDecodeError as e:
        log(f"Error: invalid plan JSON: {e}")
        return 1

    # Load workflow
    wf_path = Path(plan["workflow"]).expanduser()
    if not wf_path.exists():
        log(f"Error: workflow file not found: {plan['workflow']}")
        return 1
    try:
        with wf_path.open() as f:
            workflow = unwrap_workflow(json.load(f))
    except (ValueError, json.JSONDecodeError) as e:
        log(f"Error loading workflow: {e}")
        return 1

    # Extract schema for parameter injection
    schema = extract_schema(workflow)

    # Override defaults from plan
    host = args.host or plan.get("host", DEFAULT_LOCAL_HOST)
    seed = args.seed if args.seed is not None else plan.get("seed", 42)
    timeout = args.timeout or plan.get("timeout", 300)
    output_dir = Path(args.output_dir or plan.get("output_dir", "./test_outputs")).expanduser()

    # Expand runs
    runs = expand_runs(plan)
    total = len(runs)
    log(f"Test matrix: {total} runs")

    # Setup runner
    runner = ComfyRunner(host=host)
    ok, info = runner.check_server()
    if not ok:
        log(f"Error: cannot reach ComfyUI server at {host}")
        log(f"  Details: {info}")
        log(f"  Hint: start ComfyUI first, then re-run.")
        return 1

    # Ensure seed is in every run + fixed prompts
    for r in runs:
        r["seed"] = seed
    if plan.get("prompt"):
        for r in runs:
            if "prompt" in schema.get("parameters", {}):
                r.setdefault("prompt", plan["prompt"])
    if plan.get("negative_prompt"):
        for r in runs:
            if "negative_prompt" in schema.get("parameters", {}):
                r.setdefault("negative_prompt", plan["negative_prompt"])

    # Execute
    results: list[dict] = []
    success_count = 0
    fail_count = 0
    total_time_start = time.time()

    for i, run_args in enumerate(runs):
        run_id = i + 1
        run_dir = output_dir / f"run_{run_id:04d}"
        run_dir.mkdir(parents=True, exist_ok=True)

        # Save params for this run
        (run_dir / "params.json").write_text(
            json.dumps(run_args, indent=2, ensure_ascii=False) + "\n"
        )

        # Build status line
        param_str = "  ".join(
            f"{k}={v}" for k, v in run_args.items()
            if k not in ("seed", "prompt", "negative_prompt")
        )
        status_prefix = f"[{run_id}/{total}] {param_str}"

        try:
            # Inject params
            wf_copy, warnings = inject_params(workflow, schema, run_args)

            # Submit and time
            t0 = time.time()
            submit_resp = runner.submit(wf_copy)

            if "_http_error" in submit_resp:
                elapsed = time.time() - t0
                log(f"{status_prefix} ... FAIL (HTTP {submit_resp['_http_error']})")
                fail_count += 1
                results.append({
                    "run_id": run_id,
                    "params": run_args,
                    "status": "error",
                    "error": f"HTTP {submit_resp['_http_error']}",
                    "time_seconds": round(elapsed, 2),
                    "outputs": [],
                })
                if not args.continue_on_error:
                    log("Aborting (use --continue-on-error to keep going)")
                    break
                continue

            pid = submit_resp.get("prompt_id")
            if not pid:
                elapsed = time.time() - t0
                log(f"{status_prefix} ... FAIL (no prompt_id)")
                fail_count += 1
                results.append({
                    "run_id": run_id, "params": run_args,
                    "status": "error", "error": "no prompt_id",
                    "time_seconds": round(elapsed, 2), "outputs": [],
                })
                if not args.continue_on_error:
                    break
                continue

            if submit_resp.get("node_errors"):
                elapsed = time.time() - t0
                log(f"{status_prefix} ... FAIL (validation)")
                fail_count += 1
                results.append({
                    "run_id": run_id, "params": run_args,
                    "status": "error", "error": "validation failed",
                    "node_errors": submit_resp["node_errors"],
                    "time_seconds": round(elapsed, 2), "outputs": [],
                })
                if not args.continue_on_error:
                    break
                continue

            # Wait for completion
            wait_result = runner.poll_status(pid, timeout=timeout)
            elapsed = time.time() - t0

            if wait_result["status"] == "success":
                outputs_raw = wait_result.get("outputs") or runner.get_outputs(pid)
                downloaded = download_outputs(runner, outputs_raw, run_dir)
                success_count += 1
                log(f"{status_prefix} ... OK  {elapsed:.1f}s  ({len(downloaded)} files)")
                results.append({
                    "run_id": run_id,
                    "params": run_args,
                    "status": "success",
                    "time_seconds": round(elapsed, 2),
                    "prompt_id": pid,
                    "outputs": [str(Path(d["file"]).relative_to(output_dir)) for d in downloaded],
                    "warnings": warnings,
                })
            else:
                fail_count += 1
                err = wait_result.get("data", {}).get("status_str", wait_result["status"])
                log(f"{status_prefix} ... FAIL ({err}, {elapsed:.1f}s)")
                results.append({
                    "run_id": run_id, "params": run_args,
                    "status": wait_result["status"],
                    "error": str(err),
                    "time_seconds": round(elapsed, 2),
                    "outputs": [],
                })

        except KeyboardInterrupt:
            log(f"\nInterrupted at run {run_id}/{total}")
            results.append({
                "run_id": run_id, "params": run_args,
                "status": "interrupted",
                "time_seconds": 0, "outputs": [],
            })
            break
        except Exception as e:
            elapsed = time.time() - t0
            fail_count += 1
            log(f"{status_prefix} ... ERROR {e}")
            results.append({
                "run_id": run_id, "params": run_args,
                "status": "error", "error": str(e),
                "time_seconds": round(elapsed, 2), "outputs": [],
            })
            if not args.continue_on_error:
                log("Aborting (use --continue-on-error to keep going)")
                break

    total_elapsed = time.time() - total_time_start
    completed = len(results)

    # ── Write results.json ──
    successes = [r for r in results if r["status"] == "success"]
    times = [r["time_seconds"] for r in results if r["time_seconds"] > 0]

    summary = {
        "workflow": str(wf_path.name),
        "plan": str(plan_path.name),
        "host": host,
        "total_runs": total,
        "completed": completed,
        "success": success_count,
        "failed": fail_count,
        "total_time_seconds": round(total_elapsed, 1),
        "avg_time_per_run": round(sum(times) / len(times), 2) if times else 0,
        "min_time": round(min(times), 2) if times else 0,
        "max_time": round(max(times), 2) if times else 0,
        "seed": seed,
    }

    # Per-parameter stats
    param_stats: dict[str, dict] = {}
    for pname in plan.get("parameters", {}):
        if not plan["parameters"][pname].get("test"):
            continue
        groups: dict[str, list[float]] = {}
        for r in successes:
            val = str(r["params"].get(pname))
            groups.setdefault(val, []).append(r["time_seconds"])
        param_stats[pname] = {
            k: {
                "count": len(v),
                "avg_time": round(sum(v) / len(v), 2),
                "min_time": round(min(v), 2),
                "max_time": round(max(v), 2),
            }
            for k, v in groups.items()
        }

    report = {
        "summary": summary,
        "parameters_tested": {
            pname: plan["parameters"][pname]
            for pname in plan.get("parameters", {})
            if plan["parameters"][pname].get("test")
        },
        "parameter_stats": param_stats,
        "results": results,
    }

    results_path = output_dir / "results.json"
    results_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    log(f"\nResults → {results_path}")

    # ── Write results.csv ──
    _write_csv(output_dir, report)

    # ── Summary ──
    print()
    print(f"  {'─' * 50}")
    print(f"  完成: {success_count}/{completed} 成功"
          f"{f'  ({fail_count} 失败)' if fail_count > 0 else ''}")
    print(f"  总耗时: {total_elapsed:.0f}s  |  平均: {summary['avg_time_per_run']}s/张"
          f"  |  最快: {summary['min_time']}s  |  最慢: {summary['max_time']}s")
    print(f"  输出目录: {output_dir}")
    print(f"  {'─' * 50}")
    print()
    print(f"  Next: generate report with:")
    print(f"  python3 workflow_tester.py report --results {results_path}")
    print()

    return 0 if fail_count == 0 else 1


def _write_csv(output_dir: Path, report: dict) -> None:
    """Write results.csv from report data."""
    csv_path = output_dir / "results.csv"

    # Collect all param keys from successful runs
    successes = [r for r in report["results"] if r["status"] == "success"]
    if not successes:
        return

    param_keys = list(report.get("parameters_tested", {}).keys())

    fieldnames = ["run_id", "status", "time_seconds"] + param_keys + ["output_count", "outputs"]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in report["results"]:
            row = {
                "run_id": r["run_id"],
                "status": r["status"],
                "time_seconds": r["time_seconds"],
                "output_count": len(r.get("outputs", [])),
                "outputs": "; ".join(r.get("outputs", [])),
            }
            row.update(r.get("params", {}))
            writer.writerow(row)

    log(f"CSV → {csv_path}")


# ═══════════════════════════════════════════════════════════════════════════
# REPORT phase
# ═══════════════════════════════════════════════════════════════════════════

def cmd_report(args: argparse.Namespace) -> int:
    """Generate HTML + CSV reports from results.json."""
    results_path = Path(args.results).expanduser()
    if not results_path.exists():
        log(f"Error: results file not found: {args.results}")
        return 1

    try:
        report = json.loads(results_path.read_text())
    except json.JSONDecodeError as e:
        log(f"Error: invalid results JSON: {e}")
        return 1

    output_dir = results_path.parent

    # CSV (if not exists or force)
    csv_path = output_dir / "results.csv"
    if args.force or not csv_path.exists():
        _write_csv(output_dir, report)

    # HTML
    html_path = output_dir / "report.html"
    html = _generate_html(report, output_dir)
    html_path.write_text(html)
    log(f"HTML → {html_path}")
    log(f"CSV  → {csv_path}")

    # Open
    if not args.no_open:
        try:
            subprocess.run(["code", str(csv_path)], check=False)
        except FileNotFoundError:
            pass
        try:
            # Try xdg-open on Linux, open on macOS
            opener = "xdg-open" if sys.platform == "linux" else "open"
            subprocess.run([opener, str(html_path)], check=False)
        except (FileNotFoundError, OSError):
            # Fallback: try wslview on WSL
            try:
                subprocess.run(["wslview", str(html_path)], check=False)
            except FileNotFoundError:
                log(f"Open {html_path} in your browser manually.")

    return 0


def _generate_html(report: dict, output_dir: Path) -> str:
    """Generate a self-contained HTML report page."""
    summary = report.get("summary", {})
    results = report.get("results", [])
    successes = [r for r in results if r["status"] == "success"]
    param_stats = report.get("parameter_stats", {})
    params_tested = report.get("parameters_tested", {})

    # Build filter panel HTML
    filter_html = ""
    for pname, pdata in params_tested.items():
        vals = pdata.get("values", [])
        filter_html += f'<div class="filter-group"><h4>{pname}</h4>'
        for v in vals:
            vid = f"filter_{pname}_{v}".replace(".", "_").replace(" ", "_")
            filter_html += (
                f'<label><input type="checkbox" class="filter-check" '
                f'data-param="{pname}" data-value="{v}" checked '
                f'onchange="applyFilters()"> {v}</label>'
            )
        filter_html += f'<div class="filter-actions">'
        filter_html += f'<a href="#" onclick="toggleGroup(\'{pname}\',true)">全选</a> '
        filter_html += f'<a href="#" onclick="toggleGroup(\'{pname}\',false)">全不选</a>'
        filter_html += f'</div></div>'

    # Build image cards
    cards_html = ""
    for r in successes:
        params = r.get("params", {})
        data_attrs = " ".join(
            f'data-{pname}="{v}"'.replace(".", "_").replace(" ", "_")
            for pname, v in params.items()
            if pname in params_tested
        )
        outputs = r.get("outputs", [])
        img_tag = ""
        if outputs:
            # Find the first image in outputs
            for o in outputs:
                if o.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    img_tag = f'<img src="{o}" loading="lazy" onclick="this.classList.toggle(\'zoomed\')" />'
                    break

        param_labels = "  ".join(
            f"{k}={v}" for k, v in params.items()
            if k in params_tested and k not in ("seed",)
        )

        cards_html += f"""
        <div class="card" {data_attrs}>
            <div class="card-img">{img_tag or '<div class="no-img">(无图片)</div>'}</div>
            <div class="card-info">
                <div class="card-params">{param_labels}</div>
                <div class="card-time">⏱ {r.get("time_seconds", "?")}s</div>
                <div class="card-score" onclick="scoreCard(this, event)" data-score="0">
                    <span class="star" data-v="1">☆</span>
                    <span class="star" data-v="2">☆</span>
                    <span class="star" data-v="3">☆</span>
                    <span class="star" data-v="4">☆</span>
                    <span class="star" data-v="5">☆</span>
                </div>
            </div>
        </div>"""

    # Stats summary rows
    stat_rows = ""
    for pname, pdata in param_stats.items():
        for val, vdata in pdata.items():
            stat_rows += (
                f"<tr><td>{pname}</td><td>{val}</td>"
                f"<td>{vdata['count']}</td>"
                f"<td>{vdata['avg_time']}s</td>"
                f"<td>{vdata['min_time']}s</td>"
                f"<td>{vdata['max_time']}s</td></tr>"
            )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ComfyUI Test Report — {summary.get('workflow', '')}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a2e; color: #e0e0e0; }}
.header {{ background: #16213e; padding: 20px 30px; border-bottom: 2px solid #0f3460; }}
.header h1 {{ font-size: 1.5em; color: #e94560; }}
.header .meta {{ font-size: 0.85em; color: #888; margin-top: 5px; }}
.layout {{ display: flex; min-height: calc(100vh - 80px); }}
.sidebar {{ width: 260px; min-width: 260px; background: #16213e; padding: 20px; overflow-y: auto; border-right: 1px solid #0f3460; }}
.sidebar h3 {{ color: #e94560; margin-bottom: 15px; font-size: 0.95em; }}
.main {{ flex: 1; padding: 20px; overflow-y: auto; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.stat-card {{ background: #16213e; padding: 15px; border-radius: 8px; text-align: center; border: 1px solid #0f3460; }}
.stat-card .val {{ font-size: 1.8em; font-weight: bold; color: #e94560; }}
.stat-card .lbl {{ font-size: 0.75em; color: #888; margin-top: 4px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }}
.card {{ background: #16213e; border-radius: 10px; overflow: hidden; border: 1px solid #0f3460; transition: transform .15s; }}
.card:hover {{ transform: translateY(-2px); border-color: #e94560; }}
.card.hidden {{ display: none; }}
.card-img {{ width: 100%; aspect-ratio: 1; overflow: hidden; background: #0f3460; display: flex; align-items: center; justify-content: center; }}
.card-img img {{ width: 100%; height: 100%; object-fit: cover; cursor: pointer; transition: transform .2s; }}
.card-img img.zoomed {{ position: fixed; top: 5%; left: 5%; width: 90%; height: 90%; object-fit: contain; z-index: 9999; background: rgba(0,0,0,0.9); border-radius: 10px; }}
.no-img {{ color: #555; font-size: 0.85em; }}
.card-info {{ padding: 12px; }}
.card-params {{ font-size: 0.78em; color: #aaa; margin-bottom: 6px; line-height: 1.5; }}
.card-time {{ font-size: 0.85em; color: #4ecca3; margin-bottom: 8px; }}
.card-score {{ cursor: pointer; user-select: none; font-size: 1.2em; }}
.card-score .star {{ color: #555; transition: color .15s; }}
.card-score .star.active {{ color: #f0c040; }}
.filter-group {{ margin-bottom: 16px; }}
.filter-group h4 {{ font-size: 0.82em; color: #ccc; margin-bottom: 6px; }}
.filter-group label {{ display: block; font-size: 0.78em; color: #999; padding: 2px 0; cursor: pointer; }}
.filter-group label:hover {{ color: #e0e0e0; }}
.filter-group input {{ margin-right: 6px; }}
.filter-actions {{ margin-top: 4px; font-size: 0.7em; }}
.filter-actions a {{ color: #e94560; text-decoration: none; margin-right: 8px; }}
.filter-actions a:hover {{ text-decoration: underline; }}
.stats-table {{ width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 0.85em; }}
.stats-table th, .stats-table td {{ padding: 8px 12px; border-bottom: 1px solid #0f3460; text-align: left; }}
.stats-table th {{ color: #e94560; font-weight: 600; }}
.section-title {{ color: #e94560; font-size: 1.1em; margin: 25px 0 12px; border-bottom: 1px solid #0f3460; padding-bottom: 6px; }}
</style>
</head>
<body>
<div class="header">
    <h1>🎨 ComfyUI Test Report</h1>
    <div class="meta">
        Workflow: {summary.get('workflow', '')} &nbsp;|&nbsp;
        Total: {summary.get('total_runs', 0)} runs &nbsp;|&nbsp;
        Success: {summary.get('success', 0)} &nbsp;|&nbsp;
        Failed: {summary.get('failed', 0)} &nbsp;|&nbsp;
        Seed: {summary.get('seed', '?')}
    </div>
</div>
<div class="layout">
    <div class="sidebar">
        <h3>🔍 参数筛选</h3>
        {filter_html}
        <button onclick="resetFilters()" style="margin-top:10px;padding:6px 12px;background:#e94560;color:#fff;border:none;border-radius:4px;cursor:pointer;width:100%;">重置筛选</button>
        <button onclick="exportScores()" style="margin-top:6px;padding:6px 12px;background:#0f3460;color:#ccc;border:1px solid #555;border-radius:4px;cursor:pointer;width:100%;">📋 导出评分</button>
    </div>
    <div class="main">
        <div class="stats">
            <div class="stat-card"><div class="val">{summary.get('success', 0)}</div><div class="lbl">成功</div></div>
            <div class="stat-card"><div class="val">{summary.get('total_time_seconds', 0):.0f}s</div><div class="lbl">总耗时</div></div>
            <div class="stat-card"><div class="val">{summary.get('avg_time_per_run', 0)}s</div><div class="lbl">平均/张</div></div>
            <div class="stat-card"><div class="val">{summary.get('min_time', 0)}s</div><div class="lbl">最快</div></div>
            <div class="stat-card"><div class="val">{summary.get('max_time', 0)}s</div><div class="lbl">最慢</div></div>
            <div class="stat-card"><div class="val">{summary.get('failed', 0)}</div><div class="lbl">失败</div></div>
        </div>

        <div class="section-title">🖼 生成结果（点击图片放大，点击星星评分）</div>
        <div class="grid" id="cardGrid">
            {cards_html}
        </div>

        <div class="section-title">📊 按参数统计耗时</div>
        <table class="stats-table">
            <thead><tr><th>参数</th><th>值</th><th>次数</th><th>平均耗时</th><th>最快</th><th>最慢</th></tr></thead>
            <tbody>{stat_rows}</tbody>
        </table>
    </div>
</div>

<script>
// Restore scores from localStorage
const SCORE_KEY = 'comfy_tester_scores_' + window.location.pathname;
let scores = JSON.parse(localStorage.getItem(SCORE_KEY) || '{{}}');
document.querySelectorAll('.card-score').forEach(card => {{
    const params = card.closest('.card').querySelector('.card-params').textContent;
    if (scores[params] !== undefined) {{
        card.dataset.score = scores[params];
        card.querySelectorAll('.star').forEach(s => {{
            if (parseInt(s.dataset.v) <= scores[params]) s.classList.add('active');
        }});
    }}
}});

function scoreCard(el, event) {{
    const star = event.target.closest('.star');
    if (!star) return;
    const val = parseInt(star.dataset.v);
    el.dataset.score = val;
    el.querySelectorAll('.star').forEach(s => {{
        s.classList.toggle('active', parseInt(s.dataset.v) <= val);
    }});
    const params = el.closest('.card').querySelector('.card-params').textContent;
    scores[params] = val;
    localStorage.setItem(SCORE_KEY, JSON.stringify(scores));
}}

function applyFilters() {{
    document.querySelectorAll('.card').forEach(card => {{
        let visible = true;
        document.querySelectorAll('.filter-group').forEach(group => {{
            const param = group.querySelector('input').dataset.param;
            const cardVal = card.dataset[param];
            if (!cardVal) return;
            const anyChecked = Array.from(group.querySelectorAll('input:checked'))
                .some(cb => cb.dataset.value === cardVal);
            if (!anyChecked) visible = false;
        }});
        card.classList.toggle('hidden', !visible);
    }});
}}

function toggleGroup(param, state) {{
    document.querySelectorAll(`input[data-param="${{param}}"]`).forEach(cb => cb.checked = state);
    applyFilters();
}}

function resetFilters() {{
    document.querySelectorAll('.filter-check').forEach(cb => cb.checked = true);
    applyFilters();
}}

function exportScores() {{
    let csv = 'params,score\\n';
    for (const [params, score] of Object.entries(scores)) {{
        csv += `"${{params}}",${{score}}\\n`;
    }}
    const blob = new Blob([csv], {{type: 'text/csv'}});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'scores.csv';
    a.click();
}}
</script>
</body>
</html>"""

    return html


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ComfyUI Workflow Testing Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 workflow_tester.py plan --workflow my_workflow.json
  python3 workflow_tester.py run --plan my_workflow_test_plan.json
  python3 workflow_tester.py report --results test_outputs/my_workflow/results.json
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # plan
    p_plan = sub.add_parser("plan", help="Analyze workflow and generate test plan")
    p_plan.add_argument("--workflow", required=True, help="Path to workflow API JSON")
    p_plan.add_argument("--output", "-o", help="Output plan path (default: <workflow>_test_plan.json)")
    p_plan.add_argument("--output-dir", help="Test output directory")
    p_plan.add_argument("--seed", type=int, default=42, help="Fixed seed for reproducibility")
    p_plan.add_argument("--prompt", help="Fixed prompt (overrides workflow default)")
    p_plan.add_argument("--host", default=DEFAULT_LOCAL_HOST, help="ComfyUI server URL")
    p_plan.add_argument("--timeout", type=int, default=300, help="Timeout per run in seconds")
    p_plan.add_argument("--no-code", action="store_true", help="Don't open in VS Code")

    # run
    p_run = sub.add_parser("run", help="Execute test plan")
    p_run.add_argument("--plan", required=True, help="Path to test_plan.json")
    p_run.add_argument("--host", help="ComfyUI server URL (overrides plan)")
    p_run.add_argument("--seed", type=int, help="Fixed seed (overrides plan)")
    p_run.add_argument("--timeout", type=int, help="Timeout per run in seconds")
    p_run.add_argument("--output-dir", help="Output directory (overrides plan)")
    p_run.add_argument("--continue-on-error", action="store_true", help="Don't abort on failure")

    # report
    p_report = sub.add_parser("report", help="Generate HTML + CSV reports")
    p_report.add_argument("--results", required=True, help="Path to results.json")
    p_report.add_argument("--force", action="store_true", help="Overwrite existing CSV")
    p_report.add_argument("--no-open", action="store_true", help="Don't auto-open files")

    args = parser.parse_args(argv)

    try:
        if args.command == "plan":
            return cmd_plan(args)
        elif args.command == "run":
            return cmd_run(args)
        elif args.command == "report":
            return cmd_report(args)
    except Exception as e:
        log(f"Fatal error: {e}")
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
