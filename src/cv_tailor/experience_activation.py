"""Experience activation: use the job posting as a filter for belegte Erfahrung."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

THEMES: dict[str, dict[str, object]] = {
    "business_ownership": {
        "label": "Business Ownership / Wachstum",
        "keywords": {
            "business owner", "business ownership", "ownership", "p&l", "profit", "growth", "revenue",
            "budget", "portfolio", "commercial", "market", "opportunity", "opportunities",
            "managing director", "geschäftsverantwortung", "wachstum", "umsatz", "budget",
            "portfolio", "marktchance", "geschäftschance", "gründer", "founder", "exit",
        },
    },
    "team_leadership": {
        "label": "Team Leadership / Mobilisierung",
        "keywords": {
            "lead", "leading", "leadership", "motivate", "team", "teams", "hire",
            "recruiting", "onboarding", "development", "führung", "führen",
            "teamstruktur", "motivation", "aufbau", "rekrutierung", "entwicklung",
        },
    },
    "customer_market": {
        "label": "Customer Needs / Marktverständnis",
        "keywords": {
            "customer", "customers", "user", "users", "needs", "experience",
            "value", "proposition", "market", "trend", "insights", "kunden",
            "nutzer", "bedürfnisse", "markt", "trends", "marktlücke",
            "value proposition", "nutzererfahrung",
        },
    },
    "data_decisioning": {
        "label": "Datenbasierte Steuerung",
        "keywords": {
            "data", "data-driven", "kpi", "metrics", "analysis", "insights",
            "decision", "decisions", "daten", "datenbasiert", "kennzahlen",
            "analyse", "auswertung", "entscheidungen",
        },
    },
    "platform_marketplace": {
        "label": "Plattform / Marketplace",
        "keywords": {
            "platform", "marketplace", "classifieds", "c2c", "c2b", "seller",
            "private", "listing", "digital platform", "plattform", "marktplatz",
            "local-directory.example", "app", "apps", "website", "websites", "portal",
        },
    },
    "stakeholder_communication": {
        "label": "Stakeholder / C-Level / Cross-funktional",
        "keywords": {
            "stakeholder", "c-level", "executive", "management", "cross-functional",
            "product", "engineering", "marketing", "sales", "operations",
            "kommunikation", "geschäftsleitung", "stakeholder-management",
            "schnittstelle", "verhandlung", "reporting",
        },
    },
    "delivery_impact": {
        "label": "Delivery / Wirkung / Zahlen",
        "keywords": {
            "launch", "launched", "delivery", "scale", "scaling", "under budget",
            "ahead", "cost", "savings", "efficiency", "stability", "outage",
            "lancierung", "termin", "kosten", "einsparung", "stabilität",
            "ausfall", "skalierung", "produktivität",
        },
    },
    "ai_capability": {
        "label": "AI / Automatisierung",
        "keywords": {
            "ai", "ki", "artificial", "intelligence", "machine", "learning",
            "automation", "automatisierung", "gilde", "guild", "workflow",
            "workflows", "pilot", "use case", "anwendungsfall",
        },
    },
    "tech_cloud": {
        "label": "Tech / Cloud / Architektur",
        "keywords": {
            "cloud", "architecture", "technical", "technology", "engineering",
            "migration", "software", "plattformarchitektur", "architektur",
            "technologie", "entwicklung", "devops",
        },
    },
    "sales_partnerships": {
        "label": "Sales / Partnerschaften / Go-to-Market",
        "keywords": {
            "sales", "go-to-market", "partnership", "partnerships", "partner",
            "commercial", "contract", "negotiation", "vertrieb", "verkauf",
            "partnerschaft", "verhandlung", "vertrag", "markteintritt",
        },
    },
}

STOPWORDS = {
    "und", "oder", "der", "die", "das", "ein", "eine", "einer", "mit", "für",
    "von", "im", "in", "zu", "auf", "als", "and", "or", "the", "a", "an",
    "with", "for", "from", "into", "to", "of", "as", "we", "you", "our",
}


@dataclass(frozen=True)
class ActivatedEntry:
    id: str
    score: int
    typ: str
    snippet: str
    source: str
    position: str


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Za-zÄÖÜäöüß0-9&.+-]+", text.lower())
        if len(token) > 2 and token not in STOPWORDS
    }


def _theme_keywords(theme: str) -> set[str]:
    raw = THEMES[theme]["keywords"]
    return {str(item).lower() for item in raw}  # type: ignore[arg-type]


def detect_job_themes(job_text: str) -> list[tuple[str, int]]:
    """Return themes activated by the job posting, sorted by strength."""
    lowered = job_text.lower()
    job_tokens = _tokens(job_text)
    detected: list[tuple[str, int]] = []
    for theme in THEMES:
        score = 0
        for keyword in _theme_keywords(theme):
            if " " in keyword:
                if keyword in lowered:
                    score += 3
            elif keyword in job_tokens:
                score += 2
        if score > 0:
            detected.append((theme, score))
    return sorted(detected, key=lambda item: item[1], reverse=True)


def _score_entry(entry: dict, theme: str, job_tokens: set[str]) -> int:
    text = " ".join(
        str(entry.get(key, ""))
        for key in ("behauptung", "snippet", "kontext", "typ", "quelle_typ")
    )
    lowered = text.lower()
    entry_tokens = _tokens(text)
    score = 0
    for keyword in _theme_keywords(theme):
        if " " in keyword:
            if keyword in lowered:
                score += 4
        elif keyword in entry_tokens:
            score += 3
    score += min(len(entry_tokens & job_tokens), 5)
    if re.search(r"\d", text):
        score += 1
    return score


def activate_entries(
    job_text: str,
    beleg_index: dict,
    *,
    max_entries_per_theme: int = 7,
) -> dict[str, list[ActivatedEntry]]:
    """Rank Beleg-Index entries by job-activated themes."""
    job_tokens = _tokens(job_text)
    activated: dict[str, list[ActivatedEntry]] = {}
    entries = beleg_index.get("entries", [])
    for theme, _theme_score in detect_job_themes(job_text):
        ranked: list[ActivatedEntry] = []
        for entry in entries:
            score = _score_entry(entry, theme, job_tokens)
            if score <= 0:
                continue
            ranked.append(
                ActivatedEntry(
                    id=str(entry.get("id", "?")),
                    score=score,
                    typ=str(entry.get("typ", "other")),
                    snippet=str(entry.get("snippet", "")),
                    source=str(entry.get("quelle_datei", "")),
                    position=str(entry.get("quelle_position", "")),
                )
            )
        ranked.sort(key=lambda item: item.score, reverse=True)
        activated[theme] = ranked[:max_entries_per_theme]
    return activated


def format_activation_markdown(
    job_text: str,
    beleg_index: dict,
    *,
    max_entries_per_theme: int = 7,
) -> str:
    """Format activated experience units as markdown for analyst/writer context."""
    detected = detect_job_themes(job_text)
    activated = activate_entries(
        job_text,
        beleg_index,
        max_entries_per_theme=max_entries_per_theme,
    )
    lines = [
        "# Experience Activation Map",
        "",
        "Die Stellenanzeige dient als Filter für belegte Erfahrungseinheiten.",
        "Diese Map ist kein Beweisersatz; sie priorisiert nur relevante Belege für Analyse und Writer.",
        "",
        "## Aktivierte Themen der Anzeige",
    ]
    if not detected:
        lines.append("- Keine starken Themen automatisch erkannt.")
    for theme, score in detected:
        label = str(THEMES[theme]["label"])
        lines.append(f"- {label} (score {score})")

    lines.append("")
    lines.append("## Aktivierte Belege")
    for theme, _score in detected:
        label = str(THEMES[theme]["label"])
        lines.append("")
        lines.append(f"### {label}")
        entries = activated.get(theme, [])
        if not entries:
            lines.append("- Keine passenden Belege gefunden.")
            continue
        for entry in entries:
            snippet = entry.snippet.replace("\n", " ").strip()
            if len(snippet) > 180:
                snippet = snippet[:179].rstrip() + "…"
            lines.append(
                f"- {entry.id} [{entry.typ}, score {entry.score}] {snippet} "
                f"({entry.source}, {entry.position})"
            )
    return "\n".join(lines) + "\n"
