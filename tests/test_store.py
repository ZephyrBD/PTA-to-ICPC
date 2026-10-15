import datetime as dt

from pta2ccs.store import Store
from pta2ccs.translate import translate

TZ = "+08:00"
NOW = dt.datetime(2023, 5, 21, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
MANUAL = {"contest": {"start_time": "2023-05-21T09:00:00+08:00", "duration": "5:00:00",
                      "pta_timezone": TZ}}


def make(subs: list[dict], *, problems: list[dict] | None = None,
         now: dt.datetime = NOW, manual: dict = MANUAL) -> object:
    if problems is None:
        problems = [{"id": "A", "title": "题A"}]
    return translate("1", manual, subs, problems, None, now)


def base_sub(sid: str, status: str = "答案正确") -> dict:
    return {"id": sid, "submit_time": "2023/05/21 09:10:00", "status": status,
            "team_id": "D22", "team_name": "队", "problem_index": "A", "language": "cpp"}


def detail_sub(sid: str) -> dict:
    return dict(base_sub(sid), detail_fetched=True, program="int main(){}",
                compiler_enum="GXX",
                run_results=[{"case": "1", "result": "ACCEPTED", "time": 0.003,
                              "memory": 598016, "exitcode": 0}])


def test_first_apply_creates_events():
    store = Store()
    events = store.apply_all(make([base_sub("1")]))
    types = {(e.type, e.id) for e in events}
    assert ("submissions", "1") in types
    assert ("judgements", "1") in types
    assert ("teams", "D22") in types
    assert all(e.token > 0 for e in events)
    assert store.snapshot().submissions["1"]["id"] == "1"


def test_second_apply_same_data_no_submission_events():
    store = Store()
    store.apply_all(make([base_sub("1")]))
    events = store.apply_all(make([base_sub("1")]))
    assert [e for e in events if e.type in ("submissions", "judgements")] == []


def test_status_flip_emits_judgement_update():
    store = Store()
    store.apply_all(make([base_sub("1", "正在评测")]))
    events = store.apply_all(make([base_sub("1", "答案正确")]))
    jud_updates = [e for e in events if e.type == "judgements" and e.id == "1"]
    assert len(jud_updates) == 1
    assert jud_updates[0].data["judgement_type_id"] == "AC"
    sub_updates = [e for e in events if e.type == "submissions"]
    assert sub_updates == []  # submission itself unchanged


def test_events_since_replay():
    store = Store()
    store.apply_all(make([base_sub("1")]))
    first_token = store.events_since(None)[0].token
    store.apply_all(make([base_sub("1"), base_sub("2")]))
    replayed = store.events_since(first_token)
    assert all(e.token > first_token for e in replayed)
    assert any(e.id == "2" for e in replayed)
    assert store.is_valid_since_token(0) is True
    assert store.is_valid_since_token(store.latest_token()) is True
    assert store.is_valid_since_token(10**9) is False


def test_first_apply_emits_no_delete_events():
    store = Store()
    events = store.apply_all(make([base_sub("1")]))
    assert [e for e in events if e.data is None] == []


def test_problem_delete_emits_data_null():
    store = Store()
    store.apply_all(make([], problems=[{"id": "A", "title": "题A"},
                                        {"id": "B", "title": "题B"}]))
    events = store.apply_all(make([], problems=[{"id": "A", "title": "题A"}]))
    problem_events = [e for e in events if e.type == "problems"]
    assert len(problem_events) == 1
    assert problem_events[0].id == "B"
    assert problem_events[0].data is None
    assert problem_events[0].token > 0


def test_submission_and_judgement_delete_emits_data_null():
    store = Store()
    store.apply_all(make([base_sub("1")]))
    events = store.apply_all(make([]))
    deletes = {(e.type, e.id) for e in events if e.data is None}
    assert ("submissions", "1") in deletes
    assert ("judgements", "1") in deletes


def test_run_delete_emits_data_null():
    store = Store()
    store.apply_all(make([detail_sub("1")]))
    events = store.apply_all(make([base_sub("1")]))
    run_deletes = [e for e in events if e.type == "runs"]
    assert [(e.id, e.data) for e in run_deletes] == [("1-1", None)]


def test_contest_and_state_updates_emit_no_delete():
    store = Store()
    before = dt.datetime(2023, 5, 21, 8, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    store.apply_all(make([], now=before))
    manual = {"contest": dict(MANUAL["contest"], name="改名")}
    events = store.apply_all(make([], now=NOW, manual=manual))
    changed = {e.type for e in events}
    assert "contests" in changed
    assert "state" in changed
    assert [e for e in events if e.data is None] == []
