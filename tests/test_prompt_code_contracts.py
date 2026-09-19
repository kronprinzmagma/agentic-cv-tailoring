"""Drift guards for Prompt↔Code-Verträge.

Zwei Invarianten, die bisher nur per Konvention galten:

1. **Untrusted-Input-Grenze:** Jeder Prompt, dessen Agent die Stellenanzeige
   (00_stellenanzeige.md) im Kontext sieht, muss die Injection-Grenze
   deklarieren. Der Writer ist bewusst ausgenommen — er sieht die Anzeige
   nur via Analyse (siehe CLAUDE.md).
2. **Bullet-Wortbudget:** Die 22-Wörter-Grenze lebt als Literal in mehreren
   Prompts und als ``BULLET_MAX_WORDS`` in length_check.py. Dieser Test
   bricht, wenn eine Seite getunt wird, ohne die andere nachzuziehen.
"""
from pathlib import Path

import re

from cv_tailor.length_check import BULLET_MAX_WORDS

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

# Prompts, deren Agent die rohe Stellenanzeige im Kontext hat.
POSTING_CONSUMING_PROMPTS = [
    "analyst.md",
    "factcheck.md",
    "translator.md",
    "naturalisation.md",
    "keyword_marker.md",
    "coach_reviewer.md",
]

BULLET_RULE_PROMPTS = ["writer.md", "coach_reviewer.md"]


def test_posting_consuming_prompts_declare_untrusted_input():
    for name in POSTING_CONSUMING_PROMPTS:
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        assert "Untrusted-Input-Grenze" in text, (
            f"prompts/{name}: Agent sieht die Stellenanzeige, aber der Prompt "
            "deklariert keine Untrusted-Input-Grenze."
        )


def test_bullet_word_limit_matches_length_check():
    for name in BULLET_RULE_PROMPTS:
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        found_any = False
        for line in text.splitlines():
            if "Bullet" in line and "Wörter" in line:
                for num in re.findall(r"\d+", line):
                    found_any = True
                    assert int(num) == BULLET_MAX_WORDS, (
                        f"prompts/{name}: Bullet-Regel nennt {num} Wörter, "
                        f"length_check.BULLET_MAX_WORDS ist {BULLET_MAX_WORDS}."
                    )
        assert found_any, f"prompts/{name}: keine Bullet-Wortbudget-Zeile gefunden."


def test_analyst_source_rule_covers_gap_section():
    """Die Quellenregel muss auch fuer Abschnitt 5 (Gap vs. Framing) gelten.

    Regression (Sunrise-Lauf, 2026-09-07): Die Anforderungstabelle war sauber —
    sie listete exakt die fuenf "YOUR SKILLS"-Punkte. Aber Abschnitt 5 erklaerte
    "Opportunity Solution Trees, Usability Testing, Journey Mapping" zur echten
    Luecke, obwohl diese Begriffe ausschliesslich im Aufgabenblock ("YOUR
    CHALLENGE") stehen. Der Faktencheck machte daraus eine Klaerungsfrage.

    Die Regel in Zeile 42 war auf "fuer diese Tabelle" begrenzt und lief an
    Abschnitt 5 vorbei.
    """
    text = Path("prompts/analyst.md").read_text(encoding="utf-8")
    gap_section = text.split("### 5. Gap vs. Framing", 1)[1].split("### 6.", 1)[0]

    assert "Quellenregel" in gap_section, (
        "Abschnitt 5 muss die Quellenregel explizit uebernehmen, sonst leckt "
        "Aufgaben-Vokabular als 'echte Luecke' zurueck"
    )
    assert "Anforderungsblock" in gap_section
    assert "Selbsttest" in gap_section


def test_companion_block_titles_match_prompt_contracts():
    """The block titles emitted by companion_docs.format_for_prompt must be the
    ones the prompts describe — otherwise the rules apply to a block the model
    never sees under that name."""
    from cv_tailor.companion_docs import format_for_prompt

    analyst_title = format_for_prompt("x", role="analyst").splitlines()[0].lstrip("# ").strip()
    reviewer_title = format_for_prompt("x", role="reviewer").splitlines()[0].lstrip("# ").strip()
    analyst = (PROMPTS_DIR / "analyst.md").read_text(encoding="utf-8")
    coach = (PROMPTS_DIR / "coach_reviewer.md").read_text(encoding="utf-8")
    factcheck = (PROMPTS_DIR / "factcheck.md").read_text(encoding="utf-8")

    assert "Unternehmenskontext und Zielpublikum (Begleitrecherche" in analyst
    assert analyst_title.startswith("Unternehmenskontext und Zielpublikum")
    for text, name in ((coach, "coach_reviewer.md"), (factcheck, "factcheck.md")):
        assert "Lauf-Kontext aus Begleitrecherche" in text, name
    assert reviewer_title.startswith("Lauf-Kontext aus Begleitrecherche")


def test_analyst_forbids_requirements_from_companion_research():
    """Research is framing, never a requirement source (Quellenregel bleibt)."""
    analyst = (PROMPTS_DIR / "analyst.md").read_text(encoding="utf-8")
    block = analyst.split("**Begleitrecherche (optional, hart):**", 1)[1].split("\n\n", 1)[0]
    assert "Abschnitt 4 und 5" in block
    assert "keine** Anforderung" in block and "keine** LÜCKE" in block
