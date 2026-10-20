"""SqlCaseRepository: CaseRepository over a read-only SQL database (SQLAlchemy Core).

Safety boundary:
  * read-only connection (SQLite opened with mode=ro; for Postgres/MySQL use a read-only role)
  * every query is a fixed, parameterized template with a LIMIT; the model never writes SQL
  * only the five agent-facing tables are referenced; eval_labels is never touched here
  * every query is appended to `audit` (template, params, rows, ms) for the run trace
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .models import CaseProfile, Image, Series, Study
from .repository import CaseRepository, NotFound

MAX_ROWS = 5000
QUERY_TIMEOUT_S = 10

TEMPLATES: Dict[str, str] = {
    "list_case_ids": "SELECT case_id FROM patients ORDER BY case_id LIMIT :limit",
    "get_case_profile": "SELECT case_id, age, sex, presentation FROM patients WHERE case_id = :case_id LIMIT 1",
    "list_studies": "SELECT study_id, case_id, modality, study_order, n_series FROM studies WHERE case_id = :case_id ORDER BY study_order LIMIT :limit",
    "get_study": "SELECT study_id, case_id, modality, study_order, n_series FROM studies WHERE study_id = :study_id LIMIT 1",
    "list_series": "SELECT series_id, study_id, caption, image_count, anchor_slice, anchor_image_id FROM series WHERE study_id = :study_id ORDER BY series_id LIMIT :limit",
    "get_series": "SELECT series_id, study_id, caption, image_count, anchor_slice, anchor_image_id FROM series WHERE series_id = :series_id LIMIT 1",
    "list_images": "SELECT image_id, series_id, study_id, slice_index, uri, width, height FROM images WHERE series_id = :series_id ORDER BY slice_index LIMIT :limit",
    "get_image": "SELECT image_id, series_id, study_id, slice_index, uri, width, height FROM images WHERE image_id = :image_id LIMIT 1",
    "get_findings": "SELECT text FROM reports WHERE study_id = :study_id AND report_type = 'findings' LIMIT 1",
    # agent-parameterized template: the model supplies only `keyword`, never SQL text
    "search_series": ("SELECT se.series_id, se.study_id, se.caption, se.image_count, se.anchor_slice, se.anchor_image_id "
                      "FROM series se JOIN studies st ON st.study_id = se.study_id "
                      "WHERE st.case_id = :case_id AND (LOWER(se.caption) LIKE :pattern OR LOWER(st.modality) LIKE :pattern) "
                      "ORDER BY st.study_order, se.series_id LIMIT :limit"),
}


class SqlCaseRepository(CaseRepository):
    def __init__(self, engine: Engine):
        self.engine = engine
        self.audit: List[Dict[str, Any]] = []

    @classmethod
    def from_sqlite(cls, path: Path, index_path: Optional[Path] = None) -> "SqlCaseRepository":
        """Open the read-only SQLite backend. When index_path is given and the db is missing, build it first."""
        p = Path(path).resolve()
        if not p.exists() and index_path is not None and Path(index_path).exists():
            from .build_sqlite import build_sqlite
            from .index import load_index
            build_sqlite(load_index(index_path), p)
        if not p.exists():
            raise FileNotFoundError(f"sqlite db not found: {p}; run `build-db` first")
        # mode=ro makes the connection itself read-only at the SQLite level
        engine = create_engine(f"sqlite:///file:{p}?mode=ro&uri=true",
                               connect_args={"timeout": QUERY_TIMEOUT_S})
        return cls(engine)

    @classmethod
    def from_url(cls, url: str) -> "SqlCaseRepository":
        """Any SQLAlchemy URL, e.g. postgresql+psycopg://readonly_user:...@host/db."""
        return cls(create_engine(url, pool_pre_ping=True))

    # -- query primitive ----------------------------------------------------
    def _q(self, template: str, **params: Any) -> List[Dict[str, Any]]:
        sql = TEMPLATES[template]
        params.setdefault("limit", MAX_ROWS)
        t0 = time.time()
        with self.engine.connect() as con:
            rows = [dict(r._mapping) for r in con.execute(text(sql), params)]
        self.audit.append({"template": template, "params": {k: v for k, v in params.items() if k != "limit"},
                           "rows": len(rows), "ms": round((time.time() - t0) * 1000, 1)})
        return rows

    def drain_audit(self) -> List[Dict[str, Any]]:
        out, self.audit = self.audit, []
        return out

    def _one(self, template: str, what: str, **params: Any) -> Dict[str, Any]:
        rows = self._q(template, **params)
        if not rows:
            raise NotFound(f"unknown {what} {list(params.values())[0]!r}")
        return rows[0]

    # -- interface -----------------------------------------------------------
    def list_case_ids(self) -> List[str]:
        return [r["case_id"] for r in self._q("list_case_ids")]

    def get_case_profile(self, case_id: str) -> CaseProfile:
        return CaseProfile(**self._one("get_case_profile", "case_id", case_id=case_id))

    def list_studies(self, case_id: str) -> List[Study]:
        self.get_case_profile(case_id)
        return [_study(r) for r in self._q("list_studies", case_id=case_id)]

    def get_study(self, study_id: str) -> Study:
        return _study(self._one("get_study", "study_id", study_id=study_id))

    def list_series(self, study_id: str) -> List[Series]:
        self.get_study(study_id)
        return [_series(r) for r in self._q("list_series", study_id=study_id)]

    def get_series(self, series_id: str) -> Series:
        return _series(self._one("get_series", "series_id", series_id=series_id))

    def list_images(self, series_id: str) -> List[Image]:
        self.get_series(series_id)
        return [Image(**r) for r in self._q("list_images", series_id=series_id)]

    def get_image(self, image_id: str) -> Image:
        return Image(**self._one("get_image", "image_id", image_id=image_id))

    def get_findings(self, study_id: str) -> Optional[str]:
        self.get_study(study_id)
        rows = self._q("get_findings", study_id=study_id)
        return rows[0]["text"] if rows else None

    def search_series(self, case_id: str, keyword: str) -> List[Series]:
        kw = keyword.lower().strip().replace("%", "").replace("_", "")
        if not kw:
            return []
        return [_series(r) for r in self._q("search_series", case_id=case_id, pattern=f"%{kw}%")]

    def close(self) -> None:
        self.engine.dispose()


def _study(r: Dict[str, Any]) -> Study:
    return Study(study_id=r["study_id"], case_id=r["case_id"], modality=r["modality"], order=r["study_order"], n_series=r["n_series"])


def _series(r: Dict[str, Any]) -> Series:
    return Series(series_id=r["series_id"], study_id=r["study_id"], caption=r["caption"], count=r["image_count"],
                  anchor_slice=r["anchor_slice"], anchor_image_id=r["anchor_image_id"])
