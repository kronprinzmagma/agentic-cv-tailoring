"""Rework metrics: how much of the pipeline's CV survives until handover.

Review 2026-10-23: quality-trend measured cost, rounds and cache, but not
what matters — how much Alex (or Claude on his behalf) still rewrites after a
run. In September 80–90 % of the pipeline text reached the shipped CV
unchanged, by late October ~30 %, and the final CVs were 100–150 words
longer. Nobody saw it, because nothing kept the raw output.

Two steps:
1. `save_pipeline_raw()` freezes the pipeline's own result at the end of the
   post-processing stages (after keyword marker and translator):
   `_pipeline_raw_de.md` / `_pipeline_raw_en.md`. Never overwritten — a later
   `post-process` re-run after hand edits must not move the baseline.
2. `record_rework()` compares the raw file with the CV as it is handed over
   (the freshest of canonical and recruiter-friendly copy, same rule as the
   PDF renderer) and writes the numbers into the run's quality snapshot.
   Called by the PDF renderer (rendering = handover in the go workflow) and
   by `cv-tailor rework <run-id>`.

Older runs have no raw file. Fallbacks, marked in `rework_raw_source`:
`04_final_de.md.bak.writer` (Claude's manual-edit backup, exact) or the last
accepted writer iterations (approximate: before trim and keyword marker).
"""
from __future__ import annotations

import difflib
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from cv_tailor.cv_filename import CV_AUTHOR_TOKEN

log = structlog.get_logger()

RAW_FILENAMES = {"de": "_pipeline_raw_de.md", "en": "_pipeline_raw_en.md"}
SECTION_KEYS = ("management_summary", "schluesselkompetenzen", "berufserfahrung")
_SECTION_TITLES = {
    "management_summary": ("Management Summary",),
    "schluesselkompetenzen": ("Schlüsselkompetenzen", "Key Competencies"),
    "berufserfahrung": ("Berufserfahrung", "Professional Experience"),
}


def save_pipeline_raw(run_dir: Path) -> list[Path]:
    """Freeze 04_final_<lang>.md as the pipeline's raw output (first call wins)."""
    written: list[Path] = []
    for lang, name in RAW_FILENAMES.items():
        source = run_dir / f"04_final_{lang}.md"
        target = run_dir / name
        if source.is_file() and not target.exists():
            shutil.copyfile(source, target)
            written.append(target)
    return written


def _body(text: str) -> str:
    """The three generated sections only: no title, metadata or tail sections."""
    text = text.replace("**", "")
    parts: list[str] = []
    for titles in _SECTION_TITLES.values():
        for title in titles:
            m = re.search(rf"^##\s+{re.escape(title)}\s*$", text, re.M)
            if m:
                rest = text[m.end():]
                n = re.search(r"^##\s+", rest, re.M)
                parts.append(rest[: n.start()] if n else rest)
                break
    return "\n".join(parts)


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text)


def _iterations_text(run_dir: Path) -> str:
    iter_dir = run_dir / "03_iterationen"
    titles = {
        "management_summary": "Management Summary",
        "schluesselkompetenzen": "Schlüsselkompetenzen",
        "berufserfahrung": "Berufserfahrung",
    }
    parts = []
    for key in SECTION_KEYS:
        versions = sorted(
            p for p in iter_dir.glob(f"{key}_v*.md") if re.fullmatch(rf"{key}_v\d+\.md", p.name)
        )
        if versions:
            body = re.sub(r"^#{1,2}\s+.*\n", "", versions[-1].read_text(encoding="utf-8"), count=1)
            parts.append(f"## {titles[key]}\n\n{body}")
    return "\n\n".join(parts)


def resolve_raw(run_dir: Path, lang: str = "de") -> tuple[str, str]:
    """(raw text, source label); ('', 'none') when nothing is available."""
    raw = run_dir / RAW_FILENAMES[lang]
    if raw.is_file():
        return raw.read_text(encoding="utf-8"), "raw"
    backup = run_dir / f"04_final_{lang}.md.bak.writer"
    if backup.is_file():
        return backup.read_text(encoding="utf-8"), "bak.writer"
    if lang == "de" and (run_dir / "03_iterationen").is_dir():
        text = _iterations_text(run_dir)
        if text:
            return text, "iterationen (approx.)"
    return "", "none"


