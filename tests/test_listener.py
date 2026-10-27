"""
Unit tests for the in-Max listener. They run WITHOUT 3ds Max: a tiny fake
`pymxs` module is injected so the command handlers can be exercised, and the
queue/thread/timeout machinery is tested with plain Python handlers.

    pytest tests/
"""

import contextlib
import json
import os
import socket
import sys
import threading
import time
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "max_side"))
sys.path.insert(0, os.path.dirname(HERE))


# --------------------------------------------------------------------------
# fake pymxs (only what the handlers touch)
# --------------------------------------------------------------------------

class _Vec:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z

    def __str__(self):
        return "[%s,%s,%s]" % (self.x, self.y, self.z)


class _Layer:
    def __init__(self, name, hidden=False):
        self.name = name
        self.isHidden = hidden
        self.isFrozen = False
        self.current = name == "0"
        self.on = True


class _Node:
    def __init__(self, name, cls, layer, pos=(0, 0, 0)):
        self.name = name
        self._cls = cls
        self.layer = layer
        self.pos = _Vec(*pos)
        self.min = _Vec(-1, -1, 0)
        self.max = _Vec(1, 1, 2)
        self.rotation = "(quat 0 0 0 1)"
        self.scale = _Vec(1, 1, 1)
        self.parent = None
        self.children = types.SimpleNamespace(count=0)
        self.isHidden = False
        self.isFrozen = False
        self.isSelected = False
        self.wirecolor = "(color 1 2 3)"
        self.material = None


class _LayerManager:
    def __init__(self, layers):
        self._layers = layers

    @property
    def count(self):
        return len(self._layers)

    def getLayer(self, i):
        return self._layers[i]

    def getLayerFromName(self, name):
        for ly in self._layers:
            if ly.name == name:
                return ly
        return None


def _make_fake_pymxs():
    layers = [_Layer("0"), _Layer("Walls"), _Layer("Trees", hidden=True)]
    nodes = [
        _Node("Wall_01", "Editable_Poly", layers[1], (10, 0, 0)),
        _Node("Wall_02", "Editable_Poly", layers[1], (20, 0, 0)),
        _Node("Tree_A", "Box", layers[2], (0, 5, 0)),
    ]
    nodes[0].isSelected = True

    class _Objects(list):
        @property
        def count(self):
            return len(self)

    rt = types.SimpleNamespace()
    rt.objects = _Objects(nodes)
    rt.selection = _Objects([n for n in nodes if n.isSelected])
    rt.layerManager = _LayerManager(layers)
    rt.classOf = lambda n: getattr(n, "_cls", type(n).__name__)
    rt.superClassOf = lambda n: "GeometryClass"
    rt.getUserPropBuffer = lambda n: "flag=1\r\n"
    rt.canConvertTo = lambda n, c: False
    rt.Editable_Mesh = object()
    rt.matchPattern = lambda s, pattern="", ignoreCase=True: _wild(s.lower(), pattern.lower())
    rt.redrawViews = lambda: None
    rt.maxVersion = lambda: [27000, 55, 0, 27, 3, 0, 2025]
    rt.maxFilePath = "C:/scenes/"
    rt.maxFileName = "test.max"
    rt.getSaveRequired = lambda: False
    rt.units = types.SimpleNamespace(SystemType="centimeters", DisplayType="metric")
    rt.currentTime = "0f"
    rt.animationRange = "(interval 0f 100f)"
    rt.renderers = types.SimpleNamespace(current=object())
    rt.GetQuietMode = lambda: True
    rt.hold_calls = []
    rt.undo_stack = []  # newest first, like theHold.getUndoNames()
    rt.put = 0
    rt.max_levels = 100

    def _execute(code):
        if code == "max undo":
            rt.undo_stack.pop(0)
            return True
        return eval(code, {})  # fake MAXScript: plain python expr

    def _set_levels(n):
        rt.max_levels = n

    @contextlib.contextmanager
    def undo(onoff, label="MAXScript"):
        """Like pymxs.undo(True, label): opens a named record and SWALLOWS
        exceptions (that is what the real one does)."""
        rt.hold_calls.append("begin")
        try:
            yield
        except Exception:  # noqa: BLE001
            pass
        rt.hold_calls.append("accept:" + label)
        rt.undo_stack.insert(0, label)
        rt.put += 1

    rt.execute = _execute
    rt.theHold = types.SimpleNamespace(
        getUndoNames=lambda: list(rt.undo_stack),
        getRedoNames=lambda: [],
        getCurrentUndoLevels=lambda: len(rt.undo_stack),
        getMaxUndoLevels=lambda: rt.max_levels,
        setMaxUndoLevels=_set_levels,
        GetGlobalPutCount=lambda: rt.put,
    )
    rt.dirty = False
    rt.saved = []
    rt.getSaveRequired = lambda: rt.dirty

    def _set_save_required(v):
        rt.dirty = bool(v)

    def _save(path, useNewFile=True, quiet=False):
        rt.saved.append({"path": path, "useNewFile": useNewFile, "quiet": quiet})
        rt.dirty = False
        with open(path, "wb") as fh:
            fh.write(b"fake max scene")
        return True

    rt.setSaveRequired = _set_save_required
    rt.saveMaxFile = _save
    rt.redrawViews = lambda: None

    mod = types.ModuleType("pymxs")
    mod.runtime = rt
    mod.undo = undo
    return mod


