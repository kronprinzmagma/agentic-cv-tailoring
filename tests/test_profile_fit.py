"""Tests for profile-fit gap parsing + severity classification."""
from pathlib import Path

from cv_tailor.profile_fit import (
    FitGap,
    check_profile_fit,
    format_gaps_as_questions,
    format_gaps_for_cli,
    has_critical_gaps,
    parse_anforderungsabgleich,
)


ANALYSE_TEMPLATE = """# Analyse

### 4. Anforderungsabgleich

| Anforderung | Beleg-ID(s) | Status | Kommentar |
|---|---|---|---|
| 5+ Jahre PM-Erfahrung | BELG-007 | STARK | 20 Jahre belegt |
| Strong execution skills | BELG-021 | SCHWACH | implizit |
| AI-Governance / Audit-Readiness | — | LÜCKE | Keine direkte Erfahrung |
| B2B SaaS | BELG-030 | MITTEL | GastroSaaS war SaaS |
| Italienisch | — | LÜCKE | Nicht vorhanden — Nice to have |
| Netzwerk im Gesundheitswesen (von Vorteil) | — | LÜCKE | Kein Hinweis auf aktives Netzwerk |
| Idealerweise mit Hintergrund in Document Management, ECM, Workflow-Automatisierung | — | LÜCKE | Kein direkter ECM-Hintergrund |
| Ideally experience with regulated industries | — | LÜCKE | Not directly evidenced |

### 5. Gap vs. Framing
Some other section.
"""


def test_parse_extracts_all_data_rows(tmp_path):
    rows = parse_anforderungsabgleich(ANALYSE_TEMPLATE)
    statuses = [r[2] for r in rows]
    assert "STARK" in statuses
    assert "LÜCKE" in statuses
    assert len(rows) == 8
    # Separator row must NOT appear as data
    assert not any(r[0].startswith("--") for r in rows)


def test_parse_no_anforderungsabgleich_returns_empty():
    text = "# Analyse\n\n### 1. Etwas anderes\nKein Anforderungsabgleich hier."
    assert parse_anforderungsabgleich(text) == []


