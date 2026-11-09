"""Profile-Fit-Check: parse the analyst's Anforderungsabgleich table and
surface unmet requirements before the writer-loop starts.

Why this exists
---------------
A poorly-fitting role can only be "tailored" by stretching claims. The
writer-loop has no choice but to fill in what isn't there — which is
exactly the overreach pattern we keep fighting. This module gives the
user an early stop sign: if the analyst already classified critical
requirements as LÜCKE, ask before paying for a full optimisation run.

Deterministic, no LLM call. Reads the table from `01_analyse.md` and
returns a list of unmet requirements with their analyst comment.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FitGap:
    requirement: str
    status: str  # "LÜCKE" | "SCHWACH"
    comment: str
    severity: str  # "critical" | "soft"


# Words in the analyst's Kommentar that strongly suggest the requirement is
# optional / nice-to-have. When present, we downgrade severity rather than
# treating the row as a hard mismatch.
_NICE_TO_HAVE_MARKERS = (
    "nice to have",
    "nice-to-have",
    "wünschenswert",
    "von vorteil",
    "kann-anforderung",
    "kann anforderung",
    "plus",
    "bonus",
    "optional",
    "idealerweise",
    "ideally",
)

# Words in the requirement text that emphasise strength — Alex explicitly
# called this out as a key signal ("Strong understanding of ..." being a
# LÜCKE is a strong fit-warning).
_STRENGTH_QUALIFIERS = (
    "strong ",
    "deep ",
    "expert",
    "proven",
    "extensive",
    "stark",
    "tief",
    "ausgeprägt",
    "umfassend",
    "fundiert",
    "mehrjährig",
    "established",
)


def _row_is_nice_to_have(comment: str) -> bool:
    low = comment.lower()
    return any(m in low for m in _NICE_TO_HAVE_MARKERS)


def _row_has_strength_qualifier(requirement: str) -> bool:
    low = requirement.lower()
    return any(q in low for q in _STRENGTH_QUALIFIERS)


def parse_anforderungsabgleich(analyse_text: str) -> list[tuple[str, str, str, str]]:
    """Parse the Anforderungsabgleich table from an analyst output.

    Returns a list of (requirement, beleg_ids, status, comment) tuples.
    Returns empty list if the section / table cannot be found.
    """
    # Locate the section
    lines = analyse_text.splitlines()
    section_start = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("### 4.") and "anforderungsabgleich" in line.lower():
            section_start = i + 1
            break
        if "anforderungsabgleich" in line.strip().lower() and line.strip().startswith("###"):
            section_start = i + 1
            break
    if section_start is None:
        return []

    # Find the table start (first line beginning with `|`) and end (first
    # blank line or next `###`)
    table_lines: list[str] = []
    for j in range(section_start, len(lines)):
        s = lines[j].rstrip()
        if s.startswith("###"):
            break
        if not s.strip() and table_lines:
            break
        if s.startswith("|"):
            table_lines.append(s)

    # IN-03: locate the separator row (`|----|----|`) — everything before it
    # is header (skip), everything after is data.
    rows: list[tuple[str, str, str, str]] = []
    sep_pattern = re.compile(r"^\|[-:\s|]+\|$")
    sep_idx = next(
        (i for i, line in enumerate(table_lines) if sep_pattern.match(line)),
        -1,
    )
    data_lines = table_lines[sep_idx + 1:] if sep_idx >= 0 else table_lines
    for line in data_lines:
        if sep_pattern.match(line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        req, beleg, status, comment = cells[0], cells[1], cells[2], "|".join(cells[3:])
        rows.append((req, beleg, status.upper(), comment))
    return rows


def check_profile_fit(analyse_path: Path) -> list[FitGap]:
    """Return critical/soft fit gaps found in the analyst output.

    A row is **critical** if status is LÜCKE and the comment does not flag
    nice-to-have. A row is **soft** if status is LÜCKE-nice-to-have or
    SCHWACH with a strength qualifier in the requirement text.
    """
    if not analyse_path.exists():
        return []
    rows = parse_anforderungsabgleich(analyse_path.read_text(encoding="utf-8"))
    gaps: list[FitGap] = []
    for req, _beleg, status, comment in rows:
        if status == "LÜCKE":
            # Nice-to-have markers can sit in the analyst's Kommentar OR in
            # the requirement text itself ("Netzwerk … (von Vorteil)") —
            # postings often carry the qualifier inline. Both downgrade.
            nice = _row_is_nice_to_have(comment) or _row_is_nice_to_have(req)
            severity = "soft" if nice else "critical"
            gaps.append(FitGap(req, status, comment, severity))
        elif status == "SCHWACH" and _row_has_strength_qualifier(req):
            gaps.append(FitGap(req, status, comment, "soft"))
    return gaps


def format_gaps_for_cli(gaps: list[FitGap]) -> str:
    """Pretty-print gaps for terminal display."""
    if not gaps:
        return ""
    critical = [g for g in gaps if g.severity == "critical"]
    soft = [g for g in gaps if g.severity == "soft"]
    parts: list[str] = []
    parts.append("─" * 60)
    parts.append("Profil-Fit-Hinweise")
    parts.append("─" * 60)
    if critical:
        parts.append("")
        parts.append(f"  Kritisch ({len(critical)}): nicht belegte Muss-Anforderungen")
        for g in critical:
            parts.append(f"    • {g.requirement}")
            short_comment = g.comment.replace("\n", " ").strip()
            if len(short_comment) > 120:
                short_comment = short_comment[:117] + "..."
            if short_comment:
                parts.append(f"        ↳ {short_comment}")
    if soft:
        parts.append("")
        parts.append(f"  Beachten ({len(soft)}): schwach belegt oder Kann-Anforderung")
        for g in soft:
            parts.append(f"    • {g.requirement}  [{g.status}]")
    parts.append("")
    parts.append("─" * 60)
    return "\n".join(parts)


def has_critical_gaps(gaps: list[FitGap]) -> bool:
    return any(g.severity == "critical" for g in gaps)


def fit_confirmed_in_log(run_log_text: str) -> bool:
    """Has the user already explicitly confirmed the fit gate for this run?

    The CLI writes `profile_fit: User bestätigt …` on confirmation. `continue`
    uses this to avoid re-asking a question the user already answered —
    while a run that never passed the gate (crash or abort at the prompt)
    gets asked again. Matching is per-line so an `abgebrochen` entry never
    counts as confirmation.
    """
    for line in run_log_text.splitlines():
        if "profile_fit" in line and "bestätigt" in line:
            return True
    return False


# ── Gap-to-Question-Routing ─────────────────────────────────────────────────
#
# Alex' Feedback (2026-06-12): bei einem fehlenden Match soll zuerst gefragt
# werden, bevor die Pipeline Erfahrung transferiert oder die Lücke stehen
# lässt. Standard-CV + Zeugnisse sind eine verlustbehaftete Kompression von
# 20 Jahren Erfahrung — viele LÜCKE-Anforderungen sind beantwortbar, wurden
# bisher aber nie gefragt (das Gate bot nur fortfahren/abbrechen an).
#
# Diese Funktionen überführen Fit-Gaps in Klärungsfragen, die der Factcheck
# in `02_klaerungsfragen.md` einmischt. Antworten laufen über den bestehenden
# Pause-Mechanismus und landen via `clarifications.json` (Topic-Gating) im
# Faktkontext künftiger Läufe.

MAX_GAP_QUESTIONS = 6

_SUPPRESS_MIN_OVERLAP = 2
# Requirement filler that says nothing about the *topic* of a requirement.
# Observed 2026-09-19 (FIFA): "Proven experience working with headless CMS"
# was suppressed because "experience" + "working" both appeared somewhere
# in the clarification corpus — while "headless"/"cms" appeared nowhere.
# English postings need the same filter as German ones.
_GENERIC_WORDS = frozenset({
    # DE
    "erfahrung", "kenntnisse", "verständnis", "mehrjährige", "fundierte",
    "ausgeprägte", "fähigkeit", "umgang", "bereich", "umfeld", "jahren",
    "nachgewiesene", "praktische", "tiefe", "tiefes", "tiefen", "sehr", "gute", "hohe",
    # EN
    "experience", "working", "proven", "strong", "knowledge", "skills",
    "ability", "background", "understanding", "several", "years", "excellent",
    "solid", "record", "track", "hands-on", "professional", "familiarity",
    "expertise", "degree", "equivalent", "relevant", "demonstrated",
    "extensive", "environment", "including", "with", "within", "from",
    "using", "least", "minimum", "plus", "and", "the", "for", "of", "in",
})
# Short all-caps tokens (CMS, SEO, API, CRM) are the most topic-specific
# words a requirement carries — the length floor below must not drop them.
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,}\b")


def _significant_words(text: str) -> set[str]:
    words = {
        w for w in re.findall(r"[\wÀ-ɏ-]{4,}", text.lower())
        if w not in _GENERIC_WORDS
    }
    words |= {a.lower() for a in _ACRONYM_RE.findall(text)}
    return words


def _gap_already_answered(gap: FitGap, answered_context: str | list[str]) -> bool:
    """True when an earlier clarification likely covers this requirement.

    `answered_context` is one text or a list of chunks (one per stored
    Q/A entry). Most of the requirement's distinctive words must appear in
    the *same* chunk: at least two, and at least 60 % of them.

    Until 2026-11-03 two hits anywhere in the whole store sufficed. With 64
    entries that matched almost every requirement («business» + «cases»),
    and the gap questions silently disappeared (Alex: «Es gibt nie mehr
    Klärungsfragen»). Prefer asking twice over suppressing a question that
    was never really answered.
    """
    if not answered_context:
        return False
    words = _significant_words(gap.requirement)
    if not words:
        return False
    chunks = [answered_context] if isinstance(answered_context, str) else answered_context
    needed = max(min(_SUPPRESS_MIN_OVERLAP, len(words)), math.ceil(len(words) * 0.6))
    for chunk in chunks:
        low = chunk.lower()
        if sum(1 for w in words if w in low) >= needed:
            return True
    return False


MAX_MITTEL_QUESTIONS = 3
MAX_VOCAB_QUESTIONS = 4


def collect_question_gaps(analyse_path: Path, standard_cv_text: str = "") -> list[FitGap]:
    """Everything worth asking Alex before the writer runs, beyond the gate.

    Alex 2026-11-03 (Straumann): «Es gibt nie mehr Klärungsfragen». The gate
    rows (LÜCKE, qualified SCHWACH) almost never fire because the analyst
    rates most rows STARK or MITTEL — «business cases» landed at MITTEL
    («kein formales Business-Case-Dokument») and was then treated as unbelegt,
    although Alex founded a startup and built a business unit. Two more
    sources, both capped and never part of the fit warning:

    - `mittel`: MITTEL rows and unqualified SCHWACH rows of the
      Anforderungsabgleich — a question can lift them.
    - `vokabular`: terms of the analyst's «Schlüssel-Vokabular» table that
      the Standard-CV does not carry (or that are rated «vorsichtig»). These
      cover the Aufgabenblock, which by the Quellenregel never becomes a
      requirement row («Evaluate emerging technologies»).
    """
    if not analyse_path.exists():
        return []
    text = analyse_path.read_text(encoding="utf-8")
    gaps = check_profile_fit(analyse_path)
    gate = {g.requirement for g in gaps}

    mittel: list[FitGap] = []
    for req, _beleg, status, comment in parse_anforderungsabgleich(text):
        if req in gate:
            continue
        if "MITTEL" in status or (status == "SCHWACH" and not _row_has_strength_qualifier(req)):
            mittel.append(FitGap(req, status, comment, "mittel"))
    mittel.sort(key=lambda g: 0 if g.status == "SCHWACH" else 1)
    gaps.extend(mittel[:MAX_MITTEL_QUESTIONS])

    from cv_tailor.ats_check import match_level, parse_vocabulary

    vocab: list[FitGap] = []
    for term in parse_vocabulary(text):
        if term.status == "nicht":
            continue
        missing = not standard_cv_text or match_level(term.term, standard_cv_text) is None
        if term.status == "vorsichtig" or missing:
            status = "belegbar laut Analyst" if term.status == "ok" else "vorsichtig laut Analyst"
            vocab.append(FitGap(term.term, status, term.note, "vokabular"))
    # Terms the analyst calls belegbar but the Standard-CV lacks come first:
    # an answer there turns directly into CV text.
    vocab.sort(key=lambda g: 0 if g.status.startswith("belegbar") else 1)
    # «business case development» asks the same as the MITTEL row «developing
    # business cases»: skip terms whose words are half covered already.
    asked = [_stems(g.requirement) for g in gaps]
    picked = 0
    for g in vocab:
        words = _stems(g.requirement)
        if words and any(len(words & a) * 2 >= len(words) for a in asked):
            continue
        gaps.append(g)
        asked.append(words)
        picked += 1
        if picked == MAX_VOCAB_QUESTIONS:
            break
    return gaps


def _stems(text: str) -> set[str]:
    return {w[:6] for w in _significant_words(text)}


_SEVERITY_ORDER = {"critical": 0, "soft": 1, "mittel": 2, "vokabular": 3}
_LABELS = {
    "critical": "Muss-Anforderung",
    "soft": "Kann-Anforderung / schwach belegt",
    "mittel": "teilweise belegt",
    "vokabular": "Begriff der Anzeige",
}
_QUESTIONS = {
    "critical": "Hast du belegbare Erfahrung dazu? Falls ja: wo und in welcher Form?",
    "soft": "Hast du belegbare Erfahrung dazu? Falls ja: wo und in welcher Form?",
    "mittel": "Gibt es konkretere Beispiele, Stationen oder Zahlen, die das stärker belegen?",
    "vokabular": "Hast du das gemacht? Falls ja: an welcher Station und wie konkret?",
}


def format_gaps_as_questions(
    gaps: list[FitGap],
    *,
    answered_context: str | list[str] = "",
    max_questions: int = MAX_GAP_QUESTIONS,
) -> str:
    """Markdown question block for `02_klaerungsfragen.md`.

    Returns "" when no gap survives the already-answered filter. Critical
    gaps come first; the list is capped so a poorly-fitting posting doesn't
    produce a questionnaire.
    """
    open_gaps = [g for g in gaps if not _gap_already_answered(g, answered_context)]
    if not open_gaps:
        return ""
    open_gaps.sort(key=lambda g: _SEVERITY_ORDER.get(g.severity, 9))
    open_gaps = open_gaps[:max_questions]

    lines = [
        "## Fragen aus dem Profil-Fit-Abgleich",
        "",
        "Die Anzeige verlangt Folgendes, wozu Beleg-Index und Standard-CV schweigen "
        "oder nur teilweise etwas sagen. "
        "Wenn du dazu Erfahrung hast — auch wenn sie nicht im CV oder in den "
        "Zeugnissen steht — beschreibe sie konkret: Rolle, Kontext, was genau du "
        "getan hast. Wenn nicht, antworte «habe ich nicht» oder lass die Antwort "
        "leer; dann bleibt die Lücke ehrlich stehen.",
        "",
    ]
    for i, g in enumerate(open_gaps, 1):
        label = _LABELS.get(g.severity, g.severity)
        lines.append(f"{i}. **{g.requirement}** _({label}, Status: {g.status})_")
        comment = g.comment.replace("\n", " ").strip()
        if comment:
            lines.append(f"   Einschätzung des Analysten: {comment}")
        lines.append(f"   Frage: {_QUESTIONS.get(g.severity, _QUESTIONS['critical'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
