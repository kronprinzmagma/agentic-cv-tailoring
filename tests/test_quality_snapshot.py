"""Tests for the deterministic quality-snapshot metrics."""
import json
from pathlib import Path

import pytest

from cv_tailor.quality_snapshot import (
    _bullet_length_stats,
    _cliche_density_per_100_words,
    _count_consistency_findings,
    _count_factcheck_vetos,
    _count_v2_rounds,
    _count_words,
    _diff_row_count,
    _summary_word_count,
    detect_regressions,
    load_snapshots,
)


# ── word/text helpers ──────────────────────────────────────────────────────

def test_count_words_handles_umlauts():
    """Umlauts must count as letters, not word breaks."""
    assert _count_words("Schlüssel über fünf Wörter") == 4
    assert _count_words("Anforderungen Fähigkeiten") == 2


def test_count_words_empty():
    assert _count_words("") == 0
    assert _count_words("   \n  ") == 0


# ── Cliché density ─────────────────────────────────────────────────────────

def test_cliche_density_clean_text():
    txt = "Plattform-Ownership HealthAppConnect — Backlog und Release-Stabilität in Gesundheits-SaaS."
    assert _cliche_density_per_100_words(txt) == 0.0


def test_cliche_density_detects_filler():
    txt = "Ich bin strategisch und ganzheitlich proaktiv mit nachhaltig konsequenter Ausrichtung."
    # 10 words, 5 clichés
    assert _cliche_density_per_100_words(txt) > 30.0


def test_cliche_density_empty_returns_zero():
    assert _cliche_density_per_100_words("") == 0.0


def test_cliche_density_handles_english():
    """EN translator output triggers the EN variants too."""
    txt = "I am a strategic and holistic product manager passionate about innovation."
    assert _cliche_density_per_100_words(txt) > 0.0


# ── Bullet length stats ────────────────────────────────────────────────────

def test_bullet_length_stats_empty_doc():
    s = _bullet_length_stats("")
    assert s["count"] == 0
    assert s["mean"] == 0.0


def test_bullet_length_stats_extracts_dash_bullets():
    text = (
        "## Some heading\n"
        "- One two three\n"
        "- Four five six seven\n"
        "- Eight\n"
    )
    s = _bullet_length_stats(text)
    assert s["count"] == 3
    assert s["max"] == 4
    assert s["mean"] == round((3 + 4 + 1) / 3, 1)


def test_bullet_length_stats_ignores_non_bullet_lines():
    text = "Some paragraph without bullet.\n- Counted bullet here.\n"
    s = _bullet_length_stats(text)
    assert s["count"] == 1


# ── Summary word count ─────────────────────────────────────────────────────

def test_summary_word_count_extracts_management_summary_block():
    cv = (
        "# CV\n"
        "## Management Summary\n"
        "Ich bin ein Product Manager mit zwanzig Jahren Erfahrung.\n"
        "\n"
        "## Berufserfahrung\n"
        "### 2023 | X\n"
        "- bullet\n"
    )
    assert _summary_word_count(cv) >= 9


def test_summary_word_count_missing_section():
    assert _summary_word_count("# Foo\n\n## Berufserfahrung\n") == 0


# ── Diff row count ─────────────────────────────────────────────────────────

def test_diff_row_count_skips_header_and_separator():
    diff = (
        "# Diff\n"
        "\n"
        "| Original | Final |\n"
        "|---|---|\n"
        "| line 1 | line 1 |\n"
        "| line 2 | line 2 |\n"
        "| line 3 | line 3 |\n"
    )
    assert _diff_row_count(diff) == 3


def test_diff_row_count_no_table_in_text():
    assert _diff_row_count("# Headline\n\nOnly prose, no table here.") == 0


# ── Consistency findings detector (Q4 fix) ─────────────────────────────────

