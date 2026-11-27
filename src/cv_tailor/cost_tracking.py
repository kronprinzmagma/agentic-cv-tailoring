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
import os
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cv_tailor.logging_config import get_logger

log = get_logger(__name__)

LOG_ROOT = Path("logs")


_RUN_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-\d{2}_")


def _candidate_log_files(log_root: Path = LOG_ROOT, run_id: str | None = None) -> list[Path]:
    """Monthly llm_calls.jsonl files that can hold a run's records.

    A run id starting with its date (`YYYY-MM-DD_…`) maps to its start month
    and the following one (runs crossing a month boundary). Without a dated
    id: current and previous month. Reading only the last two months
    zeroed the cost of every older run on `quality-trend --backfill`
    (review 2026-10-23, R09).
    """
    m = _RUN_DATE_RE.match(run_id or "")
    if m:
        start = datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=UTC)
        following = (start + timedelta(days=32)).replace(day=1)
        months = {start.strftime("%Y-%m"), following.strftime("%Y-%m")}
    else:
        now = datetime.now(UTC)
        prev = now.replace(day=1) - timedelta(days=1)
        months = {now.strftime("%Y-%m"), prev.strftime("%Y-%m")}
    paths: list[Path] = []
    for month in sorted(months):
        p = log_root / month / "llm_calls.jsonl"
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

    for path in _candidate_log_files(log_root, run_id):
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


# ---------------------------------------------------------------------------
# Tageslimit fuer LLM-Kosten (Cost Guardrail)
#
# Agenten-Schleifen (Writer/Reviewer, Factcheck-Runden, Retries) koennen bei
# einem Fehler mehr Calls ausloesen als geplant. `call_llm()` prueft darum
# vor jedem Call, ob die heute (UTC) bereits verbuchten Kosten das Tageslimit
# erreicht haben, und bricht dann mit `DailyCostLimitExceeded` ab. Der Lauf
# endet sichtbar mit Fehler, statt still weiter Geld auszugeben.
#
# Quelle des Limits: Umgebungsvariable CV_TAILOR_DAILY_COST_LIMIT_USD (hat
# Vorrang), sonst `cost_limits.daily_usd` in config.yaml. 0, leer oder ein
# fehlender Eintrag schaltet die Grenze aus.
# ---------------------------------------------------------------------------

DAILY_LIMIT_ENV = "CV_TAILOR_DAILY_COST_LIMIT_USD"

# Konservativer Ersatzpreis (USD pro Million Tokens) fuer Aufrufe, deren Preis
# LiteLLM nicht kennt (neues Modell, Preistabelle hinkt nach). Vorher wurden sie
# mit 0 USD verbucht und das Tageslimit griff nie (Idee 48, 2026-11-23). Bewusst
# hoch angesetzt (ueber dem teuersten konfigurierten Modell): lieber frueher
# abbrechen als still weiter ausgeben. Ein Abbruch bei jedem unbekannten Preis
# haette dagegen jeden Lauf nach einem Modellwechsel blockiert.
FALLBACK_USD_PER_MTOK: dict[str, float] = {"input": 15.0, "output": 75.0}


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Konservative Kostenschaetzung aus Tokens (siehe FALLBACK_USD_PER_MTOK)."""
    return (
        max(int(input_tokens or 0), 0) * FALLBACK_USD_PER_MTOK["input"]
        + max(int(output_tokens or 0), 0) * FALLBACK_USD_PER_MTOK["output"]
    ) / 1_000_000


class DailyCostLimitExceeded(RuntimeError):
    """Raised before an LLM call when today's spend has reached the limit."""

    def __init__(self, spent_usd: float, limit_usd: float) -> None:
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        super().__init__(
            f"Tageslimit fuer LLM-Kosten erreicht: ${spent_usd:.2f} von "
            f"${limit_usd:.2f} (UTC-Tag). Lauf abgebrochen. Limit anpassen ueber "
            f"{DAILY_LIMIT_ENV} oder cost_limits.daily_usd in config.yaml."
        )


def compute_daily_cost(
    day: str | None = None,
    log_root: Path = LOG_ROOT,
) -> float:
    """Sum `cost_usd` of all successful calls whose timestamp falls on `day`.

    `day` is an ISO date (`YYYY-MM-DD`, UTC); default is today. Only the
    month file of that day is read. Records without a parseable timestamp
    are ignored.
    """
    if day is None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
    path = log_root / day[:7] / "llm_calls.jsonl"
    if not path.exists():
        return 0.0
    total = 0.0
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
                if not isinstance(rec, dict) or rec.get("status") != "success":
                    continue
                if not str(rec.get("timestamp") or "").startswith(day):
                    continue
                try:
                    total += float(rec.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    continue
    except OSError as exc:
        log.warning("cost_tracking.read_failed", path=str(path), error=str(exc))
    return round(total, 4)


def _parse_limit(value: Any) -> float | None:
    """Positive float or None (0, empty, negative or invalid = no limit)."""
    if value is None or value == "":
        return None
    try:
        limit = float(value)
    except (TypeError, ValueError):
        log.warning("cost_tracking.invalid_daily_limit", value=str(value))
        return None
    return limit if limit > 0 else None


def resolve_daily_limit(config: dict[str, Any] | None) -> float | None:
    """Daily limit in USD from env (preferred) or the parsed config dict."""
    env_value = os.environ.get(DAILY_LIMIT_ENV)
    if env_value is not None and env_value.strip() != "":
        return _parse_limit(env_value.strip())
    limits = (config or {}).get("cost_limits") or {}
    if not isinstance(limits, dict):
        return None
    return _parse_limit(limits.get("daily_usd"))


def check_daily_limit(
    limit_usd: float | None,
    log_root: Path = LOG_ROOT,
) -> float:
    """Raise DailyCostLimitExceeded when today's spend >= limit.

    Returns today's spend (0.0 when no limit is set; then nothing is read).
    """
    if not limit_usd:
        return 0.0
    spent = compute_daily_cost(log_root=log_root)
    if spent >= limit_usd:
        raise DailyCostLimitExceeded(spent, limit_usd)
    return spent
