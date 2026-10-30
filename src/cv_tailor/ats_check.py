"""ATS check: can an applicant tracking system read the CV, and does it find the terms?

Why this exists
---------------
Recruiters often meet the CV first as parsed text: a parser pulls the PDF text
layer apart, files it under section headings and matches terms from the posting.
Nothing in the pipeline looked at that view. Two things can go wrong without
anyone noticing:

1. **Readability.** The CV template sets headings and contact labels with wide
   letter spacing. Some PDF text extractors return «M A N A G E M E N T …» for
   it, which a section parser does not recognise (measured 2026-10-29: pypdf
   spaced, pdfplumber joined). Contact data and the text layer matter as well.
2. **Terms.** A keyword filter counts words; a person reads evidence. Terms the
   posting names and the CV could truthfully contain may be missing, and terms
   the analyst barred («nicht verwenden») may have crept in.

This check is report-only, like `redundancy_check` and `fact_limits`: it writes
`_ats.md`, never touches the CV, and never suggests inserting a word without
evidence. The term list is the analyst's own «Schlüssel-Vokabular» table
(`01_analyse.md`, section 6), so the honesty classification — natürlich
integrierbar / vorsichtig / nicht verwenden — comes from the analysis, not from
this module.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger()

REPORT_NAME = "_ats.md"

# Headings the template renders per language; a tuple lists accepted variants
# (the translator produced both «Core» and «Key Competencies»).
HEADINGS: dict[str, tuple[tuple[str, ...], ...]] = {
    "de": (
        ("Management Summary",),
        ("Schlüsselkompetenzen",),
        ("Berufserfahrung",),
        ("Ausbildung",),
        ("Zertifikate",),
        ("Sprachkenntnisse",),
        ("Software",),
    ),
    "en": (
        ("Management Summary",),
        ("Key Competencies", "Core Competencies"),
        ("Professional Experience",),
        ("Education",),
        ("Certificates",),
        ("Languages",),
        ("Skills",),
    ),
}

MIN_CHARS_PER_PAGE = 300
_STOPWORDS = frozenset((
    "und", "der", "die", "das", "den", "dem", "des", "von", "mit", "für", "zur", "zum", "im",
    "in", "an", "auf", "als", "oder", "bei", "and", "the", "of", "for", "to", "on", "with", "or",
    "by", "a",
))


@dataclass(frozen=True)
class AtsFinding:
    # readability | contact | text_layer | compound_merged | compound_check
    # | term_missing | term_loose | term_barred
    kind: str
    severity: str   # befund | hinweis
    message: str


@dataclass(frozen=True)
class VocabTerm:
    term: str
    status: str     # ok | vorsichtig | nicht
    note: str


# --------------------------------------------------------------------------- #
# 1. Vocabulary from the analysis
# --------------------------------------------------------------------------- #

def _status(cell: str) -> str | None:
    low = cell.lower()
    if "🚫" in cell or "nicht verwenden" in low:
        return "nicht"
    if "⚠" in cell or "vorsichtig" in low or "framingfähig" in low:
        return "vorsichtig"
    if "✅" in cell or "natürlich" in low or "integrierbar" in low:
        return "ok"
    return None


def parse_vocabulary(analysis_text: str) -> list[VocabTerm]:
    """Rows of the analyst's «Schlüssel-Vokabular» table; empty when absent."""
    match = re.search(
        r"Schlüssel-Vokabular[^\n]*\n(.*?)(?=\n#{2,3} |\n---|\Z)", analysis_text, re.S
    )
    if not match:
        return []
    terms: list[VocabTerm] = []
    for line in match.group(1).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("-: ") or cells[0].lower() == "begriff":
            continue
        status = _status(cells[1])
        if status is None:
            continue
        term = re.sub(r"[`*]", "", cells[0]).strip()
        note = cells[1].split("—", 1)[1].strip() if "—" in cells[1] else ""
        terms.append(VocabTerm(term, status, note))
    return terms


