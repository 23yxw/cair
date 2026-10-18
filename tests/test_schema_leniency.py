from cair_agent.llm.schemas import EvidenceRef, FinishArgs, KeyFinding, RecordEvidenceArgs


def test_record_evidence_accepts_string_refs():
    a = RecordEvidenceArgs(claim="c", source="image", confidence=0.5,
                           evidence_refs=["img:75306160", "text:profile:case-04", {"image_id": "1"}, 2])
    assert [r.key() for r in a.evidence_refs] == ["img:75306160", "text:profile:case-04", "img:1", "img:2"]


def test_evidence_ref_accepts_display_keys():
    assert EvidenceRef(image_id="img:123").image_id == "123"
    assert EvidenceRef(image_id=123).image_id == "123"
    assert EvidenceRef(text_id="text:profile:case-01").text_id == "profile:case-01"
    assert EvidenceRef.from_key("text:findings:study-1").key() == "text:findings:study-1"
    assert EvidenceRef.from_key("75312257").key() == "img:75312257"


def test_key_finding_normalizes_refs():
    kf = KeyFinding(fact="f", source="image", confidence=0.5,
                    evidence_refs=[{"image_id": "75312257", "slice_index": 339}, "img:1", "2", 3, "text:profile:case-01"])
    assert kf.evidence_refs == ["img:75312257", "img:1", "img:2", "img:3", "text:profile:case-01"]
    FinishArgs(diagnosis="x", key_findings=[kf])
