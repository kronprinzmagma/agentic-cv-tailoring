"""Translator agent: writes 04_final_en.md when the job posting is primarily English."""
from __future__ import annotations

import json
import re
from pathlib import Path

from cv_tailor.beleg_index import format_beleg_index_compact
from cv_tailor.llm import call_llm
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

TRANSLATOR_PROMPT_PATH = Path("prompts/translator.md")
BELEG_INDEX_PATH = Path("data/beleg_index.json")
MAX_TOKENS = 4096
PLACEHOLDER_MARKER = "USER FÜLLT"

# WR-06: "team" appears in both languages — removed from ENGLISH_MARKERS to avoid
# false positives in bilingual Swiss-German/English postings. The ascii_ratio
# secondary guard (>0.92) provides additional protection against mis-detection.
# Known limitation: heavy repetition of common English function words (and, the,
# you, will, etc.) can still flip the result in mixed-language postings.
# Content-specific markers (high signal, low collision with German postings).
# WR-02: function words like "we", "are", "the" are out — they recurred in
# Swiss-German postings full of English buzzwords and caused false positives.
ENGLISH_MARKERS = {
    "responsibilities", "requirements", "qualifications", "preferred",
    "experience", "candidate", "you will",
    "we are looking", "we are seeking", "the role", "your role",
    "english", "fluent", "based in", "join us", "join our",
    "what we offer", "about us", "what you bring",
}
# Content markers — always active. Low collision with primarily-English postings
# even when sprinkled with German UI chrome ("Standort:", "Über X").
GERMAN_CONTENT_MARKERS = {
    "aufgaben", "anforderungen", "qualifikationen", "erforderlich",
    "erfahrung", "kenntnisse", "kandidat", "kandidatin",
    "wir suchen", "wir bieten", "die rolle", "deine rolle", "ihre rolle",
    "bewerbung", "deutsch", "fließend", "verantwortlich für",
    "über uns", "was wir bieten", "was du mitbringst",
}
# WR-08: High-density function words. Active ONLY for short postings (<200
# words) where content markers may not fire. In long English postings,
# stopwords like "und", "das", "der" recurred from German UI chrome
# (e.g. "**Standort:** Zürich (Hybrid, Remote-Tage möglich)", "## Über X")
# and pushed the en_ratio below threshold despite a >99% ASCII body.
GERMAN_STOPWORDS = {
    "der", "die", "das", "und", "mit", "für", "von", "bei", "nicht",
}
GERMAN_MARKERS = GERMAN_CONTENT_MARKERS | GERMAN_STOPWORDS  # kept for backcompat


def is_primarily_english(text: str) -> bool:
    """Rule-based language detector for job postings.

    Decision uses a ratio rather than a raw difference so that a short
    English buzzword fragment in a long German posting doesn't tip the
    balance. Empty / very short postings raise a WARNING — silent skip
    on a corrupted posting would let a German CV ship to an English role.
    """
    # IN-06: empty / very short posting → log and bail (no translator).
    if not text or len(text.strip()) < 50:
        log.warning("is_primarily_english.short_posting", chars=len(text or ""))
        return False
    lowered = text.lower()
    word_count = len(text.split())
    # Stopwords only activate as a fallback for SHORT postings (<200 words),
    # where content markers may not fire. In long postings, German UI chrome
    # ("Standort:", "Über X", "Remote-Tage möglich") inevitably leaks stopwords
    # into an otherwise English body and biases the ratio toward DE.
    german_set = GERMAN_CONTENT_MARKERS
    if word_count < 200:
        german_set = german_set | GERMAN_STOPWORDS
    english_hits = sum(1 for marker in ENGLISH_MARKERS if marker in lowered)
    german_hits = sum(1 for marker in german_set if marker in lowered)
    total_hits = english_hits + german_hits
    # Need a minimum signal level; otherwise the posting is too generic to classify
    if total_hits < 3:
        log.info("is_primarily_english.low_signal", english=english_hits, german=german_hits)
        return False
    en_ratio = english_hits / total_hits
    ascii_ratio = sum(1 for char in text if ord(char) < 128) / max(len(text), 1)
    # Require strong English dominance AND high ASCII purity (German postings
    # with umlauts drop the ratio below 0.92; Swiss-German often drops it too).
    # Boundary inclusive (>=): 0.7 exactly + >99% ASCII is unambiguously English
    # (the German-side hits at the boundary tend to be UI chrome, not content).
    return en_ratio >= 0.7 and ascii_ratio > 0.94


def run_translator(
    ctx: RunContext,
    final_cv_path: Path | None = None,
    beleg_index_path: Path = BELEG_INDEX_PATH,
    prompt_path: Path = TRANSLATOR_PROMPT_PATH,
) -> Path | None:
    """Translate 04_final_de.md to 04_final_en.md if the posting is English."""
    posting_path = ctx.run_dir / "00_stellenanzeige.md"
    if not posting_path.exists():
        raise FileNotFoundError(f"Stellenanzeige not found: {posting_path}")
    posting_text = posting_path.read_text(encoding="utf-8")
    if not is_primarily_english(posting_text):
        write_run_log_entry(ctx.run_dir, "translator", "Übersetzung übersprungen (Stellenanzeige nicht primär englisch)")
        log.info("translator.skipped", run_id=ctx.run_id)
        return None

    if not prompt_path.exists():
        raise FileNotFoundError(f"Translator prompt not found: {prompt_path}")
    system_prompt = prompt_path.read_text(encoding="utf-8")
    if PLACEHOLDER_MARKER in system_prompt:
        log.warning("run_translator.placeholder_prompt")

    if final_cv_path is None:
        final_cv_path = ctx.run_dir / "04_final_de.md"
    if not final_cv_path.exists():
        raise FileNotFoundError(f"Final DE CV not found: {final_cv_path}")
    final_cv_text = final_cv_path.read_text(encoding="utf-8")

    if beleg_index_path.exists():
        beleg_index_compact = format_beleg_index_compact(
            json.loads(beleg_index_path.read_text(encoding="utf-8"))
        )
    else:
        beleg_index_compact = "(Beleg-Index nicht gefunden)"

    user_msg = (
        f"## Stellenanzeige\n{posting_text}\n\n"
        f"## Finaler CV (DE)\n{final_cv_text}\n\n"
        f"## Vokabular-Anker aus dem Beleg-Index\n{beleg_index_compact}"
    )

    log.info("translator.start", run_id=ctx.run_id)
    content = call_llm(
        agent="translator",
        phase="phase6_translation",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS,
        snippet_text=final_cv_text[:500],
    ).strip()

    out_path = ctx.run_dir / "04_final_en.md"
    out_path.write_text(content + "\n", encoding="utf-8")
    write_run_log_entry(ctx.run_dir, "translator", f"04_final_en.md geschrieben ({len(content)} Zeichen)")
    log.info("translator.done", run_id=ctx.run_id, chars=len(content))
    return out_path
