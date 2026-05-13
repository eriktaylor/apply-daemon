"""Tests for the generic text extractor."""

from src.text_extractor import extract_links, extract_text

SAMPLE_HTML = """
<html>
<head><style>body { color: red; }</style></head>
<body>
<script>var x = 1;</script>
<div style="display:none">Hidden content</div>
<h1>Job Alert</h1>
<p>Senior Engineer at Acme Corp</p>
<p>San Francisco, CA</p>
<a href="https://example.com/job/123">Apply now</a>
<a href="https://example.com/unsubscribe">Unsubscribe</a>
<a href="https://example.com/tracking.gif">pixel</a>
</body>
</html>
"""


def test_extract_text_removes_scripts():
    text = extract_text(SAMPLE_HTML)
    assert "var x" not in text


def test_extract_text_removes_styles():
    text = extract_text(SAMPLE_HTML)
    assert "color: red" not in text


def test_extract_text_removes_hidden():
    text = extract_text(SAMPLE_HTML)
    assert "Hidden content" not in text


def test_extract_text_preserves_content():
    text = extract_text(SAMPLE_HTML)
    assert "Senior Engineer at Acme Corp" in text
    assert "San Francisco" in text


def test_extract_text_collapses_blank_lines():
    text = extract_text("<p>A</p>\n\n\n\n<p>B</p>")
    assert "\n\n\n" not in text


def test_extract_links_filters_tracking():
    links = extract_links(SAMPLE_HTML)
    assert "https://example.com/job/123" in links
    assert not any("unsubscribe" in link for link in links)
    assert not any(".gif" in link for link in links)


def test_extract_links_skips_non_http():
    html = '<a href="mailto:test@example.com">Email</a><a href="https://example.com">Link</a>'
    links = extract_links(html)
    assert links == ["https://example.com"]