def _alternatives(term: str) -> list[str]:
    """«ISO 13485 / MDR / nDSG» → three alternatives; parentheses and quotes stripped."""
    parts = re.split(r"\s+/\s+|\s*/\s*(?=[A-ZÄÖÜa-zäöü])", term)
    cleaned = [re.sub(r"\([^)]*\)", "", p).strip(" «»\"'„“") for p in parts]
    return [c for c in cleaned if len(c) >= 2]


# --------------------------------------------------------------------------- #
# 2. Term matching
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zäöüéèàß]+", " ", text.lower()).strip()


def _tokens(norm_term: str) -> list[str]:
    return [t for t in norm_term.split() if t not in _STOPWORDS and (len(t) >= 3 or t.isdigit())]


def _stem(token: str) -> str:
    if token.isdigit() or len(token) <= 4:
        return token
    return token[: max(5, int(len(token) * 0.6))]


def match_level(term: str, text: str) -> str | None:
    """`exact` (phrase as written), `stem` (every word present, inflection or
    compound tolerated) or None. Acronyms and numbers must match exactly."""
    norm_text = f" {_norm(text)} "
    norm_term = _norm(term)
    if not norm_term:
        return None
    if f" {norm_term} " in norm_text:
        return "exact"
    # Plural acronyms: «LLMs» is the same keyword as «LLM-Modelle».
    singular = term.rstrip()[:-1]
    plural_acronym = term.rstrip().endswith("s") and singular.split()[-1].isupper()
    if plural_acronym and f" {_norm(singular)} " in norm_text:
        return "exact"
    tokens = _tokens(norm_term)
    if not tokens:
        return None
    for token in tokens:
        if len(token) <= 4 or token.isdigit():
            if f" {token} " not in norm_text:
                return None
        elif f" {_stem(token)}" not in norm_text:
            return None
    return "stem"


def _excerpt(term: str, text: str) -> str:
    stem = _stem(_norm(term).split()[0]) if _norm(term) else ""
    for line in text.splitlines():
        if stem and stem in _norm(line):
            return re.sub(r"\s+", " ", line).strip()[:140]
    return ""


def _barred_present(term: str, text: str) -> bool:
    """Barred terms count only as a standalone word or phrase, not inside a
    hyphenated compound («KI-Pilot» is a pilot project, not the role label)."""
    pattern = r"(?<![\w-])" + r"\s+".join(re.escape(w) for w in term.split()) + r"(?![\w-])"
    # Very short terms («RE») are case-sensitive, or they hit ordinary words.
    flags = 0 if len(term) <= 3 else re.IGNORECASE
    return re.search(pattern, text, flags) is not None


def check_terms(vocab: list[VocabTerm], cv_text: str) -> list[AtsFinding]:
    findings: list[AtsFinding] = []
    for entry in vocab:
        alts = _alternatives(entry.term) or [entry.term]
        levels = {alt: match_level(alt, cv_text) for alt in alts}
        if entry.status == "nicht":
            # Barred terms count only when written exactly: «Pilot» as the role
            # label, not every compound that contains the stem.
            hits = [a for a in alts if _barred_present(a, cv_text)]
            for alt in hits:
                # A hint, not a finding: across 89 runs the analyst's «nicht verwenden»
                # was often a limit on the claim («keine eigene Expertise»), not a word ban.
                findings.append(AtsFinding(
                    "term_barred", "hinweis",
                    f"«{alt}» steht im CV; die Analyse stuft den Begriff als «nicht verwenden» "
                    f"ein ({entry.note or 'nicht belegt'}). Prüfen, ob die Stelle den Grund "
                    f"verletzt: {_excerpt(alt, cv_text)}",
                ))
            continue
        if any(lv == "exact" for lv in levels.values()):
            continue
        if any(lv == "stem" for lv in levels.values()):
            findings.append(AtsFinding(
                "term_loose", "hinweis",
                f"«{entry.term}» nur sinngemäss, nicht wörtlich im CV "
                "(ein Stichwort-Filter sieht es nicht).",
            ))
        elif entry.status == "ok":
            findings.append(AtsFinding(
                "term_missing", "hinweis",
                f"«{entry.term}» fehlt im CV, obwohl der Analyst ihn als belegbar einstuft "
                f"({entry.note or 'natürlich integrierbar'}). "
                "Nur ergänzen, wenn es sachlich passt.",
            ))
    return findings


