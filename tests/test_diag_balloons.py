import json

from pta2icpc.diag_balloons import (build_report, compare, live_docs,
                                   load_local_state, load_xcpc_state, main)


# --- live_docs (NeDB NDJSON) ----------------------------------------------

def test_live_docs_overwrite_delete_ignore_and_skip():
    lines = [
        '{"_id": "a", "balloonid": "1", "done": false}',
        '{"$$indexCreated": {"fieldName": "balloonid"}}',
        '',
        'not json',
        '{"_id": "a", "balloonid": "1", "done": true}',  # overwrite
        '{"_id": "b", "balloonid": "2", "done": false}',
        '{"$$deleted": true, "_id": "b"}',               # delete b
        '{"_id": 3, "balloonid": "3"}',                  # non-str _id
        '{"balloonid": "4"}',                            # no _id
        '[1, 2, 3]',                                     # non-object
    ]
    docs = live_docs(lines)
    assert set(docs) == {"a"}
    assert docs["a"]["done"] is True


def test_live_docs_deleted_unknown_id_is_noop():
    assert live_docs(['{"$$deleted": true, "_id": "nope"}']) == {}


# --- compare ---------------------------------------------------------------

def _ours():
    return {
        1: {"team_id": "T1", "problem_id": "A", "done": False, "active": True},
        2: {"team_id": "T2", "problem_id": "B", "done": True, "active": True},
        3: {"team_id": "T3", "problem_id": "C", "done": False,
            "active": False},
    }


def _theirs():
    return {
        "x": {"_id": "x", "balloonid": "1", "teamid": "T1", "problem": "A",
              "done": False},
        "y": {"_id": "y", "balloonid": "9", "teamid": "T9", "problem": "Z",
              "done": True},  # orphan
        "z": {"_id": "z", "balloonid": "2", "teamid": "T2", "problem": "B",
              "done": False},  # done mismatch (ours True)
    }


def test_compare_reports_orphan_and_done_mismatch():
    out = compare(_ours(), _theirs())
    assert [o["balloonid"] for o in out["orphans"]] == [9]
    assert out["done_mismatch"] == [{"balloonid": 2, "ours": True,
                                     "theirs": False}]
    assert out["pair_mismatch"] == []
    assert out["missing"] == []  # id 3 is inactive → not "missing"


def test_compare_reports_missing_active_id():
    ours = {1: {"team_id": "T1", "problem_id": "A", "done": False,
                "active": True}}
    assert compare(ours, {})["missing"] == [1]


def test_compare_reports_pair_id_disagreement():
    ours = {
        1: {"team_id": "T1", "problem_id": "A", "done": False, "active": True},
        2: {"team_id": "T1", "problem_id": "A", "done": False, "active": True},
    }
    theirs = {"x": {"_id": "x", "balloonid": "1", "teamid": "T1",
                    "problem": "A", "done": False}}
    out = compare(ours, theirs)
    assert out["pair_mismatch"] == [{"pair": ["T1", "A"], "ours": [1, 2],
                                     "theirs": [1]}]


def test_compare_ignores_boolean_balloonid():
    """A JSON true would coerce to 1; it must be skipped, not counted as
    id 1 (which is a real balloon id)."""
    theirs = {"x": {"_id": "x", "balloonid": True, "teamid": "T1",
                    "problem": "A", "done": True}}
    out = compare(_ours(), theirs)
    assert out["orphans"] == []
    assert out["done_mismatch"] == []
    assert out["pair_mismatch"] == []


# --- loaders ---------------------------------------------------------------

def test_load_local_state_missing_and_corrupt(tmp_path):
    assert load_local_state(tmp_path / "nope.json") == {
        "exists": False, "next_id": 1, "entries": {}}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_local_state(bad)["exists"] is False


def test_load_local_state_defaults_active_true(tmp_path):
    path = tmp_path / "balloons.json"
    path.write_text(json.dumps({
        "next_id": 2,
        "entries": {"1": {"team_id": "T1", "problem_id": "A",
                          "done": False}}}), encoding="utf-8")
    state = load_local_state(path)
    assert state["exists"] is True
    assert state["next_id"] == 2
    assert state["entries"][1]["active"] is True


def test_load_xcpc_state_missing_path_does_not_raise(tmp_path):
    assert load_xcpc_state(tmp_path / "absent.db") == {
        "exists": False, "docs": {}}


def test_load_xcpc_state_reads_lines(tmp_path):
    path = tmp_path / "balloon.db"
    path.write_text(
        '{"_id": "a", "balloonid": "1", "teamid": "T1", "problem": "A",'
        ' "done": false}\n', encoding="utf-8")
    state = load_xcpc_state(path)
    assert state["exists"] is True
    assert set(state["docs"]) == {"a"}


# --- build_report / main ---------------------------------------------------

def test_build_report_empty_does_not_raise():
    report = build_report({"exists": False, "next_id": 1, "entries": {}},
                          {"exists": False, "docs": {}})
    assert report["local"]["entries"] == 0
    assert report["xcpc"]["live_rows"] == 0
    assert report["orphans"] == [] and report["missing"] == []


def test_main_returns_zero_on_missing_paths(tmp_path, capsys):
    rc = main(["--balloons-json", str(tmp_path / "nope.json"),
               "--xcpc-db", str(tmp_path / "nope.db"), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out)["local"]["entries"] == 0
