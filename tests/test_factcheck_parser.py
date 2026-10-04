"""Tests for the factcheck response parser (no LLM calls).

Pins the Global-No-Veto-Verdict logic and CLEAN_FINDING_MARKERS handling
that prevented the MLOpsCo false-positive blocker on 2026-05-19.
"""
from cv_tailor.agents.factcheck import _details_are_clean, _parse_factcheck_result


def test_global_no_veto_shortcuts_prose_response():
    """Real MLOpsCo v2 output: prose, not JSON, with a global verdict
    that overrules per-line gap keywords like 'nicht belegt'."""
    content = """**Belege geprüft:**

- **KI-Produkt-Ownership**: BELG-013 ✓
- **Roadmap und Priorisierung**: BELG-011 ✓
- **Kommunikation auf allen Ebenen**: BELG-016 ✓

**Keine strukturellen Vetos gefunden, aber mehrere Klärungslücken zur Titelkohärenz:**

- **Header-Lücke MediaCorp**: BELG-077 zeigt Senior Product Owner Titel. OK.
- **GastroSaaS Titel**: 'Managing Director' ist nicht belegt im Beleg-Index.

**Alle Bullets sind in den Belegen vorhanden.** Keine sachliche Drift gefunden.
"""
    veto, _ = _parse_factcheck_result(content, default_text_key="findings_markdown")
    assert veto is False, "Global No-Veto-Verdict must short-circuit per-line gap matches"


def test_real_veto_caught_when_no_global_clean_verdict():
    content = """**Befund:** Eine Behauptung 'Enterprise-Kunden aus Finanz- und Gesundheitssektor' ist nicht belegt.

Klärung erforderlich vor weiterer Optimierung.
"""
    veto, _ = _parse_factcheck_result(content, default_text_key="findings_markdown")
    assert veto is True


def test_negated_gap_in_same_line_not_a_veto():
    content = "Keine Lücken gefunden — alles belegt."
    veto, _ = _parse_factcheck_result(content, default_text_key="findings_markdown")
    assert veto is False


def test_clean_details_override_via_marker():
    """CLEAN_FINDING_MARKERS contains 'keine sachliche drift' — bug fix
    from 2026-05-18 (vorher matchte nur 'keine drift' als exakter Substring,
    scheiterte an Zwischenwörtern wie 'sachliche')."""
    details = "Keine sachliche Drift gefunden. Alle Belege passen."
    assert _details_are_clean(details) is True


def test_clean_via_checkmark_threshold():
    """≥3 ✓ + no ✗ + no unhedged 'nicht belegt' → override fires."""
    details = "BELG-A ✓\nBELG-B ✓\nBELG-C ✓\nAlle drei sauber."
    assert _details_are_clean(details) is True


def test_clean_override_blocked_by_unhedged_fail_phrase():
    """Even with multiple ✓, an unhedged 'nicht belegt' must NOT clean-override."""
    details = "BELG-A ✓\nBELG-B ✓\nBELG-C ✓\nClaim X ist nicht belegt."
    assert _details_are_clean(details) is False


def test_clean_override_respects_failure_mark():
    """A ✗ mark anywhere blocks the clean-override path."""
    details = "BELG-A ✓\nBELG-B ✓\nBELG-C ✓\nBELG-D ✗"
    assert _details_are_clean(details) is False


def test_clean_override_needs_min_3_checkmarks():
    """Single ✓ no longer qualifies (WR-07 strict heuristic)."""
    details = "BELG-A ✓"
    assert _details_are_clean(details) is False


def test_json_parsing_takes_priority():
    """When the model produces well-formed JSON, that path wins."""
    content = '{"veto": false, "findings_markdown": "all good"}'
    veto, details = _parse_factcheck_result(content, default_text_key="findings_markdown")
    assert veto is False
    assert "all good" in details
