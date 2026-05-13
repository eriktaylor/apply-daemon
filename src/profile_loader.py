"""Load my_profile/profile.md and split into LLM context vs pipeline settings.

The profile format is natural language — no keyword tables, no regex
patterns, no weighted scores. The profile loader reads the file, splits at
"## Pipeline Settings", and parses the settings table.

Convention-over-configuration: all user assets live in the my_profile/
dropzone directory (profile.md, base_resume.docx, cover_letter.md).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_PATH = Path("my_profile/profile.md")


def load_profile(path: Path | str | None = None) -> dict:
    """Load profile.md and return LLM context + pipeline settings.

    Returns:
        dict with keys:
        - name: candidate name (first non-heading line from "Who I am")
        - llm_context: everything before Pipeline Settings (sent to LLM)
        - settings: dict of pipeline settings parsed from the table

    Raises:
        FileNotFoundError: if the profile file doesn't exist.
    """
    path = Path(path) if path else DEFAULT_PROFILE_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Profile not found at {path}. "
            "Run 'cp -r my_profile_example my_profile' and customize my_profile/profile.md."
        )

    text = path.read_text(encoding="utf-8")

    # Split at ## Pipeline Settings heading (must be at start of line)
    llm_context = text
    remainder = ""
    for marker in ["\n## Pipeline Settings", "\n## Pipeline settings"]:
        if marker in text:
            llm_context, remainder = text.split(marker, 1)
            break

    # Strip Job Alert Configuration from LLM context if present
    for marker in ["\n## Job Alert Configuration", "\n## Job alert configuration"]:
        if marker in llm_context:
            llm_context = llm_context.split(marker)[0]
            break

    llm_context = llm_context.strip()

    # Extract candidate name from "Who I am" section
    name = _extract_name(llm_context)

    # Parse pipeline settings table
    settings = _parse_settings_table(remainder) if remainder else {}

    logger.debug("Profile loaded: name=%s, llm_context=%d chars, settings=%s",
                 name, len(llm_context), settings)

    return {
        "name": name,
        "llm_context": llm_context,
        "settings": settings,
    }


def _extract_name(text: str) -> str:
    """Extract candidate name from the profile text.

    Looks for the first substantive line in the "Who I am" section,
    or falls back to the # heading.
    """
    # Try to find "Who I am" section and extract first content line
    in_who_section = False
    in_html_comment = False
    for line in text.split("\n"):
        stripped = line.strip()
        if "## Who I am" in stripped or "## who i am" in stripped.lower():
            in_who_section = True
            continue
        if in_who_section:
            # Track multiline HTML comments
            if "<!--" in stripped:
                in_html_comment = True
            if in_html_comment:
                if "-->" in stripped:
                    in_html_comment = False
                continue
            if stripped.startswith("##"):
                break
            if (stripped and not stripped.startswith("#")
                    and not stripped.startswith(">")
                    and not stripped.startswith("---")):
                # First content line — name is typically the first sentence fragment
                name = stripped.split(".")[0].split(",")[0].strip()
                if name:
                    return name

    # Fallback: try # heading (e.g. "# Erik Taylor — Profile")
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            heading = stripped[2:].strip()
            parts = re.split(r"\s*[—–]\s*|\s+-\s+", heading, maxsplit=1)
            return parts[0].strip()

    return ""


def _parse_settings_table(text: str) -> dict:
    """Parse pipeline settings from a markdown table."""
    settings: dict = {}

    # Stop at the next ## heading (e.g. Job Alert Configuration)
    lines = []
    for line in text.split("\n"):
        if line.strip().startswith("## "):
            break
        lines.append(line)

    for line in lines:
        stripped = line.strip()
        if (
            not stripped.startswith("|")
            or stripped.startswith("|---")
            or stripped.startswith("| Setting")
        ):
            continue
        # Skip separator rows
        if re.match(r"^\|[\s\-:|]+\|$", stripped):
            continue
        cells = [c.strip() for c in stripped.split("|")[1:-1]]
        if len(cells) >= 2:
            key = cells[0].strip()
            value = cells[1].strip()
            # Try to convert numeric values
            try:
                settings[key] = int(value)
            except ValueError:
                # Boolean conversion
                if value.lower() in ("true", "false"):
                    settings[key] = value.lower() == "true"
                # Comma-separated list (e.g. "resume, cover_letter, interview_prep")
                elif key == "generate_assets":
                    settings[key] = [v.strip() for v in value.split(",") if v.strip()]
                else:
                    settings[key] = value

    return settings
