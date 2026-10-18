from __future__ import annotations

import json
from pathlib import Path

from cair_agent.data.index import summarize
from cair_agent.data.repository import NotFound

from .conftest import DATA_DIR, requires_data


@requires_data
def test_index_counts(index):
    s = summarize(index)
    assert s["cases"] == 10
    assert s["studies"] == 24
    assert s["images"] == 3762
    assert len(s["modalities"]) == 11


@requires_data
def test_case_ids_are_anonymized(index):
    folder_names = {d.name for d in DATA_DIR.iterdir() if d.is_dir()}
    dumped = index.model_dump_json()
    # labels section legitimately contains folder-derived labels, and image.uri is server-side only
    # (tools expose image_id, never uri) -> strip both before the leak check
    agent_visible = json.dumps([c.model_dump(exclude={"images": {"__all__": {"uri"}}}) for c in index.cases])
    for name in folder_names:
        assert name not in agent_visible
    assert all(c.profile.case_id.startswith("case-") for c in index.cases)
    assert "appendiceal" not in agent_visible.lower()
    # no sentence of any discussion.txt may be present in agent-visible content
    for lab in index.labels:
        for sent in (s.strip() for s in lab.discussion.replace("\n", " ").split(".")):
            if len(sent) > 40:
                assert sent not in agent_visible, f"discussion leak: {sent[:60]}"
    assert dumped  # sanity


@requires_data
def test_anchor_slice_matches_position(index):
    # Known: appendiceal case, coronal series 749260 position=146, anchor 75312807
    rec = next(c for c in index.cases if any(s.series_id == "749260" for s in c.series))
    se = next(s for s in rec.series if s.series_id == "749260")
    assert se.anchor_slice == 146
    assert se.anchor_image_id == "75312807"
    img = next(i for i in rec.images if i.image_id == "75312807")
    assert img.slice_index == 146


@requires_data
def test_empty_patient_text_is_none(index):
    # the patent-urachus case has empty patient_data/presentation
    empties = [c for c in index.cases if c.profile.presentation is None]
    assert len(empties) == 1
    assert empties[0].profile.age is None


@requires_data
def test_repository_navigation(repo):
    cids = repo.list_case_ids()
    assert cids[0] == "case-01"
    studies = repo.list_studies("case-01")
    assert [s.order for s in studies] == list(range(len(studies)))
    series = repo.list_series(studies[0].study_id)
    assert series
    imgs = repo.list_images(series[0].series_id)
    assert [i.slice_index for i in imgs] == sorted(i.slice_index for i in imgs)
    assert Path(imgs[0].uri).exists()
    assert repo.resolve_image(imgs[0].image_id)[:2] in (b"\xff\xd8", b"\x89P")


@requires_data
def test_repository_not_found(repo):
    import pytest
    with pytest.raises(NotFound):
        repo.get_case_profile("case-99")
    with pytest.raises(NotFound):
        repo.get_image("0")


@requires_data
def test_findings_available_but_separate(repo):
    st = repo.list_studies("case-01")[0]
    assert repo.get_findings(st.study_id)
    # repository has no way to reach discussion text
    assert not hasattr(repo, "get_discussion")