def _wild(s, pat):
    import fnmatch
    return fnmatch.fnmatchcase(s, pat)


@pytest.fixture()
def fake_pymxs(monkeypatch):
    mod = _make_fake_pymxs()
    monkeypatch.setitem(sys.modules, "pymxs", mod)
    return mod


@pytest.fixture()
def listener_mod():
    import max_mcp_listener
    return max_mcp_listener


# --------------------------------------------------------------------------
# protocol / queue / threads
# --------------------------------------------------------------------------

def test_handle_line_rejects_bad_json(listener_mod):
    ls = listener_mod.Listener({})
    r = ls.handle_line("{not json")
    assert r["ok"] is False and "JSON" in r["error"]


def test_ping_and_unknown_command(listener_mod):
    ls = listener_mod.Listener({})
    assert ls.handle_line(json.dumps({"id": 1, "cmd": "ping"}))["result"]["pong"] is True
    r = ls.handle_line(json.dumps({"id": 2, "cmd": "nope"}))
    assert r["ok"] is False and "nope" in r["error"]


def test_key_required_when_set(listener_mod):
    ls = listener_mod.Listener({}, key="secret")
    assert ls.handle_line(json.dumps({"id": 1, "cmd": "ping"}))["ok"] is False
    assert ls.handle_line(json.dumps({"id": 1, "cmd": "ping", "key": "secret"}))["ok"] is True


def test_job_runs_on_pump_thread_and_returns_result(listener_mod):
    seen = {}

    def echo(args):
        seen["thread"] = threading.current_thread()
        return {"echo": args}

    ls = listener_mod.Listener({"echo": echo})
    out = {}
    t = threading.Thread(target=lambda: out.update(
        ls.handle_line(json.dumps({"id": 7, "cmd": "echo", "args": {"a": 1}, "timeout": 5}))))
    t.start()
    # pump from THIS thread (stands in for Max's main thread)
    deadline = time.time() + 5
    while not out and time.time() < deadline:
        ls.pump()
        time.sleep(0.01)
    t.join(5)
    assert out == {"id": 7, "ok": True, "result": {"echo": {"a": 1}}}
    assert seen["thread"] is threading.current_thread()


def test_timeout_when_nobody_pumps_cancels_job(listener_mod):
    calls = []
    ls = listener_mod.Listener({"slow": lambda a: calls.append(1)})
    t0 = time.time()
    r = ls.handle_line(json.dumps({"id": 1, "cmd": "slow", "timeout": 0.3}))
    assert r["ok"] is False and "zaman asimi" in r["error"]
    assert time.time() - t0 < 2
    ls.pump()  # late pump must skip the cancelled job
    assert calls == []
    assert ls.stats["timeouts"] == 1


