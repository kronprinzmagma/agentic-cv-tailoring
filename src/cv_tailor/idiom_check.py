"""Deterministic idiom check on the English CV output.

Why this exists
---------------
The Translator produces grammatically correct English that still reads as
translated German: "disciplinary leadership" (disziplinarische Führung),
"decision basis" (Entscheidungsgrundlage), "Built internal app development"
(Aufbau der internen App-Entwicklung). The Naturalisation agent trims
wordiness but reliably misses these calques — it optimises for brevity, not
for register.

Observed cost: in the Rivia and grape runs (2026-08-12/13) an external
review flagged ~8 such phrasings per CV. Alex applies to Zurich tech/SaaS
companies where hiring managers read native English; translated English
costs credibility even when the substance is right.

This check is the deterministic safety net behind the Translator prompt
rule ("idiomatisches Business-English, keine deutschen Calques"). It scans
the English CV for known calques and writes a human-readable report to
`_idiom.md`. It never modifies the CV: some hits are legitimate in context,
so the user decides. Report-only, same contract as `redundancy_check`.

Two layers, and why the second exists
-------------------------------------
`IDIOM_PATTERNS` is a curated phrase list. It has high precision and
**no recall on unseen calques**: measured on 2026-08-14, none of the 17
patterns from the first round fired on any of the 15 calques found by hand
in the next EN CV ("gastronomy partners", "medical practice teams",
"interdisciplinary", "not as a topic", "TV offering"). Each review round
adds that round's phrases; the next batch is disjoint. The list alone is a
treadmill, not a solution.

`check_missing_articles` is the answer to that: it catches a *class* rather
than a phrase. German drops the article in CV-style clauses ("Leitung
Billing-Feature") and the translator carries it over — "Led billing
feature", "Introduced cloud-first strategy", "Initiated AI working group".
Validated across 22 historical EN CVs: 32 hits in 14 of them, no false
positives after four skip rules (determiner present, proper-noun head,
plural head, uncountable head). It fires on text nobody has reviewed.

The companion metric lives in `quality_snapshot`: nominalisation density
tracks German sentence *structure* surviving translation, which is neither
a phrase nor an article and so invisible to both layers here.

Adding a pattern
----------------
Only add high-precision patterns — a phrase that is *almost always* a
calque in a CV context. False positives train the user to ignore the
report. Each entry carries the idiomatic alternative and the German source
so the report explains itself.

Check the suggestion too, not just the pattern: two entries from the first
round recommended replacements that were themselves the next round's
problem ("deliverable specs" → "actionable specs", an LLM tell; "with
great autonomy" → "with a high degree of autonomy", corporate filler).
Both are corrected above.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPORT_FILENAME = "_idiom.md"


@dataclass(frozen=True)
class IdiomPattern:
    pattern: re.Pattern[str]
    suggestion: str   # idiomatic alternative
    source: str       # the German construction it comes from


@dataclass(frozen=True)
class IdiomFinding:
    matched: str      # the exact text found in the CV
    suggestion: str
    source: str
    context: str      # surrounding sentence fragment, for locating it


def _p(regex: str, suggestion: str, source: str) -> IdiomPattern:
    return IdiomPattern(re.compile(regex, re.IGNORECASE), suggestion, source)


# Curated, high-precision. Grouped by the kind of mistake they encode.
IDIOM_PATTERNS: tuple[IdiomPattern, ...] = (
    # --- Leadership / HR vocabulary -------------------------------------
    _p(r"\bdisciplinary leadership\b",
       "line-managed N people / N direct reports",
       "disziplinarische Führung"),
    _p(r"\bworking students?\b",
       "student assistants",
       "Werkstudent:innen"),
    # --- Nominalisations that English turns into verbs -------------------
    _p(r"\bbuilt internal\s+\w+\s+development\b",
       "brought <X> development in-house",
       "Aufbau der internen <X>-Entwicklung"),
    _p(r"\bdecision basis\b",
       "used <the analyses> to inform <the decision>",
       "Entscheidungsgrundlage"),
    _p(r"\bfurther development of\b",
       "evolution of / advanced <X>",
       "Weiterentwicklung von"),
    _p(r"\bin the (?:framework|scope) of\b",
       "as part of",
       "im Rahmen von"),
    # --- Vocabulary that exists in English but means something else ------
    _p(r"\bdigital measures\b",
       "digital activities / digital initiatives",
       "digitale Massnahmen"),
    _p(r"\bdeliverable specs?\b",
       "specs engineers can build from / requirements and user stories",
       "umsetzbare Spezifikationen"),
    _p(r"\blived practice\b",
       "hands-on practice / something I actually do",
       "gelebte Praxis"),
    _p(r"\bgastronomy\b",
       "restaurants / restaurant operators",
       "Gastronomie (im EN kein Branchenwort für Restaurantbetriebe)"),
    # --- Missing determiners / possessives -------------------------------
    _p(r"\bbased on own\b",
       "based on my own",
       "auf Basis eigener …"),
    _p(r"\bPost-acquisition\b",
       "After the acquisition",
       "Nach der Übernahme (Telegrammstil)"),
    # --- Register / collocation ------------------------------------------
    _p(r"\bwith great autonomy\b",
       "self-directed / I made the calls myself",
       "mit grosser Selbstständigkeit"),
    _p(r"\b(?:with |a )?high degree of autonomy\b",
       "self-directed / I made the calls myself",
       "Corporate-Umschreibung, keine Sprecher-Stimme"),
    _p(r"\bspecialized,? (?:non-technical )?user groups?\b",
       "expert users / domain experts",
       "spezialisierte Nutzergruppen"),
    _p(r"\bexpert user groups?\b",
       "expert users / specialists",
       "Nutzergruppen (Gruppe ist im EN redundant)"),
    _p(r"\bsuccessful exit\b",
       "exit (the qualifier is redundant)",
       "erfolgreicher Exit"),
    # --- 2026-08-14, Rivia-Nachlese ---------------------------------------
    # Zweite Charge. Keiner der 17 Patterns der ersten Runde griff auf
    # diesen Formulierungen -- siehe Modul-Docstring, Abschnitt "Recall".
    _p(r"\bmedical practice teams?\b",
       "medical practice staff",
       "Praxisteams"),
    _p(r"\bspecialist editorial teams?\b",
       "the sports/business/news desks",
       "fachspezialisierte Redaktionen"),
    _p(r"\binterdisciplinary\b",
       "cross-functional",
       "interdisziplinär (im EN-Teamkontext unüblich)"),
    _p(r"\bnot as a topic\b",
       "not a talking point",
       "nicht als Thema"),
    _p(r"\bconcrete use cases?\b",
       "specific use cases / use cases",
       "konkrete Anwendungsfälle ('konkret' ist im EN Füllwort)"),
    _p(r"\bactionable specs?\b",
       "specs engineers can build from",
       "LLM-Register: 'actionable' ist ein Erkennungsmerkmal"),
    _p(r"\bdemonstrated at\b",
       "Stationen einfach benennen, ohne Beleg-Meta",
       "belegt über / nachgewiesen bei"),
    _p(r"\bprecisely formulate\b",
       "write <requirements> precisely / write <specs> engineers can act on",
       "präzise formulieren"),
    _p(r"\bact as liaison\b",
       "act as the liaison / be the link between",
       "als Bindeglied agieren (fehlender Artikel)"),
    _p(r"\b(?:TV|digital|product|content) offerings?\b",
       "TV service / digital products",
       "Angebot (im EN meint 'offering' das Verkaufsangebot)"),
    _p(r"\bspecialized (?:sales|support|service) team\b",
       "dedicated sales team",
       "spezialisiertes Vertriebsteam"),
    _p(r"\bnew \w+ solution\b",
       "das Substantiv direkt nennen (a new eReader)",
       "Lösung als Füllwort"),
    _p(r"\bwith focus on\b",
       ", <Bereich> (Komma statt Präposition)",
       "mit Fokus auf"),
    # --- Outright wrong prepositions --------------------------------------
    _p(r"\bexit at\b",
       "exit to <acquirer>",
       "Exit an <Käufer>"),
    # --- LLM tell the translator prompt already forbids --------------------
    _p(r"—",
       "en-dash (–) instead of em-dash (—)",
       "Em-dash ist ein LLM-Muster, das Recruiter erkennen"),
)


# ---------------------------------------------------------------------------
# Structural check: missing article before a singular countable noun.
#
# Phrase patterns only catch what someone has already seen. This check catches
# a whole *class*: German drops the article in CV-style clauses ("Leitung
# Billing-Feature"), and the translator carries that over -- "Led billing
# feature", "Introduced cloud-first strategy", "Initiated AI working group".
# It is the single most recognisable German-English tell in a CV.
#
# Precision is bought with four skip rules rather than a POS tagger: a
# determiner or possessive anywhere in the noun phrase, a proper noun
# (capitalised, not a known acronym), a plural head, or an uncountable head.
# Each rule removes one class of false positive; see tests for the cases.
# ---------------------------------------------------------------------------

_ACTION_VERBS = frozenset((
    "led", "introduced", "initiated", "built", "developed", "established", "created",
    "launched", "managed", "restructured", "implemented", "designed", "defined", "drove",
    "owned", "advanced", "enabled", "ran", "coordinated", "delivered", "improved", "increased",
    "reduced", "set", "took", "brought", "gathered", "conceptualized", "conceptualised",
    "piloted", "negotiated", "hired", "scaled", "rolled", "shipped", "migrated", "automated",
    "consolidated", "streamlined", "prioritized", "prioritised", "evaluated", "analysed",
    "analyzed"
))

_DETERMINERS = frozenset((
    "a", "an", "the", "my", "our", "your", "his", "her", "their", "its", "this", "that",
    "these", "those", "each", "every", "no", "any", "one", "two", "three", "four", "five",
    "six", "seven", "eight", "nine", "ten", "several", "multiple", "many", "few", "all", "both",
    "other", "another", "same", "first", "second", "third", "new"
))

# Capitalised tokens that are acronyms, not proper nouns -- these do not
# signal "company name follows", so the clause stays a candidate.
_ACRONYMS = frozenset((
    "AI", "ML", "API", "KPI", "SaaS", "B2B", "B2C", "CRM", "ERP", "UX", "UI", "PDF", "SEO",
    "LLM", "CI", "CD", "QA", "IT", "HR", "TV", "MVP", "OKR", "SLA", "GTM"
))

# Abstract / mass nouns that correctly take no article in English.
_UNCOUNTABLE_HEADS = frozenset((
    "measurement", "development", "delivery", "ownership", "leadership", "growth", "revenue",
    "adoption", "research", "work", "training", "support", "maintenance", "governance",
    "compliance", "innovation", "collaboration", "communication", "coordination", "data",
    "software", "hardware", "content", "feedback", "analytics", "engineering", "marketing",
    "sales", "onboarding", "hiring", "budgeting", "reporting", "testing", "prioritization",
    "prioritisation", "documentation", "discovery", "quality", "performance", "efficiency",
    "visibility", "transparency", "accountability", "automation", "migration", "integration",
    "alignment", "awareness", "stability", "scalability", "reliability", "usability",
    "availability", "security", "expertise", "velocity", "throughput", "uptime", "retention",
    "churn", "engagement", "satisfaction", "seo", "go-to-market", "oversight", "buy-in",
    "headcount", "runway", "traction"
))

# Tokens that end the noun phrase -- everything after them is a modifier.
_NP_TERMINATORS = frozenset((
    "from", "to", "at", "in", "on", "for", "with", "by", "of", "across", "into", "through",
    "and", "or", "as", "after", "before", "during", "until", "per", "via", "between", "among",
    "within", "that", "which", "while", "when", "where", "in-house", "on-site", "end-to-end",
    "hands-on", "company-wide"
))

_CLAUSE_SPLIT = re.compile(r"[:;]\s+")
_TOKEN_STRIP = "*_`\"'()[],.;:!?–—-"
_WORDLIKE = re.compile(r"^[A-Za-z][A-Za-z-]*$")


def _clause_candidates(cv_text: str) -> list[tuple[str, int]]:
    """Yield (clause, offset) for every clause that could start with a verb."""
    out: list[tuple[str, int]] = []
    pos = 0
    for line in cv_text.splitlines():
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        # Skip headings, separators and table rows -- not prose.
        if not stripped or stripped.startswith(("#", "---", "|", ">")):
            pos += len(line) + 1
            continue
        base = pos + indent
        # Drop a leading list marker so the verb lands first.
        marker = re.match(r"^([-*•]\s+)", stripped)
        if marker:
            base += marker.end()
            stripped = stripped[marker.end():]
        offset = base
        for part in _CLAUSE_SPLIT.split(stripped):
            if part.strip():
                out.append((part, offset + (len(part) - len(part.lstrip()))))
            offset += len(part) + 2
        pos += len(line) + 1
    return out


def _missing_article_finding(clause: str) -> str | None:
    """Return the offending 'verb + noun phrase' fragment, or None."""
    raw = clause.strip().lstrip("*_ ")
    tokens = raw.split()
    if not tokens:
        return None
    verb = tokens[0].strip(_TOKEN_STRIP).lower()
    if verb not in _ACTION_VERBS:
        return None

    phrase: list[str] = []
    closed_by_comma = False
    consumed = 0
    for idx, tok in enumerate(tokens[1:6], start=1):
        bare = tok.strip(_TOKEN_STRIP)
        # A dash or any non-word token ends the noun phrase.
        if not bare or not _WORDLIKE.match(bare):
            break
        low = bare.lower()
        # Coordination governs the whole list ("gathered market and customer
        # needs" -- the plural "needs" is the head), so this is not a
        # missing article at all.
        if low in ("and", "or"):
            return None
        if low in _NP_TERMINATORS:
            break
        # An adverb ends the noun phrase ("delivered releases ahead",
        # "Initiated AI workflows independently") -- what precedes it is
        # the real phrase and was already accepted or rejected.
        if low.endswith("ly") or low in ("ahead", "early", "successfully"):
            break
        phrase.append(bare)
        consumed = idx
        # Punctuation directly after the token closes the phrase.
        if tok.rstrip("*_").endswith((",", ".", ";", ":", ")")):
            closed_by_comma = tok.rstrip("*_").endswith(",")
            break
    if not phrase:
        return None

    # Coordinated list: "set up backlog, refinement, and release routines".
    # The plural head at the end of the list governs, so no article is
    # missing. What follows the comma tells the two cases apart: a bare
    # noun continues a list, while a verb or gerund starts a new clause
    # ("restructured backlog, prioritized by ...", "Introduced X, achieving
    # Y") -- those still count as a missing article.
    if closed_by_comma and len(tokens) > consumed + 1:
        nxt = tokens[consumed + 1].strip(_TOKEN_STRIP).lower()
        continues_clause = (
            nxt in _ACTION_VERBS
            or nxt.endswith("ing")
            or nxt.endswith("ed")
        )
        if not continues_clause:
            return None

    lowered = [w.lower() for w in phrase]
    if any(w in _DETERMINERS for w in lowered):
        return None
    if any(w.endswith(("'s", "’s")) for w in lowered):
        return None
    # A proper noun means a named product/company follows, not a bare noun.
    if any(w[0].isupper() and w not in _ACRONYMS for w in phrase):
        return None

    head = lowered[-1]
    if head.endswith("s") and not head.endswith("ss"):
        return None          # plural needs no article
    if phrase[-1].isupper():
        return None          # bare acronym head (SEO, AI) reads as a mass noun
    if head in _UNCOUNTABLE_HEADS:
        return None
    if len(head) < 3 or not _WORDLIKE.match(head):
        return None
    return f"{tokens[0].strip(_TOKEN_STRIP)} {' '.join(phrase)}"


def check_missing_articles(cv_text: str) -> list[IdiomFinding]:
    """Find clauses that drop the article before a singular countable noun."""
    findings: list[IdiomFinding] = []
    seen: set[str] = set()
    for clause, offset in _clause_candidates(cv_text):
        fragment = _missing_article_finding(clause)
        if not fragment or fragment.lower() in seen:
            continue
        seen.add(fragment.lower())
        findings.append(
            IdiomFinding(
                matched=fragment,
                suggestion=f"Artikel ergänzen: „{fragment.split()[0]} the "
                           f"{' '.join(fragment.split()[1:])}“ (oder a/an)",
                source="fehlender Artikel — im Deutschen korrekt "
                       "(„Leitung Billing-Feature“), im Englischen nicht",
                context=_context_for(cv_text, offset, offset + len(clause)),
            )
        )
    return findings


def _context_for(text: str, start: int, end: int, width: int = 45) -> str:
    """Return a single-line fragment around the match, for locating it."""
    left = max(0, start - width)
    right = min(len(text), end + width)
    fragment = text[left:right].replace("\n", " ")
    fragment = re.sub(r"\s+", " ", fragment).strip()
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def check_idioms(cv_text: str) -> list[IdiomFinding]:
    """Scan English CV text for known German calques.

    Returns one finding per distinct matched string per pattern — repeated
    occurrences of the same phrase collapse into a single finding so the
    report stays readable.
    """
    findings: list[IdiomFinding] = []
    for entry in IDIOM_PATTERNS:
        seen: set[str] = set()
        for match in entry.pattern.finditer(cv_text):
            key = match.group(0).lower()
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                IdiomFinding(
                    matched=match.group(0),
                    suggestion=entry.suggestion,
                    source=entry.source,
                    context=_context_for(cv_text, match.start(), match.end()),
                )
            )
    findings.extend(check_missing_articles(cv_text))
    return findings


def format_findings(findings: list[IdiomFinding]) -> str:
    lines = [
        "# Englisch-Hinweise (Idiomatik)",
        "",
        "Folgende Formulierungen lesen sich wie aus dem Deutschen übersetzt.",
        "Grammatikalisch korrekt, aber im Zürcher Tech-/SaaS-Umfeld nicht",
        "branchenüblich. Report-only — nichts wurde geändert.",
        "",
    ]
    for i, f in enumerate(findings, 1):
        lines.append(f"{i}. **`{f.matched}`** → {f.suggestion}")
        lines.append(f"   - aus: {f.source}")
        lines.append(f"   - Stelle: {f.context}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_idiom_report(run_dir: Path, cv_text: str) -> Path | None:
    """Write `_idiom.md` when calques are found; return the path or None."""
    findings = check_idioms(cv_text)
    if not findings:
        return None
    report_path = run_dir / REPORT_FILENAME
    report_path.write_text(format_findings(findings), encoding="utf-8")
    return report_path
