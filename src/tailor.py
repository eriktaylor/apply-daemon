"""Cloud LLM escalation engine — shared backend for sync and batch tailoring.

All LLM calls route through OpenRouter using the OpenAI-compatible SDK.

Provides:
  - build_prompt(job_id): Gathers listing, profile, resume into a prompt string.
  - generate_immediate(job_id): Synchronous OpenRouter call → compile .docx assets.
  - submit_batch(job_ids): Runs all tailor requests concurrently via OpenRouter.
  - retrieve_batch(batch_id): No-op stub (batch is synchronous; nothing to retrieve).

Usage (immediate):
    python -m src.tailor <job_id>
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import openai
from dotenv import load_dotenv

load_dotenv()

from src.compile import generate_assets
from src.db import Database
from src.file_utils import read_dropzone_file
from src.profile_loader import load_profile
from src.research import run_deep_research

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")

_TAILOR_PROMPT = """\
You are an expert career coach helping a candidate tailor their application for a specific role.

## Candidate Profile
{profile}

## Base Resume
{resume}
{cover_letter_style}
## Target Job
Title: {title}
Company: {company}
Location: {location}
Salary: {salary}
Job Summary: {job_summary}
Full Description / Reasoning: {reason}
{research_section}{questions_section}
## Instructions
Analyze the match between this candidate and role, then generate tailored application assets.
{asset_instructions}
Ensure your response is a completely valid, properly terminated JSON object.
Respond with ONLY the JSON object.\
"""

# Asset schema fragments — only included when the asset is in generate_assets.
# "resume" maps to a LIST of 3 schema parts so the output schema is well-ordered:
#   executive_summary_rewrite → resume_bullet_edits → other_suggestions
_ASSET_SCHEMAS = {
    "resume": [
        (
            '"executive_summary_rewrite": "<A re-emphasized professional summary (3-5 sentences) '
            'to replace the executive summary at the top of the resume. '
            'THE BASE RESUME IS THE SOURCE OF TRUTH. Every claim in your output must be traceable '
            'to a claim already present in the Base Resume or Candidate Profile for this listing. '
            'Your job is to reorder and rephrase what is already there — NOT to construct new '
            'claims from existing facts. When in doubt, prefer the base wording.\\n'
            'ALLOWED operations:\\n'
            ' - Reorder: lead with the proof points most relevant to the listing.\\n'
            ' - Rephrase: change word choice, sentence structure, voice, or emphasis, while '
            'keeping the underlying claim intact.\\n'
            ' - Omit: drop claims that do not serve this listing.\\n'
            ' - Substitute synonyms drawn from the candidate\'s own base resume or profile.\\n'
            ' - Trim or expand summary length to fit the role archetype.\\n'
            'PROHIBITED operations:\\n'
            ' - DO NOT combine two separate claims into a single new claim. If the base attributes '
            'Claim A to Experience X and Claim B to Experience Y, do not write a sentence that '
            'fuses A and B as a single capability. Keep claims attributed where the base attributes '
            'them.\\n'
            ' - DO NOT transfer timelines, scopes, or domains from one claim to another. If the '
            'base says \\"N years of A\\" and separately mentions \\"B,\\" do not produce \\"N years '
            'of B.\\" Each timeline applies only to its original activity. The same rule applies to '
            'scope words (at scale, production, enterprise) and domain words (clinical, financial, '
            'industrial) — only use them where the base already uses them.\\n'
            ' - DO NOT add specifics — metrics, technologies, organizations, vertical domains, '
            'deployment contexts — that are not present verbatim or near-verbatim in the base.\\n'
            ' - DO NOT change the attributes of a specific named experience to make it fit the '
            'listing. If the base says a project involves partners X, Y, Z, do not change them to '
            'A, B, C. If a project happened at Company A, do not reframe it as if it happened in '
            'the target listing\'s industry.\\n'
            ' - DO NOT promote familiarity, awareness, exposure, or skills-list entries into '
            '\\"direct experience,\\" \\"deep experience,\\" \\"hands-on,\\" or \\"production-grade\\" '
            'claims unless the base uses the same intensifier for the same claim.\\n'
            ' - DO NOT invent role labels, deployment contexts, or industry verticals the base does '
            'not explicitly state.>"'
        ),
        (
            '"resume_bullet_edits": [\n'
            '        {{"original_bullet": "<exact original bullet text from the resume>", '
            '"slack_diff": "<the edit shown with ~~removed~~ and **added** markdown>", '
            '"clean_bullet": "<the final rewritten bullet with no markdown formatting>"}},\n'
            '        ...\n'
            '    ]\n'
            '    THE BASE BULLET IS THE SOURCE OF TRUTH. Every word in `clean_bullet` must be '
            'traceable to the matching `original_bullet`, to another bullet in the Base Resume, or '
            'to the Candidate Profile. Tailoring is reordering and re-emphasis within a single '
            'bullet, not new authorship. When in doubt, prefer the base wording.\n'
            '    ALLOWED operations (per bullet):\n'
            '     - Reorder clauses so the most role-relevant proof point leads.\n'
            '     - Rephrase: change word choice, sentence structure, voice, or emphasis while '
            'keeping the underlying claim intact.\n'
            '     - Omit clauses that do not serve this listing.\n'
            '     - Substitute synonyms drawn from the candidate\'s own base resume or profile '
            '(e.g. "built" → "engineered" when "engineered" appears elsewhere in the base).\n'
            '    PROHIBITED operations:\n'
            '     - DO NOT add specifics — metrics, technologies, organizations, vertical domains, '
            'deployment contexts, qualifiers like "safety-critical" or "production-grade" — that '
            'are not present verbatim or near-verbatim in the base bullet.\n'
            '     - DO NOT transfer scope or scale words ("at scale", "production", "enterprise") '
            'or domain words ("clinical", "financial", "industrial") into a bullet that does not '
            'already use them. Each scope/scale/domain word applies only to its original bullet.\n'
            '     - DO NOT promote familiarity, awareness, exposure, or skills-list entries into '
            '"direct experience," "deep experience," "hands-on," or "production-grade" claims '
            'unless the base bullet already uses that intensifier for that claim.\n'
            '     - DO NOT change the named attributes of an experience (partners, employers, '
            'projects, customers) to make it fit the listing.\n'
            '     - DO NOT reframe a bullet as if the candidate worked in the target role\'s '
            'industry. The domain and employer context of each bullet must remain true to the '
            'candidate\'s actual experience.\n'
            '     - DO NOT fuse claims across bullets. Each edit is bullet-scoped.\n'
            '     - DO NOT invent metrics, numbers, percentages, dates, or role titles.\n'
            '    CRITICAL: You must be surgical. Do NOT rewrite the entire resume. '
            'You are limited to a MAXIMUM of 4 bullet point edits. Only edit the 2 to 4 '
            'bullet points that will have the absolute highest impact for this specific role. '
            'Leave the rest of the resume exactly as it is.'
        ),
        (
            '"other_suggestions": "<Strategic resume audit organized in three Markdown sections:\\n'
            '**Keep / Elevate:** Bullet list of sections, experiences, or skills to retain or '
            'move higher — strong, clear signals for this specific role.\\n'
            '**Remove / De-emphasize:** Bullet list of content to cut or push down — distracting, '
            'outdated, off-brand for a corporate environment, or mismatched to this role. '
            'Not all information is an asset; advise honestly.\\n'
            '**Additional Advice:** Formatting improvements, skills gaps to address, or any '
            'tailoring advice not captured in the structured fields above.>"'
        ),
    ],
    "cover_letter": (
        '"clean_cover_letter_text": "<full cover letter text, 3-4 paragraphs, ready to send, NO markdown formatting. '
        "If a Cover Letter Style Reference is provided, match its tone and structure. "
        "You MUST incorporate at least one specific fact from the provided 'Deep Research Context' "
        "(e.g., recent funding, a specific engineering tool they use, or a recent product launch) "
        'to prove we have actively researched the company.>",\n'
        '    "cover_letter_diff_summary": "<bulleted list of what was tailored for this company, using ~~removed~~ **added** markdown diff syntax>"'
    ),
    "interview_prep": '"interview_prep_guide": "<Markdown-formatted interview prep guide: company background, likely questions, talking points, and areas to study>"',
}

# Default assets when generate_assets is not configured.
# Cover letter and interview prep are available on-demand via ChatOps
# (!coverletter, !prep) to save API tokens on the default run.
_DEFAULT_ASSETS = ["resume"]

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _get_tailor_model() -> str:
    return os.getenv("OPENROUTER_TAILOR_MODEL", "anthropic/claude-sonnet-4.6")


def _get_openrouter_client() -> openai.OpenAI:
    """Create a synchronous OpenRouter client for tailor operations."""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set. Add it to your .env file.")
    return openai.OpenAI(
        base_url=_OPENROUTER_BASE_URL,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def build_prompt(
    job_id: str,
    *,
    status_callback: Any | None = None,
    custom_questions: str = "",
    research_context_override: str = "",
) -> tuple[str, dict, str]:
    """Build the tailor prompt for a listing.

    Reads profile settings to determine whether to run deep research
    and which assets to generate. Injects research context and
    asset-specific instructions into the prompt.

    Args:
        job_id: The listing ID.
        status_callback: Optional callable(str) for progressive UI updates.
        custom_questions: Free-text application questions from the user's
            Slack thread replies. Included in the prompt so Claude can
            generate answers alongside the standard assets.

    Returns:
        (prompt_string, listing_dict, research_context_text)

    Raises:
        ValueError: If the listing is not found.
    """
    with Database() as db:
        row = db.get_listing_by_id(job_id)
        if row is None:
            raise ValueError(f"Listing not found: {job_id}")
        listing = dict(row)

    profile = load_profile()
    profile_text = profile["llm_context"]
    settings = profile["settings"]

    resume_text = read_dropzone_file("base_resume")
    if resume_text is None:
        resume_text = "(No base resume found in my_profile/ — add base_resume.docx, .md, or .pdf)"
        logger.warning("No base_resume found in my_profile/ — generating without it")

    # Load cover letter style reference (optional — .docx, .md, or .pdf)
    cover_letter_template = read_dropzone_file("cover_letter") or ""

    # Determine which assets to generate (env takes precedence over profile)
    _env_assets = os.getenv("GENERATE_ASSETS", "").strip()
    if _env_assets:
        asset_list = [a.strip() for a in _env_assets.split(",") if a.strip()]
    else:
        asset_list = settings.get("generate_assets", _DEFAULT_ASSETS)
        if isinstance(asset_list, str):
            asset_list = [a.strip() for a in asset_list.split(",") if a.strip()]

    # Deep Research: always enabled — grounds tailoring in live company data.
    # If research_context_override is provided (checkpoint resume), skip the
    # live web search and use the cached text directly.
    research_context = ""
    company = listing.get("company", "")
    if research_context_override:
        research_context = research_context_override
        logger.info("Tailor: using cached research context for %s (checkpoint resume)", company)
    elif company:
        job_desc = listing.get("job_summary", "") or listing.get("reason", "")
        if status_callback:
            status_callback("researching")
        logger.info("Running deep research for %s", company)
        research_context = run_deep_research(company, job_desc)

    # Build research section
    research_section = ""
    if research_context:
        research_section = (
            "\n## Company Research (Deep Research)\n"
            f"{research_context}\n\n"
        )

    # Build asset-specific JSON schema instructions
    # Research dossier comes first so Claude grounds subsequent assets in verified facts
    schema_parts = []

    if research_context:
        schema_parts.append(
            '"company_research_dossier": "<Summarize key facts from Deep Research: tech stack, recent news, '
            'culture, and stage. If the research does not match the Target Job company, explicitly state '
            '\'Context Mismatch\' and ignore the research data.>"'
        )

    schema_parts.append(
        '"match_analysis": "<A structured 3-part analysis:\\n'
        '**The Opportunity:** Why this role matters based on the research and job description.\\n'
        '**The Reality Check:** Explicitly address the original skills gap and location/commute concerns, '
        'and explain how the candidate\'s background overcomes them.\\n'
        '**The Verdict:** A final strategic assessment of fit.>"'
    )
    schema_parts.append('"post_research_verdict": "<YES, NO, or MAYBE — your re-scored verdict after deep research>"')
    schema_parts.append('"post_research_confidence": "<integer 0-100>"')
    schema_parts.append('"updated_skills_match": {{"matching": ["<skill1>", ...], "missing": ["<skill1>", ...]}}')

    for asset_key in asset_list:
        if asset_key in _ASSET_SCHEMAS:
            schema = _ASSET_SCHEMAS[asset_key]
            if isinstance(schema, list):
                schema_parts.extend(schema)
            else:
                schema_parts.append(schema)

    # Custom application questions — add schema key if questions were provided
    if custom_questions:
        schema_parts.append(
            '"custom_question_answers": [\n'
            '        {{"question": "<the original question>", '
            '"answer": "<a thoughtful, specific answer drawing on the candidate profile, '
            'resume, and research context. 2-4 sentences.>"}},\n'
            '        ...\n'
            '    ]'
        )

    schema_json = ",\n    ".join(schema_parts)
    asset_instructions = (
        "You MUST respond with ONLY a valid JSON object (no markdown fences, no extra text) "
        "with this exact schema:\n"
        f"{{\n    {schema_json}\n}}\n\n"
        f"ONLY generate the keys listed above. Do not add extra keys.\n"
    )
    if research_context:
        asset_instructions += (
            "CRITICAL: You must generate the company_research_dossier FIRST. "
            "Verify the provided 'Deep Research Context' matches the Target Job company. "
            "If it matches, summarize the key facts. If it is a hallucination or wrong company, "
            "explicitly state 'Context Mismatch' and ignore the research. "
            "You must ONLY use facts established in this dossier when generating the subsequent "
            "cover letter and resume edits.\n"
        )

    # Build optional cover letter style section
    cover_letter_style = ""
    if cover_letter_template and "cover_letter" in asset_list:
        cover_letter_style = (
            "\n## Cover Letter Style Reference\n"
            + cover_letter_template + "\n"
        )

    # Build optional custom questions section
    questions_section = ""
    if custom_questions:
        questions_section = (
            "\n## Custom Application Questions\n"
            "The candidate needs help answering these application-specific questions. "
            "Answer each one drawing on the candidate profile, resume, and research context.\n\n"
            f"{custom_questions}\n\n"
        )

    if status_callback:
        status_callback("tailoring")

    prompt = _TAILOR_PROMPT.format(
        profile=profile_text,
        resume=resume_text,
        cover_letter_style=cover_letter_style,
        title=listing.get("title", "Unknown"),
        company=listing.get("company", "Unknown"),
        location=listing.get("location", "not specified"),
        salary=listing.get("salary", "not listed"),
        job_summary=listing.get("job_summary", ""),
        reason=listing.get("reason", ""),
        research_section=research_section,
        questions_section=questions_section,
        asset_instructions=asset_instructions,
    )

    return prompt, listing, research_context


# ---------------------------------------------------------------------------
# Synchronous (immediate) path
# ---------------------------------------------------------------------------

def generate_immediate(
    job_id: str,
    *,
    status_callback: Any | None = None,
    custom_questions: str = "",
    research_context_cache: str = "",
) -> tuple[Path, dict]:
    """Synchronous tailoring: call OpenRouter, compile .docx, update DB.

    Used by the sweeper for real-time pencil reactions.

    Args:
        job_id: The listing ID.
        status_callback: Optional callable(str) for progressive UI updates.
            Called with "researching" during deep research and "tailoring"
            before the LLM call.
        custom_questions: Free-text application questions from Slack thread replies.
        research_context_cache: If non-empty, skip the live deep-research web
            search and use this cached text instead (checkpoint resume path).

    Returns:
        (output_directory, parsed_json)
    """
    client = _get_openrouter_client()
    prompt, listing, research_context = build_prompt(
        job_id,
        status_callback=status_callback,
        custom_questions=custom_questions,
        research_context_override=research_context_cache,
    )

    logger.info("Calling OpenRouter API (immediate) for listing %s...", job_id[:8])
    response_text = _call_openrouter(client, prompt)
    claude_json = _parse_tailor_response(response_text)

    output_path = generate_assets(
        job_id, claude_json, listing, research_context=research_context,
    )

    with Database() as db:
        db.update_pipeline_status(job_id, "tailored")

    logger.info("Immediate tailor complete: %s → %s", job_id[:8], output_path)
    return output_path, claude_json


def generate_application_assets(job_id: str) -> tuple[Path, dict]:
    """Legacy entry point — delegates to generate_immediate."""
    return generate_immediate(job_id)


# ---------------------------------------------------------------------------
# Late-intake answers-only fast path
# ---------------------------------------------------------------------------

_ANSWER_PROMPT = """\
You are an expert career coach. The candidate has already tailored their application
for this role and now needs help answering additional application questions.

