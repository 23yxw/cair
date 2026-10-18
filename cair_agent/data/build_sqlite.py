"""Materialize a CaseIndex into a SQLite database using schema.sql."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict

from .models import CaseIndex

SCHEMA = Path(__file__).with_name("schema.sql")


def build_sqlite(index: CaseIndex, out: Path) -> Dict[str, int]:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    con = sqlite3.connect(out)
    try:
        con.executescript(SCHEMA.read_text(encoding="utf-8"))
        counts = {"patients": 0, "studies": 0, "series": 0, "images": 0, "reports": 0, "eval_labels": 0}
        for c in index.cases:
            p = c.profile
            con.execute("INSERT INTO patients VALUES (?,?,?,?)", (p.case_id, p.age, p.sex, p.presentation))
            counts["patients"] += 1
            for s in c.studies:
                con.execute("INSERT INTO studies VALUES (?,?,?,?,?)", (s.study_id, s.case_id, s.modality, s.order, s.n_series))
                counts["studies"] += 1
            for se in c.series:
                con.execute("INSERT INTO series VALUES (?,?,?,?,?,?)",
                            (se.series_id, se.study_id, se.caption, se.count, se.anchor_slice, se.anchor_image_id))
                counts["series"] += 1
            con.executemany("INSERT INTO images VALUES (?,?,?,?,?,?,?)",
                            [(im.image_id, im.series_id, im.study_id, im.slice_index, im.uri, im.width, im.height) for im in c.images])
            counts["images"] += len(c.images)
            for r in c.reports:
                con.execute("INSERT INTO reports VALUES (?,?,?)", (r.study_id, r.report_type, r.text))
                counts["reports"] += 1
        for lab in index.labels:
            con.execute("INSERT INTO eval_labels VALUES (?,?,?)", (lab.case_id, lab.label, lab.discussion))
            counts["eval_labels"] += 1
        con.commit()
    finally:
        con.close()
    return counts
