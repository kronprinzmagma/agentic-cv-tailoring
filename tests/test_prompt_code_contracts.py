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
