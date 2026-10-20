"""Tool registry: the complete, agent-visible contract.

Each tool has a pydantic argument model, an expert card (rendered into the system
prompt) and a handler returning a ToolResult. Tools are the only path by which
images or case text enter the model context, and every returned reference is
registered in the EvidenceLedger as "seen".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from PIL import Image as PILImage
from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..data.models import STACK_MODALITIES
from ..data.repository import CaseRepository, NotFound
from ..imaging import sampler
from ..imaging.encode import encode_jpeg_data_url, load_image
from ..imaging.ops import contact_sheet, crop_zoom, side_by_side
from ..llm.schemas import (CompareImagesArgs, FinishArgs, GetCaseProfileArgs, ListSeriesArgs, ListStudiesArgs,
                           ReadStudyFindingsArgs, RecordEvidenceArgs, SampleImagesArgs, SearchSeriesArgs,
                           UpdateHypothesesArgs, ZoomImageArgs)
from .ledger import EvidenceLedger, LedgerError


@dataclass
class ImagePart:
    label: str
    data_url: str
    image_ids: List[str]  # all image ids visible in this part (1 for single, many for a sheet)
    resolution: str = "full"  # "sheet" until the pixels are shown at full resolution
    zoomed: bool = False


IMAGE_FETCH_TOOLS = {"sample_images", "zoom_image", "compare_images"}
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:/|\\)[^\s'\"\]]+")


def public_error(exc: BaseException) -> str:
    """Exception text safe to show the model and to store in a trace. No filesystem paths."""
    return _PATH_RE.sub("[path]", f"{type(exc).__name__}: {exc}")[:400]


def ledger_blocked_by_fetch(tool_name: str, turn_tools: List[str]) -> bool:
    """record_evidence and finish cannot run in the turn that fetched images; the pixels are not visible yet."""
    return tool_name in {"record_evidence", "finish"} and any(name in IMAGE_FETCH_TOOLS for name in turn_tools)


@dataclass
class ToolResult:
    text: str
    images: List[ImagePart] = field(default_factory=list)
    error: bool = False
    data: Dict[str, Any] = field(default_factory=dict)  # structured payload for the trace

    def n_images(self) -> int:
        return len(self.images)


@dataclass
class ToolContext:
    repo: CaseRepository
    ledger: EvidenceLedger
    settings: Settings
    images_sent: int = 0
    findings_policy: str = "hidden"
    calls_left: int = 0
    allow_sampling: bool = True
    # Set for the revisit loop: finish waits for a new full-resolution look or a retraction.
    revisit_keys: Optional[set] = None
    revisit_entry_count: int = 0
    drilldown_asked: set = field(default_factory=set)
    case_id: str = ""  # when set, every tool is bound to this case

    def images_left(self) -> int:
        return max(0, self.settings.max_images - self.images_sent)


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: Type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], ToolResult]
    card: Dict[str, str]
    terminal: bool = False

    def openai_schema(self) -> Dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for p in schema.get("properties", {}).values():
            p.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": schema}}


# --------------------------------------------------------------------------- helpers

def _label(ctx: ToolContext, image_id: str) -> str:
    im = ctx.repo.get_image(image_id)
    se = ctx.repo.get_series(im.series_id)
    st = ctx.repo.get_study(se.study_id)
    grade = f" | {st.evidence_grade} evidence" if st.evidence_grade != "primary" else ""
    cap = se.caption or "(no caption)"
    return f"[img:{image_id} | {st.study_id} {st.modality} | {cap} | slice {im.slice_index}/{se.count}{grade}]"


def _bound(ctx: ToolContext, requested_case: str) -> Optional[ToolResult]:
    if ctx.case_id and requested_case != ctx.case_id:
        return ToolResult(text=f"error: tools are bound to {ctx.case_id}", error=True)
    return None


def _bound_study(ctx: ToolContext, study_id: str) -> Optional[ToolResult]:
    if not ctx.case_id:
        return None
    if ctx.repo.get_study(study_id).case_id != ctx.case_id:
        return ToolResult(text=f"error: tools are bound to {ctx.case_id}", error=True)
    return None


def _bound_image(ctx: ToolContext, image_id: str) -> Optional[ToolResult]:
    if not ctx.case_id:
        return None
    im = ctx.repo.get_image(image_id)
    if ctx.repo.get_study(im.study_id).case_id != ctx.case_id:
        return ToolResult(text=f"error: tools are bound to {ctx.case_id}", error=True)
    return None


def note_displayed(ctx: ToolContext, part: ImagePart) -> None:
    """Mark images seen only once their pixels are placed in the model context."""
    for iid in part.image_ids:
        _mark_image_seen(ctx, iid, resolution=part.resolution)
        if part.zoomed:
            ctx.ledger.mark_zoomed(iid)


def _mark_image_seen(ctx: ToolContext, image_id: str, resolution: str = "full") -> None:
    im = ctx.repo.get_image(image_id)
    st = ctx.repo.get_study(im.study_id)
    ctx.ledger.mark_seen(f"img:{image_id}", study_id=im.study_id, series_id=im.series_id, slice_index=im.slice_index,
                         resolution=resolution, evidence_grade=st.evidence_grade)


def _open(ctx: ToolContext, image_id: str) -> PILImage.Image:
    return load_image(ctx.repo.resolve_image(image_id))


# --------------------------------------------------------------------------- handlers

def h_get_case_profile(ctx: ToolContext, a: GetCaseProfileArgs) -> ToolResult:
    if (bound := _bound(ctx, a.case_id)):
        return bound
    p = ctx.repo.get_case_profile(a.case_id)
    key = f"text:profile:{a.case_id}"
    ctx.ledger.mark_seen(key)
    d = {"case_id": p.case_id, "age": p.age or "missing", "sex": p.sex or "missing",
         "presentation": p.presentation or "missing (no admission text provided; rely on imaging)", "text_id": key[5:],
         "note": "presentation is the referral indication, not the imaging diagnosis. Before any image is returned, keep each hypothesis probability at or below 0.50."}
    return ToolResult(text=json.dumps(d, ensure_ascii=False), data=d)


def h_list_studies(ctx: ToolContext, a: ListStudiesArgs) -> ToolResult:
    if (bound := _bound(ctx, a.case_id)):
        return bound
    studies = ctx.repo.list_studies(a.case_id)
    rows = []
    for s in studies:
        ctx.ledger.mark_seen(f"study:{s.study_id}")
        rows.append({"study_id": s.study_id, "modality": s.modality, "n_series": s.n_series, "order": s.order,
                     "evidence_grade": s.evidence_grade})
    note = ("Studies are ordered by id (proxy for acquisition order; no timestamps available). "
            "'secondary' grade = annotated/illustration material derived from primary images.")
    return ToolResult(text=json.dumps({"studies": rows, "note": note}, ensure_ascii=False), data={"studies": rows})


def h_list_series(ctx: ToolContext, a: ListSeriesArgs) -> ToolResult:
    if (bound := _bound_study(ctx, a.study_id)):
        return bound
    st = ctx.repo.get_study(a.study_id)
    rows = []
    for se in ctx.repo.list_series(a.study_id):
        ctx.ledger.mark_seen(f"series:{se.series_id}", study_id=se.study_id)
        rows.append({"series_id": se.series_id, "caption": se.caption or "(no caption)", "count": se.count,
                     "anchor_slice": se.anchor_slice, "is_stack": se.count > sampler.SMALL_SERIES_MAX})
    hint = ("For is_stack=true series use sample_images strategy 'anchors' (representative slice) then 'uniform' "
            "(overview sheet) then 'neighbors' around a suspicious slice. Small series are sent in full by any strategy.")
    return ToolResult(text=json.dumps({"study_id": st.study_id, "modality": st.modality, "series": rows, "hint": hint},
                                      ensure_ascii=False), data={"series": rows})


def h_search_series(ctx: ToolContext, a: SearchSeriesArgs) -> ToolResult:
    if (bound := _bound(ctx, a.case_id)):
        return bound
    rows = []
    for se in ctx.repo.search_series(a.case_id, a.keyword):
        st = ctx.repo.get_study(se.study_id)
        ctx.ledger.mark_seen(f"series:{se.series_id}", study_id=se.study_id)
        rows.append({"series_id": se.series_id, "study_id": se.study_id, "modality": st.modality,
                     "caption": se.caption or "(no caption)", "count": se.count, "anchor_slice": se.anchor_slice})
    return ToolResult(text=json.dumps({"keyword": a.keyword, "matches": rows}, ensure_ascii=False), data={"matches": rows})


def h_read_study_findings(ctx: ToolContext, a: ReadStudyFindingsArgs) -> ToolResult:
    if (bound := _bound_study(ctx, a.study_id)):
        return bound
    if ctx.findings_policy != "visible":
        return ToolResult(text="error: radiology findings are not available in this setting.", error=True)
    txt = ctx.repo.get_findings(a.study_id)
    key = f"text:findings:{a.study_id}"
    if not txt:
        return ToolResult(text=json.dumps({"study_id": a.study_id, "findings": None, "note": "no report available"}))
    ctx.ledger.mark_seen(key, study_id=a.study_id)
    d = {"study_id": a.study_id, "findings": txt, "text_id": key[5:]}
    return ToolResult(text=json.dumps(d, ensure_ascii=False), data=d)


def h_sample_images(ctx: ToolContext, a: SampleImagesArgs) -> ToolResult:
    se = ctx.repo.get_series(a.series_id)
    if (bound := _bound_study(ctx, se.study_id)):
        return bound
    st = ctx.repo.get_study(se.study_id)
    imgs = ctx.repo.list_images(a.series_id)
    if not imgs:
        return ToolResult(text=f"error: series {a.series_id} has no images", error=True)
    idx = sampler.select(a.strategy, len(imgs), n=a.n, anchor=se.anchor_slice, center=a.center_slice, k=a.k)
    chosen = [imgs[i] for i in idx]
    left = ctx.images_left()
    if left <= 0:
        return ToolResult(text="error: image budget exhausted; call record_evidence for what you have seen and then finish.", error=True)

    parts: List[ImagePart] = []
    is_sheet = a.strategy == "uniform" and len(imgs) > sampler.SMALL_SERIES_MAX
    if is_sheet:
        pil = [_open(ctx, im.image_id) for im in chosen]
        labels = [f"slice {im.slice_index}" for im in chosen]
        sheet = contact_sheet(pil, labels, cols=4, tile=256)
        ids = [im.image_id for im in chosen]
        grade = f" | {st.evidence_grade} evidence" if st.evidence_grade != "primary" else ""
        lab = (f"[contact sheet | {st.study_id} {st.modality} | series {se.series_id} {se.caption or ''} | "
               f"slices {','.join(str(im.slice_index) for im in chosen)} of {se.count}{grade}]")
        parts.append(ImagePart(label=lab, data_url=encode_jpeg_data_url(sheet, ctx.settings.max_image_side, ctx.settings.jpeg_quality),
                               image_ids=ids, resolution="sheet"))
    else:
        truncated = False
        if len(chosen) > left:
            chosen, truncated = chosen[:left], True
        for im in chosen:
            parts.append(ImagePart(label=_label(ctx, im.image_id),
                                   data_url=encode_jpeg_data_url(_open(ctx, im.image_id), ctx.settings.max_image_side, ctx.settings.jpeg_quality),
                                   image_ids=[im.image_id], resolution="full"))
        if truncated:
            pass
    ctx.images_sent += len(parts)
    mapping = [{"slice_index": im.slice_index, "image_id": im.image_id} for im in chosen]
    d = {"series_id": se.series_id, "study_id": st.study_id, "modality": st.modality, "caption": se.caption,
         "strategy": a.strategy, "returned": mapping, "as_contact_sheet": is_sheet,
         "evidence_grade": st.evidence_grade}
    mapping_txt = ", ".join(f"{m['slice_index']}->img:{m['image_id']}" for m in mapping)
    if is_sheet:
        txt = (f"Returned {len(chosen)} slices from series {se.series_id} ({st.modality}, {se.caption or 'no caption'}) "
               f"as ONE contact sheet. These ids are resolution=sheet, not a full-resolution read. slice->image_id: {mapping_txt}. "
               "Record an overview as support='indirect'. A direct finding requires sample_images strategy='neighbors' "
               "(set center_slice to the slice index that looks abnormal) or zoom_image on one id. "
               "Do not describe the whole series as reviewed. Image follows in the next message.")
    else:
        txt = (f"Returned {len(chosen)} full-resolution image(s) from series {se.series_id} ({st.modality}, {se.caption or 'no caption'}). "
               f"slice->image_id: {mapping_txt}. Cite these img ids in record_evidence. Images follow in the next message.")
    return ToolResult(text=txt, images=parts, data=d)


def h_zoom_image(ctx: ToolContext, a: ZoomImageArgs) -> ToolResult:
    if (bound := _bound_image(ctx, a.image_id)):
        return bound
    if ctx.images_left() <= 0:
        return ToolResult(text="error: image budget exhausted; record evidence and finish.", error=True)
    if f"img:{a.image_id}" not in ctx.ledger.seen:
        return ToolResult(text=f"error: img:{a.image_id} has not been returned by sample_images yet.", error=True)
    im = _open(ctx, a.image_id)
    rec = ctx.repo.get_image(a.image_id)
    roi = tuple(a.roi) if a.roi else None
    out = crop_zoom(im, roi, scale=a.scale, max_side=ctx.settings.max_image_side)  # type: ignore[arg-type]
    ctx.images_sent += 1
    lab = _label(ctx, a.image_id).rstrip("]") + f" | ZOOM roi={a.roi or 'center'} x{a.scale}]"
    d = {"image_id": a.image_id, "series_id": rec.series_id, "roi": a.roi, "scale": a.scale}
    return ToolResult(text=f"Zoomed img:{a.image_id} (roi={a.roi or 'center'}, x{a.scale}). Image follows.",
                      images=[ImagePart(label=lab, data_url=encode_jpeg_data_url(out, ctx.settings.max_image_side, ctx.settings.jpeg_quality),
                                        image_ids=[a.image_id], resolution="full", zoomed=True)],
                      data=d)


def h_compare_images(ctx: ToolContext, a: CompareImagesArgs) -> ToolResult:
    for iid in a.image_ids:
        if (bound := _bound_image(ctx, iid)):
            return bound
    if ctx.images_left() <= 0:
        return ToolResult(text="error: image budget exhausted; record evidence and finish.", error=True)
    unseen = [i for i in a.image_ids if f"img:{i}" not in ctx.ledger.seen]
    if unseen:
        return ToolResult(text=f"error: images not yet returned by sample_images: {unseen}", error=True)
    pil = [_open(ctx, i) for i in a.image_ids]
    labels = []
    for i in a.image_ids:
        im = ctx.repo.get_image(i)
        se = ctx.repo.get_series(im.series_id)
        labels.append(f"img:{i} {se.caption or ''} s{im.slice_index}")
    strip = side_by_side(pil, labels, height=512)
    ctx.images_sent += 1
    lab = "[side-by-side | " + " vs ".join(_label(ctx, i) for i in a.image_ids) + "]"
    return ToolResult(text=f"Side-by-side of {', '.join('img:' + i for i in a.image_ids)}. Image follows.",
                      images=[ImagePart(label=lab, data_url=encode_jpeg_data_url(strip, ctx.settings.max_image_side, ctx.settings.jpeg_quality), image_ids=list(a.image_ids))],
                      data={"image_ids": a.image_ids})


def h_update_hypotheses(ctx: ToolContext, a: UpdateHypothesesArgs) -> ToolResult:
    if not ctx.ledger.has_images():
        hot = [h for h in a.hypotheses if h.probability > 0.5]
        if hot:
            names = ", ".join(f"{h.name} p={h.probability:.2f}" for h in hot)
            return ToolResult(text="error: before any image is returned, no hypothesis probability may exceed 0.50 "
                              f"({names}). The admission text ranks possibilities; it is not the imaging diagnosis.", error=True)
    ctx.ledger.update_hypotheses(a)
    hs = "; ".join(f"{h.name} (p={h.probability:.2f})" for h in a.hypotheses)
    return ToolResult(text=f"hypotheses updated: {hs}", data=a.model_dump())


def h_record_evidence(ctx: ToolContext, a: RecordEvidenceArgs) -> ToolResult:
    try:
        ev = ctx.ledger.record(a)
    except LedgerError as e:
        return ToolResult(text=f"error: {e}", error=True)
    return ToolResult(text=f"recorded {ev.id}: {ev.claim} ({ev.support}, conf={ev.confidence:.2f})", data=ev.model_dump())


def h_finish(ctx: ToolContext, a: FinishArgs) -> ToolResult:
    # reference validity is enforced here; the controller treats a non-error result as terminal
    try:
        for kf in a.key_findings:
            ctx.ledger.validate_keys(kf.evidence_refs)
    except LedgerError as e:
        return ToolResult(text=f"error: {e}", error=True)
    gate = ctx.ledger.finish_gate(calls_left=ctx.calls_left, images_left=ctx.images_left(), allow_sampling=ctx.allow_sampling,
                                  revisit_keys=ctx.revisit_keys, revisit_entry_count=ctx.revisit_entry_count,
                                  asked=ctx.drilldown_asked)
    if not gate.accept:
        return ToolResult(text=f"error: {gate.error}", error=True)
    data = a.model_dump()
    if gate.downgraded:
        data["downgraded"] = gate.downgraded
        return ToolResult(text="finish accepted; contact-sheet-only direct claims were downgraded to indirect: " + ", ".join(gate.downgraded), data=data)
    return ToolResult(text="finish accepted", data=data)


# --------------------------------------------------------------------------- registry

def build_registry(findings_policy: str = "hidden", enable_compare: bool = True) -> Dict[str, ToolSpec]:
    specs: List[ToolSpec] = [
        ToolSpec("get_case_profile",
                 "Admission information: age, sex, presentation/complaint. Fields may be 'missing'.",
                 GetCaseProfileArgs, h_get_case_profile,
                 {"applies_to": "every case, call first", "returns": "age, sex, presentation, text_id", "cost": "free", "fails_when": "unknown case_id"}),
        ToolSpec("list_studies",
                 "List imaging studies of a case with modality, number of series and evidence grade (primary vs secondary).",
                 ListStudiesArgs, h_list_studies,
                 {"applies_to": "every case, call second", "returns": "study_id, modality, n_series, order, evidence_grade", "cost": "free", "fails_when": "unknown case_id"}),
        ToolSpec("list_series",
                 "List series of a study: caption (plane/phase/view), slice count, anchor slice, whether it is a stack.",
                 ListSeriesArgs, h_list_series,
                 {"applies_to": "each study you intend to look at", "returns": "series_id, caption, count, anchor_slice, is_stack", "cost": "free", "fails_when": "unknown study_id"}),
        ToolSpec("search_series",
                 "Find series across all studies of a case by a plane/phase/view/modality keyword (parameterized query over the case "
                 "database). Useful to locate e.g. the sagittal series, bone-window CT or T2 MRI without listing every study.",
                 SearchSeriesArgs, h_search_series,
                 {"applies_to": "cases with many studies/series", "returns": "matching series with study and modality", "cost": "1 tool call", "fails_when": "unknown case_id"}),
        ToolSpec("sample_images",
                 "Fetch images from a series. Small series (<=6) are returned in full. For stacks: 'anchors' = the representative "
                 "slice at full resolution; 'uniform' = n evenly spaced slices as ONE labelled contact sheet (overview); "
                 "'neighbors' = slices center_slice +/- k as individual images (targeted review). Returned images count against the image budget.",
                 SampleImagesArgs, h_sample_images,
                 {"applies_to": "all modalities; stacks (CT/MRI/tomosynthesis) need anchors->uniform->neighbors", "returns": "labelled images + slice->image_id map", "cost": "1 image per returned part", "fails_when": "image budget exhausted"}),
        ToolSpec("zoom_image",
                 "Crop a region of an already-returned image (normalized roi [x0,y0,x1,y1]) and magnify it. Use for small lesions, "
                 "margins, calcifications, physis/joint lines. Costs 1 image.",
                 ZoomImageArgs, h_zoom_image,
                 {"applies_to": "any image already seen", "returns": "one magnified crop", "cost": "1 image", "fails_when": "image not seen yet / budget exhausted"}),
        ToolSpec("update_hypotheses",
                 "Record your current ranked differential (2-6 entries with probabilities) and what changed. Before any image is returned, "
                 "every probability must be <= 0.50: the admission text is the referral, not the diagnosis. Free (does not count against the budget). "
                 "Call it after the case map and whenever evidence changes your ranking.",
                 UpdateHypothesesArgs, h_update_hypotheses,
                 {"applies_to": "after the case map; after evidence shifts beliefs", "returns": "ack", "cost": "none",
                  "fails_when": "fewer than 2 hypotheses, or a probability above 0.50 before any image has been returned"}),
        ToolSpec("record_evidence",
                 "Append one observation or fact to the evidence ledger. Do not call it in the same turn as sample_images, zoom_image, "
                 "or compare_images; those images are not seen until the next turn. Cite only ids returned by tools: image_id is the bare number "
                 "(e.g. \"75312257\"), text_id is e.g. \"profile:case-01\". support='direct' is what you saw on a full-resolution primary "
                 "image and must not contain your leading diagnosis name. support='indirect' is an interpretation, a contact-sheet overview, "
                 "or a reading of an annotated/illustration image. Free (does not count against the budget).",
                 RecordEvidenceArgs, h_record_evidence,
                 {"applies_to": "the turn after images are shown, not the turn that fetches them", "returns": "evidence id", "cost": "none",
                  "fails_when": "called in the same turn as an image fetch, a cited id was never shown, a direct claim cites only a contact sheet or secondary image, or a direct claim names the leading diagnosis"}),
        ToolSpec("finish",
                 "Submit the final structured diagnosis. key_findings[].evidence_refs is a list of STRING keys exactly as returned by "
                 "tools, e.g. [\"img:75312257\", \"text:profile:case-01\"]. Include >=2 differential diagnoses, each with name, why, and "
                 "confidence, e.g. {\"name\": \"Cecal diverticulitis\", \"why\": \"less likely because ...\", \"confidence\": 0.15}.",
                 FinishArgs, h_finish,
                 {"applies_to": "when evidence is sufficient or budget is nearly exhausted", "returns": "accepted / validation error", "cost": "none",
                  "fails_when": "an evidence ref was never seen, a differential is missing confidence, or a direct finding still cites only a contact sheet"},
                 terminal=True),
    ]
    if enable_compare:
        specs.insert(5, ToolSpec("compare_images",
                                 "Place 2-4 already-returned images side by side (multi-plane, pre/post, left/right). Costs 1 image.",
                                 CompareImagesArgs, h_compare_images,
                                 {"applies_to": "multi-plane confirmation, follow-up comparison", "returns": "one strip image", "cost": "1 image", "fails_when": "images not seen yet"}))
    if findings_policy == "visible":
        specs.insert(3, ToolSpec("read_study_findings",
                                 "Read the radiologist's written findings for a study (available in this setting).",
                                 ReadStudyFindingsArgs, h_read_study_findings,
                                 {"applies_to": "any study", "returns": "findings text + text_id", "cost": "negligible", "fails_when": "no report"}))
    return {s.name: s for s in specs}


def render_expert_cards(registry: Dict[str, ToolSpec]) -> str:
    lines = []
    for s in registry.values():
        c = s.card
        lines.append(f"- {s.name}: applies to: {c['applies_to']}; returns: {c['returns']}; cost: {c['cost']}; fails when: {c['fails_when']}")
    return "\n".join(lines)


def execute(registry: Dict[str, ToolSpec], ctx: ToolContext, name: str, arguments: Dict[str, Any]) -> ToolResult:
    spec = registry.get(name)
    if spec is None:
        return ToolResult(text=f"error: unknown tool {name!r}. Available: {', '.join(registry)}", error=True)
    if "__parse_error__" in arguments:
        return ToolResult(text="error: tool arguments were not valid JSON", error=True)
    try:
        args = spec.args_model.model_validate(arguments)
    except ValidationError as e:
        return ToolResult(text=f"error: invalid arguments for {name}: {e.errors(include_url=False)}", error=True)
    try:
        return spec.handler(ctx, args)
    except NotFound as e:
        return ToolResult(text=f"error: {public_error(e)}", error=True)
    except Exception as e:  # noqa: BLE001 - surface to model, never crash the loop
        return ToolResult(text=f"error: {public_error(e)}", error=True)