## Candidate Profile
{profile}

## Base Resume
{resume}

## Target Job
Title: {title}
Company: {company}
Location: {location}

## Research Context
{research_context}

## Questions to Answer
{custom_questions}

## Instructions
Answer each question drawing on the candidate profile, resume, and research context.
Each answer should be specific, authentic, and 2-4 sentences.

Respond with ONLY a valid JSON object:
{{
    "custom_question_answers": [
        {{"question": "<the original question>", "answer": "<your answer>"}},
        ...
    ]
}}
"""


def generate_answers_only(job_id: str, custom_questions: str) -> tuple[Path, dict]:
    """Lightweight fast-path: answer application questions without full tailoring.

    Loads cached deep_research_context.txt from the existing output directory
    to provide grounded answers without re-running research or regenerating assets.

    Args:
        job_id: The listing ID (must already be tailored).
        custom_questions: Free-text questions from the user.

    Returns:
        (output_directory, parsed_answers_json)
    """
    client = _get_openrouter_client()

    with Database() as db:
        row = db.get_listing_by_id(job_id)
        if row is None:
            raise ValueError(f"Listing not found: {job_id}")
        listing = dict(row)

    profile = load_profile()
    profile_text = profile["llm_context"]
    resume_text = read_dropzone_file("base_resume") or ""

    # Find existing output directory and load cached research
    research_context = ""
    output_path = _find_existing_output(job_id)
    if output_path:
        research_file = output_path / "deep_research_context.txt"
        if research_file.exists():
            research_context = research_file.read_text(encoding="utf-8")

    prompt = _ANSWER_PROMPT.format(
        profile=profile_text,
        resume=resume_text,
        title=listing.get("title", "Unknown"),
        company=listing.get("company", "Unknown"),
        location=listing.get("location", "not specified"),
        research_context=research_context or "(No research context available)",
        custom_questions=custom_questions,
    )

    logger.info("Calling OpenRouter API (answers-only) for listing %s...", job_id[:8])
    response_text = _call_openrouter(client, prompt, max_tokens=1024)
    answers_json = _parse_answers_response(response_text)

    # Save answers to the existing output directory
    if not output_path:
        title = listing.get("title", "role")
        company = listing.get("company", "company")
        company_slug = re.sub(r"[^\w]+", "_", company).strip("_")
        title_slug = re.sub(r"[^\w]+", "_", title).strip("_")[:30]
        dir_name = f"{company_slug}_{title_slug}_{job_id[:8]}"
        output_path = OUTPUT_DIR / dir_name
        output_path.mkdir(parents=True, exist_ok=True)

    company = listing.get("company", "company")
    company_slug = re.sub(r"[^\w]+", "_", company).strip("_")
    _save_custom_answers(output_path, company_slug, answers_json)

    logger.info("Answers-only complete: %s → %s", job_id[:8], output_path)
    return output_path, answers_json


def _find_existing_output(job_id: str) -> Path | None:
    """Find the existing output directory for a job_id."""
    if not OUTPUT_DIR.exists():
        return None
    for d in OUTPUT_DIR.iterdir():
        if d.is_dir() and job_id[:8] in d.name:
            return d
    return None



def _parse_answers_response(text: str) -> dict:
    """Parse the JSON response from the answers-only prompt."""
    text = text.strip()
    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if json_match:
        text = json_match.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse answers response: {e}\nRaw: {text[:500]}")
    if "custom_question_answers" not in data:
        raise RuntimeError("Answers response missing required field: custom_question_answers")
    return data


def _save_custom_answers(output_path: Path, company_slug: str, answers_json: dict) -> None:
    """Save custom question answers as a Markdown file."""
    answers = answers_json.get("custom_question_answers", [])
    if not answers:
        return
    lines = [f"# Custom Application Answers — {company_slug}\n"]
    for i, qa in enumerate(answers, 1):
        q = qa.get("question", "")
        a = qa.get("answer", "")
        lines.append(f"## Q{i}: {q}\n")
        lines.append(f"{a}\n")
    (output_path / "Custom_Answers.md").write_text(
        "\n".join(lines), encoding="utf-8",
    )
    # Also save raw JSON
    (output_path / "custom_answers.json").write_text(
        json.dumps(answers_json, indent=2), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# On-demand single-asset generation (ChatOps: !coverletter, !prep)
# ---------------------------------------------------------------------------

_COVER_LETTER_PROMPT = """\
You are an expert career coach. Generate a tailored cover letter for this candidate.

