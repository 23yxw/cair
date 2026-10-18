"""Evidence ledger: structured claims bound to references the agent actually saw.

`seen` is the set of reference keys produced by tools during this run
(img:<id>, text:<id>, series:<id>, study:<id>). record()/validate_keys() reject
anything outside it, which is the code-level guard against fabricated citations.
"""

from __future__ import annotations

from typing import Dict, List, Set

from ..llm.schemas import Evidence, EvidenceRef, RecordEvidenceArgs, UpdateHypothesesArgs


class LedgerError(ValueError):
    pass


class EvidenceLedger:
    def __init__(self) -> None:
        self.seen: Set[str] = set()
        self.seen_meta: Dict[str, Dict[str, str]] = {}  # key -> {study_id, series_id, slice_index}
        self.entries: List[Evidence] = []
        self.hypothesis_history: List[UpdateHypothesesArgs] = []

    def update_hypotheses(self, args: UpdateHypothesesArgs) -> None:
        self.hypothesis_history.append(args)

    # -- what the agent has observed ---------------------------------------
    def mark_seen(self, key: str, **meta: str) -> None:
        self.seen.add(key)
        if meta:
            self.seen_meta[key] = {k: str(v) for k, v in meta.items() if v is not None}

    def validate_keys(self, keys: List[str]) -> List[str]:
        bad = [k for k in keys if k not in self.seen]
        if bad:
            raise LedgerError(
                f"evidence_refs not seen in this run: {bad}. Only cite ids returned by tools "
                f"(e.g. {sorted(self.seen)[:5]}...)."
            )
        return keys

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
        ev = Evidence(id=f"e{len(self.entries) + 1}", claim=args.claim, source=args.source, evidence_refs=refs,
                      support=args.support, confidence=args.confidence, contradictions=args.contradictions)
        self.entries.append(ev)
        return ev

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
            refs = ", ".join(r.key() + (f"@{r.slice_index}" if r.slice_index is not None and r.image_id is None else "") for r in e.evidence_refs)
            c = f" | contradictions: {'; '.join(e.contradictions)}" if e.contradictions else ""
            lines.append(f"[{e.id}] ({e.source}, {e.support}, conf={e.confidence:.2f}) {e.claim}  <- {refs}{c}")
        return "\n".join(lines)

    def as_dicts(self) -> List[dict]:
        return [e.model_dump() for e in self.entries]