def test_check_classifies_critical_lücke(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    gaps = check_profile_fit(p)
    severities = {g.severity for g in gaps}
    requirements = {g.requirement for g in gaps}
    assert "critical" in severities  # AI-Governance has no nice-to-have marker
    assert "AI-Governance / Audit-Readiness" in requirements


def test_check_downgrades_nice_to_have_to_soft(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    gaps = check_profile_fit(p)
    nice = next(g for g in gaps if g.requirement == "Italienisch")
    assert nice.severity == "soft"


def test_von_vorteil_in_requirement_text_downgrades(tmp_path: Path):
    """Pharmasuisse 2026-06-11: '(von Vorteil)' stand im Anforderungstext,
    nicht im Kommentar — wurde fälschlich als kritisch geflaggt."""
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    gaps = check_profile_fit(p)
    netzwerk = next(g for g in gaps if g.requirement.startswith("Netzwerk"))
    assert netzwerk.severity == "soft"


def test_idealerweise_in_requirement_text_downgrades(tmp_path: Path):
    """PEAX 2026-06-19: 'Idealerweise mit Hintergrund in Document Management,
    ECM …' stand als Nice-to-have im Anforderungstext, wurde aber als
    kritische Muss-Lücke ans Gate gemeldet."""
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    gaps = check_profile_fit(p)
    ecm = next(g for g in gaps if "Document Management" in g.requirement)
    assert ecm.severity == "soft"
    # English "Ideally …" qualifier downgrades too
    ideally = next(g for g in gaps if g.requirement.startswith("Ideally"))
    assert ideally.severity == "soft"


def test_check_picks_up_schwach_with_strength_qualifier(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    gaps = check_profile_fit(p)
    # "Strong execution skills" — Strength qualifier + SCHWACH = soft gap
    strong_gaps = [g for g in gaps if "execution" in g.requirement.lower()]
    assert strong_gaps
    assert strong_gaps[0].severity == "soft"


def test_has_critical_gaps_short_circuit(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    assert has_critical_gaps(check_profile_fit(p))


def test_format_gaps_cli_human_readable(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    out = format_gaps_for_cli(check_profile_fit(p))
    assert "Kritisch" in out or "kritisch" in out.lower()
    assert "AI-Governance" in out


# ── Gap-to-Question-Routing ─────────────────────────────────────────────────

GOVERNANCE_GAP = FitGap(
    requirement="Tiefes Verständnis von IT-Governance, Security und Compliance",
    status="LÜCKE",
    comment="Nicht belegt. HealthApp hat regulatorisches Umfeld, aber keine explizite Governance-Verantwortung.",
    severity="critical",
)
NETZWERK_GAP = FitGap(
    requirement="Netzwerk im Gesundheitswesen (von Vorteil)",
    status="LÜCKE",
    comment="Kein Hinweis auf aktives Netzwerk.",
    severity="soft",
)


def test_questions_block_contains_requirement_and_instruction():
    out = format_gaps_as_questions([GOVERNANCE_GAP])
    assert "## Fragen aus dem Profil-Fit-Abgleich" in out
    assert "IT-Governance" in out
    assert "habe ich nicht" in out  # explicit opt-out instruction
    assert "Muss-Anforderung" in out


def test_questions_critical_sorted_first():
    out = format_gaps_as_questions([NETZWERK_GAP, GOVERNANCE_GAP])
    assert out.index("IT-Governance") < out.index("Netzwerk im Gesundheitswesen")


def test_questions_capped():
    gaps = [
        FitGap(f"Anforderung Nummer {i} mit speziellen Details", "LÜCKE", "", "critical")
        for i in range(10)
    ]
    out = format_gaps_as_questions(gaps, max_questions=4)
    assert out.count("Frage: Hast du belegbare Erfahrung") == 4


def test_already_answered_gap_suppressed():
    """Two distinctive requirement words in the stored Q/A corpus → skip."""
    answered = (
        "Frage: Erfahrung mit IT-Governance?\n"
        "Antwort: Bei HealthApp habe ich Compliance-Anforderungen im eHealth-Umfeld umgesetzt."
    )
    out = format_gaps_as_questions([GOVERNANCE_GAP], answered_context=answered)
    assert out == ""


def test_unrelated_answers_do_not_suppress():
    answered = "Frage: Italienisch? Antwort: Nein, nur Grundkenntnisse aus den Ferien."
    out = format_gaps_as_questions([GOVERNANCE_GAP], answered_context=answered)
    assert "IT-Governance" in out


def test_fit_confirmed_in_log_matches_confirmation():
    from cv_tailor.profile_fit import fit_confirmed_in_log
    log_text = (
        "- [10:00:01] run: Stellenanzeige: posting.md\n"
        "- [10:00:42] profile_fit: User bestätigt trotz 2 Profile-Fit-Hinweis(en)\n"
    )
    assert fit_confirmed_in_log(log_text)


def test_fit_confirmed_in_log_ignores_abort_and_unrelated_lines():
    from cv_tailor.profile_fit import fit_confirmed_in_log
    log_text = (
        "- [10:00:42] profile_fit: Run vom User wegen Profile-Fit-Gaps abgebrochen\n"
        "- [10:01:00] web: Faktencheck bestätigt Belege\n"
    )
    assert not fit_confirmed_in_log(log_text)
    assert not fit_confirmed_in_log("")


def test_gap_not_suppressed_by_english_filler_words():
    """'experience' + 'working' alone must not count as an earlier answer
    (observed 2026-09-19: headless-CMS question silently dropped)."""
    from cv_tailor.profile_fit import FitGap, _gap_already_answered

    gap = FitGap(
        requirement="Proven experience working with headless CMS",
        status="LÜCKE", comment="", severity="critical",
    )
    corpus = "I have experience working with analytics tools at MediaCorp."
    assert _gap_already_answered(gap, corpus) is False
    assert _gap_already_answered(gap, corpus + " We used a headless CMS there.") is True


# ── Erweiterte Klärungsfragen (Alex 2026-11-03: «Es gibt nie mehr Klärungsfragen») ──

QUESTION_ANALYSE = """\
### 4. Anforderungsabgleich

| Anforderung | Beleg-ID(s) | Status | Kommentar |
|---|---|---|---|
| Experience leading AI initiatives | BELG-020 | STARK | belegt |
| Experience developing business cases and defining KPIs | BELG-015 | MITTEL | kein formales Business-Case-Dokument |
| Presentation skills in English | BELG-039 | SCHWACH | nur deklariert |

---

### 6. Schlüssel-Vokabular der Anzeige

| Begriff | Bewertung |
|---|---|
| business case development | **natürlich integrierbar** — GastroSaaS |
| emerging technologies | **natürlich integrierbar** — Trend Scouting MediaHoldingCo |
| product roadmap | **natürlich integrierbar** — überall |
| portfolio of AI initiatives | **vorsichtig** — kein formales Portfolio |
| AI governance | **nicht verwenden** — nicht belegt |
"""


def test_mittel_and_unqualified_schwach_become_questions(tmp_path):
    from cv_tailor.profile_fit import collect_question_gaps

    p = tmp_path / "01_analyse.md"
    p.write_text(QUESTION_ANALYSE, encoding="utf-8")
    gaps = collect_question_gaps(p, "Roadmap für die Produkte verantwortet.")
    reqs = {g.requirement: g for g in gaps}
    assert reqs["Experience developing business cases and defining KPIs"].severity == "mittel"
    assert reqs["Presentation skills in English"].severity == "mittel"
    # Gate unverändert: MITTEL und unqualifiziertes SCHWACH lösen keine Fit-Warnung aus.
    assert check_profile_fit(p) == []


def test_vocabulary_terms_missing_in_standard_cv_become_questions(tmp_path):
    from cv_tailor.profile_fit import collect_question_gaps

    p = tmp_path / "01_analyse.md"
    p.write_text(QUESTION_ANALYSE, encoding="utf-8")
    gaps = collect_question_gaps(p, "Product Roadmap für alle Apps verantwortet.")
    vocab = [g.requirement for g in gaps if g.severity == "vokabular"]
    assert "emerging technologies" in vocab
    assert "portfolio of AI initiatives" in vocab        # vorsichtig → fragen
    assert "product roadmap" not in vocab                 # steht im Standard-CV
    assert "AI governance" not in vocab                   # nicht verwenden
    # deckt dieselbe Frage wie die MITTEL-Zeile ab → nicht doppelt
    assert "business case development" not in vocab


def test_suppression_needs_one_entry_covering_the_requirement():
    """Zwei Treffer verstreut über den ganzen Speicher sind keine Antwort."""
    from cv_tailor.profile_fit import _gap_already_answered

    gap = FitGap("Experience developing business cases, defining KPIs and evaluating business impact",
                 "MITTEL", "", "mittel")
    chunks = [
        "Frage: Business-Analytics bei MediaCorp? Antwort: ja.",
        "Frage: Welche KPIs? Antwort: Ertrag, Churn.",
    ]
    assert _gap_already_answered(gap, chunks) is False
    covered = chunks + [
        "Frage: Hast du Business Cases entwickelt, KPIs definiert und den Impact evaluiert? "
        "Antwort: Ja, developing business cases bei GastroSaaS, defining KPIs, evaluating impact."
    ]
    assert _gap_already_answered(gap, covered) is True


def test_mittel_question_wording():
    gap = FitGap("Experience developing business cases", "MITTEL", "", "mittel")
    out = format_gaps_as_questions([gap])
    assert "teilweise belegt" in out
    assert "konkretere Beispiele" in out