## Candidate Profile
{profile}

## Base Resume
{resume}
{cover_letter_style}
## Target Job
Title: {title}
Company: {company}
Location: {location}

## Research Context
{research_context}
{tailor_context}
## Instructions
Write a 3-4 paragraph cover letter ready to send. NO markdown formatting.
If a Cover Letter Style Reference is provided, match its tone and structure.
You MUST incorporate at least one specific fact from the Research Context
(e.g., recent funding, a specific engineering tool they use, or a recent product launch).
If a Prior Tailor Analysis is provided, use the match analysis and strategic suggestions
to sharpen the angle and highlight the strongest alignment points.

Respond with ONLY a valid JSON object:
{{
    "clean_cover_letter_text": "<full cover letter text>"
}}
"""

_INTERVIEW_PREP_PROMPT = """\
You are an expert career coach. Generate an interview preparation guide for this candidate.

## Candidate Profile
{profile}

## Base Resume
{resume}

## Target Job
Title: {title}
Company: {company}
Location: {location}

## Research Context
{research_context}
{tailor_context}
## Instructions
Create a comprehensive Markdown-formatted interview prep guide including:
- Company background and culture
- Likely interview questions (technical and behavioral)
- Talking points that connect the candidate's experience to this role, using the
  Prior Tailor Analysis (if provided) to sharpen which strengths to emphasize
