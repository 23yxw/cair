"""Evidence ledger: structured claims bound to references the agent actually saw.

`seen` is the set of reference keys produced by tools during this run
(img:<id>, text:<id>, series:<id>, study:<id>). record()/validate_keys() reject
anything outside it, which is the code-level guard against fabricated citations.

Image keys carry resolution (`full` or `sheet`) and evidence_grade (`primary`
or `secondary`). A direct image claim must cite a full-resolution primary image.
Contact-sheet tiles and annotated images can support an indirect impression only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from ..llm.schemas import Evidence, EvidenceRef, RecordEvidenceArgs, UpdateHypothesesArgs


class LedgerError(ValueError):
    pass


@dataclass
class AuditItem:
    """A consistency issue found without looking at the images."""

    hard: bool
    type: str
    detail: str
    action: str = ""


@dataclass
class FinishGate:
    accept: bool
    error: str = ""
    downgraded: List[str] = field(default_factory=list)


class EvidenceLedger:
    def __init__(self) -> None:
        self.seen: Set[str] = set()
        self.seen_meta: Dict[str, Dict[str, str]] = {}  # key -> {study_id, series_id, slice_index, resolution, evidence_grade}
        self.entries: List[Evidence] = []
        self.hypothesis_history: List[UpdateHypothesesArgs] = []
        self.findings_policy: str = "hidden"
        self.zoomed: Set[str] = set()  # image ids opened again with zoom_image

    def update_hypotheses(self, args: UpdateHypothesesArgs) -> None:
        self.hypothesis_history.append(args)

    def top_hypothesis_name(self) -> Optional[str]:
        if not self.hypothesis_history:
            return None
        return max(self.hypothesis_history[-1].hypotheses, key=lambda h: h.probability).name

    def has_images(self) -> bool:
        return any(k.startswith("img:") for k in self.seen)

    # -- what the agent has observed ---------------------------------------
    def mark_seen(self, key: str, **meta: str) -> None:
        self.seen.add(key)
        if meta:
            self.seen_meta[key] = {k: str(v) for k, v in meta.items() if v is not None}

    def mark_zoomed(self, image_id: str) -> None:
        self.zoomed.add(image_id)

    def full_image_keys(self) -> Set[str]:
        return {k for k in self.seen if k.startswith("img:") and self._resolution(k) == "full"}

    def review_keys(self) -> Set[str]:
        """Full-resolution images plus zoom revisits. Used to tell a new look from the baseline."""
        return self.full_image_keys() | {f"zoom:{i}" for i in self.zoomed}

    def validate_keys(self, keys: List[str]) -> List[str]:
        bad = [k for k in keys if k not in self.seen]
        if bad:
            raise LedgerError(
                f"evidence_refs not seen in this run: {bad}. Only cite ids returned by tools "
                f"(e.g. {sorted(self.seen)[:5]}...)."
            )
        return keys

    def _resolution(self, key: str) -> str:
        return self.seen_meta.get(key, {}).get("resolution", "full")

    def _grade(self, key: str) -> str:
        return self.seen_meta.get(key, {}).get("evidence_grade", "primary")

    def _is_full_primary(self, ref: EvidenceRef) -> bool:
        key = ref.key()
        return bool(ref.image_id) and self._resolution(key) == "full" and self._grade(key) == "primary"

    def _normalize_ref(self, ref: EvidenceRef) -> EvidenceRef:
        key = ref.key()
        if key == "?":
            raise LedgerError("evidence ref must include image_id, text_id, series_id or study_id")
        if key not in self.seen:
            raise LedgerError(f"evidence ref {key} was not returned by any tool in this run")
        meta = self.seen_meta.get(key, {})
        # fill hierarchy from what the tool recorded, so refs are self-describing
        return EvidenceRef(
            study_id=ref.study_id or meta.get("study_id"),
            series_id=ref.series_id or meta.get("series_id"),
            image_id=ref.image_id,
            slice_index=ref.slice_index if ref.slice_index is not None else (int(meta["slice_index"]) if "slice_index" in meta else None),
            text_id=ref.text_id,
        )

    def record(self, args: RecordEvidenceArgs) -> Evidence:
        refs = [self._normalize_ref(r) for r in args.evidence_refs]
        self._check_record(args, refs)
        ev = Evidence(id=f"e{len(self.entries) + 1}", claim=args.claim, source=args.source, evidence_refs=refs,
                      support=args.support, confidence=args.confidence, contradictions=args.contradictions)
        self.entries.append(ev)
        return ev

    def _check_record(self, args: RecordEvidenceArgs, refs: List[EvidenceRef]) -> None:
        if args.source == "study_findings":
            if self.findings_policy != "visible":
                raise LedgerError("source='study_findings' is unavailable when radiology reports are hidden. Cite the image itself with source='image'.")
            if any(not (r.text_id or "").startswith("findings:") for r in refs):
                raise LedgerError("study_findings claims must cite text:findings:<study_id> returned by read_study_findings")
        name = self.top_hypothesis_name()
        if args.support == "direct" and name and len(name) >= 4 and name.casefold() in args.claim.casefold():
            raise LedgerError(
                f"direct claims must describe what is visible, not the diagnosis {name!r}. "
                "Record the observation as support='direct' and the disease name as support='indirect'."
            )
        if args.support != "direct" or args.source != "image":
            return
        image_refs = [r for r in refs if r.image_id]
        if not image_refs:
            raise LedgerError("a direct image claim must cite at least one image id")
        if any(self._is_full_primary(r) for r in image_refs):
            return
        sheet = [r.key() for r in image_refs if self._resolution(r.key()) == "sheet"]
        secondary = [r.key() for r in image_refs if self._grade(r.key()) == "secondary"]
        if sheet:
            raise LedgerError(
                "direct findings cannot cite only contact-sheet images "
                f"({', '.join(sheet[:6])}). Call sample_images strategy='neighbors' or zoom_image on one slice, "
                "or resubmit this overview as support='indirect'."
            )
        if secondary:
            raise LedgerError(
                "direct findings cannot rest only on secondary (annotated or illustration) images "
                f"({', '.join(secondary[:6])}). Cite a primary image, or resubmit as support='indirect'."
            )
        raise LedgerError("a direct image claim needs a full-resolution primary image")

    def sheet_only_direct(self) -> List[Evidence]:
        """Direct image claims that cite a contact sheet and no full-resolution primary image."""
        out = []
        for e in self.entries:
            if e.support != "direct" or e.source != "image":
                continue
            imgs = [r for r in e.evidence_refs if r.image_id]
            if not imgs or any(self._is_full_primary(r) for r in imgs):
                continue
            if any(self._resolution(r.key()) == "sheet" for r in imgs):
                out.append(e)
        return out

    def downgrade_sheet_only(self) -> List[str]:
        ids = []
        for e in self.sheet_only_direct():
            e.support = "indirect"
            e.confidence = min(e.confidence, 0.6)
            ids.append(e.id)
        return ids

    def _series_ids(self, e: Evidence) -> Set[str]:
        return {r.series_id for r in e.evidence_refs if r.series_id}

    def _suggest_center(self, series_id: str) -> Optional[int]:
        slices = []
        for meta in self.seen_meta.values():
            if meta.get("series_id") == series_id and meta.get("resolution") == "sheet" and meta.get("slice_index"):
                slices.append(int(meta["slice_index"]))
        if not slices:
            return None
        slices.sort()
        return slices[len(slices) // 2]

    def finish_gate(self, *, calls_left: int, images_left: int, allow_sampling: bool,
                    revisit_keys: Optional[Set[str]], revisit_entry_count: int, asked: Set[str]) -> FinishGate:
        """Decide whether finish may stop the loop.

        A direct claim that only cites a contact sheet is rejected once per series while
        sampling budget remains. The next attempt downgrades those claims. During a critic
        revisit, finish also waits for a new full-resolution look or an explicit retraction.
        """
        if revisit_keys is not None:
            new_look = self.review_keys() - revisit_keys
            retracted = any(e.contradictions for e in self.entries[revisit_entry_count:])
            if not new_look and not retracted:
                return FinishGate(False, "revisit is still open: sample a new full-resolution image (neighbors or zoom_image) "
                                   "or record_evidence with contradictions pointing at the challenged claim, then finish")
        pending = self.sheet_only_direct()
        if not pending:
            return FinishGate(True)
        series_ids = []
        for e in pending:
            for sid in self._series_ids(e):
                if sid not in series_ids:
                    series_ids.append(sid)
        fresh = [s for s in series_ids if s not in asked]
        if fresh and allow_sampling and calls_left > 0 and images_left > 0:
            asked.update(fresh)
            bits = []
            for sid in fresh:
                center = self._suggest_center(sid)
                where = f" center_slice={center}" if center is not None else ""
                bits.append(f"series {sid}: sample_images strategy='neighbors'{where}")
            return FinishGate(False, "direct findings cite only a contact sheet. Before finish, " + "; ".join(bits)
                              + ". This check is raised once per series.")
        return FinishGate(True, downgraded=self.downgrade_sheet_only())

    def audit(self) -> List[AuditItem]:
        """Issues a text-only reviewer must not ignore. Hard items force a revisit when images remain."""
        items: List[AuditItem] = []
        support = self._audit_direct_support()
        if support:
            items.append(support)
        for e in self.entries:
            if e.source == "study_findings" and any(r.image_id for r in e.evidence_refs):
                items.append(AuditItem(True, "modality_mismatch",
                                       f"{e.id} uses source=study_findings but cites an image id. Cite text:findings:<study_id> or source='image'.",
                                       ""))
            imgs = [r for r in e.evidence_refs if r.image_id]
            if e.support == "direct" and e.source == "image" and imgs and all(self._grade(r.key()) == "secondary" for r in imgs):
                items.append(AuditItem(True, "unsupported",
                                       f"{e.id} is direct but cites only secondary images ({', '.join(r.key() for r in imgs[:4])}).",
                                       ""))
        items.extend(self._audit_conflicts())
        return items

    def cited_full_primary_ids(self, limit: int = 4) -> List[str]:
        """Full-resolution primary images cited by direct claims, highest confidence first."""
        best: Dict[str, float] = {}
        for e in self.entries:
            if e.support != "direct" or e.source != "image":
                continue
            for ref in e.evidence_refs:
                if ref.image_id and self._is_full_primary(ref):
                    best[ref.image_id] = max(best.get(ref.image_id, 0.0), e.confidence)
        return [iid for iid, _ in sorted(best.items(), key=lambda kv: -kv[1])[:limit]]

    def _has_full_primary_direct(self) -> bool:
        for e in self.entries:
            if e.support == "direct" and e.source == "image" and any(self._is_full_primary(r) for r in e.evidence_refs):
                return True
        return False

    def _audit_direct_support(self) -> Optional[AuditItem]:
        if not self.has_images() or self._has_full_primary_direct():
            return None
        sheets: Dict[str, List[int]] = {}
        for meta in self.seen_meta.values():
            if meta.get("resolution") == "sheet" and meta.get("series_id"):
                sheets.setdefault(meta["series_id"], [])
                if meta.get("slice_index"):
                    sheets[meta["series_id"]].append(int(meta["slice_index"]))
        if sheets:
            sid = max(sheets, key=lambda s: len(sheets[s]))
            slices = sorted(sheets[sid])
            center = slices[len(slices) // 2] if slices else None
            where = f" center_slice={center}" if center is not None else ""
            return AuditItem(True, "unsupported",
                             f"No direct finding cites a full-resolution primary image. Series {sid} was seen as a contact sheet.",
                             f"sample_images series_id={sid} strategy=neighbors{where}")
        full_ids = sorted(k[4:] for k in self.full_image_keys())
        action = f"zoom_image image_id={full_ids[0]}" if full_ids else ""
        return AuditItem(True, "unsupported",
                         "No direct finding cites a full-resolution primary image.", action)

    def _audit_conflicts(self) -> List[AuditItem]:
        by_series: Dict[str, List[Evidence]] = {}
        for e in self.entries:
            if e.support != "direct":
                continue
            for sid in self._series_ids(e):
                by_series.setdefault(sid, []).append(e)
        items = []
        for sid, claims in by_series.items():
            complete = [e for e in claims if re.search(r"\bcomplete\b", e.claim, re.I)]
            partial = [e for e in claims if re.search(r"\bpartial\b", e.claim, re.I)]
            if complete and partial and {e.id for e in complete} != {e.id for e in partial}:
                ids = ", ".join(e.id for e in complete + partial)
                items.append(AuditItem(True, "contradiction",
                                       f"Series {sid}: {ids} disagree on complete versus partial.",
                                       f"zoom_image the cited slice in series {sid} and record which description stands, with contradictions pointing at the other entry"))
            negated = [e for e in claims if re.search(r"\bno\b.{0,30}\b(lesion|metastasis|metastases|fracture)\b", e.claim, re.I)]
            positive = [e for e in claims if re.search(r"\b(lesion|metastasis|metastases|fracture)\b", e.claim, re.I)
                        and not re.search(r"\bno\b.{0,30}\b(lesion|metastasis|metastases|fracture)\b", e.claim, re.I)]
            if negated and positive:
                items.append(AuditItem(False, "contradiction",
                                       f"Series {sid}: {', '.join(e.id for e in negated)} denies a lesion that "
                                       f"{', '.join(e.id for e in positive)} affirms.",
                                       ""))
        return items

    # -- views ----------------------------------------------------------------
    def hypotheses_text(self) -> str:
        if not self.hypothesis_history:
            return "(no hypothesis updates recorded)"
        lines = []
        for i, h in enumerate(self.hypothesis_history, 1):
            hs = "; ".join(f"{x.name} p={x.probability:.2f}" for x in h.hypotheses)
            lines.append(f"[h{i}] {hs}" + (f" -- {h.note}" if h.note else ""))
        return "\n".join(lines)

    def summary_text(self) -> str:
        if not self.entries:
            return "(ledger empty)"
        lines = []
        for e in self.entries:
            refs = ", ".join(self._ref_label(r) for r in e.evidence_refs)
            c = f" | contradictions: {'; '.join(e.contradictions)}" if e.contradictions else ""
            lines.append(f"[{e.id}] ({e.source}, {e.support}, conf={e.confidence:.2f}) {e.claim}  <- {refs}{c}")
        return "\n".join(lines)

    def _ref_label(self, ref: EvidenceRef) -> str:
        key = ref.key()
        extra = ""
        if ref.image_id:
            res = self._resolution(key)
            grade = self._grade(key)
            extra = f" {res}/{grade}"
        elif ref.slice_index is not None:
            extra = f"@{ref.slice_index}"
        return key + extra

    def as_dicts(self) -> List[dict]:
        return [e.model_dump() for e in self.entries]
