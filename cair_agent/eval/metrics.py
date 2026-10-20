"""Per-run metrics computed from the trace + report: evidence, cost and safety."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from ..data.repository import CaseRepository
from ..llm.schemas import DiagnosisReport
from ..trace.recorder import read_trace


def seen_keys(events: List[Dict[str, Any]]) -> Set[str]:
    """Everything the model was actually shown, reconstructed from the trace."""
    seen: Set[str] = set()
    for e in events:
        if e["type"] == "tool_result" and not e.get("error"):
            for iid in e.get("image_ids", []):
                seen.add(f"img:{iid}")
            d = e.get("data") or {}
            if "text_id" in d:
                seen.add(f"text:{d['text_id']}")
            for s in d.get("studies", []):
                seen.add(f"study:{s['study_id']}")
            for s in d.get("series", []):
                seen.add(f"series:{s['series_id']}")
        if e["type"] == "case_end" and e.get("seen"):
            seen.update(e["seen"])
    return seen


def sampled_studies(events: List[Dict[str, Any]], repo: CaseRepository) -> Set[str]:
    out: Set[str] = set()
    for e in events:
        if e["type"] == "tool_result" and not e.get("error"):
            for iid in e.get("image_ids", []):
                try:
                    out.add(repo.get_image(iid).study_id)
                except Exception:  # noqa: BLE001
                    pass
    return out


def sampling_metrics(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sheet-only direct claims, and the share of listed series opened at full resolution."""
    sheet_ids: Set[str] = set()
    full_ids: Set[str] = set()
    full_series: Set[str] = set()
    listed: Set[str] = set()
    for e in events:
        if e.get("type") != "tool_result" or e.get("error"):
            continue
        data = e.get("data") or {}
        if e.get("name") == "list_series":
            for se in data.get("series") or []:
                if se.get("series_id"):
                    listed.add(se["series_id"])
        elif e.get("name") == "sample_images":
            ids = [str(i) for i in (e.get("image_ids") or [])]
            sid = data.get("series_id")
            if data.get("as_contact_sheet"):
                sheet_ids.update(ids)
            else:
                full_ids.update(ids)
                if sid:
                    full_series.add(sid)
        elif e.get("name") == "zoom_image":
            full_ids.update(str(i) for i in (e.get("image_ids") or []))
            if data.get("series_id"):
                full_series.add(data["series_id"])
    sheet_only = 0
    for e in events:
        if e.get("type") != "evidence_added" or e.get("support") != "direct" or e.get("source") != "image":
            continue
        ids = [str(r.get("image_id")) for r in (e.get("evidence_refs") or []) if r.get("image_id")]
        if ids and all(i in sheet_ids and i not in full_ids for i in ids):
            sheet_only += 1
    coverage = round(len(listed & full_series) / len(listed), 2) if listed else 1.0
    return {"sheet_only_direct": sheet_only, "full_res_series_coverage": coverage}


def evidence_metrics(report: DiagnosisReport, events: List[Dict[str, Any]], repo: CaseRepository, case_id: str) -> Dict[str, Any]:
    seen = seen_keys(events)
    kfs = report.key_findings
    unsupported = sum(1 for kf in kfs if any(r not in seen for r in kf.evidence_refs))
    img_backed = sum(1 for kf in kfs if any(r.startswith("img:") for r in kf.evidence_refs))
    studies = repo.list_studies(case_id)
    primary = {s.study_id for s in studies if s.evidence_grade == "primary"}
    sampled = sampled_studies(events, repo)
    return {
        "key_findings": len(kfs),
        "unsupported_claims": unsupported,
        "image_backed_findings": img_backed,
        "primary_study_coverage": round(len(primary & sampled) / len(primary), 2) if primary else 1.0,
        "all_study_coverage": round(len({s.study_id for s in studies} & sampled) / len(studies), 2) if studies else 1.0,
        **sampling_metrics(events),
    }


def cost_metrics(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    end = next((e for e in events if e["type"] == "case_end"), {})
    u = end.get("usage_total", {})
    n_schema_err = sum(1 for e in events if e["type"] == "tool_result" and e.get("error"))
    n_retry = sum(1 for e in events if e["type"] == "llm_request" and e.get("attempt", 0) > 0)
    return {
        "tool_calls": end.get("tool_calls", 0), "images_sent": end.get("images_sent", 0),
        "llm_calls": u.get("calls", 0), "prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0),
        "total_tokens": u.get("total_tokens", 0), "elapsed_s": end.get("elapsed_s", 0),
        "tool_errors": n_schema_err, "llm_retries": n_retry, "stop_reason": end.get("stop_reason"),
    }


def leak_check(trace_path: Path, folder_names: Iterable[str], discussions: Iterable[str]) -> Dict[str, Any]:
    """Trace must not contain the raw case folder names or any sentence of discussion.txt."""
    text = Path(trace_path).read_text(encoding="utf-8")
    hits: List[str] = []
    for name in folder_names:
        if name in text:
            hits.append(f"folder:{name}")
    for d in discussions:
        for sent in (s.strip() for s in d.replace("\n", " ").split(".")):
            if len(sent) > 40 and sent in text:
                hits.append(f"discussion:{sent[:50]}")
    return {"leak_free": not hits, "hits": hits[:5]}


def load_run(trace_path: Path):
    events = read_trace(trace_path)
    fin = next(e for e in events if e["type"] == "finish")
    return events, DiagnosisReport.model_validate(fin["report"])
