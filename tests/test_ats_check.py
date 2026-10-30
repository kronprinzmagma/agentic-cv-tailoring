"""ATS check: vocabulary table, term matching, PDF text-layer findings."""
from pathlib import Path

from cv_tailor import ats_check as A

ANALYSIS = """
### 5. Gap vs. Framing

text

### 6. Schlüssel-Vokabular der Anzeige

| Begriff | Bewertung |
|---|---|
| Requirements Engineering / Anforderungen | ✅ natürlich integrierbar — Zertifikat belegt |
| Epics und User Stories | ✅ natürlich integrierbar — wörtlich im Zeugnis |
| Domänenwissen Gesundheitswesen | ⚠️ nur vorsichtig/framingfähig — Kontext, keine Expertise |
| ISO 13485 / MDR | 🚫 nicht verwenden als eigene Erfahrung — unbelegt |
| «Pilot» (Rollenbezeichnung der Anzeige) | 🚫 nicht verwenden — Anzeigen-Echo |

---

### 7. Experience Activation
"""


def test_parse_vocabulary_reads_status_and_note() -> None:
    vocab = A.parse_vocabulary(ANALYSIS)
    assert [v.status for v in vocab] == ["ok", "ok", "vorsichtig", "nicht", "nicht"]
    assert vocab[0].term == "Requirements Engineering / Anforderungen"
    assert vocab[0].note == "Zertifikat belegt"
    assert A.parse_vocabulary("kein Vokabular hier") == []


def test_alternatives_split_on_slash_and_drop_parentheses() -> None:
    assert A._alternatives("ISO 13485 / MDR / nDSG") == ["ISO 13485", "MDR", "nDSG"]
    assert A._alternatives("«Pilot» (Rollenbezeichnung der Anzeige)") == ["Pilot"]


def test_match_level_exact_stem_and_none() -> None:
    text = "Anforderungen erhoben, Epics und User Stories abgeleitet. KI & LLM-Modelle im Einsatz."
    assert A.match_level("Epics und User Stories", text) == "exact"
    assert A.match_level("Anforderung", text) == "stem"          # inflection
    assert A.match_level("Requirements Engineering", text) is None
    assert A.match_level("LLMs", text) == "exact"                # plural acronym
    assert A.match_level("MDR", text) is None                    # acronyms never by stem


def test_acronyms_are_not_matched_inside_longer_words() -> None:
    assert A.match_level("ISO 13485", "ISO 9001 und 13485er Reihe") is None


def test_missing_ok_term_is_a_hint_not_a_finding_of_substance() -> None:
    vocab = A.parse_vocabulary(ANALYSIS)
    findings = A.check_terms(vocab, "Epics und User Stories abgeleitet, Anforderungen erhoben.")
    kinds = {(f.kind, f.severity) for f in findings}
    assert ("term_missing", "hinweis") not in kinds  # «Anforderungen» covers the alternative
    # nothing for the cautious term, nothing for absent barred terms
    assert not [f for f in findings if f.kind == "term_barred"]


def test_ok_term_only_loosely_present_is_flagged_as_loose() -> None:
    vocab = [A.VocabTerm("Priorisierung", "ok", "")]
    findings = A.check_terms(vocab, "Backlog priorisiert")
    assert [f.kind for f in findings] == ["term_loose"]


def test_barred_term_standalone_is_a_finding_but_not_inside_a_compound() -> None:
    vocab = A.parse_vocabulary(ANALYSIS)
    inside = A.check_terms(vocab, "Evaluation eines KI-Pilot-Projekts bei der Firma")
    assert not [f for f in inside if f.kind == "term_barred"]
    standalone = A.check_terms(vocab, "Als Pilot der Plattform eingesetzt, ISO 13485 begleitet")
    barred = [f for f in standalone if f.kind == "term_barred"]
    assert {f.severity for f in barred} == {"hinweis"}
    assert len(barred) == 2   # «Pilot» and «ISO 13485»


def test_cautious_and_barred_terms_never_create_missing_hints() -> None:
    vocab = A.parse_vocabulary(ANALYSIS)
    findings = A.check_terms(vocab, "Epics und User Stories, Requirements Engineering")
    assert findings == []


