"""Per-run quality snapshot for regression tracking.

Captures deterministic metrics from each finished run's artifacts and
appends a JSONL record to `logs/quality_snapshots.jsonl`. The intent is
not to grade individual runs (the eval-suite does that with named cases)
but to surface **drift over time** as prompts and configuration change:

- A jump in `factcheck_iter_vetos` across the trailing median means the
  writer started drifting more from belegs (suspect: prompt change or
  model swap loosened the bound).
- A jump in `consistency_findings` means the writer is breaking station
  headers more often.
- A jump in `writer_round_2_count` means the reviewer loop converges
  later than before (suspect: reviewer too strict, or writer too weak).
- A drop in `cache_hit_rate` means caching broke (cost regression).

The snapshot is light (no LLM calls), runs in milliseconds, and is
captured at pipeline completion. A `quality_trend()` helper compares the
latest snapshot against the trailing-N median and returns a list of
metrics that deviated by more than a configured threshold — surfaced via
the Web-UI status payload so the user sees a warning if quality regresses.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Wörter, die zwischen "kein Inhalt" und "Marketing-Sprache" oszillieren.
# Bewusst zurückhaltend — nur klare Floskeln, nichts was in einem konkret
# belegten Bullet sinnvoll wäre. Lowercase-Substring-Match.
_CLICHE_WORDS = (
    "strategisch", "ganzheitlich", "nachhaltig", "synergetisch", "proaktiv",
    "konsequent", "umfassend", "passioniert", "leidenschaftlich",
    "state-of-the-art", "best-in-class", "world-class",
    "in der praxis bedeutet",
    "end-to-end", "customer-centric",
    # EN-Varianten (Translator-Output kann sie produzieren)
    "strategic", "holistic", "passionate", "proactive",
    "comprehensive", "innovative", "robust",
    "across the board",
)

from cv_tailor.cost_tracking import compute_run_cost
from cv_tailor.logging_config import get_logger

log = get_logger(__name__)

SNAPSHOTS_PATH = Path("logs") / "quality_snapshots.jsonl"


def _git_sha_short(path: Path | None = None) -> str:
    """Return short SHA of HEAD (or the prompts directory if given). Empty on failure."""
    try:
        cmd = ["git", "rev-parse", "--short", "HEAD"]
        if path is not None:
            # Hash of the latest commit that touched `path`
            cmd = ["git", "log", "-1", "--pretty=%h", "--", str(path)]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
        return out
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _count_words(text: str) -> int:
    return len(re.findall(r"\b[\wäöüÄÖÜß]+\b", text))


def _count_v2_rounds(iter_dir: Path) -> dict[str, int]:
    """Per-section count of writer rounds (max round_num found in artifacts)."""
    rounds: dict[str, int] = {}
    if not iter_dir.exists():
        return rounds
    for f in iter_dir.iterdir():
        m = re.match(r"^(management_summary|schluesselkompetenzen|berufserfahrung)_v(\d+)\.md$", f.name)
        if not m:
            continue
        section, vn = m.group(1), int(m.group(2))
        rounds[section] = max(rounds.get(section, 0), vn)
    return rounds


def _count_factcheck_vetos(iter_dir: Path) -> int:
    """Count v2 factcheck artifacts that look like a veto (vs clean)."""
    if not iter_dir.exists():
        return 0
    veto_signals = ("nicht belegt", "echte lücke", "kritische lücke", "veto")
    clean_signals = ("keine drift", "keine sachliche drift", "keine strukturellen vetos", "alles belegt")
    count = 0
    for f in iter_dir.glob("*_v*_factcheck.md"):
        text = f.read_text(encoding="utf-8").lower()
        if any(s in text for s in clean_signals):
            continue
        if any(s in text for s in veto_signals):
            count += 1
    return count


def _count_consistency_findings(iter_dir: Path) -> int:
    """Sum actual consistency findings across all consistency artifacts.

    The consistency_check writes either:
    - "Keine strukturelle Drift gefunden." (clean)
    - "# Konsistenz-Check: Drift gegen Standard-CV erkannt" + numbered items
      starting with "Header-Drift für '...'" (findings present)

    Previous heuristic counted files containing the word "drift" anywhere,
    which matched the failure-heading itself and gave a constant 2 per run
    regardless of actual findings. New approach: only count actual
    "Header-Drift"-prefixed lines, which are the real findings.
    """
    if not iter_dir.exists():
        return 0
    finding_pattern = re.compile(r"^\s*\d+\.\s+Header-Drift\s+für", re.M)
    count = 0
    for f in iter_dir.glob("*_consistency.md"):
        text = f.read_text(encoding="utf-8")
        count += len(finding_pattern.findall(text))
    return count


def _cliche_density_per_100_words(final_cv_text: str) -> float:
    """Per-100-word frequency of marketing-style filler words.

    Counts substring hits of `_CLICHE_WORDS` in the lowercased CV body.
    Lower is better. Empty CV returns 0. The metric is a coarse signal —
    not every "strategisch" is filler, but a steady rise across runs hints
    that the writer prompt is drifting toward marketing register.
    """
    if not final_cv_text:
        return 0.0
    text = final_cv_text.lower()
    hits = sum(text.count(w) for w in _CLICHE_WORDS)
    wc = _count_words(final_cv_text)
    if wc == 0:
        return 0.0
    return round(100 * hits / wc, 2)


def _bullet_length_stats(final_cv_text: str) -> dict[str, float]:
    """Bullet-length spread across all bullets in the CV.

    Returns mean / stddev / max in word units. Uniformly-sized bullets
    (low stddev) hint at templated generation; high stddev signals
    deliberate emphasis (a long lead bullet + short follow-ups). The
    "max" tracks worst-offender per CV.
    """
    bullets: list[int] = []
    for line in final_cv_text.splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ", "• ")):
            wc = _count_words(s[2:])
            if wc > 0:
                bullets.append(wc)
    if not bullets:
        return {"mean": 0.0, "stddev": 0.0, "max": 0, "count": 0}
    mean = sum(bullets) / len(bullets)
    stddev = statistics.pstdev(bullets) if len(bullets) > 1 else 0.0
    return {
        "mean": round(mean, 1),
        "stddev": round(stddev, 1),
        "max": max(bullets),
        "count": len(bullets),
    }


def _count_bolds_in_experience(final_cv_text: str) -> list[int]:
    """Return per-station bold counts in the Berufserfahrung section."""
    section = re.search(
        r"##\s+(?:Berufserfahrung|Professional Experience|Work Experience)(.*?)(?=^##\s|\Z)",
        final_cv_text, flags=re.S | re.M | re.I,
    )
    if not section:
        return []
    body = section.group(1)
    stations = re.split(r"^###\s+", body, flags=re.M)[1:]
    return [len(re.findall(r"\*\*[^*]+\*\*", "\n".join(s.splitlines()[1:]))) for s in stations]


def _summary_word_count(final_cv_text: str) -> int:
    m = re.search(
        r"##\s+(?:Management Summary)(.*?)(?=^##\s|\Z)",
        final_cv_text, flags=re.S | re.M,
    )
    return _count_words(m.group(1)) if m else 0


def _diff_row_count(diff_text: str) -> int:
    """Count Markdown table rows in 05_diff.md (excluding header + separator)."""
    rows = 0
    in_table = False
    for line in diff_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            if re.fullmatch(r"\|[\s:|-]+\|", stripped):
                in_table = True
                continue
            if in_table:
                rows += 1
    return rows


def capture_run_snapshot(
    run_dir: Path,
    snapshots_path: Path = SNAPSHOTS_PATH,
) -> dict[str, Any]:
    """Read a finished run's artifacts and append a quality snapshot.

    Idempotent: if a snapshot for this run_id already exists, it's replaced
    in-place (so re-running cost tracker after re-render doesn't append
    duplicates). The full snapshot dict is returned for caller inspection.

    Safe to call mid-run — missing artifacts produce zero/null values
    without raising. Caller should only invoke this once at completion.
    """
    run_id = run_dir.name
    iter_dir = run_dir / "03_iterationen"

    # Read artifacts defensively — any may be missing on a partial run.
    final_de = (run_dir / "04_final_de.md").read_text(encoding="utf-8") if (run_dir / "04_final_de.md").exists() else ""
    final_en = (run_dir / "04_final_en.md").read_text(encoding="utf-8") if (run_dir / "04_final_en.md").exists() else ""
    diff_text = (run_dir / "05_diff.md").read_text(encoding="utf-8") if (run_dir / "05_diff.md").exists() else ""
    profile_fit_text = (run_dir / "_profile_fit.md").read_text(encoding="utf-8") if (run_dir / "_profile_fit.md").exists() else ""

    v2_rounds = _count_v2_rounds(iter_dir)
    cost = compute_run_cost(run_id)
    cache_hit_rate = 0.0
    if cost["total_input_tokens"]:
        cache_hit_rate = round(
            cost["total_cache_read_input_tokens"] / cost["total_input_tokens"], 3
        )

    snapshot = {
        "run_id": run_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha_short(),
        "prompts_sha": _git_sha_short(Path("prompts")),
        # Quality metrics
        "writer_round_2_count": sum(1 for v in v2_rounds.values() if v >= 2),
        "writer_rounds_by_section": v2_rounds,
        "factcheck_iter_vetos": _count_factcheck_vetos(iter_dir),
        "consistency_findings": _count_consistency_findings(iter_dir),
        "summary_word_count": _summary_word_count(final_en or final_de),
        "diff_row_count": _diff_row_count(diff_text),
        "berufserfahrung_bolds_by_station": _count_bolds_in_experience(final_en or final_de),
        "cliche_density_per_100_words": _cliche_density_per_100_words(final_en or final_de),
        "bullet_length_stats": _bullet_length_stats(final_en or final_de),
        "profile_fit_warned": bool(profile_fit_text and "Kritisch" in profile_fit_text),
        "language": "en" if final_en else "de",
        # Cost / token metrics
        "total_cost_usd": cost["total_cost_usd"],
        "calls": cost["calls"],
        "total_input_tokens": cost["total_input_tokens"],
        "total_output_tokens": cost["total_output_tokens"],
        "cache_hit_rate": cache_hit_rate,
        "rate_limit_retries": cost["rate_limit_retries"],
        "errors": cost["errors"],
    }
    # Decide outlier AFTER the rest of the snapshot is built so the heuristic
    # can use both the new snapshot's metrics and the historical baseline.
    snapshot["is_outlier"] = _is_outlier(snapshot, snapshots_path)

    _upsert_snapshot(snapshot, snapshots_path)
    log.info(
        "quality_snapshot.captured",
        run_id=run_id,
        cost=cost["total_cost_usd"],
        v2_rounds=snapshot["writer_round_2_count"],
        factcheck_vetos=snapshot["factcheck_iter_vetos"],
    )
    return snapshot


def _is_outlier(snapshot: dict[str, Any], snapshots_path: Path) -> bool:
    """Flag runs that look like accidental re-starts or stuck iterations.

    Heuristic: a run is an outlier when its `calls` value is >= 2× the
    median of the last 10 non-outlier prior snapshots. Catches the
    pathological case where a Web-UI server was restarted mid-run and the
    pipeline re-entered, doubling LLM consumption without producing two
    artifacts. These outliers should NOT pollute the regression baseline.

    Returns False if there's no history to compare against yet.
    """
    prior = load_snapshots(snapshots_path)
    baseline = [s for s in prior if not s.get("is_outlier")][-10:]
    if len(baseline) < 3:
        return False
    median_calls = statistics.median(s.get("calls", 0) or 0 for s in baseline)
    if median_calls == 0:
        return False
    return (snapshot.get("calls", 0) or 0) >= 2 * median_calls


def _upsert_snapshot(snapshot: dict[str, Any], path: Path) -> None:
    """Append-or-replace by run_id. Atomic via tmp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") != snapshot["run_id"]:
                existing.append(rec)
    existing.append(snapshot)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in existing) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_snapshots(snapshots_path: Path = SNAPSHOTS_PATH, limit: int | None = None) -> list[dict[str, Any]]:
    """Return snapshots in capture order (oldest first), newest at end."""
    if not snapshots_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in snapshots_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit is not None:
        out = out[-limit:]
    return out


