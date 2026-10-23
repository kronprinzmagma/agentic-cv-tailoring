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
    # Portfolio only: the heading this claim sits under. "Wachstumskanten"
    # marks a known gap — it must never render as an activated Beleg.
    section: str = ""


PORTFOLIO_GAP_SECTION = "Wachstumskanten"


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
    quelle_typ: str | None = None,
    exclude_quelle_typ: str | None = None,
) -> dict[str, list[ActivatedEntry]]:
    """Rank Beleg-Index entries by job-activated themes.

    `quelle_typ` / `exclude_quelle_typ` restrict the pool by provenance. The
    portfolio gets its own budget rather than competing for the same slots:
    it carries several hundred claims about current work and would otherwise
    crowd out Zeugnis evidence, which is the harder-to-replace source.
    """
    job_tokens = _tokens(job_text)
    activated: dict[str, list[ActivatedEntry]] = {}
    entries = beleg_index.get("entries", [])
    if quelle_typ is not None:
        entries = [e for e in entries if e.get("quelle_typ") == quelle_typ]
    if exclude_quelle_typ is not None:
        entries = [e for e in entries if e.get("quelle_typ") != exclude_quelle_typ]
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
                    section=str(entry.get("section", "") or ""),
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
    max_portfolio_per_theme: int = 5,
) -> str:
    """Format activated experience units as markdown for analyst/writer context.

    Two blocks with separate budgets: evidenced history (Standard-CV +
    Zeugnisse) and current practice (portfolio). The portfolio is excluded
    from the compact Beleg-Index for token reasons, so this map is the only
    path by which current work reaches analyst and writer.
    """
    detected = detect_job_themes(job_text)
    activated = activate_entries(
        job_text,
        beleg_index,
        max_entries_per_theme=max_entries_per_theme,
        exclude_quelle_typ="portfolio",
    )
    activated_portfolio = activate_entries(
        job_text,
        beleg_index,
        max_entries_per_theme=max_portfolio_per_theme,
        quelle_typ="portfolio",
    )
    # Wachstumskanten are self-declared gaps. Ranked among evidence they read
    # as competence ("Voice-Agents" scored 9 under Team Leadership on the
    # first run). They stay in the map — the honesty limits should travel —
    # but in their own block, and never inside "Aktivierte Belege".
    gap_entries: dict[str, ActivatedEntry] = {}
    for theme, hits in list(activated_portfolio.items()):
        kept = []
        for entry in hits:
            if entry.section == PORTFOLIO_GAP_SECTION:
                gap_entries.setdefault(entry.id, entry)
            else:
                kept.append(entry)
        activated_portfolio[theme] = kept
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

    if gap_entries:
        lines.append("")
        lines.append("## Bekannte Lücken — NICHT als Kompetenz verwenden")
        lines.append("")
        lines.append(
            "Selbst deklarierte Wachstumskanten aus dem Portfolio. Sie stehen hier, damit "
            "die Ehrlichkeitsgrenze mitreist, **nicht** als Beleg. Diese Themen dürfen im CV "
            "weder als Erfahrung noch als Kompetenz erscheinen."
        )
        for entry in gap_entries.values():
            snippet = entry.snippet.replace("\n", " ").strip()
            if len(snippet) > 180:
                snippet = snippet[:179].rstrip() + "…"
            lines.append(f"- {entry.id} {snippet}")

    portfolio_hits = {t: e for t, e in activated_portfolio.items() if e}
    if portfolio_hits:
        lines.append("")
        lines.append("## Aktuelle Praxis (Portfolio)")
        lines.append("")
        # The limit steers word choice; it must not become a sentence pattern.
        # The first version ended with "… gemessen — nicht implementiert" and the
        # writer copied that shape three times into the CV ("Steuerung, nicht
        # Modellbau" / "entschieden, nicht gebaut" / "bei mir, nicht beim
        # Engineering"). Defensive negations were deliberately removed from the
        # writer prompt on 2026-05-11; this channel reintroduced them.
        lines.append("Laufende Eigenprojekte und Weiterbildung.")
        lines.append("")
        lines.append(
            "**Skalierungsgrenze — sie steuert die Wortwahl, sie wird nie ausgesprochen.** "
            "Diese Projekte belegen Steuerungs-, Urteils- und Produktkompetenz: "
            "Spezifikation, Architektur-Entscheide, Eval-Gating, Trade-offs. Der Code "
            "entstand grösstenteils im Agentic-Coding-Modus — Engineering-Handwerk und "
            "Recall-Wissen über die eingesetzten Techniken sind damit nicht belegt."
        )
        lines.append("")
        # Scope matters: the verb list separates steering from engineering for
        # *these projects*. Read as a general style rule it overshoots — it
        # turned BELG-021 ("gemeinsam mit den Stakeholdern priorisiert") into
        # "entschieden", making documented collaboration sound like a solo call.
        lines.append(
            "- **Nur für Aussagen über diese Eigenprojekte** — zulässige Verben: "
            "verantwortet, evaluiert, gemessen, priorisiert, konzipiert, pilotiert, "
            "beurteilt. Nicht verwenden: implementiert, entwickelt (im "
            "Engineering-Sinn), programmiert, gebaut."
        )
        lines.append(
            "- Für belegte Stationen gilt weiterhin der Wortlaut des jeweiligen "
            "Belegs. Diese Liste ist keine allgemeine Stilvorgabe und darf eine "
            "belegte Zusammenarbeit nicht zur Alleinentscheidung verkürzen."
        )
        lines.append(
            "- **Die Abgrenzung selbst gehört nicht in den CV-Text.** Keine Sätze der "
            "Form «X, nicht Y» — wer die Grenze ausspricht, lenkt den Blick darauf. "
            "Die richtige Wortwahl trägt sie unsichtbar."
        )
        for theme, _score in detected:
            entries = portfolio_hits.get(theme, [])
            if not entries:
                continue
            label = str(THEMES[theme]["label"])
            lines.append("")
            lines.append(f"### {label}")
            for entry in entries:
                snippet = entry.snippet.replace("\n", " ").strip()
                if len(snippet) > 180:
                    snippet = snippet[:179].rstrip() + "…"
                lines.append(
                    f"- {entry.id} [{entry.typ}, score {entry.score}] {snippet} "
                    f"({entry.source}, {entry.position})"
                )
    return "\n".join(lines) + "\n"
