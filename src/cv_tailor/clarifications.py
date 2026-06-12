"""Persistent clarification memory for user-provided fact context.

Clarifications are sensitive, user-provided facts. They are stored locally in
data/clarifications.json, which is gitignored via the data/ directory rules.

Topic gating (added 2026-05-18). Each entry carries a `topics` list derived
deterministically from the question/answer text at save-time. When the
clarifications are formatted into a prompt, the caller passes the active
posting/analysis text; only entries whose topics overlap with the current
context are included. This implements the CLAUDE.md rule "Frühere Klärungen
sind keine Themenliste — sie dürfen nur genutzt werden, wenn die aktuelle
Anzeige dasselbe Thema aktiviert." Entries without a `topics` field
(pre-migration) fall back to universal inclusion.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cv_tailor.logging_config import get_logger

log = get_logger(__name__)

CLARIFICATIONS_PATH = Path("data/clarifications.json")

# WR-02: module-level lock serialises concurrent read-modify-write on the store file
_store_lock = threading.Lock()

# Topic taxonomy — small set of high-signal labels with keyword anchors.
# Keys are topic IDs; values are lowercase keyword lists. A topic activates
# when any keyword appears as a substring (case-insensitive, word-boundary
# is enforced for short ambiguous tokens via a regex pattern in `_match`).
# Keep this list short, high-precision; over-tagging dilutes the filter.
_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "analytics": (
        "analytics", "dashboard", "kpi", "metriken", "metrik",
        "data literacy", "datenanalyse", "produktanalyse",
        "ab-test", "a/b-test", "experiment",
    ),
    "user_research": (
        "userpanel", "user panel", "usertest", "user test", "usability",
        "ux research", "ux-research", "betatest", "beta-test",
        "nutzerfeedback", "kundenfeedback", "nutzerbefragung",
        "interview", "testingtime",
    ),
    "languages": (
        "sprachkenntnisse", "sprachniveau", "französisch", "italienisch",
        "portugiesisch", "spanisch", "english", "fluent", "fließend",
        "b1", "b2", "c1", "c2", "muttersprache",
    ),
    "subscription_saas": (
        "subscription", "self-service", "self service", "saas",
        "abomodell", "abo-modell", "pricing-modell", "subscription-modell",
        "freemium", "tenant", "multi-tenant",
    ),
    "ml_ai": (
        "ki-", "ki ", " ki.", "(ki", "künstliche intelligenz",
        " ai ", "ai-", "ml ", "ml-", "machine learning",
        "llm", "genai", "gen-ai", "generative ai",
        "rag", "agentic", "modell", "training",
    ),
    "team_management": (
        "führung", "geführt", "leitung", "leiten", "team-aufbau",
        "teamaufbau", "disziplinarisch", "personalführung", "hiring",
        "rekrutierung", "stellenbesetzung", "organisations",
    ),
    "domain_health": (
        "praxis", "praxisassist", "arzt", "ärztin", "patient",
        "spital", "klinik", "ehealth", "gesundheits", "medizin",
        "healthapp", "healthappconnect",
    ),
    "domain_media": (
        "redaktion", "redakteur", "publishing", "newsroom",
        "tv-", "radio", "rundfunk", "mediacorp",
        "content-empfehlung", "recommendation",
    ),
    "domain_finance": (
        "bank", "underwriter", "aktuar", "wealth", "asset management",
        "compliance officer", "kyc", "aml", "versicherung",
    ),
    "domain_gastro": (
        "gastronomie", "restaurant", "reservation", "gastrosaas",
        "servicepersonal", "gastrobetrieb",
    ),
    "tech_stack": (
        "cloud", "aws", "gcp", "azure", "kubernetes", "docker",
        "python", "typescript", "react", "etl", "pipeline",
        "feature store", "mlops",
    ),
    "compliance_security": (
        "gdpr", "dsgvo", "regulator", "regulier", "compliance",
        "security", "sicherheit", "audit",
    ),
}

# Universal entries — always included regardless of current topics.
# Saved when an entry has no high-confidence topic match (low keyword density).
_UNIVERSAL_TOPIC = "*"


def _match_topics(text: str) -> list[str]:
    """Classify free text against the topic taxonomy.

    Deterministic, keyword-based, no LLM cost. Returns a sorted list of
    topic IDs (zero-or-more matches). Empty/short text returns the universal
    topic so the entry is never silently excluded.
    """
    if not text or len(text.strip()) < 20:
        return [_UNIVERSAL_TOPIC]
    lowered = text.lower()
    matched: set[str] = set()
    for topic, keywords in _TOPIC_KEYWORDS.items():
        for kw in keywords:
            kw_is_alpha = kw.replace(" ", "").isalpha()
            # Whole-token match for short purely-alphabetic tokens (<=4 chars).
            # Otherwise (longer phrases, or hyphen-suffixed prefix markers
            # like "ml-" / "ki-" which intentionally match the start of a
            # compound like "ml-basiert" / "ki-Fachgruppe") use substring.
            if len(kw) <= 4 and kw_is_alpha:
                if re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", lowered):
                    matched.add(topic)
                    break
            elif kw in lowered:
                matched.add(topic)
                break
    if not matched:
        return [_UNIVERSAL_TOPIC]
    return sorted(matched)


def _resolve_entry_topics(entry: dict[str, Any]) -> list[str]:
    """Read or compute topics for an entry without writing back to disk.

    Pre-migration entries (no `topics` field) get classified on the fly so
    the existing data continues to work without a one-shot migration step.
    The classification is in-memory only; persisting it requires
    `migrate_topics()` (called from save and a CLI command).
    """
    topics = entry.get("topics")
    if isinstance(topics, list) and topics:
        return topics
    # Classify question + answer text together
    qa = (entry.get("questions_markdown") or "") + "\n" + (entry.get("answers_markdown") or "")
    return _match_topics(qa)


def _read_store(path: Path = CLARIFICATIONS_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("clarifications.read_invalid_json", path=str(path))
        return {"version": 1, "entries": []}
    if not isinstance(data, dict):
        return {"version": 1, "entries": []}
    data.setdefault("version", 1)
    data.setdefault("entries", [])
    return data


def _write_store(data: dict[str, Any], path: Path = CLARIFICATIONS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # WR-02: write to a temp file and atomically replace to avoid partial writes
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX


def save_run_clarification(run_dir: Path, path: Path = CLARIFICATIONS_PATH) -> bool:
    """Persist a run's clarification Q/A pair for future runs.

    Returns True when a new entry was stored, False if the run has no complete
    clarification files or has already been imported.
    """
    questions_path = run_dir / "02_klaerungsfragen.md"
    answers_path = run_dir / "02_antworten.md"
    if not questions_path.exists() or not answers_path.exists():
        return False

    # WR-02: hold the lock across the entire read-modify-write to prevent
    # concurrent pipelines from both reading, both appending, and last-writer-wins.
    with _store_lock:
        data = _read_store(path)
        run_id = run_dir.name
        entries = data["entries"]
        if any(entry.get("run_id") == run_id for entry in entries):
            return False

        posting_path = run_dir / "00_stellenanzeige.md"
        posting_title = posting_path.stem if posting_path.exists() else ""
        questions_md = questions_path.read_text(encoding="utf-8").strip()
        answers_md = answers_path.read_text(encoding="utf-8").strip()
        entry = {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "posting_title": posting_title,
            "questions_markdown": questions_md,
            "answers_markdown": answers_md,
            # Topic-gating: classify Q+A together so the entry only re-surfaces
            # in future runs that activate the same topic. See _match_topics.
            "topics": _match_topics(questions_md + "\n" + answers_md),
        }
        entries.append(entry)
        _write_store(data, path)
    log.info("clarifications.saved", run_id=run_id, topics=entry["topics"], path=str(path))
    return True


def all_clarifications_text(path: Path = CLARIFICATIONS_PATH) -> str:
    """Concatenated Q/A text of every stored clarification.

    Used by the gap-to-question routing to suppress profile-fit questions
    Alex has effectively answered in an earlier run. Deliberately unfiltered
    (no topic gating): suppression should consider everything ever answered,
    not just topic-matching entries.
    """
    data = _read_store(path)
    parts: list[str] = []
    for entry in data.get("entries", []):
        parts.append(entry.get("questions_markdown", ""))
        parts.append(entry.get("answers_markdown", ""))
    return "\n".join(p for p in parts if p)


def format_clarifications_for_prompt(
    path: Path = CLARIFICATIONS_PATH,
    limit: int = 12,
    *,
    current_context: str | None = None,
) -> str:
    """Return compact Markdown context for Analyst/Factcheck/Writer prompts.

    Topic gating: if ``current_context`` is provided (the active posting text
    plus any analysis output), only entries whose topics overlap with the
    current context are included. Universal entries (topic ``*`` — short or
    unclassifiable past Q/As) are always included as a backstop. Pass
    ``current_context=None`` to include everything (preserves legacy
    behaviour and is used by code paths without posting context).
    """
    data = _read_store(path)
    entries = data.get("entries", [])
    if not entries:
        return ""

    # Resolve current-context topics once. Empty context → no filter.
    current_topics: set[str] = set()
    if current_context:
        current_topics = set(_match_topics(current_context))

    selected_entries: list[dict[str, Any]] = []
    for entry in entries[-limit:]:
        if not current_topics:
            selected_entries.append(entry)
            continue
        entry_topics = set(_resolve_entry_topics(entry))
        if _UNIVERSAL_TOPIC in entry_topics:
            selected_entries.append(entry)
            continue
        if entry_topics & current_topics:
            selected_entries.append(entry)
    if not selected_entries:
        log.info(
            "clarifications.gated_all_filtered",
            available=len(entries),
            current_topics=sorted(current_topics),
        )
        return ""
    if current_topics:
        log.info(
            "clarifications.gated",
            available=len(entries),
            included=len(selected_entries),
            current_topics=sorted(current_topics),
        )

    parts = ["## Frühere Klärungen von Alex", ""]
    parts.append(
        "Diese Informationen stammen aus früheren Läufen. Nutze sie nur als "
        "zusätzlichen Faktkontext, wenn die aktuelle Stellenanzeige dasselbe Thema "
        "aktiviert. Frühere Rollen-/Stellenbegriffe dürfen keine neuen Anforderungen "
        "oder Klärungsfragen in die aktuelle Analyse einführen. Erfinde daraus keine "
        "stärkeren Claims. Nutzergruppen aus früheren Antworten dürfen nicht an "
        "neue Aktivitäts-Bullets angehängt werden, deren Beleg-Snippet sie nicht "
        "selbst nennt."
    )
    for entry in selected_entries:
        topics = _resolve_entry_topics(entry)
        topic_label = ", ".join(t for t in topics if t != _UNIVERSAL_TOPIC) or "universal"
        parts.extend(
            [
                "",
                f"### {entry.get('run_id', 'unknown')} _(Topics: {topic_label})_",
                "",
                "Antworten:",
                entry.get("answers_markdown", "").strip(),
            ]
        )
    return "\n".join(parts).strip()


def migrate_topics(path: Path = CLARIFICATIONS_PATH) -> dict[str, Any]:
    """Persist auto-derived topics into pre-migration entries.

    Idempotent: entries already carrying a non-empty `topics` list are left
    untouched. Returns a summary dict with counts. Safe to run at any time;
    callers without access to the lock can invoke it via the CLI.
    """
    with _store_lock:
        data = _read_store(path)
        updated = 0
        for entry in data.get("entries", []):
            existing = entry.get("topics")
            if isinstance(existing, list) and existing:
                continue
            qa = (entry.get("questions_markdown") or "") + "\n" + (entry.get("answers_markdown") or "")
            entry["topics"] = _match_topics(qa)
            updated += 1
        if updated:
            _write_store(data, path)
    log.info("clarifications.migrated_topics", updated=updated)
    return {"updated": updated, "total": len(data.get("entries", []))}


def count_clarifications(path: Path = CLARIFICATIONS_PATH) -> int:
    data = _read_store(path)
    return len(data.get("entries", []))