# Metrics where a higher value indicates worse quality. Their median across
# the trailing window is the baseline; latest > median * (1 + threshold)
# triggers a regression flag.
_REGRESSION_METRICS_HIGHER_IS_WORSE = {
    "writer_round_2_count": 0.5,
    "factcheck_iter_vetos": 0.5,
    "consistency_findings": 0.5,
    "rate_limit_retries": 1.0,
    "errors": 0.5,
    "total_cost_usd": 0.25,           # cost creep alert
    "cliche_density_per_100_words": 0.5,  # marketing-Sprache schleicht sich ein
}

# Metrics where a lower value indicates worse quality (regression flag if
# latest < median * (1 - threshold)).
_REGRESSION_METRICS_LOWER_IS_WORSE = {
    "cache_hit_rate": 0.2,
}


def detect_regressions(
    snapshots_path: Path = SNAPSHOTS_PATH,
    window: int = 5,
) -> list[dict[str, Any]]:
    """Compare latest snapshot against trailing-window median.

    Returns a list of regression findings (one per deviating metric). Empty
    list = no regression. `window` is the number of *prior* runs used to
    compute the baseline median (the latest snapshot is excluded from the
    baseline). Needs at least 3 prior runs to trigger.
    """
    snaps = load_snapshots(snapshots_path)
    if len(snaps) < 4:
        return []
    latest = snaps[-1]
    # Outlier runs (manually re-triggered, mid-run server restart, etc.)
    # would distort the median upward — exclude from baseline. The latest
    # snapshot itself is also evaluated as an outlier; if it is one, skip
    # regression detection (no signal to report from a broken run).
    if latest.get("is_outlier"):
        return []
    history = [s for s in snaps[:-1] if not s.get("is_outlier")]
    baseline = history[-window:] if len(history) > window else history
    if len(baseline) < 3:
        return []
    findings: list[dict[str, Any]] = []
    for metric, threshold in _REGRESSION_METRICS_HIGHER_IS_WORSE.items():
        values = [s.get(metric, 0) or 0 for s in baseline]
        median = statistics.median(values)
        latest_val = latest.get(metric, 0) or 0
        if median == 0:
            # Spike from zero baseline → flag if latest is itself non-zero
            if latest_val > 0 and latest_val >= 2:
                findings.append({
                    "metric": metric, "latest": latest_val, "median": 0,
                    "delta_pct": None, "direction": "regression",
                })
            continue
        if latest_val > median * (1 + threshold):
            findings.append({
                "metric": metric, "latest": latest_val, "median": median,
                "delta_pct": round(100 * (latest_val - median) / median, 1),
                "direction": "regression",
            })
    for metric, threshold in _REGRESSION_METRICS_LOWER_IS_WORSE.items():
        values = [s.get(metric, 0) or 0 for s in baseline]
        median = statistics.median(values)
        latest_val = latest.get(metric, 0) or 0
        if median == 0:
            continue
        if latest_val < median * (1 - threshold):
            findings.append({
                "metric": metric, "latest": latest_val, "median": median,
                "delta_pct": round(100 * (latest_val - median) / median, 1),
                "direction": "regression",
            })
    return findings