- Areas to study or prepare

Respond with ONLY a valid JSON object:
{{
    "interview_prep_guide": "<Markdown-formatted interview prep guide>"
}}
"""

_POLISH_RESUME_PROMPT = """\
You are a professional resume writer performing a final integration pass.

## Candidate Profile
{profile}

## Base Resume (original, unmodified)
{resume}

## Targeted Role
Title: {title}
Company: {company}

## Tailor Run — Edits to Integrate

### Executive Summary Rewrite
{executive_summary}

### Bullet Edits
{bullet_edits}

### Strategic Audit (what to keep, what to remove, other advice)
{other_suggestions}

## Company Research Context
{research_context}

## Instructions

**Integrate:** Seamlessly merge the `executive_summary_rewrite` and `resume_bullet_edits` \
into the structure of the base resume. The final document must read as a single cohesive \
document — not as a patchwork of edits.

**Refine:** Use `other_suggestions` and the company research context to adjust tone, \
reorder sections, and ensure the document targets this specific company throughout. \
Maintain one consistent, professional voice.

**Audit — Zero Hallucination:** You may rearrange the resume, remove sections, condense \
bullets, or rewrite for clarity. You may NOT invent metrics, job titles, skills, \
certifications, or achievements that do not appear in the base resume or the tailor edits. \
If a suggested edit hallucinates an achievement, reject it and revert to the original bullet.

