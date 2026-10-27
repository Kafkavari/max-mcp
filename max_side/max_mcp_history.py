"""
Command history, undo bookkeeping and scene checkpoints -- pure Python, no
pymxs, no Qt, so all of it is unit-testable outside 3ds Max. The listener
wires these to the scene (max_mcp_listener.py); the settings window shows
them (max_mcp_ui.py).

History : <config dir>/history.jsonl, one JSON object per MCP request,
          rotated when it grows past max_bytes (history.1.jsonl, .2 ...).
Undo    : every scene-changing request is accepted as the undo record
          "MCP: <tool> #<id>". Inside the same record the bridge stamps
          rootNode user property "mcp_seq" = <id>; because that stamp is part
          of the record, undoing the record restores the previous stamp. So
          the stamp tells us which bridge record is still "in effect" and
          "go back to #N" = undo until stamp < N (verified after every step).
Checkpt : optional copy of the scene before a request, saved with
          saveMaxFile ... useNewFile:false (the user's file name/path stay),
          restored through holdMaxFile/fetchMaxFile semantics so the name
          stays too. Limited by count and total size, oldest deleted first.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

CHANGING_COMMANDS = ("set_layer_hidden", "run_maxscript", "run_python")
# recorded in history but never wrapped in an undo record / checkpoint
META_COMMANDS = ("history", "undo", "undo_info")
UNDO_PREFIX = "MCP: "
MARKER_PROP = "mcp_seq"
MAX_OUTPUT_CHARS = 4000


def undo_label(tool: str, entry_id: int) -> str:
    return "%s%s #%d" % (UNDO_PREFIX, tool, entry_id)


def parse_undo_label(label: str) -> tuple[str, int] | None:
    m = re.fullmatch(re.escape(UNDO_PREFIX) + r"(\S+) #(\d+)", label or "")
    return (m.group(1), int(m.group(2))) if m else None


def is_changing(cmd: str, args: dict[str, Any] | None) -> bool:
    """Does this request open an undo record? run_* with undo=False do not."""
    if cmd not in CHANGING_COMMANDS:
        return False
    if cmd in ("run_maxscript", "run_python") and args and args.get("undo") is False:
        return False
    return True


def summarize_args(cmd: str, args: dict[str, Any] | None, width: int = 80) -> str:
    args = args or {}
    if cmd in ("run_maxscript", "run_python"):
        code = str(args.get("code") or "")
        first = code.strip().splitlines()[0] if code.strip() else ""
        more = " (+%d lines)" % (len(code.strip().splitlines()) - 1) if code.count("\n") else ""
        return (first[:width] + ("..." if len(first) > width else "")) + more
    parts = ["%s=%r" % (k, v) for k, v in args.items() if v not in ("", None, False)]
    s = ", ".join(parts)
    return s[:width] + ("..." if len(s) > width else "")


def truncate(text: Any, limit: int = MAX_OUTPUT_CHARS) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "... [%d more chars]" % (len(s) - limit)


# --------------------------------------------------------------------------
# JSONL store with rotation
# --------------------------------------------------------------------------

class HistoryStore:
    def __init__(self, path: Path | str, max_bytes: int = 5 * 1024 * 1024, keep_files: int = 2):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.keep_files = keep_files
        self._last_id = self._read_last_id()

    def _rotated(self, n: int) -> Path:
        return self.path.with_name("%s.%d%s" % (self.path.stem, n, self.path.suffix))

    def _read_last_id(self) -> int:
        last = 0
        for p in [self.path] + [self._rotated(i) for i in range(1, self.keep_files + 1)]:
            for e in self._read_file(p):
                last = max(last, int(e.get("id", 0)))
            if last:
                break
        return last

    @staticmethod
    def _read_file(p: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue  # a torn last line after a crash
        except OSError:
            pass
        return out

    def next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def append(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            fh.flush()

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        oldest = self._rotated(self.keep_files)
        if oldest.exists():
            oldest.unlink()
        for i in range(self.keep_files - 1, 0, -1):
            src = self._rotated(i)
            if src.exists():
                os.replace(src, self._rotated(i + 1))
        os.replace(self.path, self._rotated(1))

    def load(self, limit: int = 200) -> list[dict[str, Any]]:
        """Newest last; spans rotated files when the current one is short."""
        entries = self._read_file(self.path)
        i = 1
        while len(entries) < limit and i <= self.keep_files:
            older = self._read_file(self._rotated(i))
            entries = older + entries
            i += 1
        return entries[-limit:]

    def get(self, entry_id: int) -> dict[str, Any] | None:
        for e in reversed(self.load(100000)):
            if e.get("id") == entry_id:
                return e
        return None

    def files(self) -> list[Path]:
        return [p for p in [self.path] + [self._rotated(i) for i in range(1, self.keep_files + 1)] if p.exists()]


def make_entry(entry_id: int, cmd: str, args: dict[str, Any] | None, client: str | None,
               started: float, finished: float, ok: bool, result: Any = None,
               error: str | None = None, changing: bool = False, undo_label_: str | None = None,
               before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
               checkpoint: str | None = None, put_count: int | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {
        "id": entry_id,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "ts": round(started, 3),
        "client": client or "",
        "tool": cmd,
        "summary": summarize_args(cmd, args),
        "args": args or {},
        "duration_ms": int((finished - started) * 1000),
        "ok": ok,
        "changing": changing,
        "undo_label": undo_label_,
        "checkpoint": checkpoint,
        "put_count": put_count,
    }
    if ok:
        e["output"] = truncate(result)
    else:
        e["error"] = truncate(error or "")
    if before is not None and after is not None:
        e["diff"] = scene_diff(before, after)
    return e


def undone_ids(entries: list[dict[str, Any]]) -> set[int]:
    """Ids marked as undone by later max_undo / revert entries."""
    out: set[int] = set()
    for e in entries:
        for i in e.get("undone_ids") or []:
            out.add(int(i))
    return out


def scene_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Cheap diff of two snapshots {objects: [names], selection: [names],
    put_count: int}. Added/removed by name; 'changed' from the put counter."""
    b_obj, a_obj = set(before.get("objects") or []), set(after.get("objects") or [])
    d: dict[str, Any] = {
        "added": sorted(a_obj - b_obj),
        "removed": sorted(b_obj - a_obj),
        "selection_before": list(before.get("selection") or []),
        "selection_after": list(after.get("selection") or []),
    }
    pb, pa = before.get("put_count"), after.get("put_count")
    d["scene_changed"] = (pa != pb) if (pb is not None and pa is not None) else None
    return d