def format_trend_table(snapshots: list[dict[str, Any]]) -> str:
    """Format snapshots as a compact ASCII table for CLI consumption."""
    if not snapshots:
        return "(keine Snapshots)"
    headers = [
        ("run", lambda s: (s.get("run_id") or "")[5:][:30]),
        ("cost", lambda s: f"${(s.get('total_cost_usd') or 0):.2f}"),
        ("calls", lambda s: str(s.get("calls") or 0)),
        ("v2", lambda s: str(s.get("writer_round_2_count") or 0)),
        ("fc-veto", lambda s: str(s.get("factcheck_iter_vetos") or 0)),
        ("cons", lambda s: str(s.get("consistency_findings") or 0)),
        ("cache%", lambda s: f"{int(100 * (s.get('cache_hit_rate') or 0))}"),
        ("summary-w", lambda s: str(s.get("summary_word_count") or 0)),
        ("diff-rows", lambda s: str(s.get("diff_row_count") or 0)),
        ("fit-warn", lambda s: "y" if s.get("profile_fit_warned") else ""),
        ("clichés/100w", lambda s: f"{(s.get('cliche_density_per_100_words') or 0):.2f}"),
        ("bullet-σ", lambda s: f"{((s.get('bullet_length_stats') or {}).get('stddev') or 0):.1f}"),
        ("outlier", lambda s: "★" if s.get("is_outlier") else ""),
    ]
    rows = [[label for label, _ in headers]] + [[fn(s) for _, fn in headers] for s in snapshots]
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    out_lines = []
    for ri, row in enumerate(rows):
        out_lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)))
        if ri == 0:
            out_lines.append("  ".join("-" * w for w in widths))
    return "\n".join(out_lines)