# --------------------------------------------------------------------------- #
# 3. PDF readability
# --------------------------------------------------------------------------- #

def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).upper()


def _letters(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def check_extracted_text(
    parser: str,
    text: str,
    lang: str,
    email: str | None,
    phone_digits: str | None,
    pages: int,
) -> list[AtsFinding]:
    """Pure check of one parser's text layer — separate from file IO for tests."""
    findings: list[AtsFinding] = []
    if pages and len(text) / pages < MIN_CHARS_PER_PAGE:
        findings.append(AtsFinding(
            "text_layer", "befund",
            f"{parser}: nur {len(text) // max(pages, 1)} Zeichen pro Seite, "
            "kaum Textebene (Bild-PDF?).",
        ))
    collapsed, letters = _collapse(text), _letters(text)
    spaced_out: list[str] = []
    absent: list[str] = []
    for variants in HEADINGS.get(lang, HEADINGS["de"]):
        if any(v.upper() in collapsed for v in variants):
            continue
        is_spaced = any(_letters(v) in letters for v in variants)
        (spaced_out if is_spaced else absent).append(variants[0])
    if spaced_out:
        findings.append(AtsFinding(
            "readability", "hinweis",
            f"{parser}: {len(spaced_out)} Überschriften kommen buchstabengesperrt heraus "
            "(M A N A G E …): "
            f"{', '.join(spaced_out)}. Ein Abschnitts-Parser erkennt sie womöglich nicht; "
            "der Fliesstext ist davon nicht betroffen.",
        ))
    if absent:
        findings.append(AtsFinding(
            "readability", "befund",
            f"{parser}: Überschriften fehlen in der Textebene: {', '.join(absent)}.",
        ))
    if email and email.lower() not in text.lower():
        findings.append(
            AtsFinding("contact", "befund", f"{parser}: E-Mail-Adresse nicht intakt lesbar.")
        )
    if phone_digits and phone_digits not in re.sub(r"\D", "", text):
        findings.append(
            AtsFinding("contact", "befund", f"{parser}: Telefonnummer nicht intakt lesbar.")
        )
    return findings


def station_markers(cv_md: str) -> list[tuple[str, str, str]]:
    """(label, header marker, bullet marker) per station of the Berufserfahrung section.

    The header marker is the role (or, without one, the whole title) because it is
    shown once next to the bullets; the bullet marker is the first six words of the
    first body line. Stations without a body line carry nothing to misplace.
    """
    section = re.search(
        r"## (?:Berufserfahrung|Professional Experience)\n(.*?)(?=\n## |\Z)", cv_md, re.S
    )
    if not section:
        return []
    markers: list[tuple[str, str, str]] = []
    for block in re.split(r"\n(?=### )", section.group(1)):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip() and ln.strip() != "---"]
        if not lines or not lines[0].startswith("###") or len(lines) < 2:
            continue
        heading = lines[0].lstrip("# ").strip()
        title = heading.split("|", 1)[-1].strip()
        role = re.split(r"\s+[–—-]\s+", title, maxsplit=1)[-1].strip()
        body = re.sub(r"^[-*]\s+", "", lines[1]).replace("**", "")
        bullet = " ".join(_norm(body).split()[:6])
        if _norm(role) and bullet:
            markers.append((title, _norm(role), bullet))
    return markers


