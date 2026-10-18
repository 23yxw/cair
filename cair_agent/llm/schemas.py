"""Pydantic schemas for tool arguments, evidence, critic and final report."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Source = Literal["patient_text", "study_findings", "image", "tool"]
Support = Literal["direct", "indirect", "absent"]


class EvidenceRef(BaseModel):
    """Pointer to something the agent actually saw during this run."""

    study_id: Optional[str] = None
    series_id: Optional[str] = None
    image_id: Optional[str] = None
    slice_index: Optional[int] = None
    text_id: Optional[str] = None  # e.g. 'profile:case-01', 'findings:study-181355'

    @field_validator("image_id", "text_id", "series_id", "study_id", mode="before")
    @classmethod
    def _strip_prefix(cls, v, info):
        # models often echo our display keys ("img:123", "text:profile:case-01"); accept them
        if isinstance(v, (int, float)):
            v = str(int(v))
        if isinstance(v, str):
            prefix = {"image_id": "img:", "text_id": "text:", "series_id": "series:", "study_id": "study:"}[info.field_name]
            if v.startswith(prefix) and info.field_name != "study_id":
                v = v[len(prefix):]
            v = v.strip() or None
        return v

    @classmethod
    def from_key(cls, key: str) -> "EvidenceRef":
        if key.startswith("img:"):
            return cls(image_id=key[4:])
        if key.startswith("text:"):
            return cls(text_id=key[5:])
        if key.startswith("series:"):
            return cls(series_id=key[7:])
        if key.startswith("study:"):
            return cls(study_id=key[6:])
        return cls(image_id=key)  # bare numeric id -> image

    def key(self) -> str:
        if self.image_id:
            return f"img:{self.image_id}"
        if self.text_id:
            return f"text:{self.text_id}"
        if self.series_id:
            return f"series:{self.series_id}"
        if self.study_id:
            return f"study:{self.study_id}"
        return "?"


class Evidence(BaseModel):
    id: str = ""
    claim: str
    source: Source
    evidence_refs: List[EvidenceRef] = Field(min_length=1)
    support: Support = "direct"
    confidence: float = Field(ge=0.0, le=1.0)
    contradictions: List[str] = Field(default_factory=list)


# ---- tool argument schemas -------------------------------------------------

class GetCaseProfileArgs(BaseModel):
    case_id: str


class ListStudiesArgs(BaseModel):
    case_id: str


class ListSeriesArgs(BaseModel):
    study_id: str


class ReadStudyFindingsArgs(BaseModel):
    study_id: str


class SearchSeriesArgs(BaseModel):
    case_id: str
    keyword: str = Field(min_length=2, max_length=40, description="plane/phase/view/modality keyword, e.g. 'sagittal', 'bone window', 'T2', 'ultrasound'")


class SampleImagesArgs(BaseModel):
    series_id: str
    strategy: Literal["anchors", "uniform", "neighbors"] = "anchors"
    n: int = Field(default=8, ge=1, le=16, description="number of slices for 'uniform'")
    center_slice: Optional[int] = Field(default=None, description="center slice index for 'neighbors'")
    k: int = Field(default=2, ge=1, le=4, description="half-window for 'neighbors'")


class ZoomImageArgs(BaseModel):
    image_id: str
    roi: Optional[List[float]] = Field(default=None, description="[x0,y0,x1,y1] normalized 0-1; default center crop")
    scale: float = Field(default=2.0, ge=1.0, le=4.0)

    @field_validator("roi")
    @classmethod
    def _roi_len(cls, v):
        if v is not None and len(v) != 4:
            raise ValueError("roi must have 4 numbers")
        return v


class CompareImagesArgs(BaseModel):
    image_ids: List[str] = Field(min_length=2, max_length=4)


class RecordEvidenceArgs(BaseModel):
    claim: str
    source: Source
    evidence_refs: List[EvidenceRef] = Field(min_length=1)
    support: Support = "direct"
    confidence: float = Field(ge=0.0, le=1.0)
    contradictions: List[str] = Field(default_factory=list)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _accept_keys(cls, v):
        # accept display keys / bare ids as well as EvidenceRef dicts
        out = []
        for item in v or []:
            if isinstance(item, (str, int, float)):
                out.append(EvidenceRef.from_key(str(item).strip() if isinstance(item, str) else str(int(item))))
            else:
                out.append(item)
        return out


class Differential(BaseModel):
    name: str
    why: str
    confidence: float = Field(ge=0.0, le=1.0)


class KeyFinding(BaseModel):
    fact: str
    source: Source
    evidence_refs: List[str] = Field(min_length=1, description="keys exactly as returned by tools, e.g. [\"img:75312257\", \"text:profile:case-01\"]")
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _normalize_refs(cls, v):
        # accept EvidenceRef-style dicts or bare ids and normalize to display keys
        out = []
        for item in v or []:
            if isinstance(item, dict):
                out.append(EvidenceRef.model_validate(item).key())
            elif isinstance(item, (int, float)):
                out.append(f"img:{int(item)}")
            elif isinstance(item, str):
                s = item.strip()
                out.append(s if ":" in s else f"img:{s}")
        return out


class Hypothesis(BaseModel):
    name: str
    probability: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class UpdateHypothesesArgs(BaseModel):
    hypotheses: List[Hypothesis] = Field(min_length=1, max_length=6)
    note: str = Field(default="", description="what changed since the last update and why")


class FinishArgs(BaseModel):
    diagnosis: str
    differential_diagnoses: List[Differential] = Field(default_factory=list)
    key_findings: List[KeyFinding] = Field(min_length=1)
    uncertainty: List[str] = Field(default_factory=list)
    next_best_check: Optional[str] = None


# ---- critic / final report -------------------------------------------------

IssueType = Literal["contradiction", "unsupported", "laterality", "modality_mismatch", "missing_key_study", "other"]


class CriticIssue(BaseModel):
    type: IssueType
    detail: str


class CriticResult(BaseModel):
    issues: List[CriticIssue] = Field(default_factory=list)
    decision: Literal["finalize", "revisit"]
    suggested_action: Optional[str] = Field(default=None, description="what to look at next if revisit")


class DiagnosisReport(BaseModel):
    diagnosis: str
    differential_diagnoses: List[Differential] = Field(default_factory=list)
    key_findings: List[KeyFinding] = Field(min_length=1)
    uncertainty: List[str] = Field(default_factory=list)
    next_best_check: Optional[str] = None
    trace_id: str = ""
    degraded: bool = False  # True when produced by the template fallback


# ---- transport ---------------------------------------------------------------

class ToolCall(BaseModel):
    id: str
    name: str
    arguments: Dict[str, Any]