**Output:** Return the final, polished resume as plain text with consistent formatting. \
Use Markdown-style headers (# for name, ## for sections, - for bullets) so it can be \
converted to a clean Word document.

Respond with ONLY a valid JSON object:
{{
    "polished_resume_text": "<full polished resume in Markdown format, ready to submit>"
}}
"""


def generate_cover_letter_only(job_id: str) -> tuple[Path, dict]:
    """On-demand cover letter generation using cached research + tailor context.

    Loads deep_research_context.txt and assets.json from the existing output
    directory. Saves a versioned Cover_Letter_{Company}[_vN].docx.

    Returns:
        (output_directory, parsed_json)
    """
    client = _get_openrouter_client()
    listing, profile_text, resume_text, research_context, output_path, assets_json = (
        _load_ondemand_context(job_id)
    )

    cover_letter_template = read_dropzone_file("cover_letter") or ""
    cover_letter_style = ""
    if cover_letter_template:
        cover_letter_style = (
            "\n## Cover Letter Style Reference\n"
            + cover_letter_template + "\n"
        )

    prompt = _COVER_LETTER_PROMPT.format(
        profile=profile_text,
        resume=resume_text,
        cover_letter_style=cover_letter_style,
        title=listing.get("title", "Unknown"),
        company=listing.get("company", "Unknown"),
        location=listing.get("location", "not specified"),
        research_context=research_context or "(No research context available)",
        tailor_context=_format_tailor_context(assets_json),
    )

    logger.info("Calling OpenRouter API (cover letter only) for listing %s...", job_id[:8])
    response_text = _call_openrouter(client, prompt, max_tokens=2048)
    cl_json = _parse_single_asset_response(response_text, "clean_cover_letter_text")

    # Compile cover letter .docx — versioned so repeat calls produce _v2, _v3, …
    company = listing.get("company", "company")
    company_slug = re.sub(r"[^\w]+", "_", company).strip("_")
    title = listing.get("title", "role")
    cover_letter_text = cl_json.get("clean_cover_letter_text", "")
    if cover_letter_text:
        from src.compile import _generate_cover_letter
        dest = _next_version_path(output_path, f"Cover_Letter_{company_slug}", ".docx")
        _generate_cover_letter(dest, cover_letter_text, title, company)

    # Save JSON (always overwrite — the latest JSON is canonical)
    (output_path / "cover_letter.json").write_text(
        json.dumps(cl_json, indent=2), encoding="utf-8",
    )

    logger.info("Cover letter generated: %s → %s", job_id[:8], output_path)
    return output_path, cl_json


def generate_interview_prep_only(job_id: str) -> tuple[Path, dict]:
    """On-demand interview prep generation using cached research + tailor context.

    Returns:
        (output_directory, parsed_json)
    """
    client = _get_openrouter_client()
    listing, profile_text, resume_text, research_context, output_path, assets_json = (
        _load_ondemand_context(job_id)
    )

    prompt = _INTERVIEW_PREP_PROMPT.format(
        profile=profile_text,
        resume=resume_text,
        title=listing.get("title", "Unknown"),
        company=listing.get("company", "Unknown"),
        location=listing.get("location", "not specified"),
        research_context=research_context or "(No research context available)",
        tailor_context=_format_tailor_context(assets_json),
    )

    logger.info("Calling OpenRouter API (interview prep only) for listing %s...", job_id[:8])
    response_text = _call_openrouter(client, prompt, max_tokens=2048)
    prep_json = _parse_single_asset_response(response_text, "interview_prep_guide")

    # Save interview prep as Markdown — versioned so repeat calls produce _v2, _v3, …
    company = listing.get("company", "company")
    company_slug = re.sub(r"[^\w]+", "_", company).strip("_")
    title = listing.get("title", "role")
    prep_text = prep_json.get("interview_prep_guide", "")
    if prep_text:
        dest = _next_version_path(output_path, f"Interview_Prep_{company_slug}", ".md")
        dest.write_text(
            f"# Interview Prep: {title} at {company}\n\n{prep_text}\n",
            encoding="utf-8",
        )

    (output_path / "interview_prep.json").write_text(
        json.dumps(prep_json, indent=2), encoding="utf-8",
    )

    logger.info("Interview prep generated: %s → %s", job_id[:8], output_path)
    return output_path, prep_json


def generate_polish_resume(job_id: str) -> tuple[Path, dict]:
    """On-demand polished resume generation — integrates tailor edits into a final document.

    Loads the executive summary rewrite, bullet edits, other_suggestions, and
    research context from the existing output directory, then asks the LLM to
    produce a single coherent polished resume.

    Returns:
        (output_directory, parsed_json)

    Raises:
        RuntimeError: If no tailor assets (assets.json) are found for this job.
    """
    client = _get_openrouter_client()
    listing, profile_text, resume_text, research_context, output_path, assets_json = (
        _load_ondemand_context(job_id)
    )

    if not assets_json:
        raise RuntimeError(
            f"No tailor assets found for {job_id[:8]}. "
            "Run a tailor pass first (add ✏️ to the card), then use !polish."
        )

    executive_summary = assets_json.get("executive_summary_rewrite", "").strip()
    bullet_edits_raw = assets_json.get("resume_bullet_edits", [])
    other_suggestions = assets_json.get("other_suggestions", "").strip()

    # Format bullet edits as readable text for the prompt
    if isinstance(bullet_edits_raw, list):
        bullet_lines = []
        for edit in bullet_edits_raw:
            if isinstance(edit, dict):
                orig = edit.get("original_bullet", "")
                diff = edit.get("slack_diff", "")
                clean = edit.get("clean_bullet", "")
                bullet_lines.append(
                    f"- Original: {orig}\n  Edit:     {diff}\n  Final:    {clean}"
                )
        bullet_edits_text = "\n".join(bullet_lines) if bullet_lines else "(none)"
    else:
        bullet_edits_text = str(bullet_edits_raw) if bullet_edits_raw else "(none)"

    prompt = _POLISH_RESUME_PROMPT.format(
        profile=profile_text,
        resume=resume_text,
        title=listing.get("title", "Unknown"),
        company=listing.get("company", "Unknown"),
        executive_summary=executive_summary or "(none)",
        bullet_edits=bullet_edits_text,
        other_suggestions=other_suggestions or "(none)",
        research_context=research_context or "(No research context available)",
    )

    logger.info("Calling OpenRouter API (polish resume) for listing %s...", job_id[:8])
    response_text = _call_openrouter(client, prompt, max_tokens=4096)
    polish_json = _parse_single_asset_response(response_text, "polished_resume_text")

    # Write polished resume .docx — versioned so repeat calls produce _v2, _v3, …
    company = listing.get("company", "company")
    company_slug = re.sub(r"[^\w]+", "_", company).strip("_")
    polished_text = polish_json.get("polished_resume_text", "")
    if polished_text:
        from src.compile import _write_polished_resume_docx
        dest = _next_version_path(output_path, f"Polished_Resume_{company_slug}", ".docx")
        _write_polished_resume_docx(dest, polished_text)

    (output_path / "polished_resume.json").write_text(
        json.dumps(polish_json, indent=2), encoding="utf-8",
    )

    logger.info("Polished resume generated: %s → %s", job_id[:8], output_path)
    return output_path, polish_json


def _next_version_path(folder: Path, stem: str, suffix: str) -> Path:
    """Return the first non-existent versioned path in *folder*.

    First attempt: ``{stem}{suffix}``
    Subsequent:    ``{stem}_v2{suffix}``, ``{stem}_v3{suffix}``, …
    """
    path = folder / f"{stem}{suffix}"
    v = 2
    while path.exists():
        path = folder / f"{stem}_v{v}{suffix}"
        v += 1
    return path


def _format_tailor_context(assets_json: dict) -> str:
    """Format a Prior Tailor Analysis block for inclusion in on-demand prompts.

    Returns an empty string when no meaningful tailor content is present, so
    callers can embed it directly with a surrounding newline separator.
    """
    parts: list[str] = []
    match = (assets_json.get("match_analysis") or "").strip()
    if match:
        parts.append(f"### Match Analysis\n{match}")
    dossier = (assets_json.get("company_research_dossier") or "").strip()
    if dossier:
        parts.append(f"### Company Research Dossier\n{dossier}")
    suggestions = (assets_json.get("other_suggestions") or "").strip()
    if suggestions:
        parts.append(f"### Strategic Resume Suggestions\n{suggestions}")
    if not parts:
        return ""
    return "\n## Prior Tailor Analysis\n" + "\n\n".join(parts) + "\n"


def _load_ondemand_context(job_id: str) -> tuple[dict, str, str, str, Path, dict]:
    """Load shared context for on-demand asset generation.

    Returns:
        (listing, profile_text, resume_text, research_context, output_path, assets_json)

    ``assets_json`` is the parsed content of ``assets.json`` (empty dict if the
    tailor run has not yet produced that file).
    """
    with Database() as db:
        row = db.get_listing_by_id(job_id)
        if row is None:
            raise ValueError(f"Listing not found: {job_id}")
        listing = dict(row)

    profile = load_profile()
    profile_text = profile["llm_context"]
    resume_text = read_dropzone_file("base_resume") or ""

    research_context = ""
    assets_json: dict = {}
    output_path = _find_existing_output(job_id)
    if output_path:
        research_file = output_path / "deep_research_context.txt"
        if research_file.exists():
            research_context = research_file.read_text(encoding="utf-8")
        assets_file = output_path / "assets.json"
        if assets_file.exists():
            try:
                assets_json = json.loads(assets_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
    else:
        # Create output dir if it doesn't exist yet
        company = listing.get("company", "company")
        title = listing.get("title", "role")
        company_slug = re.sub(r"[^\w]+", "_", company).strip("_")
        title_slug = re.sub(r"[^\w]+", "_", title).strip("_")[:30]
        dir_name = f"{company_slug}_{title_slug}_{job_id[:8]}"
        output_path = OUTPUT_DIR / dir_name
        output_path.mkdir(parents=True, exist_ok=True)

    return listing, profile_text, resume_text, research_context, output_path, assets_json


def _strip_code_fence(text: str) -> str:
    """Remove a markdown code fence if the text is wrapped in one.

    Handles truncated responses where the closing fence is missing — the regex
    approach requires a closing ``` to match at all, so truncated API responses
    (hit max_tokens mid-content) silently leave the opening fence in place and
    cause json.loads to fail at char 0.
    """
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    content = lines[1:]  # drop opening fence line (```json or ```)
    if content and content[-1].strip().startswith("```"):
        content = content[:-1]  # drop closing fence if present
    return "\n".join(content).strip()


def _parse_single_asset_response(text: str, required_key: str) -> dict:
    """Parse JSON response that should contain a single asset key."""
    text = _strip_code_fence(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse asset response: {e}\nRaw: {text[:500]}")
    if required_key not in data:
        raise RuntimeError(f"Asset response missing required field: {required_key}")
    return data


# ---------------------------------------------------------------------------
# Concurrent batch path — OpenRouter (runs all requests in parallel, inline)
# ---------------------------------------------------------------------------

async def _tailor_one_async(
    client: openai.AsyncOpenAI,
    job_id: str,
    prompt: str,
    listing: dict,
    model: str,
) -> None:
    """Tailor one listing asynchronously. Updates DB on completion or failure."""
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content or ""
        parsed = _parse_tailor_response(text)
        generate_assets(job_id, parsed, listing)
        with Database() as db:
            db.update_pipeline_status(job_id, "tailored")
        logger.info("Batch tailor complete for %s", job_id[:8])
    except (RuntimeError, ValueError):
        logger.error("Failed to parse tailor response for %s", job_id[:8], exc_info=True)
        with Database() as db:
            db.update_pipeline_status(job_id, "failed_api")
    except Exception:
        logger.error("Tailor failed for %s", job_id[:8], exc_info=True)
        with Database() as db:
            db.update_pipeline_status(job_id, "failed_compilation")


async def _run_concurrent_tailor(
    valid_jobs: list[tuple[str, str, dict]],
    model: str,
    api_key: str,
) -> None:
    """Fire all tailor requests concurrently and await completion."""
    async_client = openai.AsyncOpenAI(
        base_url=_OPENROUTER_BASE_URL,
        api_key=api_key,
    )
    tasks = [
        _tailor_one_async(async_client, job_id, prompt, listing, model)
        for job_id, prompt, listing in valid_jobs
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


def submit_batch(job_ids: list[str]) -> str:
    """Tailor all listings concurrently via OpenRouter.

    Unlike the old Anthropic Batch API, this runs all requests in parallel
    and waits for all to complete before returning. The returned batch_id
    is a synthetic timestamp string (used for Slack notifications).

    Returns:
        A synthetic batch identifier string.
    """
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set. Add it to your .env file.")
    model = _get_tailor_model()

    # Build prompts and mark jobs as processing
    valid_jobs: list[tuple[str, str, dict]] = []
    batch_id = f"openrouter-{int(time.time())}"
    for job_id in job_ids:
        try:
            prompt, listing, _research_ctx = build_prompt(job_id)
            valid_jobs.append((job_id, prompt, listing))
        except ValueError:
            logger.warning("Skipping %s — listing not found", job_id[:8])

    if not valid_jobs:
        raise ValueError("No valid job IDs to submit")

    with Database() as db:
        for job_id, _, _ in valid_jobs:
            db.set_batch_id(job_id, batch_id)

    logger.info("Starting concurrent tailor for %d listings (batch=%s)...", len(valid_jobs), batch_id)
    asyncio.run(_run_concurrent_tailor(valid_jobs, model, api_key))
    logger.info("Concurrent tailor complete for batch %s", batch_id)
    return batch_id


def retrieve_batch(batch_id: str) -> bool:
    """No-op stub — OpenRouter batch submission is synchronous.

    All tailoring happens inline in submit_batch, so there is nothing
    to retrieve afterward. Returns True so batch_process.py treats it
    as immediately complete.
    """
    logger.debug("retrieve_batch: no-op for OpenRouter batch %s", batch_id)
    return True


# ---------------------------------------------------------------------------
# OpenRouter client helpers
# ---------------------------------------------------------------------------

class ResponseTruncatedError(RuntimeError):
    """Raised when OpenRouter returns finish_reason='length' — the model ran
    out of token budget mid-response, so the JSON envelope is invalid. The
    caller should surface this to the user rather than retry blindly, since
    a retry at the same budget will truncate again."""


def _call_openrouter(
    client: openai.OpenAI,
    prompt: str,
    max_tokens: int = 4096,
) -> str:
    """Make a chat completion request via OpenRouter.

    Uses json_object response format so the model returns valid JSON directly.
    Returns the raw text content of the response.

    Raises ResponseTruncatedError if finish_reason='length' (token cap hit).
    """
    model = _get_tailor_model()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    choice = resp.choices[0]
    content = choice.message.content
    if not content:
        raise RuntimeError("Empty response from OpenRouter API")
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise ResponseTruncatedError(
            f"Model hit max_tokens={max_tokens} before completing the JSON response"
        )
    return content


def _parse_tailor_response(text: str) -> dict:
    """Parse the JSON response from the tailor prompt.

    Expected keys: match_analysis, custom_cover_letter, resume_bullet_edits.
    """
    text = _strip_code_fence(text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse tailor response as JSON: {e}\nRaw: {text[:500]}")

    # match_analysis is always required; other keys are dynamic
    if "match_analysis" not in data:
        raise RuntimeError("Tailor response missing required field: match_analysis")

    if "resume_bullet_edits" in data and not isinstance(data["resume_bullet_edits"], list):
        raise RuntimeError("resume_bullet_edits must be an array")

    return data


# Keep old _save_assets for backward compatibility with existing tests
def _save_assets(
    job_id: str,
    listing: dict,
    assets: dict,
) -> Path:
    """Save generated assets to output/<job_id>/ (markdown-only, legacy)."""
    title_slug = listing.get("title", "role").lower().replace(" ", "_")[:30]
    company_slug = listing.get("company", "co").lower().replace(" ", "_")[:20]
    dir_name = f"{company_slug}_{title_slug}_{job_id[:8]}"

    output_path = OUTPUT_DIR / dir_name
    output_path.mkdir(parents=True, exist_ok=True)

    (output_path / "match_analysis.md").write_text(
        f"# Match Analysis: {listing.get('title', '')} at {listing.get('company', '')}\n\n"
        f"{assets['match_analysis']}\n",
        encoding="utf-8",
    )
    (output_path / "cover_letter.md").write_text(
        f"# Cover Letter: {listing.get('title', '')} at {listing.get('company', '')}\n\n"
        f"{assets['custom_cover_letter']}\n",
        encoding="utf-8",
    )
    bullets = assets["resume_bullet_edits"]
    bullet_text = f"# Resume Edits: {listing.get('title', '')} at {listing.get('company', '')}\n\n"
    for i, bullet in enumerate(bullets, 1):
        bullet_text += f"## Edit {i}\n{bullet}\n\n"
    (output_path / "resume_edits.md").write_text(bullet_text, encoding="utf-8")
    (output_path / "assets.json").write_text(
        json.dumps(assets, indent=2), encoding="utf-8",
    )

    return output_path


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if len(sys.argv) < 2:
        print("Usage: python -m src.tailor <job_id>")
        sys.exit(1)

    job_id = sys.argv[1]
    try:
        output = generate_immediate(job_id)
        print(f"Assets saved to: {output}")
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
