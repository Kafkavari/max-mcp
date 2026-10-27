"""History store + rotation, undo bookkeeping by labels, checkpoint naming
and pruning. Pure Python, no 3ds Max."""

import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "max_side"))
sys.path.insert(0, os.path.dirname(HERE))

import max_mcp_history as H  # noqa: E402


def test_labels_and_changing():
    assert H.undo_label("run_python", 12) == "MCP: run_python #12"
    assert H.parse_undo_label("MCP: run_python #12") == ("run_python", 12)
    assert H.parse_undo_label("Move") is None
    assert H.is_changing("run_python", {"code": "x"})
    assert not H.is_changing("run_python", {"code": "x", "undo": False})
    assert H.is_changing("set_layer_hidden", {})
    assert not H.is_changing("selection", {})


def test_summaries_and_truncate():
    assert H.summarize_args("run_python", {"code": "a = 1\nb = 2\nc = 3"}) == "a = 1 (+2 lines)"
    assert H.summarize_args("query_objects", {"name_pattern": "Wall*", "detail": False}) == "name_pattern='Wall*'"
    assert H.truncate("x" * 10, 4).startswith("xxxx... [6 more chars]")
    assert H.truncate({"a": 1}) == '{"a": 1}'


def test_store_append_load_ids_and_rotation(tmp_path):
    p = tmp_path / "h" / "history.jsonl"
    st = H.HistoryStore(p, max_bytes=600, keep_files=2)
    assert st.next_id() == 1
    for i in range(1, 31):
        st.append({"id": i, "tool": "t", "pad": "x" * 40})
    files = st.files()
    assert p in files and len(files) >= 2, "rotation should have produced older files"
    allv = st.load(1000)
    ids = [e["id"] for e in allv]
    # spans rotated files, oldest first; the very oldest were dropped by rotation
    assert ids == list(range(ids[0], 31)) and 1 < ids[0] < 20 and len(ids) > 10
    assert [e["id"] for e in st.load(3)] == [28, 29, 30]
    assert st.get(29)["id"] == 29 and st.get(999) is None
    # a new store (Max restarted) continues numbering and survives a torn line
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"id": 31, "tool": "t"')  # no newline, broken json
    st2 = H.HistoryStore(p, max_bytes=600, keep_files=2)
    assert st2.next_id() == 31


def test_make_entry_and_diff():
    before = {"objects": ["A", "B"], "selection": ["A"], "put_count": 5}
    after = {"objects": ["A", "C"], "selection": [], "put_count": 9}
    t0 = time.time()
    e = H.make_entry(4, "run_python", {"code": "rt.Box()"}, "claude-code", t0, t0 + 0.25, True,
                     result={"result": 1}, changing=True, undo_label_="MCP: run_python #4",
                     before=before, after=after, checkpoint="c:/cp.max", put_count=9)
    assert e["duration_ms"] == 250 and e["client"] == "claude-code" and e["ok"]
    assert e["diff"] == {"added": ["C"], "removed": ["B"], "selection_before": ["A"],
                         "selection_after": [], "scene_changed": True}
    e2 = H.make_entry(5, "info", {}, None, t0, t0, False, error="boom")
    assert e2["error"] == "boom" and "output" not in e2 and e2["changing"] is False
    json.dumps(e)  # serialisable


class _Stack:
    """Fake undo stack, newest first, like theHold.getUndoNames()."""

    def __init__(self):
        self.names = []
        self.undone = []

    def push(self, name):
        self.names.insert(0, name)

    def undo(self):
        self.undone.append(self.names.pop(0))


def _accept(stk, tr, eid, label):
    """What the executor does: push the level, remember label + depth."""
    stk.push(label)
    tr.accepted(eid, label, len(stk.names))


def test_undo_tracker_status_and_revert():
    stk = _Stack()
    tr = H.UndoTracker(lambda: stk.names, stk.undo, max_levels=lambda: 20)
    stk.push("Create Box")  # user, before
    for i in (1, 2):
        _accept(stk, tr, i, H.undo_label("run_python", i))
    stk.push("Move")  # user action after #2
    lbl3 = H.undo_label("set_layer_hidden", 3)
    _accept(stk, tr, 3, lbl3)

    assert tr.status(99) == {"revertable": False, "reason": "no undo record for this entry in this Max session", "steps": 0}
    s3 = tr.status(3)
    assert s3["revertable"] and s3["steps"] == 1 and s3["foreign_steps"] == []
    s2 = tr.status(2)
    assert s2["steps"] == 3 and s2["foreign_steps"] == ["Move (#2)"] and s2["user_steps_between"]
    assert tr.own_positions() == {0: 3, 2: 2, 3: 1}
    assert tr.revert(2) == 3
    assert stk.undone == [lbl3, "Move", H.undo_label("run_python", 2)]
    assert stk.names == [H.undo_label("run_python", 1), "Create Box"]
    assert tr.status(2)["revertable"] is False and "no longer" in tr.status(2)["reason"]
    assert tr.status(1)["revertable"] and tr.status(1)["steps"] == 1
    with pytest.raises(RuntimeError, match="cannot revert #3"):
        tr.revert(3)


