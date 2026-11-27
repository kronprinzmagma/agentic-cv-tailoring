"""Quellen für den Sektions-Faktencheck, die der kompakte Beleg-Index nicht enthält.

Auszählung 2026-11-25: 41 von 119 Beanstandungen betrafen Aussagen mit Quelle,
die der Faktencheck nicht sah. Er bekommt darum den Standard-CV (ohne
Kontaktblock), die Portfolio-Belege, die Analyse oder Writer im Lauf gesehen
haben, und die Antworten des Laufs. Keine Voll-Index-Injektion.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from cv_tailor.ausgangssperre import ohne_geburtsdatum, ohne_kontaktblock
from cv_tailor.beleg_index import load_beleg_index
from cv_tailor.clarifications import (
    CLARIFICATIONS_PATH,
    EXCLUDED_BUDGET,
    EXCLUDED_LIMIT,
    select_clarifications,
)
from cv_tailor.experience_activation import PORTFOLIO_GAP_SECTION

STANDARD_CV_PATH = Path("data/standard_cv.md")
# Klärungen, die am Zeichenbudget des gecachten Prefix scheiterten, kommen nur
# dazu, wenn sie zum geprüften Text passen.
EXTRA_CLARIFICATION_CHARS = 14_000
MAX_CHUNK_CHARS = 1_500
CHUNKS_PER_LINE = 4
MIN_SHARED_WORDS = 2
_BELEG_ID_RE = re.compile(r"BELG-\d+")
_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöü]{6,}")


def _demote(text: str) -> str:
    """Überschriften zwei Ebenen tiefer, damit nur der Entwurf Entwurf ist."""
    return re.sub(r"^(#{1,4}) ", r"##\1 ", text, flags=re.MULTILINE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def standard_cv_block(path: Path | None = None) -> str:
    path = path or STANDARD_CV_PATH
    text = _read(path)
    if not text:
        return ""
    return "## Standard-CV (Beleg-Quelle, nicht zu prüfen)\n" + _demote(ohne_kontaktblock(text))


def portfolio_block(run_dir: Path, index_path: Path) -> str:
    """Portfolio-Belege, die im Lauf aktiviert oder zitiert sind; nie Wachstumskanten."""
    seen = set()
    for name in ("01_analyse.md", "_experience_activation.md"):
        seen |= set(_BELEG_ID_RE.findall(_read(run_dir / name)))
    lines = [
        f"{e['id']} [{e['typ']}] {ohne_geburtsdatum(e['snippet'])[:400]} ({e['quelle_position']})"
        for e in load_beleg_index(index_path).get("entries", [])
        if e["id"] in seen
        and e.get("quelle_typ") == "portfolio"
        and e.get("section") != PORTFOLIO_GAP_SECTION
    ]
    if not lines:
        return ""
    return "## Portfolio-Belege dieses Laufs\n" + "\n".join(lines)


def run_answers_block(run_dir: Path) -> str:
    text = _read(run_dir / "02_antworten.md")
    if not text:
        return ""
    return "## Zusatzkontext aus früheren Klärungen: Antworten dieses Laufs\n" + _demote(text)


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text)}


def extra_clarifications_block(
    section_text: str, gating_context: str | None, path: Path | None = None
) -> str:
    """Passende Absätze aus themenrelevanten Klärungen ausserhalb des Budgets.

    Je Entwurfszeile die Absätze mit der grössten Wortdeckung; seltene Wörter
    zählen mehr. Das Topic-Gating gegen die Anzeige bleibt: Klärungen ohne
    Themenbezug kommen nie dazu.
    """
    if not gating_context:
        return ""
    selection = select_clarifications(path or CLARIFICATIONS_PATH, current_context=gating_context)
    chunks: list[str] = []
    for idx, reason in selection.excluded:
        if reason in (EXCLUDED_BUDGET, EXCLUDED_LIMIT):
            answer = selection.entries[idx].get("answers_markdown", "")
            chunks += [c.strip() for c in re.split(r"\n\s*\n|\n(?=#{1,4} )", answer) if c.strip()]
    chunks = list(dict.fromkeys(c[:MAX_CHUNK_CHARS] for c in chunks))
    chunk_words = [_words(c) for c in chunks]
    frequency = Counter(w for words in chunk_words for w in words)
    best: dict[int, float] = {}
    for line in section_text.splitlines():
        words = _words(line)
        scored = []
        for i, candidate in enumerate(chunk_words):
            shared = words & candidate
            if len(shared) >= MIN_SHARED_WORDS:
                scored.append((sum(1 / frequency[w] for w in shared), i))
        for score, i in sorted(scored, reverse=True)[:CHUNKS_PER_LINE]:
            best[i] = max(score, best.get(i, 0.0))
    chosen: list[int] = []
    used = 0
    for i in sorted(best, key=lambda k: (-best[k], k)):
        if used + len(chunks[i]) <= EXTRA_CLARIFICATION_CHARS:
            chosen.append(i)
            used += len(chunks[i])
    if not chosen:
        return ""
    parts = [_demote(chunks[i]) for i in sorted(chosen)]
    return "## Zusatzkontext aus früheren Klärungen: zum Abschnitt passend\n" + "\n\n".join(parts)


def static_blocks(run_dir: Path, index_path: Path) -> list[str]:
    """Blöcke für den gecachten Prefix: im Lauf unverändert."""
    blocks = (standard_cv_block(), portfolio_block(run_dir, index_path), run_answers_block(run_dir))
    return [b for b in blocks if b]
