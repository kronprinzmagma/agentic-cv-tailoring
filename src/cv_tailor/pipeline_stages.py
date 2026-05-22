"""Shared pipeline-stage helpers.

Extracted to remove duplication between `cli.py` and `web.py`, both of which
ran the same sequence of post-writer stages with subtle drift (Web had
quality-snapshot capture, CLI didn't; Web had translated_path tracking,
CLI re-derived it). These helpers are now the single source of truth.

Each stage:
- Takes a `RunContext` and an optional `progress_cb: Callable[[str], None]`
- Returns a small dict with stage-specific output (file paths, flags)
- Never imports CLI or Web modules — pure pipeline plumbing

The two callers (CLI + Web) still own the orchestration: they decide
which stages to call in which order, and how to report progress to their
respective UIs. The bodies of those stages now live here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from cv_tailor.cv_filename import write_friendly_copy
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry
from cv_tailor.quality_snapshot import capture_run_snapshot

log = get_logger(__name__)

ProgressCb = Optional[Callable[[str], None]]


def _emit(progress_cb: ProgressCb, message: str) -> None:
    if progress_cb is not None:
        progress_cb(message)


def run_diff_stage(ctx: RunContext, progress_cb: ProgressCb = None) -> Path:
    """Run the diff agent and return the 05_diff.md path."""
    from cv_tailor.agents.diff_agent import run_diff_agent

    _emit(progress_cb, "Diff-Agent läuft …")
    diff_path = run_diff_agent(ctx)
    write_run_log_entry(ctx.run_dir, "pipeline", "Diff-Stage abgeschlossen")
    return diff_path


def run_keyword_marker_stage(ctx: RunContext, progress_cb: ProgressCb = None) -> None:
    """Run the post-hoc keyword marker on 04_final_de.md (and EN if present)."""
    from cv_tailor.agents.keyword_marker import run_keyword_marker

    _emit(progress_cb, "Keyword-Marker läuft …")
    run_keyword_marker(ctx)
    write_run_log_entry(ctx.run_dir, "pipeline", "Keyword-Marker-Stage abgeschlossen")


def run_translator_stage(ctx: RunContext, progress_cb: ProgressCb = None) -> Path | None:
    """Run the translator; returns 04_final_en.md path or None when skipped."""
    from cv_tailor.agents.translator import run_translator

    _emit(progress_cb, "Translator prüft Sprache …")
    translated_path = run_translator(ctx)
    if translated_path is not None:
        _emit(progress_cb, f"  04_final_en.md geschrieben ({translated_path.stat().st_size} Bytes)")
    else:
        _emit(progress_cb, "  Übersetzung übersprungen (Stellenanzeige nicht primär englisch)")
    write_run_log_entry(ctx.run_dir, "pipeline", "Translator-Stage abgeschlossen")
    return translated_path


def write_friendly_copies(
    ctx: RunContext,
    translated_path: Path | None,
    progress_cb: ProgressCb = None,
) -> dict[str, Path | None]:
    """Write recruiter-friendly named copies alongside the canonical 04_final_*.md files."""
    posting_in_run = ctx.run_dir / "00_stellenanzeige.md"
    friendly_de = write_friendly_copy(ctx.run_dir / "04_final_de.md", posting_in_run, "de")
    if friendly_de is not None:
        _emit(progress_cb, f"  ↳ {friendly_de.name}")
    friendly_en: Path | None = None
    if translated_path is not None:
        friendly_en = write_friendly_copy(ctx.run_dir / "04_final_en.md", posting_in_run, "en")
        if friendly_en is not None:
            _emit(progress_cb, f"  ↳ {friendly_en.name}")
    return {"de": friendly_de, "en": friendly_en}


def capture_snapshot_safely(ctx: RunContext) -> None:
    """Capture a quality snapshot; never raises out of the pipeline.

    Snapshot failure must not break run completion — the rest of the
    pipeline output is already on disk. Errors are recorded in the run log.
    """
    try:
        capture_run_snapshot(ctx.run_dir)
    except Exception as exc:  # noqa: BLE001
        write_run_log_entry(
            ctx.run_dir, "quality_snapshot",
            f"Snapshot-Capture fehlgeschlagen: {exc}",
        )


def run_postprocess_stages(
    ctx: RunContext,
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    """Run Diff → Keyword-Marker → Translator → Friendly-Copy → Snapshot.

    Returns a dict describing the stage outputs so the caller can wire
    them into its UI / status state. This is the post-writer sequence
    that both CLI and Web ran nearly identically.
    """
    diff_path = run_diff_stage(ctx, progress_cb)
    run_keyword_marker_stage(ctx, progress_cb)
    translated_path = run_translator_stage(ctx, progress_cb)
    friendly = write_friendly_copies(ctx, translated_path, progress_cb)
    capture_snapshot_safely(ctx)
    return {
        "diff_path": diff_path,
        "translated_path": translated_path,
        "friendly_de": friendly["de"],
        "friendly_en": friendly["en"],
    }