def test_tracker_works_with_generic_labels():
    """Inside python.Execute Max names every level 'MAXScript'; the depth
    bookkeeping still tells the bridge's levels from the user's."""
    stk = _Stack()
    tr = H.UndoTracker(lambda: stk.names, stk.undo)
    _accept(stk, tr, 1, "MAXScript")
    stk.push("MAXScript")   # user ran a script by hand
    _accept(stk, tr, 2, "MAXScript")
    assert tr.own_positions() == {0: 2, 2: 1}
    assert tr.plan_ai_undo(steps=1) == [2]
    with pytest.raises(ValueError, match="refused.*MAXScript \\(#2\\)"):
        tr.plan_ai_undo(to_id=1)
    assert tr.ai_undo(steps=1) == [2]
    assert tr.status(1)["foreign_steps"] == ["MAXScript (#1)"]
    # when the stack reaches Max's undo limit the oldest levels get dropped and
    # depths shift: generic-label bookkeeping must give up rather than guess
    tr.max_levels = lambda: 2
    assert tr.position(1) is None and not tr.status(1)["revertable"]
    with pytest.raises(ValueError, match="not on the undo stack"):
        tr.plan_ai_undo(to_id=1)


def test_plan_ai_undo_rules():
    stk = _Stack()
    tr = H.UndoTracker(lambda: stk.names, stk.undo)
    with pytest.raises(ValueError, match="empty"):
        tr.plan_ai_undo(steps=1)
    _accept(stk, tr, 1, H.undo_label("run_python", 1))
    _accept(stk, tr, 2, H.undo_label("run_python", 2))
    assert tr.plan_ai_undo(steps=1) == [2]
    assert tr.plan_ai_undo(to_id=1) == [2, 1]
    with pytest.raises(ValueError, match="only 2 step"):
        tr.plan_ai_undo(steps=5)
    stk.push("Scale")
    _accept(stk, tr, 3, H.undo_label("set_layer_hidden", 3))
    assert tr.plan_ai_undo(steps=1) == [3]
    with pytest.raises(ValueError, match="refused.*'Scale \\(#2\\)'"):
        tr.plan_ai_undo(to_id=2)
    with pytest.raises(ValueError, match="refused"):
        tr.plan_ai_undo(steps=2)
    with pytest.raises(ValueError, match="not on the undo stack"):
        tr.plan_ai_undo(to_id=42)
    assert tr.ai_undo(steps=1) == [3] and stk.names[0] == "Scale"
    # the stack keeps "Scale" on top: the AI cannot pass it
    with pytest.raises(ValueError, match="refused"):
        tr.ai_undo(to_id=2)
    assert H.undone_ids([{"undone_ids": [3]}, {"x": 1}, {"undone_ids": ["7"]}]) == {3, 7}


def test_undo_tracker_pushed_out_by_limit():
    stk = _Stack()
    tr = H.UndoTracker(lambda: stk.names, stk.undo)
    _accept(stk, tr, 7, H.undo_label("run_maxscript", 7))
    stk.names = stk.names[:0]  # Max dropped it (undo limit reached)
    assert not tr.status(7)["revertable"]
    # stack replaced by unrelated levels of the same depth but other labels
    stk.push("Rotate")
    assert tr.position(7) is None


def test_checkpoint_naming_and_prune(tmp_path):
    cm = H.CheckpointManager(tmp_path / "cp", max_count=3, max_bytes=10_000)
    assert cm.should_save("off", True) is False
    assert cm.should_save("changing", True) and not cm.should_save("changing", False)
    assert cm.should_save("all", False)
    p = cm.path_for(12, "run_python", when=0)
    assert p.name.startswith("cp_000012_run_python_") and p.suffix == ".max"
    assert H.CheckpointManager.entry_id_of(p) == 12
    cm.dir.mkdir()
    for i in range(1, 6):
        f = cm.path_for(i, "t", when=i); f.write_bytes(b"x" * 100)
        os.utime(f, (i, i))
    removed = cm.prune()
    assert [H.CheckpointManager.entry_id_of(r) for r in removed] == [1, 2]
    assert [H.CheckpointManager.entry_id_of(r) for r in cm.list()] == [3, 4, 5]
    cm.max_bytes = 150
    assert [H.CheckpointManager.entry_id_of(r) for r in cm.prune()] == [3, 4]