# --- PDF text layer --------------------------------------------------------

JOINED = (
    "Jane Doe jane@example.org +41 79 000 00 00\n"
    "MANAGEMENT SUMMARY text SCHLÜSSELKOMPETENZEN text BERUFSERFAHRUNG text "
    "AUSBILDUNG text ZERTIFIKATE text SPRACHKENNTNISSE text SOFTWARE & TOOLS"
) * 3


def test_clean_text_layer_has_no_findings() -> None:
    assert A.check_extracted_text("pypdf", JOINED, "de", "jane@example.org", "41790000000", 1) == []


def test_letter_spaced_headings_are_one_aggregated_hint() -> None:
    spaced = JOINED.replace("MANAGEMENT SUMMARY", "M A N A G E M E N T  S U M M A R Y").replace(
        "BERUFSERFAHRUNG", "B E R U F S E R F A H R U N G"
    )
    findings = A.check_extracted_text("pypdf", spaced, "de", "jane@example.org", "41790000000", 1)
    assert [(f.kind, f.severity) for f in findings] == [("readability", "hinweis")]
    assert "Management Summary" in findings[0].message and "Berufserfahrung" in findings[0].message


def test_absent_heading_contact_and_thin_text_layer_are_findings() -> None:
    text = JOINED.replace("BERUFSERFAHRUNG", "XX").replace("jane@example.org", "jane at example")
    findings = A.check_extracted_text(
        "pdfplumber", text, "de", "jane@example.org", "41790000000", 1
    )
    assert {(f.kind, f.severity) for f in findings} == {
        ("readability", "befund"), ("contact", "befund"),
    }
    thin = A.check_extracted_text("pypdf", "Jane", "de", None, None, 3)
    assert thin[0].kind == "text_layer"


def test_english_headings_accept_both_competency_variants() -> None:
    text = JOINED.replace("SCHLÜSSELKOMPETENZEN", "CORE COMPETENCIES")
    en = (
        "MANAGEMENT SUMMARY CORE COMPETENCIES PROFESSIONAL EXPERIENCE EDUCATION "
        "CERTIFICATES LANGUAGES SKILLS & TOOLS jane@example.org " * 5
    )
    assert A.check_extracted_text("pypdf", en, "en", "jane@example.org", None, 1) == []
    assert A.check_extracted_text("pypdf", text, "de", None, None, 1)  # DE heading missing


def test_contact_is_read_from_the_standard_cv_header(tmp_path: Path) -> None:
    cv = tmp_path / "standard_cv.md"
    cv.write_text(
        "# Jane Doe\n\n**Mobile:** +41 79 000 00 00\n**E-Mail:** jane@example.org\n",
        encoding="utf-8",
    )
    assert A._contact_from_standard_cv(cv) == ("jane@example.org", "41790000000")
    assert A._contact_from_standard_cv(tmp_path / "missing.md") == (None, None)


# --- report ----------------------------------------------------------------

def test_write_ats_report_counts_and_writes_the_report(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "01_analyse.md").write_text(ANALYSIS, encoding="utf-8")
    (tmp_path / "04_final_de.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(A, "_cv_text", lambda run_dir, lang, std: (
        "Als Pilot eingesetzt. Epics und User Stories." if lang == "de" else ""
    ))
    report, befunde, hinweise = A.write_ats_report(tmp_path, standard_cv_path=tmp_path / "none.md")
    assert report.name == "_ats.md" and report.is_file()
    # barred «Pilot» and the missing «Requirements Engineering / Anforderungen» are hints
    assert (befunde, hinweise) == (0, 2)
    body = report.read_text(encoding="utf-8")
    assert "Begriffe DE" in body and "Report-only" in body


def test_report_without_vocabulary_table_says_so(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "01_analyse.md").write_text("### 1. Kontext\n", encoding="utf-8")
    monkeypatch.setattr(A, "_cv_text", lambda run_dir, lang, std: "text" if lang == "de" else "")
    _, befunde, hinweise = A.write_ats_report(tmp_path, standard_cv_path=tmp_path / "none.md")
    assert (befunde, hinweise) == (0, 1)


