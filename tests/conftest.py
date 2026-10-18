from __future__ import annotations

import os
from pathlib import Path

import pytest

from cair_agent.data.index import build_index
from cair_agent.data.repository import FileCaseRepository

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("CAIR_DATA_DIR", ROOT / "data_subset10"))

requires_data = pytest.mark.skipif(not DATA_DIR.exists(), reason="dataset not present")


@pytest.fixture(scope="session")
def index():
    if not DATA_DIR.exists():
        pytest.skip("dataset not present")
    # read_sizes=False keeps the session fixture fast; sizes are tested separately
    return build_index(DATA_DIR, read_sizes=False)


@pytest.fixture(scope="session")
def repo(index):
    return FileCaseRepository(index)
