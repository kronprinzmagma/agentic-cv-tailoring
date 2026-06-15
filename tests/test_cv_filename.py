"""Tests for recruiter-friendly filename generation."""
from cv_tailor.cv_filename import friendly_cv_filename


POSTING_FULL = """# Product Manager – AI-Platform-Co

**Standort:** Zürich
**Unternehmen:** AI-Platform-Co

## Über AI-Platform-Co
"""

POSTING_NO_COMPANY_LINE = "# Senior Engineer at FictionalCorp\n\nDescription...\n"

POSTING_SLASHES = "# Tech Lead / Lead Engineer\n\n**Unternehmen:** Big Co (Suisse) S.A.\n"


def test_full_posting_produces_company_and_position():
    fn = friendly_cv_filename(POSTING_FULL, language="de")
    assert "AI-Platform-Co" in fn
    assert "Product Manager" in fn
    assert fn.endswith(".md")
    assert "_EN" not in fn


def test_english_language_adds_suffix():
    fn = friendly_cv_filename(POSTING_FULL, language="en", extension=".pdf")
    assert fn.endswith("_EN.pdf")


def test_position_strips_trailing_company_after_dash():
    """When heading is 'Product Manager – AI-Platform-Co' the trailing dash-section
    should be dropped to avoid duplicating the company name."""
    fn = friendly_cv_filename(POSTING_FULL, language="de")
    # Position should be "Product Manager", not "Product Manager – AI-Platform-Co"
    assert "Product Manager" in fn
    # Should not contain the dash form
    assert " – " not in fn


def test_no_company_line_still_yields_filename():
    fn = friendly_cv_filename(POSTING_NO_COMPANY_LINE, language="de")
    assert "Senior Engineer" in fn  # position survives
    # Filesystem-safe (no slashes, etc.)
    assert "/" not in fn


def test_fallback_slug_used_when_neither_parsed():
    fn = friendly_cv_filename(
        "no heading or company at all\n\njust a paragraph",
        fallback_slug="custom-fallback",
    )
    assert "custom-fallback" in fn


def test_unsafe_chars_sanitized():
    """Company names with slashes or colons must not produce path-illegal filenames."""
    fn = friendly_cv_filename(POSTING_SLASHES, language="de")
    assert "/" not in fn
    assert ":" not in fn


def test_extension_normalization():
    """Both '.pdf' and 'pdf' should be accepted."""
    a = friendly_cv_filename(POSTING_FULL, extension=".pdf")
    b = friendly_cv_filename(POSTING_FULL, extension="pdf")
    assert a == b


def test_author_token_uses_config(monkeypatch, tmp_path):
    """The exported CV_AUTHOR_TOKEN is built from output.author_name in config.yaml."""
    from cv_tailor import cv_filename as cf
    # Demo persona override
    monkeypatch.setenv("CV_AUTHOR_NAME", "Alex Müller")
    author, anchor = cf._load_output_config()
    assert author == "Alex Müller"
    token = cf._author_token(author)
    assert token == "CV_Alex_Müller"


def test_default_token_when_env_and_config_missing(monkeypatch, tmp_path):
    from cv_tailor import cv_filename as cf
    monkeypatch.delenv("CV_AUTHOR_NAME", raising=False)
    bogus = tmp_path / "missing.yaml"
    author, anchor = cf._load_output_config(config_path=bogus)
    assert author == "Author"
    assert anchor == "000000"


def test_token_replaces_spaces_with_underscores():
    from cv_tailor.cv_filename import _author_token
    assert _author_token("Alex Müller") == "CV_Alex_Müller"
    assert _author_token("Alex Müller") == "CV_Alex_Müller"
    assert _author_token("Three Word Name") == "CV_Three_Word_Name"


def test_token_strips_filesystem_illegal_chars():
    from cv_tailor.cv_filename import _author_token
    # Slashes and colons must not survive
    token = _author_token("Bad/Name:Test")
    assert "/" not in token
    assert ":" not in token