def test_very_short_barred_terms_are_case_sensitive() -> None:
    vocab = [A.VocabTerm("RE", "nicht", "Kürzel")]
    assert A.check_terms(vocab, "Gesamtverantwortung, Re-Launch, re-use") == []
    assert [f.kind for f in A.check_terms(vocab, "Erfahrung in RE und Betrieb")] == ["term_barred"]


# --- reading order -----------------------------------------------------------

CV_MD = """## Berufserfahrung

### 2023–2025 | Beta GmbH – Senior Product Owner

- Anforderungen direkt mit Praxisteams erhoben und Epics abgeleitet.

---

### 2015–2023 | Gamma AG – Projektleiter Datenprodukte

- Interne App-Entwicklung aufgebaut und Roadmap verantwortet.

---

### 2011–2014 | Delta – Gründer

- Produkt über Hypothesentests entwickelt und verkauft.
"""

GOOD = (
    "Senior Product Owner Beta GmbH Anforderungen direkt mit Praxisteams erhoben und Epics "
    "abgeleitet Projektleiter Datenprodukte Gamma AG Interne App-Entwicklung aufgebaut und "
    "Roadmap Gründer Delta Produkt über Hypothesentests entwickelt und verkauft"
)
BROKEN = (
    "Senior Product Owner Beta GmbH Projektleiter Datenprodukte Gamma AG Gründer Delta "
    "Anforderungen direkt mit Praxisteams erhoben und Epics abgeleitet Interne "
    "App-Entwicklung aufgebaut und Roadmap Produkt über Hypothesentests entwickelt"
)


def test_station_markers_use_role_and_first_bullet_words() -> None:
    markers = A.station_markers(CV_MD)
    assert [m[1] for m in markers] == [
        "senior product owner", "projektleiter datenprodukte", "gründer",
    ]
    assert markers[0][2] == "anforderungen direkt mit praxisteams erhoben und"


def test_stations_without_a_body_line_are_ignored() -> None:
    md = "## Berufserfahrung\n\n### 2007–2011 | Epsilon AG – Berater\n\n---\n"
    assert A.station_markers(md) == []


def test_reading_order_passes_when_headers_sit_with_their_bullets() -> None:
    assert A.check_reading_order("pypdf", GOOD, A.station_markers(CV_MD)) == []


def test_reading_order_flags_all_headers_before_all_bullets() -> None:
    findings = A.check_reading_order("pypdf", BROKEN, A.station_markers(CV_MD))
    assert [(f.kind, f.severity) for f in findings] == [("reading_order", "befund")]
    assert "Gamma AG" in findings[0].message and "pypdf" in findings[0].message


def test_reading_order_ignores_stations_it_cannot_locate() -> None:
    assert A.check_reading_order("pdfplumber", "ganz anderer Text", A.station_markers(CV_MD)) == []


# --- Komposita: Bindestrich am Zeilenende (Jobcoach-Recherche 2026-10-30) -------

COMPOUND_MD = """# Jane Doe

**Kontakt:** jane-doe@example.org
**GitHub:** [github.com/jane-doe](https://github.com/jane-doe)

## Berufserfahrung

### 2015–2023 | Acme AG – Product Owner

- **Release-Stabilität** im SAFe-Umfeld, Stakeholder-Koordination 2015-2023, 80-100 %.
- Größen-Änderung und Über-Prüfung der Cloud-First-Strategie.
"""


def test_compound_words_skip_ranges_urls_and_emails() -> None:
    words = A.compound_words(COMPOUND_MD)
    assert words == {
        "Release-Stabilität", "SAFe-Umfeld", "Stakeholder-Koordination",
        "Größen-Änderung", "Über-Prüfung", "Cloud-First-Strategie",
    }


def test_intact_compounds_are_clean_even_with_a_line_break_after_the_hyphen() -> None:
    text = (
        "Release-\nStabilität im SAFe-Umfeld, Stakeholder-Koordination\n"
        "Größen-Änderung Über-Prüfung Cloud-First-\nStrategie"
    )
    assert A.check_compounds("pypdf", COMPOUND_MD, text) == []