def test_handler_exception_becomes_error(listener_mod):
    def boom(_a):
        raise ValueError("kaboom")

    ls = listener_mod.Listener({"boom": boom})
    out = {}
    t = threading.Thread(target=lambda: out.update(
        ls.handle_line(json.dumps({"id": 1, "cmd": "boom", "timeout": 5}))))
    t.start()
    while not out:
        ls.pump()
        time.sleep(0.01)
    t.join()
    assert out["ok"] is False and "ValueError: kaboom" in out["error"]


def test_socket_roundtrip_with_client(listener_mod):
    """Real TCP: listener on a free port, serve_blocking in a thread (it is the
    'main thread' for this test), max_mcp.client.call from here."""
    from max_mcp import client

    port = _free_port()
    ls = listener_mod.Listener({"double": lambda a: a["n"] * 2}, port=port)
    ls.start()
    th = threading.Thread(target=ls.serve_blocking, kwargs={"seconds": 10}, daemon=True)
    th.start()
    try:
        assert client.call("double", {"n": 21}, port=port, timeout=5) == 42
        with pytest.raises(client.MaxError, match="unknown|bilinmeyen"):
            client.call("missing", port=port, timeout=5)
        assert client.call("_stop", port=port)
    finally:
        ls.stop()
        th.join(5)
    assert ls.stats["handled"] == 1


def test_client_reports_unreachable():
    from max_mcp import client
    with pytest.raises(client.MaxError, match="cannot reach"):
        client.call("ping", port=_free_port(), timeout=1)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --------------------------------------------------------------------------
# handlers against the fake pymxs
# --------------------------------------------------------------------------

def test_h_info(fake_pymxs, listener_mod):
    d = listener_mod.h_info({})
    assert d["scene_file"] == "test.max" and d["object_count"] == 3
    assert d["dirty"] is False and d["listener"]["pump_on_main_thread"] is True


def test_h_selection(fake_pymxs, listener_mod):
    d = listener_mod.h_selection({"detail": True})
    assert d["count"] == 1 and d["objects"][0]["name"] == "Wall_01"
    assert d["objects"][0]["user_props"] == "flag=1"
    assert d["objects"][0]["bbox_max"] == [1, 1, 2]


def test_h_query_objects_filters(fake_pymxs, listener_mod):
    d = listener_mod.h_query_objects({"name_pattern": "wall_*"})
    assert d["matched"] == 2 and {o["name"] for o in d["objects"]} == {"Wall_01", "Wall_02"}
    d = listener_mod.h_query_objects({"class_name": "box"})
    assert d["matched"] == 1 and d["objects"][0]["layer"] == "Trees"
    d = listener_mod.h_query_objects({"layer": "walls", "limit": 1})
    assert d["matched"] == 2 and d["returned"] == 1
    d = listener_mod.h_query_objects({"selected_only": True})
    assert d["matched"] == 1


def test_h_layers_and_hide(fake_pymxs, listener_mod):
    d = listener_mod.h_layers({})
    byname = {l["name"]: l for l in d["layers"]}
    assert byname["Trees"]["hidden"] is True and byname["Walls"]["objects"] == 2
    r = listener_mod.h_set_layer_hidden({"name": "Walls", "hidden": True})
    assert r["hidden"] is True
    with pytest.raises(ValueError):
        listener_mod.h_set_layer_hidden({"name": "Nope"})


def test_h_run_maxscript_and_python(fake_pymxs, listener_mod):
    r = listener_mod.h_run_maxscript({"code": "2+3"})
    assert r["result"] == "5"
    r = listener_mod.h_run_python({"code": "rt.objects.count * 10"})
    assert r["result"] == 30
    r = listener_mod.h_run_python({"code": "print('hi')\nresult = [n.name for n in rt.objects]"})
    assert r["result"] == ["Wall_01", "Wall_02", "Tree_A"] and r["output"].strip() == "hi"
    with pytest.raises(ZeroDivisionError):
        listener_mod.h_run_python({"code": "1/0"})


