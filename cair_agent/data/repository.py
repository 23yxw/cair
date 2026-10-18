"""CaseRepository: the only data access path available to agent tools.

Two backends implement the same interface:
  * FileCaseRepository  - backed by the JSON CaseIndex built from the dataset folders
  * SqlCaseRepository   - backed by a read-only SQL database (see sql_repository.py)

Neither exposes discussion.txt (ground truth) or raw folder names.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

from .index import load_index
from .models import CaseIndex, CaseProfile, Image, Series, Study


class CaseRepository(ABC):
    @abstractmethod
    def list_case_ids(self) -> List[str]: ...

    @abstractmethod
    def get_case_profile(self, case_id: str) -> CaseProfile: ...

    @abstractmethod
    def list_studies(self, case_id: str) -> List[Study]: ...

    @abstractmethod
    def get_study(self, study_id: str) -> Study: ...

    @abstractmethod
    def list_series(self, study_id: str) -> List[Series]: ...

    @abstractmethod
    def get_series(self, series_id: str) -> Series: ...

    @abstractmethod
    def list_images(self, series_id: str) -> List[Image]: ...

    @abstractmethod
    def get_image(self, image_id: str) -> Image: ...

    @abstractmethod
    def get_findings(self, study_id: str) -> Optional[str]: ...

    def search_series(self, case_id: str, keyword: str) -> List[Series]:
        """Series of a case whose caption or study modality contains keyword (case-insensitive)."""
        kw = keyword.lower().strip()
        out: List[Series] = []
        for st in self.list_studies(case_id):
            for se in self.list_series(st.study_id):
                if kw and (kw in se.caption.lower() or kw in st.modality.lower()):
                    out.append(se)
        return out

    def resolve_image(self, image_id: str) -> bytes:
        """Default: uri is a local path."""
        return Path(self.get_image(image_id).uri).read_bytes()

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class NotFound(KeyError):
    pass


class FileCaseRepository(CaseRepository):
    def __init__(self, index: CaseIndex):
        self._index = index
        self._profiles: Dict[str, CaseProfile] = {}
        self._studies: Dict[str, Study] = {}
        self._series: Dict[str, Series] = {}
        self._images: Dict[str, Image] = {}
        self._findings: Dict[str, str] = {}
        self._studies_by_case: Dict[str, List[Study]] = {}
        self._series_by_study: Dict[str, List[Series]] = {}
        self._images_by_series: Dict[str, List[Image]] = {}
        for c in index.cases:
            cid = c.profile.case_id
            self._profiles[cid] = c.profile
            self._studies_by_case[cid] = sorted(c.studies, key=lambda s: s.order)
            for s in c.studies:
                self._studies[s.study_id] = s
            for se in c.series:
                self._series[se.series_id] = se
                self._series_by_study.setdefault(se.study_id, []).append(se)
            for im in c.images:
                self._images[im.image_id] = im
                self._images_by_series.setdefault(im.series_id, []).append(im)
            for r in c.reports:
                if r.report_type == "findings":
                    self._findings[r.study_id] = r.text
        for lst in self._images_by_series.values():
            lst.sort(key=lambda im: im.slice_index)

    @classmethod
    def from_path(cls, index_path: Path) -> "FileCaseRepository":
        return cls(load_index(index_path))

    def list_case_ids(self) -> List[str]:
        return list(self._profiles)

    def get_case_profile(self, case_id: str) -> CaseProfile:
        try:
            return self._profiles[case_id]
        except KeyError:
            raise NotFound(f"unknown case_id {case_id!r}") from None

    def list_studies(self, case_id: str) -> List[Study]:
        self.get_case_profile(case_id)
        return list(self._studies_by_case.get(case_id, []))

    def get_study(self, study_id: str) -> Study:
        try:
            return self._studies[study_id]
        except KeyError:
            raise NotFound(f"unknown study_id {study_id!r}") from None

    def list_series(self, study_id: str) -> List[Series]:
        self.get_study(study_id)
        return list(self._series_by_study.get(study_id, []))

    def get_series(self, series_id: str) -> Series:
        try:
            return self._series[series_id]
        except KeyError:
            raise NotFound(f"unknown series_id {series_id!r}") from None

    def list_images(self, series_id: str) -> List[Image]:
        self.get_series(series_id)
        return list(self._images_by_series.get(series_id, []))

    def get_image(self, image_id: str) -> Image:
        try:
            return self._images[image_id]
        except KeyError:
            raise NotFound(f"unknown image_id {image_id!r}") from None

    def get_findings(self, study_id: str) -> Optional[str]:
        self.get_study(study_id)
        return self._findings.get(study_id)
