"""Stil- und Gerüst-Check: patterns Alex removed from every October CV."""

from __future__ import annotations

from cv_tailor.style_check import check_style, format_findings_for_writer

TOKENS = ["acme", "beta", "gamma"]


def kinds(section: str, text: str) -> list[str]:
    return [f.kind for f in check_style(section, text, TOKENS)]


def test_meta_and_antithesis_in_summary() -> None:
    text = (
        "Was mich für diese Rolle qualifiziert: Ich verbinde Fach und Technik. "
        "Bei Acme habe ich 3 Teams geführt und 20 % gespart.\n\n"
        "Produkte stehen und fallen mit der Adoption, nicht mit der Technik.\n"
    )
    # The thesis paragraph has no first person either.
    assert kinds("management_summary", text) == ["meta", "antithese", "telegrammstil"]


def test_nicht_sondern_and_trailing_abgrenzung() -> None:
    assert "antithese" in kinds(
        "management_summary",
        "Ich nutze KI nicht als Thema, sondern als Werkzeug. 2 Jahre, 5 Leute.",
    )
    assert "antithese" in kinds(
        "management_summary",
        "KI eingesetzt – nicht um ein Thema zu bewirtschaften. 2 Jahre, 5 Leute.",
    )


def test_summary_needs_two_numbers() -> None:
    assert "zahlen" in kinds("management_summary", "Ich habe bei Acme Produkte geführt.")
    assert "zahlen" not in kinds(
        "management_summary", "Bei Acme in zwölf Monaten 23 Kunden gewonnen."
    )


def test_nominal_bullets_flagged_unless_action_or_zeugnis_wording() -> None:
    text = (
        "- Aufbau der internen App-Entwicklung und Migration zu einem Team.\n"
        "- Strategische Weiterentwicklung und Stabilisierung von Acme Connect.\n"
        "- Verrechnungsfunktion für Praxen in zwei Monaten zum Rollout gebracht.\n"
        "- Gesamtverantwortung für das digitale Portfolio der Gruppe.\n"
        "- Interne App-Entwicklung aufgebaut.\n"
    )
    findings = [f for f in check_style("berufserfahrung", text, TOKENS) if f.kind == "nominalstil"]
    assert [f.excerpt[:10] for f in findings] == ["- Aufbau d", "- Strategi"]


def test_nominal_check_only_in_berufserfahrung() -> None:
    assert "nominalstil" not in kinds(
        "schluesselkompetenzen", "**Aufbau von Teams** - Teams bei Acme aufgebaut."
    )


def test_sharp_s() -> None:
    assert "orthografie" in kinds("berufserfahrung", "- Gleichermaßen geführt.")


def test_competences_without_station_only_beyond_two() -> None:
    two_missing = (
        "**A** - Bei Acme Roadmap verantwortet.\n**B** - Bei Beta Team geführt.\n"
        "**C** - Märkte erschliessen.\n**D** - Daten auswerten.\n"
    )
    assert "kompetenz_ohne_station" not in kinds("schluesselkompetenzen", two_missing)
    three_missing = two_missing + "**E** - Stakeholder führen.\n"
    findings = [f for f in check_style("schluesselkompetenzen", three_missing, TOKENS)]
    assert [f.kind for f in findings] == ["kompetenz_ohne_station"]
    assert "3 Kompetenzen ohne Station" in findings[0].message


def test_writer_block() -> None:
    assert format_findings_for_writer([]).startswith("Keine")
    block = format_findings_for_writer(
        check_style("berufserfahrung", "- Gleichermaßen geführt.", TOKENS)
    )
    assert "[orthografie]" in block and "Gleichermaßen" in block


def test_trailing_nicht_at_sentence_end() -> None:
    text = "Bei Acme 3 Teams, 20 % gespart. Daten und KI sind mein Alltag – nicht Anspruch.\n"
    assert "antithese" in kinds("management_summary", text)


# Benchmark 2026-10-23 (vier Gesprächsfälle): Selbstpositionierung in der
# Summary. Alex: von sich überzeugt, aber keinesfalls arrogant.
SUMMARY_NUMBERS = "Bei Acme 3 Teams geführt, 20 % gespart. "


def test_degree_in_summary_flagged() -> None:
    text = SUMMARY_NUMBERS + "Meinen M.A. HSG bringe ich mit einer klaren Haltung ein.\n"
    found = kinds("management_summary", text)
    assert "abschluss" in found and "meta" in found


def test_degree_outside_summary_and_scrum_master_ok() -> None:
    assert "abschluss" not in kinds("schluesselkompetenzen", "**Agile** - CSPO, M.A. HSG.\n")
    scrum = SUMMARY_NUMBERS + "Mit dem Scrum Master geplant.\n"
    assert "abschluss" not in kinds("management_summary", scrum)


def test_self_positioning_flagged() -> None:
    for sentence in (
        "Was ich mitbringe: ein klares Urteil.",
        "Meine Stärke liegt in der Übersetzung.",
        "Ich bin gewohnt, schnell Klarheit zu schaffen.",
        "Wo Lieferung zählte, habe ich geliefert.",
        "Digitale Plattformen brauchen beides: Tempo und Sorgfalt.",
        "Das ist der Kern dessen, was die NZZ unter Adoption versteht.",
    ):
        assert "meta" in kinds("management_summary", SUMMARY_NUMBERS + sentence + "\n"), sentence


def test_plain_achievement_not_flagged() -> None:
    text = SUMMARY_NUMBERS + "Bei HealthApp habe ich die Releasezeit halbiert.\n"
    assert kinds("management_summary", text) == []


def test_summary_paragraph_without_first_person() -> None:
    text = (
        "GastroSaaS gegründet, in 12 Monaten aufgebaut und an local-directory.example verkauft.\n\n"
        + SUMMARY_NUMBERS
        + "Ich führte 5 Personen.\n"
    )
    found = [
        f for f in check_style("management_summary", text, TOKENS) if f.kind == "telegrammstil"
    ]
    assert len(found) == 1 and "GastroSaaS gegründet" in found[0].excerpt


def test_summary_station_list_with_semicolons() -> None:
    text = "Ich habe bei A 3 Teams geführt; bei B 20 % gespart; bei C ein Produkt lanciert.\n"
    assert "stationsliste" in kinds("management_summary", text)