def test_merged_compound_is_a_finding_and_names_the_merged_word() -> None:
    text = (
        "ReleaseStabilität im SAFe-Umfeld Stakeholder-Koordination "
        "Größen-Änderung Über-Prüfung Cloud-First-Strategie"
    )
    findings = A.check_compounds("pdftotext", COMPOUND_MD, text)
    assert len(findings) == 1
    f = findings[0]
    assert (f.kind, f.severity) == ("compound_merged", "befund")
    assert "Release-Stabilität" in f.message and "pdftotext" in f.message
    assert "ReleaseStabilität" in f.message


def test_umlaut_compound_merged() -> None:
    text = (
        "Release-Stabilität SAFe-Umfeld Stakeholder-Koordination "
        "GrößenÄnderung Über-Prüfung Cloud-First-Strategie"
    )
    findings = A.check_compounds("pdftotext", COMPOUND_MD, text)
    assert [f.kind for f in findings] == ["compound_merged"]
    assert "Größen-Änderung" in findings[0].message


def test_missing_compound_without_merged_form_is_still_a_finding() -> None:
    text = "Release-Stabilität SAFe-Umfeld Stakeholder-Koordination Größen-Änderung Über-Prüfung"
    findings = A.check_compounds("pypdf", COMPOUND_MD, text)
    assert len(findings) == 1 and "Cloud-First-Strategie" in findings[0].message


def test_pdftotext_is_skipped_cleanly_when_binary_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(A.shutil, "which", lambda _name: None)
    assert A._extract_pdftotext(tmp_path / "x.pdf") is None


def test_pdftotext_extracts_when_available(monkeypatch, tmp_path: Path) -> None:
    class Done:
        returncode = 0
        stdout = "Seite eins\fSeite zwei\f"

    monkeypatch.setattr(A.shutil, "which", lambda _name: "/usr/bin/pdftotext")
    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: Done())
    assert A._extract_pdftotext(tmp_path / "x.pdf") == ("Seite eins\fSeite zwei\f", 2)


def test_report_has_compound_section_and_a_merge_is_a_befund(monkeypatch, tmp_path: Path) -> None:
    """A fused compound is a Befund, so `verify` (which fails on any Befund) stops."""
    run = tmp_path / "2026-10-30_demo"
    run.mkdir()
    (run / "04_final_de.md").write_text(
        "# Finaler CV (DE)\n\n## Management Summary\n\nIm SAFe-Umfeld gearbeitet.\n\n"
        "## Schlüsselkompetenzen\n\n**Backlog** - Release-Stabilität.\n\n"
        "## Berufserfahrung\n\n### 2020–2023 | Acme AG – Product Owner\n\n- Erster Bullet.\n",
        encoding="utf-8",
    )
    (run / "04_final_de.pdf").write_bytes(b"%PDF-1.4 dummy")
    std = tmp_path / "std.md"
    std.write_text("# Jane Doe\n\n**E-Mail:** jane@example.org\n", encoding="utf-8")
    fused = (
        "Management Summary Im SAFeUmfeld gearbeitet Schlüsselkompetenzen Backlog "
        "ReleaseStabilität Berufserfahrung Ausbildung Zertifikate Sprachkenntnisse Software "
        "jane@example.org " + "x" * 700
    )
    monkeypatch.setattr(A, "_extract_pypdf", lambda _p: None)
    monkeypatch.setattr(A, "_extract_pdfplumber", lambda _p: None)
    monkeypatch.setattr(A, "_extract_pdftotext", lambda _p: (fused, 1))
    report, befunde, _ = A.write_ats_report(run, standard_cv_path=std)
    text = report.read_text(encoding="utf-8")
    assert "## Komposita DE" in text
    assert befunde >= 1 and "compound_merged" in text and "SAFe-Umfeld" in text


def test_spaced_hyphen_as_separator_does_not_glue_the_next_word() -> None:
    """DE competencies read «Titel - Feature-Adoption gemessen»: the lone hyphen separates."""
    md = "**Wirkung messen** - Feature-Adoption gemessen."
    assert A.check_compounds("pdftotext", md, "Wirkung messen - Feature-Adoption gemessen.") == []