@pytest.fixture()
def recorded(fake_pymxs, listener_mod, tmp_path, monkeypatch):
    """execute_recorded() wired to the fake scene and a temp history dir."""
    monkeypatch.setenv("MAX_MCP_CONFIG", str(tmp_path / "config.json"))
    for k in ("MAX_MCP_PORT", "MAX_MCP_KEY", "MAX_MCP_CHECKPOINT_MODE", "MAX_MCP_AI_UNDO"):
        monkeypatch.delenv(k, raising=False)
    listener_mod._hist.update({"store": None, "tracker": None, "cp": None})
    listener_mod._state["cfg"] = None
    listener_mod._history_setup(listener_mod.current_config())
    return listener_mod


def test_execute_recorded_undo_records_and_history(recorded, fake_pymxs):
    rt = fake_pymxs.runtime
    calls = rt.hold_calls
    r = recorded.execute_recorded("run_python", {"code": "rt.objects.count"}, client="test-client")
    assert r["result"] == 3
    assert calls[-2:] == ["begin", "accept:MCP: run_python #1"]
    assert rt.undo_stack == ["MCP: run_python #1"]
    # read-only command: no undo record
    n = len(calls)
    recorded.execute_recorded("layers", {})
    assert len(calls) == n
    # error inside a changing command: propagated, the half record rolled back
    with pytest.raises(ZeroDivisionError):
        recorded.execute_recorded("run_python", {"code": "rt.objects.count; 1/0"})
    assert calls[-2:] == ["begin", "accept:MCP: run_python #3"]
    assert rt.undo_stack == ["MCP: run_python #1"]  # rolled back with max undo
    # undo=False -> no record
    n = len(calls)
    recorded.execute_recorded("run_python", {"code": "1", "undo": False})
    assert len(calls) == n
    h = recorded.h_history({"limit": 10, "full": True})
    ids = [(e["id"], e["tool"], e["ok"]) for e in h["entries"]]
    assert ids == [(1, "run_python", True), (2, "layers", True), (3, "run_python", False), (4, "run_python", True)]
    e1 = h["entries"][0]
    assert e1["client"] == "test-client" and e1["undo_state"] == "on_stack" and e1["args"]["code"] == "rt.objects.count"
    assert e1["diff"]["added"] == [] and e1["diff"]["selection_before"] == ["Wall_01"]
    assert "error" in h["entries"][2] and "ZeroDivisionError" in h["entries"][2]["error"]
    assert h["entries"][3]["undo_label"] is None


def test_ai_undo_only_bridge_steps(recorded, fake_pymxs):
    rt = fake_pymxs.runtime
    recorded.execute_recorded("run_python", {"code": "1"})   # #1
    recorded.execute_recorded("run_python", {"code": "2"})   # #2
    assert rt.undo_stack == ["MCP: run_python #2", "MCP: run_python #1"]
    r = recorded.execute_recorded("undo", {"steps": 1})
    assert r["undone_ids"] == [2] and rt.undo_stack == ["MCP: run_python #1"]
    h = recorded.h_history({"limit": 10})
    states = {e["id"]: e.get("undo_state") for e in h["entries"]}
    assert states[2] == "undone" and states[1] == "on_stack" and h["entries"][-1]["tool"] == "undo"
    # a user step in between -> refused, user step named
    recorded.execute_recorded("run_python", {"code": "3"})   # #4 (id 3 was the undo entry)
    rt.undo_stack.insert(0, "Move")
    recorded.execute_recorded("run_python", {"code": "4"})   # #5
    with pytest.raises(PermissionError, match="refused.*'Move"):
        recorded.execute_recorded("undo", {"to_history_id": 4})
    assert rt.undo_stack[0] == "MCP: run_python #5"  # nothing was undone
    r = recorded.execute_recorded("undo", {"to_history_id": 5})
    assert r["undone_ids"] == [5] and rt.undo_stack[0] == "Move"
    # target fell off the stack
    rt.undo_stack.clear()
    with pytest.raises(PermissionError, match="not on the undo stack"):
        recorded.execute_recorded("undo", {"to_history_id": 1})
    with pytest.raises(PermissionError, match="empty"):
        recorded.execute_recorded("undo", {"steps": 1})


