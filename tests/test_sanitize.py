from apps.core.sanitize import clean_html, strip_html


def test_clean_html_removes_scripts_and_handlers() -> None:
    dirty = '<p onclick="x()">Hi <script>alert(1)</script><a href="javascript:evil()">l</a></p>'
    cleaned = clean_html(dirty)
    assert "script" not in cleaned and "onclick" not in cleaned and "javascript:" not in cleaned
    assert "Hi" in cleaned


def test_clean_html_keeps_safe_links_with_rel() -> None:
    cleaned = clean_html('<a href="https://example.com">x</a>')
    assert 'href="https://example.com"' in cleaned and "noopener" in cleaned


def test_strip_html_returns_text_only() -> None:
    assert strip_html("<b>bold</b> <i>text</i>") == "bold text"