def resolve_final(run_dir: Path, lang: str = "de") -> Path | None:
    """Freshest of canonical and recruiter-friendly copy — the renderer's rule."""
    canonical = run_dir / f"04_final_{lang}.md"
    candidates = [canonical] if canonical.is_file() else []
    candidates += [
        p for p in run_dir.glob(f"{CV_AUTHOR_TOKEN}-*.md")
        if p.is_file() and (lang == "en") == p.stem.endswith("_EN")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def retire_outputs(run_dir: Path, lang: str, *, markdown: bool = False) -> list[Path]:
    """Rename a language's output artifacts to `<name>.stale` so nothing serves them.

    Web, renderer and download pick files by exact name or `*.md` / `*.pdf`
    globs. After a failed re-translation or re-render the previous output
    stayed active and looked current (review 2026-10-23, R05/R07). PDFs are
    always retired; the Markdown too with `markdown=True` (outdated EN after
    a rejected re-translation of a changed DE CV). Returns the new paths.
    """
    suffix_ok = (lambda p: p.stem.endswith("_EN")) if lang == "en" else (
        lambda p: not p.stem.endswith("_EN")
    )
    targets = [run_dir / f"04_final_{lang}.pdf"]
    targets += [p for p in run_dir.glob(f"{CV_AUTHOR_TOKEN}-*.pdf") if suffix_ok(p)]
    if markdown:
        targets.append(run_dir / f"04_final_{lang}.md")
        targets += [p for p in run_dir.glob(f"{CV_AUTHOR_TOKEN}-*.md") if suffix_ok(p)]
    retired: list[Path] = []
    for path in targets:
        if path.is_file():
            stale = path.with_name(path.name + ".stale")
            path.replace(stale)
            retired.append(stale)
    if retired:
        log.info("rework.outputs_retired", run_dir=str(run_dir), lang=lang, files=len(retired))
    return retired


def measure_rework(run_dir: Path) -> dict[str, Any] | None:
    """Compare raw pipeline output with the handover CV; None without both."""
    lang = "en" if (run_dir / "04_final_en.md").is_file() else "de"
    final_path = resolve_final(run_dir, lang)
    raw_text, raw_source = resolve_raw(run_dir, lang)
    if final_path is None or not raw_text:
        return None
    raw_words = _words(_body(raw_text))
    final_words = _words(_body(final_path.read_text(encoding="utf-8")))
    if not raw_words or not final_words:
        return None
    matcher = difflib.SequenceMatcher(None, raw_words, final_words, autojunk=False)
    kept = sum(block.size for block in matcher.get_matching_blocks())

    from cv_tailor.fact_limits import check_text, load_fact_limits

    limits = load_fact_limits()
    return {
        "rework_language": lang,
        "rework_kept_pct": round(100 * kept / len(final_words), 1),
        "rework_word_delta": len(final_words) - len(raw_words),
        "rework_raw_words": len(raw_words),
        "rework_final_words": len(final_words),
        "rework_raw_source": raw_source,
        "fact_limits_raw": len(check_text(raw_text, limits)) if limits else None,
        "fact_limits_final": (
            len(check_text(final_path.read_text(encoding="utf-8"), limits)) if limits else None
        ),
        "rework_measured_at": datetime.now(UTC).isoformat(),
    }


def record_rework(run_dir: Path, snapshots_path: Path | None = None) -> dict[str, Any] | None:
    """Measure and merge the rework fields into the run's quality snapshot."""
    from cv_tailor.quality_snapshot import SNAPSHOTS_PATH, update_snapshot_fields

    metrics = measure_rework(run_dir)
    if metrics is None:
        return None
    update_snapshot_fields(run_dir.name, metrics, snapshots_path or SNAPSHOTS_PATH)
    log.info("rework.recorded", run_id=run_dir.name, kept_pct=metrics["rework_kept_pct"])
    return metrics