def test_ai_undo_disabled_switch(recorded, fake_pymxs, monkeypatch):
    recorded.execute_recorded("run_python", {"code": "1"})
    monkeypatch.setenv("MAX_MCP_AI_UNDO", "false")
    recorded._state["cfg"] = None
    with pytest.raises(PermissionError, match="disabled"):
        recorded.execute_recorded("undo", {"steps": 1})
    assert fake_pymxs.runtime.undo_stack == ["MCP: run_python #1"]


def test_window_revert_past_user_steps_and_rerun(recorded, fake_pymxs):
    rt = fake_pymxs.runtime
    recorded.execute_recorded("run_python", {"code": "1"})   # #1
    rt.undo_stack.insert(0, "Rotate")
    recorded.execute_recorded("run_python", {"code": "2"})   # #2
    st = recorded.history_status(1)
    assert st["revertable"] and st["steps"] == 3 and st["foreign_steps"] == ["Rotate (#2)"]
    r = recorded.revert_to(1)
    assert r == {"undone_steps": 3, "undone_ids": [2, 1]} and rt.undo_stack == []
    assert not recorded.history_status(1)["revertable"]
    r2 = recorded.rerun(2)
    assert r2["result"] == 2 and rt.undo_stack == ["MCP: run_python #4"]
    with pytest.raises(ValueError):
        recorded.rerun(3)  # the revert entry itself


def test_deferred_level_accounting_python_execute_mode(recorded, fake_pymxs, monkeypatch):
    """Inside python.Execute the level appears only after the call returns
    (as 'MAXScript'); the next entry reconciles it, and a failed call's level
    is rolled back."""
    rt = fake_pymxs.runtime
    pushed = []

    @contextlib.contextmanager
    def deferred_undo(onoff, label="MAXScript"):
        try:
            yield
        except Exception:  # noqa: BLE001
            pass
        pushed.append("MAXScript")  # Max will create it after the call

    monkeypatch.setattr(fake_pymxs, "undo", deferred_undo)
    recorded.execute_recorded("run_python", {"code": "1"})          # #1
    assert rt.undo_stack == [] and recorded._hist["pending"]["id"] == 1
    rt.undo_stack.insert(0, pushed.pop())                           # python.Execute returned
    recorded.execute_recorded("run_python", {"code": "2"})          # #2 reconciles #1
    rt.undo_stack.insert(0, pushed.pop())
    assert recorded.h_history({"limit": 5})["entries"][0]["undo_state"] == "on_stack"
    assert recorded.h_history({"limit": 5})["entries"][1]["undo_state"] == "on_stack"
    assert recorded._hist["tracker"].own_positions() == {0: 2, 1: 1}
    r = recorded.execute_recorded("undo", {"to_history_id": 1})
    assert r["undone_ids"] == [2, 1] and rt.undo_stack == []
    # failed call: the half level is rolled back on reconcile
    with pytest.raises(ZeroDivisionError):
        recorded.execute_recorded("run_python", {"code": "1/0"})   # #4
    rt.undo_stack.insert(0, pushed.pop())
    recorded.pump()  # nothing to pump (no listener) but reconciles
    assert rt.undo_stack == []


def test_checkpoint_mode_saves_copy_keeps_name_and_dirty(recorded, fake_pymxs, monkeypatch):
    rt = fake_pymxs.runtime
    monkeypatch.setenv("MAX_MCP_CHECKPOINT_MODE", "changing")
    recorded._state["cfg"] = None
    rt.dirty = True
    recorded.execute_recorded("layers", {})
    assert rt.saved == []
    r = recorded.execute_recorded("run_python", {"code": "1"})
    assert len(rt.saved) == 1 and rt.saved[0]["useNewFile"] is False and rt.saved[0]["quiet"] is True
    assert os.path.basename(rt.saved[0]["path"]).startswith("cp_000002_run_python_")
    assert rt.maxFileName == "test.max" and rt.dirty is True  # name kept, dirty restored
    e = recorded.h_history({"limit": 1})["entries"][0]
    assert e["checkpoint"] == rt.saved[0]["path"]
    assert recorded.set_undo_levels(44) == 44


def test_no_save_command_exists(listener_mod):
    """The bridge must never save the user's scene."""
    assert not any("save" in k for k in listener_mod.HANDLERS)
