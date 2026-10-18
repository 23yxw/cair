"""File vs SQLite backend consistency and read-only guarantees."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from cair_agent.data.build_sqlite import build_sqlite
from cair_agent.data.repository import NotFound
from cair_agent.data.sql_repository import SqlCaseRepository

from .conftest import requires_data


@pytest.fixture(scope="module")
def sql_repo(index, tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "cair.sqlite"
    counts = build_sqlite(index, db)
    assert counts["images"] == 3762 and counts["eval_labels"] == 10
    repo = SqlCaseRepository.from_sqlite(db)
    yield repo
    repo.close()


@requires_data
def test_backends_agree_field_by_field(repo, sql_repo):
    assert repo.list_case_ids() == sql_repo.list_case_ids()
    for cid in repo.list_case_ids():
        assert repo.get_case_profile(cid) == sql_repo.get_case_profile(cid)
        fs, ss = repo.list_studies(cid), sql_repo.list_studies(cid)
        assert fs == ss
        for st in fs:
            assert repo.get_findings(st.study_id) == sql_repo.get_findings(st.study_id)
            fse = sorted(repo.list_series(st.study_id), key=lambda s: s.series_id)
            sse = sorted(sql_repo.list_series(st.study_id), key=lambda s: s.series_id)
            assert fse == sse
            for se in fse:
                assert repo.list_images(se.series_id) == sql_repo.list_images(se.series_id)


@requires_data
def test_search_series_agrees_and_is_parameterized(repo, sql_repo):
    for kw in ("sagittal", "bone window", "ultrasound", "T2", "zzz"):
        for cid in repo.list_case_ids():
            f = sorted(s.series_id for s in repo.search_series(cid, kw))
            q = sorted(s.series_id for s in sql_repo.search_series(cid, kw))
            assert f == q, (cid, kw)
    # wildcard / injection attempts are neutralized by the fixed template + escaping
    assert sql_repo.search_series("case-01", "%") == []
    assert sql_repo.search_series("case-01", "' OR 1=1 --") == []


@requires_data
def test_sql_not_found_and_audit(sql_repo):
    sql_repo.drain_audit()
    with pytest.raises(NotFound):
        sql_repo.get_study("study-0")
    audit = sql_repo.drain_audit()
    assert audit and audit[0]["template"] == "get_study" and audit[0]["rows"] == 0
    assert "limit" not in audit[0]["params"]


@requires_data
def test_sql_connection_is_read_only(sql_repo):
    with pytest.raises(Exception) as ei:
        with sql_repo.engine.connect() as con:
            con.execute(text("DELETE FROM patients"))
            con.commit()
    assert "readonly" in str(ei.value).lower() or "read-only" in str(ei.value).lower()


@requires_data
def test_eval_labels_not_reachable_via_repository(sql_repo):
    from cair_agent.data.sql_repository import TEMPLATES
    assert not any("eval_labels" in sql for sql in TEMPLATES.values())
    assert not any("discussion" in sql for sql in TEMPLATES.values())


def test_schema_sql_is_portable():
    # no sqlite-only syntax: AUTOINCREMENT / WITHOUT ROWID / PRAGMA
    from cair_agent.data.build_sqlite import SCHEMA
    s = SCHEMA.read_text().upper()
    for bad in ("AUTOINCREMENT", "WITHOUT ROWID", "PRAGMA"):
        assert bad not in s
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text())
