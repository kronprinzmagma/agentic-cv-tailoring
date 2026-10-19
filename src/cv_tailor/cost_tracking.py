"""Aggregate per-run LLM cost and token usage from JSONL logs.

Every `call_llm()` writes a JSONL record to `logs/YYYY-MM/llm_calls.jsonl`
with `cost_usd`, `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens` and `run_id`.
This module sums those records per run_id and exposes a compact summary
for the Web-UI status bar plus a per-agent breakdown for diagnostics.

Read-only. Runs in O(N) over the current+previous month log files, which
is well under a millisecond for normal volumes (a run produces ~20-30
entries; a month tops out at a few thousand).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from cv_tailor.logging_config import get_logger

log = get_logger(__name__)

LOG_ROOT = Path("logs")


def _candidate_log_files(log_root: Path = LOG_ROOT) -> list[Path]:
    """Return current + previous month's llm_calls.jsonl paths (if they exist).

    Runs almost always finish within the same UTC month they started, but
    a run that crosses month boundary would split its records across two
    files. Scanning two months handles that without breaking O(N).
    """
    now = datetime.now(timezone.utc)
    months = {now.strftime("%Y-%m")}
    # Previous month
    first_of_month = now.replace(day=1)
    prev_month_dt = first_of_month - timedelta(days=1)
    months.add(prev_month_dt.strftime("%Y-%m"))
    paths: list[Path] = []
    for m in sorted(months):
        p = log_root / m / "llm_calls.jsonl"
        if p.exists():
            paths.append(p)
    return paths


def compute_run_cost(
    run_id: str,
    log_root: Path = LOG_ROOT,
) -> dict[str, Any]:
    """Sum tokens + USD cost for all successful calls of one run_id.

    Returns a dict with totals and a per-agent breakdown:
        {
          "run_id": str,
          "total_cost_usd": float,
          "total_input_tokens": int,
          "total_output_tokens": int,
          "total_cache_read_input_tokens": int,
          "total_cache_creation_input_tokens": int,
          "calls": int,
          "rate_limit_retries": int,
          "errors": int,
          "by_agent": [
            {"agent": str, "calls": int, "input_tokens": int,
             "output_tokens": int, "cache_read": int, "cost_usd": float},
            ...
          ],
        }

    Empty defaults are returned when the run has no entries (yet) — caller
    can treat zero-cost as "no calls billed".
    """
    if not run_id:
        return _empty_summary("")

    total_cost = 0.0
    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_creation = 0
    calls_ok = 0
    rate_limit_retries = 0
    errors = 0
    per_agent: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read": 0, "cache_creation": 0, "cost_usd": 0.0,
        }
    )

    for path in _candidate_log_files(log_root):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("run_id") != run_id:
                        continue
                    status = rec.get("status")
                    if status == "rate_limit":
                        rate_limit_retries += 1
                        continue
                    if status == "error":
                        errors += 1
                        continue
                    if status != "success":
                        continue
                    calls_ok += 1
                    cost = float(rec.get("cost_usd") or 0.0)
                    in_tok = int(rec.get("input_tokens") or 0)
                    out_tok = int(rec.get("output_tokens") or 0)
                    cache_r = int(rec.get("cache_read_input_tokens") or 0)
                    cache_c = int(rec.get("cache_creation_input_tokens") or 0)
                    total_cost += cost
                    total_in += in_tok
                    total_out += out_tok
                    total_cache_read += cache_r
                    total_cache_creation += cache_c
                    agent = rec.get("agent") or "unknown"
                    bucket = per_agent[agent]
                    bucket["calls"] += 1
                    bucket["input_tokens"] += in_tok
                    bucket["output_tokens"] += out_tok
                    bucket["cache_read"] += cache_r
                    bucket["cache_creation"] += cache_c
                    bucket["cost_usd"] += cost
        except OSError as exc:
            log.warning("cost_tracking.read_failed", path=str(path), error=str(exc))
            continue

    by_agent = sorted(
        (
            {"agent": agent, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in bucket.items()}}
            for agent, bucket in per_agent.items()
        ),
        key=lambda b: b["cost_usd"],
        reverse=True,
    )

    return {
        "run_id": run_id,
        "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cache_read_input_tokens": total_cache_read,
        "total_cache_creation_input_tokens": total_cache_creation,
        "calls": calls_ok,
        "rate_limit_retries": rate_limit_retries,
        "errors": errors,
        "by_agent": by_agent,
    }


def _empty_summary(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_input_tokens": 0,
        "total_cache_creation_input_tokens": 0,
        "calls": 0,
        "rate_limit_retries": 0,
        "errors": 0,
        "by_agent": [],
    }


def format_compact(summary: dict[str, Any]) -> str:
    """Return a one-line `$0.74 · 26 Calls · 87% Cache` style string."""
    cost = summary.get("total_cost_usd", 0.0) or 0.0
    calls = summary.get("calls", 0) or 0
    cache_read = summary.get("total_cache_read_input_tokens", 0) or 0
    in_tok = summary.get("total_input_tokens", 0) or 0
    cache_pct = round(100 * cache_read / max(in_tok, 1)) if in_tok else 0
    parts = [f"${cost:.2f}", f"{calls} Calls"]
    if cache_read:
        parts.append(f"{cache_pct}% Cache-Hits")
    return " · ".join(parts)
