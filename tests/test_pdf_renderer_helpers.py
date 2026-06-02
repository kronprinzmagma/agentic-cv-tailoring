"""Tests for PDF-renderer helpers (no headless browser needed)."""
from cv_tailor.pdf_renderer import _strip_generated_tail


def test_strips_translator_skills_and_tools_section():
    """MLOpsCo regression: Translator emitted ## Skills & Tools with
    Languages inline. Renderer must strip before appending real tail."""
    text = (
        "## Management Summary\nText.\n\n"
        "## Professional Experience\nText.\n\n"
        "## Skills & Tools\n\n"
        "German (native), English (C1), French (B1)\n"
        "MS Office, VS Code\n"
    )
    out = _strip_generated_tail(text)
    assert "## Skills & Tools" not in out
    assert "MS Office" not in out
    # Real sections preserved
    assert "## Management Summary" in out
    assert "## Professional Experience" in out


def test_strips_education_block_too():
    """Education is a tail-section — never produced by writer/translator."""
    text = (
        "## Professional Experience\nText.\n\n"
        "## Education\n- 2005-2007 something\n"
    )
    out = _strip_generated_tail(text)
    assert "## Education" not in out


def test_strips_languages_block():
    text = (
        "## Professional Experience\nText.\n\n"
        "## Languages\nGerman, English\n"
    )
    out = _strip_generated_tail(text)
    assert "## Languages" not in out


def test_german_tail_variants_also_stripped():
    """Defensive: DE variants must also be caught (writer shouldn't produce them
    but we don't want to depend on prompt discipline alone)."""
    text = (
        "## Berufserfahrung\nText.\n\n"
        "## Sprachkenntnisse\nDeutsch\n\n"
        "## Software & Tools\nMS Office\n"
    )
    out = _strip_generated_tail(text)
    assert "Sprachkenntnisse" not in out
    assert "Software & Tools" not in out


def test_legitimate_headings_preserved():
    """Don't accidentally drop core CV sections."""
    text = (
        "## Management Summary\nIntro.\n\n"
        "## Schlüsselkompetenzen\n- **Headline** - text.\n\n"
        "## Berufserfahrung\n### 2023 | Company\n- bullet\n"
    )
    out = _strip_generated_tail(text)
    assert "Management Summary" in out
    assert "Schlüsselkompetenzen" in out
    assert "Berufserfahrung" in out


def test_handles_and_vs_ampersand_in_heading():
    """Translator sometimes writes 'Skills and Tools' (no '&'). Must still be caught."""
    text = "## Professional Experience\nText.\n\n## Skills and Tools\nMS Office\n"
    out = _strip_generated_tail(text)
    assert "Skills and Tools" not in out
    assert "MS Office" not in out