def test_consistency_findings_counts_only_numbered_drift_lines(tmp_path):
    """Q4 regression: the old detector matched the heading itself and gave
    constant 2 per run. New detector only counts numbered `Header-Drift für`."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "x_v1_consistency.md").write_text(
        "# Konsistenz-Check: Drift gegen Standard-CV erkannt\n\n"
        "1. Header-Drift für 'healthapp': ... weicht ab.\n"
        "2. Header-Drift für 'mediacorp': ... weicht ab.\n",
        encoding="utf-8",
    )
    (iter_dir / "y_v1_consistency.md").write_text(
        "Keine strukturelle Drift gefunden.\n",
        encoding="utf-8",
    )
    assert _count_consistency_findings(iter_dir) == 2  # only 2 real findings


def test_consistency_findings_zero_on_clean_runs(tmp_path):
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "x_v1_consistency.md").write_text(
        "Keine strukturelle Drift gefunden.\n", encoding="utf-8"
    )
    assert _count_consistency_findings(iter_dir) == 0


# ── Factcheck vetos detector ───────────────────────────────────────────────

def test_factcheck_vetos_treats_clean_signals_as_zero(tmp_path):
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "x_v1_factcheck.md").write_text(
        "Keine Drift gefunden. Alles belegt.", encoding="utf-8"
    )
    (iter_dir / "y_v2_factcheck.md").write_text(
        "Eine Behauptung ist nicht belegt — BELG-XYZ fehlt für 'Managing Director'.",
        encoding="utf-8",
    )
    # First is clean, second has real veto signal
    assert _count_factcheck_vetos(iter_dir) == 1


# ── v2-rounds counter ──────────────────────────────────────────────────────

def test_count_v2_rounds_picks_max_version_per_section(tmp_path):
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    for f in [
        "management_summary_v1.md", "management_summary_v2.md",
        "schluesselkompetenzen_v1.md",
        "berufserfahrung_v1.md", "berufserfahrung_v2.md", "berufserfahrung_v3.md",
        # Decoy: review/factcheck files must be ignored by the v-counter
        "management_summary_v1_review_coach.md",
        "berufserfahrung_v1_factcheck.md",
    ]:
        (iter_dir / f).write_text("", encoding="utf-8")
    rounds = _count_v2_rounds(iter_dir)
    assert rounds == {
        "management_summary": 2,
        "schluesselkompetenzen": 1,
        "berufserfahrung": 3,
    }


# ── Regression detector ────────────────────────────────────────────────────

def _write_snaps(path: Path, snapshots: list[dict]):
    path.write_text("\n".join(json.dumps(s) for s in snapshots) + "\n", encoding="utf-8")


def test_detect_regressions_returns_empty_without_baseline(tmp_path):
    """Need at least 4 snapshots to even start considering regression."""
    p = tmp_path / "snaps.jsonl"
    _write_snaps(p, [
        {"run_id": "a", "writer_round_2_count": 1, "total_cost_usd": 1.0,
         "cache_hit_rate": 0.5, "calls": 10},
        {"run_id": "b", "writer_round_2_count": 1, "total_cost_usd": 1.0,
         "cache_hit_rate": 0.5, "calls": 10},
    ])
    assert detect_regressions(p) == []


def test_detect_regressions_flags_clear_spike(tmp_path):
    p = tmp_path / "snaps.jsonl"
    baseline = [{"run_id": f"r{i}", "writer_round_2_count": 1,
                 "total_cost_usd": 1.0, "calls": 10,
                 "cache_hit_rate": 0.5} for i in range(5)]
    spike = {"run_id": "spike", "writer_round_2_count": 3,  # 200% above median
             "total_cost_usd": 1.0, "calls": 10, "cache_hit_rate": 0.5}
    _write_snaps(p, baseline + [spike])
    findings = detect_regressions(p)
    metrics = {f["metric"] for f in findings}
    assert "writer_round_2_count" in metrics


def test_detect_regressions_ignores_outliers_in_baseline(tmp_path):
    """The outlier flag must exclude that snapshot from the median baseline."""
    p = tmp_path / "snaps.jsonl"
    baseline = [{"run_id": f"r{i}", "writer_round_2_count": 1,
                 "total_cost_usd": 1.0, "calls": 10,
                 "cache_hit_rate": 0.5} for i in range(4)]
    # Inject a wildly inflated outlier — should be ignored by detector
    outlier = {"run_id": "outlier", "writer_round_2_count": 9,
               "total_cost_usd": 5.0, "calls": 100,
               "cache_hit_rate": 0.5, "is_outlier": True}
    latest = {"run_id": "latest", "writer_round_2_count": 1,
              "total_cost_usd": 1.05, "calls": 11,
              "cache_hit_rate": 0.5}
    _write_snaps(p, baseline + [outlier] + [latest])
    findings = detect_regressions(p)
    assert findings == []  # baseline median = 1, latest = 1 → no regression


def test_bullet_length_stats_counts_paragraph_style_berufserfahrung():
    """Writer produces paragraph-style entries (no `- ` prefix) since ~2026-06;
    the metric silently dropped to count=0 on every new run."""
    cv = (
        "## Management Summary\n\n"
        "Ein Absatz Prosa, der nicht als Bullet zählen darf.\n\n"
        "## Berufserfahrung\n\n"
        "### 2023–2025 | HealthApp – Senior Product Owner\n\n"
        "Backlog-Ownership für HealthAppConnect mit datenbasierter Priorisierung.\n\n"
        "Aufbau und Leitung der KI-Fachgruppe.\n\n"
        "---\n\n"
        "### 2015 | MediaHoldingCo – Head of Products and Innovation\n\n"
        "Gesamtverantwortung für das digitale Portfolio der Blick Gruppe.\n"
    )
    s = _bullet_length_stats(cv)
    assert s["count"] == 3
    assert s["mean"] > 0
