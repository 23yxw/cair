"""Scan the dataset directory and build an anonymized CaseIndex.

Layout expected (per case folder):
  <diagnosis-name>/
    patient_data.txt, presentation.txt, discussion.txt, LICENSE
    study-<id>-<modality>/
      series.json, findings.md, image_urls.txt
      <series_id>[-caption]/NNNN-<image_id>.(jpg|png|jpeg)

Case folder names are diagnosis labels, so cases are renamed case-01..case-NN
(sorted by folder name) and the mapping is stored only in `labels`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image as PILImage

from .models import CaseIndex, CaseProfile, CaseRecord, EvalLabel, Image, Report, Series, Study

IMG_EXT = {".jpg", ".jpeg", ".png"}
STUDY_DIR_RE = re.compile(r"^study-(\d+)-(.+)$")
IMG_FILE_RE = re.compile(r"^(\d+)-(\d+)\.(jpg|jpeg|png)$", re.IGNORECASE)


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace").strip() if p.exists() else ""


def _parse_patient_data(text: str) -> Tuple[Optional[str], Optional[str]]:
    age = sex = None
    for line in text.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "age" and v:
            age = v
        elif k in ("gender", "sex") and v:
            sex = v
    return age, sex


def _image_size(p: Path) -> Tuple[Optional[int], Optional[int]]:
    try:
        with PILImage.open(p) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None, None


def _index_study(case_id: str, study_dir: Path, order: int, read_sizes: bool) -> Tuple[Study, List[Series], List[Image], Optional[Report]]:
    m = STUDY_DIR_RE.match(study_dir.name)
    assert m, f"unexpected study dir: {study_dir}"
    study_id = f"study-{m.group(1)}"
    meta = json.loads(_read_text(study_dir / "series.json") or "{}")
    modality = (meta.get("modality") or m.group(2)).strip().lower()

    # series_id -> directory (dir name starts with series id)
    series_dirs: Dict[str, Path] = {}
    for d in study_dir.iterdir():
        if d.is_dir():
            sid = d.name.split("-", 1)[0]
            series_dirs[sid] = d

    series_out: List[Series] = []
    images_out: List[Image] = []
    for s in meta.get("series", []):
        sid = str(s["series_id"])
        sdir = series_dirs.get(sid)
        files = sorted(f for f in sdir.iterdir() if f.suffix.lower() in IMG_EXT) if sdir else []
        imgs: List[Image] = []
        for f in files:
            fm = IMG_FILE_RE.match(f.name)
            if not fm:
                continue
            w, h = _image_size(f) if read_sizes else (None, None)
            imgs.append(Image(
                image_id=fm.group(2), series_id=sid, study_id=study_id,
                slice_index=int(fm.group(1)), uri=str(f.resolve()), width=w, height=h,
            ))
        anchor_id = str(s["anchor_image_id"]) if s.get("anchor_image_id") is not None else None
        anchor_slice = next((im.slice_index for im in imgs if im.image_id == anchor_id), None)
        if anchor_slice is None and imgs:
            pos = s.get("position")
            anchor_slice = int(pos) if isinstance(pos, int) and 0 <= pos < len(imgs) else len(imgs) // 2
        series_out.append(Series(
            series_id=sid, study_id=study_id, caption=(s.get("caption") or "").strip(),
            count=len(imgs) or int(s.get("count") or 0), anchor_slice=anchor_slice, anchor_image_id=anchor_id,
        ))
        images_out.extend(imgs)

    study = Study(study_id=study_id, case_id=case_id, modality=modality, order=order, n_series=len(series_out))
    findings = _read_text(study_dir / "findings.md")
    report = Report(study_id=study_id, report_type="findings", text=findings) if findings else None
    return study, series_out, images_out, report


def build_index(data_dir: Path, read_sizes: bool = True) -> CaseIndex:
    data_dir = Path(data_dir)
    case_dirs = sorted(d for d in data_dir.iterdir() if d.is_dir() and not d.name.startswith("."))
    index = CaseIndex(data_dir=str(data_dir.resolve()))
    for i, cdir in enumerate(case_dirs, start=1):
        case_id = f"case-{i:02d}"
        age, sex = _parse_patient_data(_read_text(cdir / "patient_data.txt"))
        presentation = _read_text(cdir / "presentation.txt") or None
        profile = CaseProfile(case_id=case_id, age=age, sex=sex, presentation=presentation)

        studies: List[Study] = []
        series: List[Series] = []
        images: List[Image] = []
        reports: List[Report] = []
        study_dirs = sorted((d for d in cdir.iterdir() if d.is_dir() and STUDY_DIR_RE.match(d.name)),
                            key=lambda d: int(STUDY_DIR_RE.match(d.name).group(1)))
        for order, sdir in enumerate(study_dirs):
            st, se, im, rep = _index_study(case_id, sdir, order, read_sizes)
            studies.append(st)
            series.extend(se)
            images.extend(im)
            if rep:
                reports.append(rep)
        index.cases.append(CaseRecord(profile=profile, studies=studies, series=series, images=images, reports=reports))
        index.labels.append(EvalLabel(
            case_id=case_id,
            label=re.sub(r"-\d+$", "", cdir.name).replace("-", " "),
            discussion=_read_text(cdir / "discussion.txt"),
        ))
    return index


def save_index(index: CaseIndex, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(index.model_dump_json(indent=1), encoding="utf-8")


def load_index(path: Path) -> CaseIndex:
    return CaseIndex.model_validate_json(Path(path).read_text(encoding="utf-8"))


def ensure_index(data_dir: Path, index_path: Path) -> CaseIndex:
    """Load the case index, building it from data_dir on first use."""
    index_path = Path(index_path)
    if index_path.exists():
        return load_index(index_path)
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise FileNotFoundError(
            f"dataset not found at {data_dir}. Unpack the case data there, or set CAIR_DATA_DIR in .env to its location.")
    index = build_index(data_dir)
    if not index.cases:
        raise FileNotFoundError(f"no case folders found under {data_dir}")
    save_index(index, index_path)
    return index


def summarize(index: CaseIndex) -> dict:
    modalities = sorted({s.modality for c in index.cases for s in c.studies})
    return {
        "cases": len(index.cases),
        "studies": sum(len(c.studies) for c in index.cases),
        "series": sum(len(c.series) for c in index.cases),
        "images": sum(len(c.images) for c in index.cases),
        "modalities": modalities,
    }
