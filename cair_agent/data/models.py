"""Core data models shared by the file and SQL repositories.

Everything the agent can see goes through these models. They never carry the
original case folder name (which is the diagnosis label) or raw file paths.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

# Modalities whose images are *secondary* evidence (derived from, or annotating, primary images)
SECONDARY_MODALITIES = {"annotated image", "illustration"}
# Modalities that come as stacks (many slices per series)
STACK_MODALITIES = {"ct", "mri", "mammography", "nuclear medicine"}


class CaseProfile(BaseModel):
    case_id: str
    age: Optional[str] = None
    sex: Optional[str] = None
    presentation: Optional[str] = None


class Study(BaseModel):
    study_id: str
    case_id: str
    modality: str
    order: int  # order within case (by study id, used as a proxy for time)
    n_series: int = 0

    @property
    def evidence_grade(self) -> str:
        return "secondary" if self.modality.lower() in SECONDARY_MODALITIES else "primary"


class Series(BaseModel):
    series_id: str
    study_id: str
    caption: str = ""
    count: int
    anchor_slice: Optional[int] = None  # 0-based index of the representative slice
    anchor_image_id: Optional[str] = None


class Image(BaseModel):
    image_id: str
    series_id: str
    study_id: str
    slice_index: int
    uri: str  # local path or URL; never shown to the model
    width: Optional[int] = None
    height: Optional[int] = None


class Report(BaseModel):
    study_id: str
    report_type: str  # 'findings'
    text: str


class CaseRecord(BaseModel):
    """Full in-memory record for one case as produced by the indexer."""

    profile: CaseProfile
    studies: List[Study]
    series: List[Series]
    images: List[Image]
    reports: List[Report]


class EvalLabel(BaseModel):
    """Ground truth. Only the evaluator may read this; never the agent."""

    case_id: str
    label: str  # derived from folder name
    discussion: str


class CaseIndex(BaseModel):
    data_dir: str
    cases: List[CaseRecord] = Field(default_factory=list)
    labels: List[EvalLabel] = Field(default_factory=list)

    def case_ids(self) -> List[str]:
        return [c.profile.case_id for c in self.cases]