def check_reading_order(
    parser: str, text: str, stations: list[tuple[str, str, str]]
) -> list[AtsFinding]:
    """Each station's header must stand between the previous and its own first bullet.

    A parser that follows the PDF's internal order (not the page geometry) reads
    «all headers, then all bullets» when bullets are painted late, e.g. because
    list items are positioned elements (found 2026-10-30 in the template). It then
    attaches every bullet to the wrong employer.
    """
    norm = f" {_norm(text)} "
    bullets = [norm.find(f" {b}") for _, _, b in stations]
    broken: list[str] = []
    previous = -1
    for index, ((label, header, _), pos) in enumerate(zip(stations, bullets, strict=True)):
        if pos < 0:
            continue
        if index > 0 and previous >= 0:
            window_ok = pos > previous and norm.find(f" {header}", previous, pos) >= 0
            if not window_ok:
                broken.append(label)
        previous = pos
    if not broken:
        return []
    return [AtsFinding(
        "reading_order", "befund",
        f"{parser}: Tätigkeitsbeschreibungen stehen nicht bei ihrer Station in der Textebene "
        f"(betroffen: {', '.join(broken[:4])}). Ein ATS, das der PDF-Textreihenfolge folgt, "
        "ordnet sie dem falschen Arbeitgeber zu.",
    )]


def _contact_from_standard_cv(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file():
        return None, None
    head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:25])
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", head)
    phone = re.search(r"\+\d[\d ]{8,}\d", head)
    return (email.group(0) if email else None, re.sub(r"\D", "", phone.group(0)) if phone else None)


def _extract_pypdf(pdf: Path) -> tuple[str, int] | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf))
        return "\n".join(p.extract_text() or "" for p in reader.pages), len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - parser missing or PDF unreadable, never fatal
        log.warning("ats_check.parser_failed", parser="pypdf", error=str(exc))
        return None


def _extract_pdfplumber(pdf: Path) -> tuple[str, int] | None:
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf)) as doc:
            return "\n".join(p.extract_text() or "" for p in doc.pages), len(doc.pages)
    except Exception as exc:  # noqa: BLE001 - see pypdf
        log.warning("ats_check.parser_failed", parser="pdfplumber", error=str(exc))
        return None


