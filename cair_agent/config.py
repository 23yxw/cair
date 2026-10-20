"""Runtime configuration loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

Backend = Literal["file", "sqlite"]
FindingsPolicy = Literal["hidden", "visible"]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass
class Settings:
    base_url: str = field(default_factory=lambda: os.environ.get("OPENAI_BASE_URL", ""))
    api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    model: str = field(default_factory=lambda: os.environ.get("CAIR_MODEL", "Qwen3.6-35B-A3B"))

    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("CAIR_DATA_DIR", "./data_subset10")))
    index_path: Path = field(default_factory=lambda: Path(os.environ.get("CAIR_INDEX_PATH", "./runs/case_index.json")))
    sqlite_path: Path = field(default_factory=lambda: Path(os.environ.get("CAIR_SQLITE_PATH", "./runs/cair.sqlite")))

    backend: Backend = field(default_factory=lambda: os.environ.get("CAIR_BACKEND", "file"))  # type: ignore[assignment]
    findings_policy: FindingsPolicy = field(default_factory=lambda: os.environ.get("CAIR_FINDINGS_POLICY", "hidden"))  # type: ignore[assignment]

    max_tool_calls: int = field(default_factory=lambda: _env_int("CAIR_MAX_TOOL_CALLS", 14))
    max_images: int = field(default_factory=lambda: _env_int("CAIR_MAX_IMAGES", 24))
    llm_timeout_s: int = field(default_factory=lambda: _env_int("CAIR_LLM_TIMEOUT_S", 120))
    case_timeout_s: int = field(default_factory=lambda: _env_int("CAIR_CASE_TIMEOUT_S", 900))

    # Imaging
    max_image_side: int = 1024
    jpeg_quality: int = 85
    contact_sheet_n: int = 8

    # How many images stay in the controller context. Sheets are dropped before full-resolution images.
    image_keep_messages: int = 4

    def require_llm(self) -> None:
        if not self.base_url or not self.api_key:
            raise RuntimeError("OPENAI_BASE_URL and OPENAI_API_KEY must be set (see .env.example)")


def get_settings() -> Settings:
    return Settings()
