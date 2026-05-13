"""Generic, template-agnostic text and link extraction from HTML emails."""

from __future__ import annotations

from bs4 import BeautifulSoup


def extract_text(html: str) -> str:
    """Extract readable text from any HTML email. Template-agnostic."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove script, style, and hidden elements
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(
        attrs={"style": lambda s: s and "display:none" in s.replace(" ", "")}
    ):
        tag.decompose()
    for tag in soup.find_all(
        attrs={"style": lambda s: s and "visibility:hidden" in s.replace(" ", "")}
    ):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)

    # Collapse multiple blank lines
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")

    return text.strip()


def extract_links(html: str) -> list[str]:
    """Extract all href URLs from HTML, filtering out tracking/unsubscribe links."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    skip_patterns = [
        "unsubscribe",
        "optout",
        "preferences",
        "tracking",
        "beacon",
        ".gif",
    ]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(p in href.lower() for p in skip_patterns):
            continue
        if href.startswith("http"):
            links.append(href)
    return links


def get_html_body(msg) -> str | None:
    """Extract HTML body from an email.message.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(charset, errors="replace")
    elif msg.get_content_type() == "text/html":
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(charset, errors="replace")
    return None
