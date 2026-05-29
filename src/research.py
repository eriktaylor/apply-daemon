"""Deep Research module — company intelligence via OpenRouter + web scraping.

Pipeline: OpenRouter query generation → DuckDuckGo + Trafilatura scraping →
Top-chunk selection → OpenRouter synthesis.

Exposes a single callable function for injection into the tailoring engine:
    context = run_deep_research("Acme Corp", "Backend engineer role...")

Returns a concise Markdown string (~2000 words max) or "" on any failure.
"""

from __future__ import annotations

import logging
import os
import time

import openai

logger = logging.getLogger(__name__)

_MAX_WORDS = 2000
_CHUNK_SIZE = 500  # words per chunk
_TOP_CHUNKS = 15   # max chunks to pass to synthesis


# ---------------------------------------------------------------------------
# OpenRouter helpers
# ---------------------------------------------------------------------------

def _get_openrouter_config() -> tuple[str, str]:
    """Return (api_key, model) from env."""
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    model = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite")
    return api_key, model


def _openrouter_generate(
    api_key: str, model: str, prompt: str, max_tokens: int = 512,
) -> str:
    """Generate text via OpenRouter. Returns raw response text."""
    client = openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.0,
    )
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------

def _chunk_text(text: str, chunk_size: int = _CHUNK_SIZE) -> list[str]:
    """Split text into word-bounded chunks."""
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# Search + Scrape
# ---------------------------------------------------------------------------

def _generate_search_queries(
    api_key: str, model: str, company_name: str, job_description: str,
) -> list[str]:
    """Use the LLM to generate 3 targeted search queries about the company."""
    prompt = f"""\
Generate exactly 3 web search queries to research the company "{company_name}" \
for a job application. Focus on:
1. Their tech stack and engineering tools
2. Recent news, funding rounds, or product launches
3. Company culture and engineering blog posts

Job context: {job_description[:500]}

Respond with ONLY 3 queries, one per line, no numbering or bullets.\
"""
    raw = _openrouter_generate(api_key, model, prompt)
    queries = [q.strip().strip("-•*0123456789.") for q in raw.strip().split("\n") if q.strip()]
    queries = queries[:3]
    if not queries:
        queries = [
            f"{company_name} tech stack engineering",
            f"{company_name} funding news",
            f"{company_name} culture engineering blog",
        ]
    return queries


def _search_duckduckgo(query: str, max_results: int = 3) -> list[str]:
    """Search DuckDuckGo and return URLs."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        urls = [r["href"] for r in results if r.get("href")]
        logger.debug("DuckDuckGo extracted %d URLs for query: %s", len(urls), query)
        return urls
    except Exception:
        logger.debug("DuckDuckGo search failed for: %s", query, exc_info=True)
        return []


def _scrape_url(url: str) -> str:
    """Scrape a URL using trafilatura. Returns extracted text or empty string."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url, no_ssl=True)
        if not downloaded:
            return ""
        text = trafilatura.extract(downloaded) or ""
        return text
    except Exception:
        logger.debug("Scraping failed for: %s", url, exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_deep_research(company_name: str, job_description_text: str) -> str:
    """Run the full deep research pipeline for a company.

    Stage 1: Generate search queries via OpenRouter
    Stage 2: Search + scrape top URLs with trafilatura
    Stage 3: Chunk text and select top chunks (by position)
    Stage 4: Synthesize into concise Markdown context via OpenRouter

    Returns:
        Markdown string (~2000 words max) with company research context,
        or "" if the pipeline fails at any point.
    """
    try:
        return _run_deep_research_inner(company_name, job_description_text)
    except Exception:
        logger.warning(
            "Deep research failed for %s — falling back to standard tailoring",
            company_name,
            exc_info=True,
        )
        return ""


def _run_deep_research_inner(company_name: str, job_description_text: str) -> str:
    """Inner implementation — raises on failure for the outer wrapper to catch."""
    api_key, model = _get_openrouter_config()

    # --- Stage 1: Query generation ---
    logger.info("Deep Research [%s] Stage 1: Generating search queries", company_name)
    queries = _generate_search_queries(api_key, model, company_name, job_description_text)
    logger.info("Deep Research [%s] Queries: %s", company_name, queries)

    # --- Stage 2: Search + Scrape ---
    logger.info("Deep Research [%s] Stage 2: Searching and scraping", company_name)
    all_text: list[str] = []
    seen_urls: set[str] = set()

    for i, query in enumerate(queries):
        if i > 0:
            time.sleep(2)
        urls = _search_duckduckgo(query)
        for url in urls:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            content = _scrape_url(url)
            if content and len(content) > 100:
                all_text.append(content)
                logger.debug("Scraped %d chars from %s", len(content), url)

    if not all_text:
        logger.warning("Deep Research [%s] No content scraped", company_name)
        return ""

    combined = "\n\n".join(all_text)
    logger.info(
        "Deep Research [%s] Scraped %d pages, %d total words",
        company_name, len(all_text), len(combined.split()),
    )

    # --- Stage 3: Chunk + Select top chunks (by position) ---
    logger.info("Deep Research [%s] Stage 3: Selecting top chunks", company_name)
    chunks = _chunk_text(combined)
    if not chunks:
        return ""
    top_chunks = chunks[:_TOP_CHUNKS]

    # --- Stage 4: Synthesize ---
    logger.info("Deep Research [%s] Stage 4: Synthesizing %d chunks", company_name, len(top_chunks))
    research_input = "\n\n---\n\n".join(top_chunks)

    synthesis_prompt = f"""\
You are a career research assistant. Synthesize the following scraped web content \
about "{company_name}" into a concise company research brief for a job applicant.

## Scraped Content
{research_input[:8000]}

## Instructions
Write a structured Markdown brief covering:
1. **Company Overview** — What they do, stage, size
2. **Tech Stack & Engineering** — Languages, frameworks, infrastructure
3. **Recent News** — Funding, launches, acquisitions
4. **Culture & Values** — Work style, engineering culture, notable practices

Keep it factual and concise. Maximum 500 words. If information is missing \
for a section, skip it. Do not fabricate details.\
"""

    synthesis = _openrouter_generate(api_key, model, synthesis_prompt, max_tokens=1024)
    if not synthesis.strip():
        return ""

    # Enforce word limit
    words = synthesis.split()
    if len(words) > _MAX_WORDS:
        synthesis = " ".join(words[:_MAX_WORDS])

    logger.info(
        "Deep Research [%s] Complete: %d words of context",
        company_name, len(synthesis.split()),
    )
    return synthesis
