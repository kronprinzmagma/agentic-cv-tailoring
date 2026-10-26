"""Deterministic style and structure check for the German writer output.

Ideen-Scouting 2026-10-23, Kanal A: in 20 of 34 raw drafts stood meta
sentences and antitheses («Was mich … qualifiziert», «nicht X, sondern Y»);
every October shipped CV had them removed by hand. Nominal-style bullets
(«Strategische Weiterentwicklung und Stabilisierung von …») were rewritten to
participles (Galaxus 5→1, Livit 6→2). Summaries gained numbers during rework
(Allianz Suisse 1→5, Galaxus 3→7), and competence points gained a station
(Galaxus 0→4, Livit 0→3). The manual KI-Stil-Check did all of this in every run.

Same contract as `idiom_check` and `length_check`: only patterns that are
almost always wrong in this CV, findings go to the writer as Pflichtkorrektur
(soft veto, «streichen, nicht umformulieren»), nothing is rewritten here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

META_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), msg)
    for p, msg in (
        (
            r"\bwas mich\b[^.\n]{0,60}\b(qualifiziert|auszeichnet|differenziert|ausmacht)",
            "Meta-Satz über die eigene Eignung — streichen, die Belege sprechen lassen.",
        ),
        (
            r"\b(ist|sind) mein (Terrain|Element|Spielfeld)\b",
            "Selbstbeschreibung als These — durch einen Beleg ersetzen oder streichen.",
        ),
        (r"\bdas ist keine theorie\b|\bkein trendthema\b", "Beteuerung — streichen."),
        (r"\bmein anspruch\b", "Beteuerung — streichen."),
        # Benchmark 2026-10-23 (vier Gesprächsfälle): ChatGPT beanstandete in
        # jedem Fall Selbstpositionierung in der Summary. Alex: von sich
        # überzeugt, aber keinesfalls arrogant.
        (
            r"\bwas ich mitbringe\b|\bbringe ich mit\b",
            "Selbstpositionierung («… bringe ich mit») — die Leistung nennen statt ankündigen.",
        ),
        (
            r"\bmeine (grösste |besondere )?stärke\b|\bmein kern\b|\bklarer? haltung\b",
            "Aussage über die eigene Stärke oder Haltung — durch einen Beleg ersetzen "
            "oder streichen.",
        ),
        (
            r"\bich bin (es )?gewohnt\b",
            "Selbsteinordnung — durch einen Beleg ersetzen oder streichen.",
        ),
        (
            r"\bwo [^.,\n]{1,40} zählte, habe ich\b",
            "Selbstbewertung der Leistung — die Leistung nennen, die Wertung dem Leser überlassen.",
        ),
        (
            r"\b(brauchen|braucht) beides\b|\bwas [^.\n]{1,40} unter [^.\n]{1,40} versteh",
            "These über Branche oder Firma — streichen, die Belege tragen die Passung.",
        ),
    )
)

# The degree belongs in the education block. In the summary it reads as
# showing off (Alex: «Niemand in der Schweiz gibt mit seinem Abschluss an»).
SUMMARY_DEGREE = re.compile(r"\bM\.\s?A\.|\bHSG\b|(?<!Scrum )\bMaster\b|\bBachelor\b|\bStudium\b")

ANTITHESIS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), msg)
    for p, msg in (
        (
            r"\bnicht\b[^.;:\n]{1,60},\s*sondern\b",
            "Kontrastformel «nicht …, sondern …» — direkte Aussage machen.",
        ),
        (r"\bstehen und fallen mit\b", "Kontrastformel — direkte Aussage machen."),
        (r"[–—-]\s+nicht\s+(um|als|weil|nur)\b", "Angehängte Abgrenzung «– nicht …» — streichen."),
        (
            r"[–—-]\s+nicht\s+\w+\s*[.!](\s|$)",
            "Angehängte Abgrenzung «– nicht X.» am Satzende — streichen.",
        ),
    )
)

# Bullet opening in nominal style: «Aufbau der …», «Strategische Weiterentwicklung von …».
NOMINAL_BULLET = re.compile(
    r"^(?:[-*]\s+)?(?:(?:Strategische|Konsequente|Erfolgreiche|Operative|Gezielte|Kontinuierliche)\s+)?"
    r"(?:[A-ZÄÖÜ][\wäöü-]*(?:ung|ion)|Aufbau|Ausbau|Umbau)"
    r"(?:\s+und\s+[A-ZÄÖÜ][\wäöü-]*(?:ung|ion))?"
    r"\s+(?:von|des|der|eines|einer|zur|zum|für)\b"
)

# A participle anywhere in the bullet means it already reads as an action
# («Verrechnungsfunktion … zum Rollout gebracht»).
PARTICIPLE = re.compile(
    r"\b(?:ge\w+[tn]|\w+iert|\w+(?:ein|auf|aus|ab|an|um|zu)ge\w+[tn])\b", re.IGNORECASE
)
# «…verantwortung für …» is Zeugnis wording Alex keeps (Gesamtverantwortung für das Portfolio).
ACCEPTED_NOMINAL_HEAD = re.compile(r"^(?:[-*]\s+)?\w*verantwortung\b", re.IGNORECASE)

SHARP_S = re.compile("ß")
FIRST_PERSON = re.compile(r"\b(?:ich|mein\w*|mir|mich)\b", re.IGNORECASE)

NUMBER = re.compile(
    r"\b\d[\d'’.,]*\s*%?|\b(?:zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|elf|zwölf)\b",
    re.IGNORECASE,
)
SUMMARY_MIN_NUMBERS = 2
# Alex' October CVs leave one to three competence points without a station;
# the raw drafts leave three to six. Flag only beyond that.
MAX_COMPETENCES_WITHOUT_STATION = 2

COMPETENCE_LINE = re.compile(r"^\s*(?:[-*]\s+)?\*\*[^*]+\*\*\s*[-–:]\s*(?P<body>.+)$")


@dataclass(frozen=True)
class StyleFinding:
    kind: str
    excerpt: str
    message: str


def _line_findings(line: str, *, nominal: bool) -> list[StyleFinding]:
    plain = line.replace("**", "").strip()
    out: list[StyleFinding] = []
    for pattern, msg in META_PATTERNS:
        if pattern.search(plain):
            out.append(StyleFinding("meta", plain[:160], msg))
            break  # one finding per kind and line
    for pattern, msg in ANTITHESIS_PATTERNS:
        if pattern.search(plain):
            out.append(StyleFinding("antithese", plain[:160], msg))
            break  # one finding per kind and line
    if SHARP_S.search(plain):
        out.append(
            StyleFinding("orthografie", plain[:160], "«ß» → «ss» (Schweizer Rechtschreibung).")
        )
    if (
        nominal
        and NOMINAL_BULLET.match(plain)
        and not PARTICIPLE.search(plain)
        and not ACCEPTED_NOMINAL_HEAD.match(plain)
    ):
        out.append(
            StyleFinding(
                "nominalstil",
                plain[:160],
                "Bullet im Nominalstil — als Handlung im Partizip schreiben "
                "(«… aufgebaut», «… eingeführt»).",
            )
        )
    return out


_GENERIC_TOKENS = (
    "career",
    "family",
    "parental",
    "sabbatical",
    "leave",
    "independent",
    "self",
    "freelance",
    "own initiative",
)


def station_tokens(standard_cv_path) -> list[str]:
    """Company tokens of the Standard-CV (plus «Blick»), without generic EN aliases."""
    from cv_tailor.consistency_check import _company_tokens

    tokens = list(_company_tokens(standard_cv_path).keys()) + ["blick"]
    return [t for t in tokens if not any(g in t for g in _GENERIC_TOKENS)]


def check_style(
    section: str, text: str, station_tokens: list[str] | None = None
) -> list[StyleFinding]:
    """Findings for one writer section.

    `section` is management_summary, schluesselkompetenzen or berufserfahrung.
    """
    findings: list[StyleFinding] = []
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for line in lines:
        findings.extend(_line_findings(line, nominal=section == "berufserfahrung"))

    if section == "management_summary":
        # Digicomp target 2026-10-23: full first-person sentences, no lists
        # across stations. Raw drafts opened in telegram style («GastroSaaS
        # gegründet, … aufgebaut») and stacked stations with semicolons.
        for para in (p for p in text.split("\n\n") if p.strip()):
            plain = para.replace("**", "").strip()
            if plain.startswith(("#", "---")):
                continue
            if not FIRST_PERSON.search(plain):
                findings.append(
                    StyleFinding(
                        "telegrammstil",
                        plain[:160],
                        "Absatz ohne Ich-Form — in ganzen Sätzen mit «ich» schreiben "
                        "(«GastroSaaS habe ich gegründet …»).",
                    )
                )
            if plain.count(";") >= 2:
                findings.append(
                    StyleFinding(
                        "stationsliste",
                        plain[:160],
                        "Semikolon-Liste über mehrere Stationen — höchstens zwei Stationen "
                        "pro Absatz, je in einem ganzen Satz.",
                    )
                )
        for line in lines:
            plain = line.replace("**", "").strip()
            if SUMMARY_DEGREE.search(plain):
                findings.append(
                    StyleFinding(
                        "abschluss",
                        plain[:160],
                        "Abschluss in der Summary — gehört in die Ausbildung, hier streichen.",
                    )
                )
        numbers = NUMBER.findall(text.replace("**", ""))
        if len(numbers) < SUMMARY_MIN_NUMBERS:
            findings.append(
                StyleFinding(
                    "zahlen",
                    f"{len(numbers)} Zahlenbeleg(e)",
                    f"Summary braucht mindestens {SUMMARY_MIN_NUMBERS} Zahlenbelege "
                    "aus den Belegen "
                    "oder der Pflichtsubstanz (Teamgrösse, Zeit, Budget, Ergebnis).",
                )
            )

    if section == "schluesselkompetenzen" and station_tokens:
        tokens = [t.lower() for t in station_tokens]
        without = [
            line.replace("**", "").strip()
            for line in lines
            if (m := COMPETENCE_LINE.match(line))
            and not any(t in m.group("body").lower() for t in tokens)
        ]
        if len(without) > MAX_COMPETENCES_WITHOUT_STATION:
            findings.append(
                StyleFinding(
                    "kompetenz_ohne_station",
                    " | ".join(w.split(" - ")[0][:60] for w in without),
                    f"{len(without)} Kompetenzen ohne Station "
                    f"(höchstens {MAX_COMPETENCES_WITHOUT_STATION}): "
                    "eine belegte Handlung mit Station nennen («… bei HealthApp»), "
                    "statt eine Fähigkeit "
                    "zu behaupten.",
                )
            )
    return findings


def format_findings_for_writer(findings: list[StyleFinding]) -> str:
    if not findings:
        return "Keine Stil- oder Gerüstbefunde.\n"
    lines = [
        "# Stil- und Gerüst-Check",
        "",
        "Meta-Sätze und Kontrastformeln streichen, nicht umformulieren. Nominalstil als "
        "Handlung schreiben. Fehlende Zahlen oder Stationen nur aus belegtem Material ergänzen.",
        "",
    ]
    for i, f in enumerate(findings, start=1):
        lines.append(f"{i}. [{f.kind}] {f.message}")
        lines.append(f"   «{f.excerpt}»")
    return "\n".join(lines) + "\n"