# --------------------------------------------------------------------------
# undo bookkeeping (what the scene functions report is injected)
# --------------------------------------------------------------------------

class UndoTracker:
    """Knows which undo levels are the bridge's own and performs "undo back to
    entry N" with verification. The scene side provides:
        undo_names() -> list[str]   current undo stack labels, newest FIRST
        do_undo()    -> None        one `max undo`

    A record is identified by its label AND its depth: when the bridge accepted
    entry N the stack had `level` entries, so N now sits at index
    len(stack) - level (newest first) as long as nothing below it changed.
    That survives Max renaming the level: when the pump runs inside a
    `python.Execute` call (dotNet-timer fallback, 3dsmaxbatch) Max wraps the
    whole call in one level named "MAXScript" and the bridge's own label is
    lost, so the label alone is not enough. Positions are re-derived from the
    live stack on every call; nothing is cached.
    """

    def __init__(self, undo_names, do_undo, max_levels=lambda: 20):
        self.undo_names = undo_names
        self.do_undo = do_undo
        self.max_levels = max_levels
        self.records: dict[int, dict[str, Any]] = {}  # id -> {label, level}

    def accepted(self, entry_id: int, label: str, level: int) -> None:
        self.records[int(entry_id)] = {"label": label, "level": int(level)}

    # --- positions ----------------------------------------------------------
    def _names(self) -> list[str]:
        return list(self.undo_names() or [])

    def position(self, entry_id: int, names: list[str] | None = None) -> int | None:
        """Index (newest first) of the entry's level, or None if gone."""
        rec = self.records.get(int(entry_id))
        if rec is None:
            return None
        names = self._names() if names is None else names
        if parse_undo_label(rec["label"]):
            # unique bridge label (live QTimer pump): find it directly
            return names.index(rec["label"]) if rec["label"] in names else None
        # generic label (Max renamed the level): depth bookkeeping. Once the
        # stack is full Max drops the oldest level and every depth shifts, so
        # positions can no longer be trusted -> report "gone".
        if len(names) >= int(self.max_levels()):
            return None
        pos = len(names) - rec["level"]
        if 0 <= pos < len(names) and names[pos] == rec["label"]:
            return pos
        return None

    def own_positions(self, names: list[str] | None = None) -> dict[int, int]:
        """index -> entry id for every bridge record still on the stack."""
        names = self._names() if names is None else names
        out: dict[int, int] = {}
        for eid in self.records:
            pos = self.position(eid, names)
            if pos is not None:
                out[pos] = eid
        return out

    def status(self, entry_id: int) -> dict[str, Any]:
        """For the UI row: revertable, steps needed, labels of steps above the
        entry that are NOT bridge records (user actions undone too)."""
        if int(entry_id) not in self.records:
            return {"revertable": False, "reason": "no undo record for this entry in this Max session", "steps": 0}
        names = self._names()
        pos = self.position(entry_id, names)
        if pos is None:
            return {"revertable": False, "reason": "no longer in the undo stack (undone, or pushed out by the undo limit)",
                    "steps": 0}
        own = self.own_positions(names)
        foreign = ["%s (#%d)" % (names[i], i + 1) for i in range(pos) if i not in own]
        return {"revertable": True, "reason": "", "steps": pos + 1, "label": names[pos],
                "foreign_steps": foreign, "user_steps_between": bool(foreign)}

    def plan_ai_undo(self, steps: int | None = None, to_id: int | None = None) -> list[int]:
        """Entry ids the AI may undo, top of the stack first. Raises ValueError
        when a non-bridge (user) level sits in the way, when the target is not
        on the stack, or when the stack is too short."""
        names = self._names()
        own = self.own_positions(names)
        if to_id is not None:
            pos = self.position(int(to_id), names)
            if pos is None:
                raise ValueError("history #%d is not on the undo stack (already undone, or pushed out by the undo limit)" % int(to_id))
            span = list(range(pos + 1))
        else:
            k = max(int(steps or 1), 1)
            if not names:
                raise ValueError("undo stack is empty")
            if len(names) < k:
                raise ValueError("only %d step(s) on the undo stack" % len(names))
            span = list(range(k))
        foreign = ["%s (#%d)" % (names[i], i + 1) for i in span if i not in own]
        if foreign:
            raise ValueError(
                "refused: step(s) not made by the bridge would be undone too: %s. "
                "Use the settings window to revert past them." % ", ".join(repr(n) for n in foreign))
        return [own[i] for i in span]

    def ai_undo(self, steps: int | None = None, to_id: int | None = None) -> list[int]:
        """Undo the planned bridge levels one by one, verifying each. Returns
        the history ids that were undone (top first)."""
        ids = self.plan_ai_undo(steps, to_id)
        undone: list[int] = []
        for eid in ids:
            names = self._names()
            if self.position(eid, names) != 0:
                raise RuntimeError("undo stack changed while undoing (entry #%d is not on top, top is %r)"
                                   % (eid, names[0] if names else None))
            self.do_undo()
            if self.position(eid) is not None:
                raise RuntimeError("max undo did not remove the level of entry #%d" % eid)
            undone.append(eid)
        return undone

    def revert(self, entry_id: int) -> int:
        """Window action: undo everything down to and including entry_id, user
        levels in between too. Verifies after every step."""
        st = self.status(entry_id)
        if not st["revertable"]:
            raise RuntimeError("cannot revert #%d: %s" % (entry_id, st["reason"]))
        done = 0
        limit = int(st["steps"]) + 2  # small slack, still bounded
        while self.position(entry_id) is not None and done < limit:
            self.do_undo()
            done += 1
        if self.position(entry_id) is not None:
            raise RuntimeError("entry #%d still on the stack after %d undo steps" % (entry_id, done))
        return done