def _extract_pdftotext(pdf: Path) -> tuple[str, int] | None:
    """Poppler, the engine many applicant tracking systems build on.

    It drops a hyphen at the end of a line as if it were syllable hyphenation, so it is
    the extractor that fuses «SAFe-Umfeld» to «SAFeUmfeld». Skipped when not installed.
    """
    binary = shutil.which("pdftotext")
    if binary is None:
        log.info("ats_check.parser_missing", parser="pdftotext")
        return None
    try:
        done = subprocess.run(
            [binary, "-enc", "UTF-8", str(pdf), "-"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except Exception as exc:  # noqa: BLE001 - see pypdf
        log.warning("ats_check.parser_failed", parser="pdftotext", error=str(exc))
        return None
    if done.returncode != 0:
        log.warning("ats_check.parser_failed", parser="pdftotext", error=done.stderr[:200])
        return None
    return done.stdout, max(done.stdout.count("\f"), 1)


EXTRACTORS = ("pypdf", "pdfplumber", "pdftotext")


def extract_all(pdf: Path) -> list[tuple[str, str, int]]:
    """(parser, text, pages) for every extractor that could read the PDF."""
    out: list[tuple[str, str, int]] = []
    for name in EXTRACTORS:
        result = globals()[f"_extract_{name}"](pdf)  # looked up late, tests replace them
        if result is not None:
            out.append((name, result[0], result[1]))
    return out


def check_pdf(
    pdf: Path,
    lang: str,
    standard_cv_path: Path,
    stations: list[tuple[str, str, str]] | None = None,
    extracted: list[tuple[str, str, int]] | None = None,
) -> list[AtsFinding]:
    email, phone = _contact_from_standard_cv(standard_cv_path)
    findings: list[AtsFinding] = []
    texts = extracted if extracted is not None else extract_all(pdf)
    for name, text, pages in texts:
        findings.extend(check_extracted_text(name, text, lang, email, phone, pages))
        if stations:
            findings.extend(check_reading_order(name, text, stations))
    if not texts:
        findings.append(
            AtsFinding("text_layer", "befund", f"{pdf.name}: mit keinem Parser lesbar.")
        )
    return findings


# --------------------------------------------------------------------------- #
# 3b. Hyphenated compounds must survive the text layer
# --------------------------------------------------------------------------- #

# Same pattern as `keepCompounds` in the PDF template: letters only, so ranges
# («2015-2023»), phone numbers, URLs and e-mail addresses never count.
_COMPOUND_RE = re.compile(r"(?<![\w@/.:=-])[^\W\d_]+(?:-[^\W\d_]+)+(?![\w@/-]|\.\w)")


def _clean_for_compounds(cv_md: str) -> str:
    text = re.sub(r"\]\([^)]*\)", "]", cv_md)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\S+@\S+", " ", text)
    return text.replace("*", "")


def compound_words(cv_md: str) -> set[str]:
    """Hyphenated words of the CV markdown, without links, URLs and e-mail addresses."""
    return set(_COMPOUND_RE.findall(_clean_for_compounds(cv_md)))


def _count(word: str, text: str) -> int:
    """Whole-word occurrences of `word` in lower-case `text` (not inside a longer word)."""
    return len(re.findall(rf"(?<![\w-]){re.escape(word)}(?!\w)", text))


def check_compounds(parser: str, cv_md: str, text: str) -> list[AtsFinding]:
    """Every hyphenated word of the CV must come out of `parser` with its hyphen.

    A line-end hyphen that the parser keeps and only separates by a line break
    («Release-\nStabilität») is fine; one it removes («ReleaseStabilität») is not,
    because a keyword filter then no longer finds «Release-Stabilität». A word counts
    as lost when it occurs less often in the text than in the markdown; the fused
    form (one hyphen removed) is named when it is there.
    """
    found = _COMPOUND_RE.findall(_clean_for_compounds(cv_md))
    wanted = {w.lower(): w for w in found}
    wanted_count = Counter(w.lower() for w in found)
    joined = re.sub(r"\s+", " ", re.sub(r"(?<=\w)-\s+", "-", text)).lower()
    findings: list[AtsFinding] = []
    for low in sorted(wanted):
        word = wanted[low]
        if _count(low, joined) >= wanted_count[low]:
            continue
        positions = [m.start() for m in re.finditer("-", low)]
        fused = next(
            (
                word[:k] + word[k + 1:]
                for k in positions
                if _count(low[:k] + low[k + 1:], joined)
            ),
            None,
        )
        if fused:
            findings.append(AtsFinding(
                "compound_merged", "befund",
                f"{parser}: «{word}» kommt als «{fused}» heraus "
                "(Bindestrich am Zeilenende verloren).",
            ))
        else:
            findings.append(AtsFinding(
                "compound_merged", "befund", f"{parser}: «{word}» fehlt im Text."
            ))
    return findings


def check_pdf_compounds(
    cv_md: str, extracted: list[tuple[str, str, int]]
) -> list[AtsFinding]:
    findings: list[AtsFinding] = []
    for name, text, _pages in extracted:
        findings.extend(check_compounds(name, cv_md, text))
    if not any(name == "pdftotext" for name, _t, _p in extracted):
        findings.append(AtsFinding(
            "compound_check", "hinweis",
            "pdftotext (Poppler) nicht installiert: Bindestriche am Zeilenende sind nur mit "
            "pypdf und pdfplumber geprüft, die sie nicht entfernen. Installieren: "
            "`brew install poppler`.",
        ))
    return findings


# --------------------------------------------------------------------------- #
# 4. Report
# --------------------------------------------------------------------------- #

def _cv_text(run_dir: Path, lang: str, standard_cv_path: Path) -> str:
    """The text a keyword filter sees: generated sections plus the Standard-CV tail."""
    from cv_tailor.pdf_renderer import build_template_markdown
    from cv_tailor.rework import resolve_final

    final = resolve_final(run_dir, lang)
    if final is None:
        return ""
    tail = standard_cv_path
    if lang == "en":
        from cv_tailor.pdf_renderer import STANDARD_CV_PATH_EN
        tail = STANDARD_CV_PATH_EN if STANDARD_CV_PATH_EN.is_file() else standard_cv_path
    try:
        return build_template_markdown(final, standard_cv_path=tail, lang=lang)
    except Exception as exc:  # noqa: BLE001 - fall back to the raw final text
        log.warning("ats_check.template_markdown_failed", error=str(exc))
        return final.read_text(encoding="utf-8")


def _final_markdown(run_dir: Path, lang: str) -> str:
    from cv_tailor.rework import resolve_final

    final = resolve_final(run_dir, lang)
    return final.read_text(encoding="utf-8") if final is not None else ""


def format_report(sections: dict[str, list[AtsFinding]]) -> str:
    lines = [
        "# ATS-Check",
        "",
        "Report-only, nichts wird im CV geändert. **Befund** = bitte prüfen (PDF nicht lesbar), "
        "**Hinweis** = Information. Begriffe stammen aus dem Schlüssel-Vokabular der Analyse; "
        "ergänzt wird nur, was sachlich belegt ist (ISO, MDR und Ähnliches bleiben "
        "absichtlich draussen, wenn die Analyse sie sperrt).",
        "",
    ]
    for title, findings in sections.items():
        lines.append(f"## {title}")
        if not findings:
            lines += ["", "Keine Auffälligkeiten.", ""]
            continue
        lines.append("")
        for severity in ("befund", "hinweis"):
            for f in (x for x in findings if x.severity == severity):
                lines.append(f"- **{severity.capitalize()}** ({f.kind}): {f.message}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_ats_report(
    run_dir: Path,
    pdf_paths: list[Path] | None = None,
    standard_cv_path: Path | None = None,
) -> tuple[Path, int, int]:
    """Write `_ats.md`. Returns (path, befunde, hinweise).

    Terms are checked per language that exists; PDFs only when given or found
    next to the run (`04_final_<lang>.pdf`).
    """
    from cv_tailor.pdf_renderer import STANDARD_CV_PATH

    std = standard_cv_path or STANDARD_CV_PATH
    analysis = run_dir / "01_analyse.md"
    vocab = parse_vocabulary(analysis.read_text(encoding="utf-8")) if analysis.is_file() else []
    sections: dict[str, list[AtsFinding]] = {}
    for lang in ("de", "en"):
        text = _cv_text(run_dir, lang, std)
        if not text:
            continue
        label = lang.upper()
        if vocab:
            sections[f"Begriffe {label} (Textebene inkl. Standard-CV-Tail)"] = check_terms(
                vocab, text
            )
        else:
            sections[f"Begriffe {label}"] = [
                AtsFinding(
                    "term_missing", "hinweis",
                    "Keine Vokabular-Tabelle in 01_analyse.md gefunden, "
                    "Begriffsabgleich übersprungen.",
                )
            ]
        pdf = next((p for p in (pdf_paths or []) if p.name == f"04_final_{lang}.pdf"), None) or (
            run_dir / f"04_final_{lang}.pdf"
        )
        if pdf.is_file():
            extracted = extract_all(pdf)
            sections[f"PDF-Lesbarkeit {label}"] = check_pdf(
                pdf, lang, std, station_markers(_final_markdown(run_dir, lang)), extracted
            )
            sections[f"Komposita {label}"] = check_pdf_compounds(text, extracted)
    report = run_dir / REPORT_NAME
    report.write_text(format_report(sections), encoding="utf-8")
    all_findings = [f for fs in sections.values() for f in fs]
    return (
        report,
        sum(1 for f in all_findings if f.severity == "befund"),
        sum(1 for f in all_findings if f.severity == "hinweis"),
    )
