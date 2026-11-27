"""Faktencheck-Urteil: festes Antwortformat statt Stichwortsuche (ab 2026-11-19).

Vorher las der Code Prosa per Stichwortsuche und hob `veto: true` auf, wenn
im Befund «keine Drift» o. ä. stand (MLOpsCo-Workaround 2026-05-19). Das
Faktencheck-Eval zeigte, dass genau diese Regeln eingebaute Fehler
durchliessen. Jetzt gilt: nur ein gültiges JSON-Urteil zählt; Prosa ist kein
Urteil (→ Wiederholung, dann Veto); ein gelistetes Problem ist immer ein Veto.
"""

import json
from pathlib import Path

from cv_tailor.agents.factcheck import (
    FACTCHECK_RESPONSE_FORMAT,
    FACTCHECK_SCHEMA,
    GRADES,
    _format_problems,
    _parse_verdict,
    _section_verdict_usable,
    _verdict_known,
)


def _answer(**fields) -> str:
    base = {
        "has_gaps": False,
        "veto": False,
        "probleme": [],
        "questions_markdown": "",
        "findings_markdown": "",
    }
    base.update(fields)
    return json.dumps(base, ensure_ascii=False)


def test_prose_is_not_a_verdict_even_with_clean_wording():
    """Früher Freispruch per «Keine strukturellen Vetos» — heute unbekannt."""
    prose = "**Keine strukturellen Vetos gefunden.** Alle Bullets sind in den Belegen vorhanden."
    assert _parse_verdict(prose) is None
    assert _verdict_known(prose) is False


def test_prose_with_gap_wording_is_not_a_verdict_either():
    assert _parse_verdict("Claim X ist nicht belegt. Klärung erforderlich.") is None


def test_explicit_veto_is_kept_despite_clean_marker_in_findings():
    """Früher hob «Keine Drift gefunden» das Veto auf (Eval: 6 verlorene Treffer)."""
    v = _parse_verdict(_answer(veto=True, findings_markdown="Keine Drift gefunden. Alles belegt."))
    assert v is not None and v["veto"] is True


def test_listed_problem_is_a_veto_even_if_flag_is_false():
    problem = {
        "behauptung": "Führung eines 15-köpfigen Teams",
        "art": "zahl",
        "beleg_ids": ["BELG-012"],
        "begruendung": "Beleg nennt 6 Personen.",
    }
    v = _parse_verdict(_answer(veto=False, probleme=[problem]))
    assert v["veto"] is True
    rendered = _format_problems(v["probleme"])
    assert "15-köpfigen" in rendered and "[zahl]" in rendered and "BELG-012" in rendered


def test_clean_answer_is_clean():
    v = _parse_verdict(_answer(findings_markdown="Keine Drift gefunden."))
    assert v["veto"] is False and v["has_gaps"] is False


def test_markdown_fence_is_tolerated():
    assert _parse_verdict("```json\n" + _answer(veto=True) + "\n```")["veto"] is True


def test_json_without_boolean_verdict_is_unknown():
    assert _parse_verdict("{}") is None
    assert _parse_verdict('{"veto": "nein"}') is None
    assert _parse_verdict("[]") is None


def test_schema_is_strict_and_complete():
    """Structured Outputs verlangen additionalProperties=false und alle Felder als required."""
    assert FACTCHECK_SCHEMA["additionalProperties"] is False
    assert set(FACTCHECK_SCHEMA["required"]) == set(FACTCHECK_SCHEMA["properties"])
    item = FACTCHECK_SCHEMA["properties"]["probleme"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    assert FACTCHECK_RESPONSE_FORMAT["json_schema"]["strict"] is True


def _problem(**fields) -> dict:
    return {"behauptung": "x", "art": "sonstiges", "beleg_ids": [], "begruendung": "y", **fields}


def test_only_hints_do_not_block():
    v = _parse_verdict(_answer(probleme=[_problem(einstufung="hinweis")]))
    assert v["veto"] is False and v["probleme"] == [] and len(v["hinweise"]) == 1


def test_everything_else_blocks():
    for einstufung in ("widerspruch", "ueberzeichnung", "kein_beleg", "neu", None):
        v = _parse_verdict(_answer(probleme=[_problem(einstufung=einstufung)]))
        assert v["veto"] is True, einstufung
    assert _parse_verdict(_answer(probleme=[_problem()]))["veto"] is True
    assert _parse_verdict(_answer(probleme=["Text"]))["veto"] is True


def test_section_verdict_needs_list_boolean_and_named_passage():
    assert _section_verdict_usable(_answer()) is True
    assert _section_verdict_usable('{"veto": false}') is False
    assert _section_verdict_usable('{"has_gaps": false, "probleme": []}') is False
    assert _section_verdict_usable(_answer(probleme=3)) is False
    assert _section_verdict_usable(_answer(veto=True)) is False
    hint = _problem(einstufung="hinweis")
    assert _section_verdict_usable(_answer(veto=True, probleme=[hint])) is False


def test_prompt_names_every_grade():
    prompt = Path("prompts/factcheck.md").read_text(encoding="utf-8")
    assert all(f"`{grade}`" in prompt for grade in GRADES)
    assert FACTCHECK_SCHEMA["properties"]["probleme"]["items"]["properties"]["einstufung"][
        "enum"
    ] == list(GRADES)


def test_clean_section_verdict_must_cover_the_lines():
    text = "### Station\n- a\n- b\n---\n- c\n"
    assert _section_verdict_usable(_answer(), text) is False
    assert _section_verdict_usable(_answer(geprueft=["a", "b"]), text) is False
    assert _section_verdict_usable(_answer(geprueft=["a", "b", "c"]), text) is True
    # Ein Veto bleibt ein Veto, auch ohne vollen Nachweis.
    problem = _problem(einstufung="kein_beleg")
    assert _section_verdict_usable(_answer(probleme=[problem]), text) is True