# --------------------------------------------------------------------------
# checkpoints
# --------------------------------------------------------------------------

class CheckpointManager:
    MODES = ("off", "changing", "all")

    def __init__(self, directory: Path | str, max_count: int = 10, max_bytes: int = 2 * 1024 ** 3):
        self.dir = Path(directory)
        self.max_count = max_count
        self.max_bytes = max_bytes

    def should_save(self, mode: str, changing: bool) -> bool:
        return mode == "all" or (mode == "changing" and changing)

    def path_for(self, entry_id: int, tool: str, when: float | None = None) -> Path:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(when or time.time()))
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", tool)[:24]
        return self.dir / ("cp_%06d_%s_%s.max" % (entry_id, safe, ts))

    def list(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(self.dir.glob("cp_*.max"), key=lambda p: p.stat().st_mtime)

    def prune(self) -> list[Path]:
        """Delete oldest checkpoints beyond max_count / max_bytes."""
        removed: list[Path] = []
        files = self.list()
        total = sum(p.stat().st_size for p in files)
        while files and (len(files) > self.max_count or total > self.max_bytes):
            p = files.pop(0)
            try:
                total -= p.stat().st_size
                p.unlink()
                removed.append(p)
            except OSError:
                pass
        return removed

    @staticmethod
    def entry_id_of(path: Path | str) -> int | None:
        m = re.match(r"cp_(\d+)_", Path(path).name)
        return int(m.group(1)) if m else None
