"""Tests for cost aggregation from llm_calls.jsonl."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cv_tailor.cost_tracking import compute_run_cost, format_compact


@pytest.fixture
def fake_logs(tmp_path: Path) -> Path:
    """A logs/ dir with a single month file we control fully.

    Returns the root path that callers pass as `log_root=` to
    compute_run_cost. We avoid monkeypatching the module-level LOG_ROOT
    because the default arg is captured at function-define time.
    """
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    month_dir = tmp_path / month
    month_dir.mkdir(parents=True)
    log = month_dir / "llm_calls.jsonl"

    records = [
        # run 'A': two successful calls + one rate-limit retry
        {"run_id": "A", "agent": "writer", "status": "success",
         "cost_usd": 0.10, "input_tokens": 1000, "output_tokens": 200,
         "cache_read_input_tokens": 600, "cache_creation_input_tokens": 100},
        {"run_id": "A", "agent": "writer", "status": "success",
         "cost_usd": 0.05, "input_tokens": 800, "output_tokens": 150,
         "cache_read_input_tokens": 700, "cache_creation_input_tokens": 0},
        {"run_id": "A", "agent": "writer", "status": "rate_limit"},
        {"run_id": "A", "agent": "factcheck", "status": "success",
         "cost_usd": 0.01, "input_tokens": 200, "output_tokens": 50,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        # run 'B': nothing for the target
        {"run_id": "B", "agent": "writer", "status": "success",
         "cost_usd": 0.99, "input_tokens": 999, "output_tokens": 999},
        # malformed line (should be skipped silently)
        "this is not valid json",
    ]
    with log.open("w", encoding="utf-8") as fh:
        for r in records:
            if isinstance(r, str):
                fh.write(r + "\n")
            else:
                fh.write(json.dumps(r) + "\n")
    return tmp_path


def test_compute_run_cost_aggregates_successes_only(fake_logs):
    s = compute_run_cost("A", log_root=fake_logs)
    assert s["calls"] == 3
    # Cost rounded to 4 decimals: 0.10 + 0.05 + 0.01 = 0.16
    assert s["total_cost_usd"] == 0.16
    assert s["total_input_tokens"] == 2000
    assert s["total_output_tokens"] == 400
    assert s["total_cache_read_input_tokens"] == 1300
    assert s["rate_limit_retries"] == 1
    assert s["errors"] == 0


def test_compute_run_cost_breakdown_sorted_desc(fake_logs):
    s = compute_run_cost("A", log_root=fake_logs)
    agents = [a["agent"] for a in s["by_agent"]]
    assert agents == ["writer", "factcheck"]
    assert s["by_agent"][0]["calls"] == 2


def test_compute_run_cost_missing_run_returns_empty(fake_logs):
    s = compute_run_cost("does-not-exist")
    assert s["calls"] == 0
    assert s["total_cost_usd"] == 0.0
    assert s["by_agent"] == []


def test_compute_run_cost_empty_run_id():
    """Defensive: an empty run_id never tries to read disk."""
    s = compute_run_cost("")
    assert s["calls"] == 0


def test_format_compact_includes_cache_pct(fake_logs):
    s = compute_run_cost("A", log_root=fake_logs)
    compact = format_compact(s)
    assert "$0.16" in compact
    assert "3 Calls" in compact
    assert "Cache-Hits" in compact


def test_format_compact_no_cache_no_cache_string():
    """No cache reads → no cache % in the compact string (keeps it terse)."""
    s = {"total_cost_usd": 0.5, "calls": 2,
         "total_cache_read_input_tokens": 0, "total_input_tokens": 100}
    out = format_compact(s)
    assert "Cache" not in out
