"""
End-to-end check with a HEADLESS 3ds Max (3dsmaxbatch.exe). Optional; needs a
local Max install. It starts 3dsmaxbatch with a small scene, runs the listener
in blocking mode on Max's main thread, then drives every MCP tool through
max_mcp.server and prints the results.

    python tests/e2e_batch.py [--version 2025] [--port 30026] [--keep]

Temp files and the batch log go to the system temp dir. Nothing is written to
the Max startup folder. If another 3dsmaxbatch is running we wait for it.
Note: in batch mode there is no Qt event loop, so the QTimer pump is NOT
exercised here; that part needs an interactive Max (see README).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from max_mcp import paths  # noqa: E402

MAX_PY = r'''
# e2e helper module: the MAXScript side drives the pump through python.Execute
# (one job per call, exactly like the dotNet-timer fallback), so every scene
# change becomes its own undo level and the undo tools can be exercised.
import sys, os, time
sys.path.insert(0, r"{max_side}")
import pymxs, max_mcp_listener
rt = pymxs.runtime
deadline = time.time() + {seconds}
ls = max_mcp_listener.start(pump="none", port={port})
rt.MaxMcp_E2E_Stop = False

def tick():
    ls.pump(max_jobs=1)
    if ls.stop_requested or time.time() > deadline:
        rt.MaxMcp_E2E_Stop = True
'''

MAX_MS = r'''
(
    resetMaxFile #noPrompt
    units.SystemType = #centimeters
    theHold.setMaxUndoLevels 50
    local lyWalls = LayerManager.newLayerFromName "Walls"
    local lyTrees = LayerManager.newLayerFromName "Trees"
    for i = 1 to 3 do ( local b = Box name:("Wall_0" + i as string) width:100 length:20 height:300 pos:[i*200,0,0]; lyWalls.addNode b; setUserProp b "kind" "wall" )
    for i = 1 to 2 do ( local c = Cylinder name:("Tree_" + i as string) radius:30 height:500 pos:[0,i*300,0]; lyTrees.addNode c )
    select $Wall_01
    saveMaxFile @"{scene}" quiet:true
    format "[e2e] scene ready: % objects, file %\n" objects.count maxFileName
    python.Execute "import sys; sys.path.insert(0, r'{pydir}'); import e2e_max"
    global MaxMcp_E2E_Stop = false
    while MaxMcp_E2E_Stop != true do ( python.Execute "e2e_max.tick()"; sleep 0.01 )
    python.Execute "e2e_max.max_mcp_listener.stop()"
    format "[e2e] maxscript end\n"
)
'''


def _entry_id_of(server, tool, needle):
    h = json.loads(server.max_history(limit=20, full=True))
    for e in reversed(h["entries"]):
        if e["tool"] == tool and needle in json.dumps(e.get("args")):
            return e["id"]
    raise KeyError(needle)


def _check_checkpoint(server, tmp):
    h = json.loads(server.max_history(limit=20))
    cps = [e["checkpoint"] for e in h["entries"] if e.get("checkpoint")]
    info = json.loads(server.max_info())
    return json.dumps({"checkpoints": [os.path.basename(c) for c in cps],
                       "all_exist": all(os.path.isfile(c) for c in cps),
                       "scene_file": info["scene_file"], "scene_dir_is_tmp": os.path.normcase(info["scene_path"].rstrip("\\/")) == os.path.normcase(tmp),
                       "dirty": info["dirty"]})


def _batch_running():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq 3dsmaxbatch.exe", "/NH"],
                       capture_output=True, text=True)
    return "3dsmaxbatch.exe" in r.stdout


def _wait_port(port, seconds):
    t_end = time.time() + seconds
    while time.time() < t_end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(1)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=int, default=2025)
    ap.add_argument("--port", type=int, default=30026)
    ap.add_argument("--seconds", type=int, default=180, help="listener lifetime inside Max")
    ap.add_argument("--keep", action="store_true", help="keep temp files")
    a = ap.parse_args()

    exe = os.environ.get("MAX_MCP_3DSMAXBATCH") or paths.batch_exe(a.version)
    if not exe or not os.path.isfile(str(exe)):
        print("3dsmaxbatch.exe not found (set MAX_MCP_3DSMAXBATCH)")
        return 2
    os.environ["MAX_MCP_PORT"] = str(a.port)
    from max_mcp import client, server  # after env so the client picks the port
    client.configure()

    tmp = tempfile.mkdtemp(prefix="max_mcp_e2e_")
    py = os.path.join(tmp, "e2e_max.py")
    ms = os.path.join(tmp, "e2e_run.ms")
    log = os.path.join(tmp, "batch.log")
    with open(py, "w", encoding="utf-8") as fh:
        fh.write(MAX_PY.format(max_side=os.path.join(ROOT, "max_side"), port=a.port, seconds=a.seconds))
    scene = os.path.join(tmp, "e2e_user_scene.max")
    with open(ms, "w", encoding="utf-8") as fh:
        fh.write(MAX_MS.replace("{pydir}", tmp).replace("{scene}", scene))
    os.environ["MAX_MCP_CONFIG"] = os.path.join(tmp, "config.json")  # history + checkpoints land in tmp
    os.environ["MAX_MCP_CHECKPOINT_MODE"] = "changing"

    while _batch_running():
        print("another 3dsmaxbatch is running, waiting...")
        time.sleep(10)

    print("starting:", exe, "log:", log)
    proc = subprocess.Popen([str(exe), ms, "-log", log])
    results = {}
    try:
        t_end = time.time() + 120
        while not _wait_port(a.port, 2):
            if proc.poll() is not None or time.time() > t_end:
                print("listener never came up (batch rc=%s); see %s" % (proc.returncode, log))
                return 1
        print("listener is up on port", a.port)
        steps = [
            ("max_info", lambda: server.max_info()),
            ("max_selection", lambda: server.max_selection(detail=True)),
            ("max_query_objects(name_pattern='wall_*')", lambda: server.max_query_objects(name_pattern="wall_*")),
            ("max_query_objects(class_name='Cylinder', detail)", lambda: server.max_query_objects(class_name="Cylinder", detail=True)),
            ("max_layers", lambda: server.max_layers()),
            ("max_set_layer_hidden('Trees', True)", lambda: server.max_set_layer_hidden("Trees", True)),
            ("max_layers (after hide)", lambda: server.max_layers()),
            ("max_run_maxscript", lambda: server.max_run_maxscript("(for o in objects collect o.name) as string")),
            ("max_run_maxscript (create UndoMe, undo record)", lambda: server.max_run_maxscript("b = Box name:\"UndoMe\"; isValidNode b")),
            ("max_run_maxscript (max undo, no record)", lambda: server.max_run_maxscript("max undo; isValidNode $UndoMe", undo=False)),
            ("max_run_python (error inside undo -> cancelled)", lambda: server.max_run_python("rt.Box(name='Half'); 1/0")),
            ("max_query_objects(Half) expect 0", lambda: server.max_query_objects(name_pattern="Half")),
            ("max_run_python", lambda: server.max_run_python("print('hello from max'); result = [o.name for o in rt.objects]")),
            ("max_run_python (error)", lambda: server.max_run_python("1/0")),
            ("max_run_maxscript (error)", lambda: server.max_run_maxscript("undefinedThing + 1")),
            ("max_run_python (timeout 2s)", lambda: server.max_run_python("import time; time.sleep(4)", timeout=2)),
            ("max_info (after timeout, still alive)", lambda: server.max_info()),
            ("max_viewport_capture", lambda: server.max_viewport_capture(path=os.path.join(tmp, "cap.png"), max_size=512)),
            # --- history / AI undo / checkpoints ---
            ("history: two MCP changes (A, B)", lambda: server.max_run_maxscript("Box name:\"A_box\"; Box name:\"B_box\"; objects.count") + server.max_run_python("rt.Sphere(name='C_sph'); result = rt.objects.count")),
            ("max_history", lambda: server.max_history(limit=6)),
            ("max_undo steps=1 (removes C_sph)", lambda: server.max_undo(steps=1)),
            ("query C_sph expect 0", lambda: server.max_query_objects(name_pattern="C_sph")),
            ("max_undo to_history_id (A/B box entry)", lambda: server.max_undo(to_history_id=_entry_id_of(server, "run_maxscript", "A_box"))),
            ("query *_box expect 0", lambda: server.max_query_objects(name_pattern="*_box")),
            ("max_undo steps=1 again (next bridge level or error)", lambda: server.max_undo(steps=1)),
            ("max_undo on empty stack -> error", lambda: server.max_undo(steps=5)),
            ("max_history after undo (states)", lambda: server.max_history(limit=5)),
            ("checkpoint: scene_file unchanged + file exists", lambda: _check_checkpoint(server, tmp)),
            ("restore_checkpoint via window function (not a tool)", lambda: server.max_run_python(
                "import max_mcp_listener, max_mcp_history, glob, os\n"
                "cps = sorted(glob.glob(os.path.join(r'%s', 'checkpoints', 'cp_*.max')))\n"
                "rt.Box(name='AfterCp')\n"
                "r = max_mcp_listener.restore_checkpoint(cps[0])\n"
                "result = dict(r, after_cp_exists=rt.getNodeByName('AfterCp') is not None, scene_file=str(rt.maxFileName), objects=rt.objects.count)" % tmp,
                undo=False)),
        ]
        for name, fn in steps:
            t0 = time.time()
            try:
                out = fn()
                ok = True
            except Exception as exc:  # noqa: BLE001
                out = "%s: %s" % (type(exc).__name__, exc)
                ok = False
            dt = time.time() - t0
            results[name] = (ok, out)
            print("\n=== %s  [%s, %.2fs]\n%s" % (name, "ok" if ok else "ERROR", dt, out))
    finally:
        try:
            client.call("_stop", port=a.port, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            print("batch did not exit; leaving it alone (not killing)")
    print("\nbatch exit code:", proc.returncode)
    hist = os.path.join(tmp, "history.jsonl")
    if os.path.isfile(hist):
        with open(hist, "r", encoding="utf-8") as fh:
            rows = fh.read().splitlines()
        print("history.jsonl: %d rows, last: %s" % (len(rows), rows[-1][:300] if rows else ""))
    if os.path.isfile(log):
        raw = open(log, "rb").read()
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
        lines = [l + "\n" for l in text.splitlines() if "[e2e]" in l or "MaxMcp" in l or "rror" in l]
        print("--- batch log excerpt ---\n" + "".join(lines[-40:]))
    summary = {k: v[0] for k, v in results.items()}
    print("summary:", json.dumps(summary, indent=1))
    if not a.keep:
        print("temp dir kept for inspection anyway:", tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
