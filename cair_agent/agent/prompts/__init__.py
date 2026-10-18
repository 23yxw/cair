"""Versioned prompt files. Each file starts with a `prompt_version: <id>` line, then `---`."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

_DIR = Path(__file__).parent


def load_prompt(name: str) -> Tuple[str, str]:
    """Return (version, body) for prompts/<name>.md."""
    text = (_DIR / f"{name}.md").read_text(encoding="utf-8")
    head, _, body = text.partition("\n---\n")
    version = head.split(":", 1)[1].strip() if head.startswith("prompt_version:") else "unversioned"
    return version, body.strip()
