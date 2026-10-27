"""
max-mcp settings + history window for 3ds Max (PySide6, Max's bundled Qt).
All logic lives in max_mcp_settings / max_mcp_history / max_mcp_listener;
this file is only widgets.

Open from Max:  macroScript "MaxMcp Settings" (category max-mcp), or
    python.Execute "import max_mcp_ui; max_mcp_ui.show()"
"""

from __future__ import annotations

import json
import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import max_mcp_settings  # noqa: E402

_dialog = None
GREY = QtGui.QColor(140, 140, 140)


def _max_main_window():
    try:
        from qtmax import GetQMaxMainWindow
        return GetQMaxMainWindow()
    except Exception:  # noqa: BLE001
        return None


def _listener():
    import max_mcp_listener
    return max_mcp_listener


# ==========================================================================
# Settings tab
# ==========================================================================

class SettingsTab(QtWidgets.QWidget):
    def __init__(self, model: max_mcp_settings.SettingsModel, status_lbl: QtWidgets.QLabel, parent=None):
        super().__init__(parent)
        self.model = model
        self.status_lbl = status_lbl
        self._build()
        self._fill()

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("Start")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_restart = QtWidgets.QPushButton("Restart")
        for b in (self.btn_start, self.btn_stop, self.btn_restart):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.btn_start.clicked.connect(lambda: self._guard(self.model.start))
        self.btn_stop.clicked.connect(lambda: self._guard(self.model.stop))
        self.btn_restart.clicked.connect(lambda: self._guard(self.model.restart))

        form = QtWidgets.QFormLayout()
        self.port = QtWidgets.QSpinBox(); self.port.setRange(1, 65535)
        self.host = QtWidgets.QLineEdit()
        self.key_enabled = QtWidgets.QCheckBox("require shared key")
        self.key = QtWidgets.QLineEdit()
        self.btn_gen = QtWidgets.QPushButton("Generate")
        self.btn_gen.clicked.connect(lambda: self.key.setText(self.model.generate_key()))
        keyrow = QtWidgets.QHBoxLayout(); keyrow.addWidget(self.key); keyrow.addWidget(self.btn_gen)
        self.autostart = QtWidgets.QCheckBox("start listener when 3ds Max starts (needs the startup loader)")
        self.ai_undo = QtWidgets.QCheckBox("AI may undo its own steps (max_undo tool)")
        self.pump = QtWidgets.QSpinBox(); self.pump.setRange(5, 5000); self.pump.setSuffix(" ms")
        self.timeout = QtWidgets.QDoubleSpinBox(); self.timeout.setRange(1, 3600); self.timeout.setSuffix(" s")
        self.http_host = QtWidgets.QLineEdit()
        self.http_port = QtWidgets.QSpinBox(); self.http_port.setRange(1, 65535)
        self.cp_mode = QtWidgets.QComboBox(); self.cp_mode.addItems(["off", "changing", "all"])
        self.cp_mode.setToolTip("off: no scene copies. changing: copy the scene before every scene-changing "
                                "request. all: before every request. Copies go to <config dir>/checkpoints.")
        self.cp_count = QtWidgets.QSpinBox(); self.cp_count.setRange(1, 500)
        self.cp_mb = QtWidgets.QSpinBox(); self.cp_mb.setRange(10, 1000000); self.cp_mb.setSuffix(" MB")
        self.undo_levels = QtWidgets.QSpinBox(); self.undo_levels.setRange(1, 10000)
        self.btn_undo_levels = QtWidgets.QPushButton("Apply to Max")
        self.btn_undo_levels.clicked.connect(self._apply_undo_levels)
        ulrow = QtWidgets.QHBoxLayout(); ulrow.addWidget(self.undo_levels); ulrow.addWidget(self.btn_undo_levels); ulrow.addStretch(1)
        form.addRow("Listener port", self.port)
        form.addRow("Bind address", self.host)
        form.addRow("Shared key", keyrow)
        form.addRow("", self.key_enabled)
        form.addRow("", self.autostart)
        form.addRow("", self.ai_undo)
        form.addRow("Pump interval", self.pump)
        form.addRow("Call timeout", self.timeout)
        form.addRow("MCP HTTP bind address", self.http_host)
        form.addRow("MCP HTTP port", self.http_port)
        form.addRow("Scene checkpoints", self.cp_mode)
        form.addRow("Checkpoints to keep", self.cp_count)
        form.addRow("Checkpoints max size", self.cp_mb)
        form.addRow("Max undo levels (scene)", ulrow)
        box = QtWidgets.QGroupBox("Settings  (saved to %s)" % self.model.path)
        box.setLayout(form)
        lay.addWidget(box)
        self.env_lbl = QtWidgets.QLabel("")
        self.env_lbl.setWordWrap(True)
        self.env_lbl.setStyleSheet("color: #d08020")
        lay.addWidget(self.env_lbl)
        row2 = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("Save")
        self.btn_save_restart = QtWidgets.QPushButton("Save + Restart")
        self.btn_reload = QtWidgets.QPushButton("Reload")
        row2.addWidget(self.btn_save); row2.addWidget(self.btn_save_restart); row2.addWidget(self.btn_reload)
        row2.addStretch(1)
        lay.addLayout(row2)
        self.btn_save.clicked.connect(self._save)
        self.btn_save_restart.clicked.connect(lambda: self._save() and self._guard(self.model.restart))
        self.btn_reload.clicked.connect(self._fill)

        lbox = QtWidgets.QGroupBox("Startup loader")
        ll = QtWidgets.QHBoxLayout(lbox)
        self.loader_lbl = QtWidgets.QLabel(""); self.loader_lbl.setWordWrap(True)
        self.btn_loader_install = QtWidgets.QPushButton("Install loader")
        self.btn_loader_remove = QtWidgets.QPushButton("Remove loader")
        ll.addWidget(self.loader_lbl, 1); ll.addWidget(self.btn_loader_install); ll.addWidget(self.btn_loader_remove)
        lay.addWidget(lbox)
        self.btn_loader_install.clicked.connect(self._install_loader)
        self.btn_loader_remove.clicked.connect(self._remove_loader)

        sbox = QtWidgets.QGroupBox("Client configuration (generated from the values above)")
        sl = QtWidgets.QVBoxLayout(sbox)
        srow = QtWidgets.QHBoxLayout()
        self.snippet_combo = QtWidgets.QComboBox()
        self.btn_copy = QtWidgets.QPushButton("Copy")
        self.python_exe = QtWidgets.QLineEdit("python")
        self.python_exe.setPlaceholderText("python executable for the MCP side")
        srow.addWidget(self.snippet_combo, 1); srow.addWidget(QtWidgets.QLabel("python:")); srow.addWidget(self.python_exe); srow.addWidget(self.btn_copy)
        sl.addLayout(srow)
        self.snippet_txt = QtWidgets.QPlainTextEdit(); self.snippet_txt.setReadOnly(True)
        self.snippet_txt.setFont(QtGui.QFont("Consolas")); self.snippet_txt.setMinimumHeight(120)
        sl.addWidget(self.snippet_txt)
        lay.addWidget(sbox)
        self.snippet_combo.currentIndexChanged.connect(self._show_snippet)
        self.python_exe.textChanged.connect(self._update_snippets)
        for w in (self.port, self.http_port):
            w.valueChanged.connect(self._update_snippets)
        for w in (self.host, self.key, self.http_host):
            w.textChanged.connect(self._update_snippets)
        self.key_enabled.toggled.connect(self._update_snippets)
        self.btn_copy.clicked.connect(self._copy_snippet)

    def values(self):
        return {
            "port": self.port.value(), "host": self.host.text().strip(),
            "key": self.key.text().strip(), "key_enabled": self.key_enabled.isChecked(),
            "autostart": self.autostart.isChecked(), "pump_interval_ms": self.pump.value(),
            "timeout": self.timeout.value(), "http_host": self.http_host.text().strip(),
            "http_port": self.http_port.value(), "checkpoint_mode": self.cp_mode.currentText(),
            "checkpoint_max_count": self.cp_count.value(), "checkpoint_max_mb": self.cp_mb.value(),
            "history_max_mb": self.model.values.get("history_max_mb", 5),
            "ai_undo_enabled": self.ai_undo.isChecked(),
        }

    def _fill(self):
        v = self.model.load()
        self.port.setValue(int(v["port"])); self.host.setText(v["host"])
        self.key.setText(v["key"]); self.key_enabled.setChecked(bool(v["key_enabled"]))
        self.autostart.setChecked(bool(v["autostart"])); self.ai_undo.setChecked(bool(v["ai_undo_enabled"]))
        self.pump.setValue(int(v["pump_interval_ms"]))
        self.timeout.setValue(float(v["timeout"])); self.http_host.setText(v["http_host"])
        self.http_port.setValue(int(v["http_port"]))
        self.cp_mode.setCurrentText(str(v["checkpoint_mode"]))
        self.cp_count.setValue(int(v["checkpoint_max_count"])); self.cp_mb.setValue(int(v["checkpoint_max_mb"]))
        try:
            self.undo_levels.setValue(int(_listener().h_undo_info({})["max_levels"]))
        except Exception:  # noqa: BLE001
            pass
        env = self.model.env_overrides()
        self.env_lbl.setText(("Environment overrides in effect (they win over this file): " +
                              ", ".join("%s=%s" % kv for kv in env.items())) if env else "")
        self._update_snippets()
        self._refresh_loader()

    def _save(self):
        try:
            p = self.model.save(self.values())
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "max-mcp", "Not saved:\n" + str(exc))
            return False
        self.status_lbl.setText("saved: %s" % p)
        self._update_snippets()
        return True

    def _guard(self, fn):
        try:
            fn()
            return True
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "max-mcp", str(exc))
            return False

    def _apply_undo_levels(self):
        self._guard(lambda: _listener().set_undo_levels(self.undo_levels.value()))

    def _refresh_loader(self):
        inst = self.model.loader_installed()
        self.loader_lbl.setText("%s: %s" % ("INSTALLED" if inst else "not installed", self.model.loader_path()))
        self.btn_loader_install.setEnabled(not inst)
        self.btn_loader_remove.setEnabled(inst)

    def _install_loader(self):
        if QtWidgets.QMessageBox.question(self, "max-mcp", "Write the startup loader to\n%s ?" % self.model.loader_path()
                                          ) == QtWidgets.QMessageBox.StandardButton.Yes:
            self._guard(self.model.install_loader)
            self._refresh_loader()

    def _remove_loader(self):
        if QtWidgets.QMessageBox.question(self, "max-mcp", "Delete the startup loader\n%s ?" % self.model.loader_path()
                                          ) == QtWidgets.QMessageBox.StandardButton.Yes:
            self._guard(self.model.remove_loader)
            self._refresh_loader()

    def _update_snippets(self):
        try:
            self._snips = self.model.snippets(self.values(), python_exe=self.python_exe.text() or "python")
        except Exception as exc:  # noqa: BLE001
            self._snips = {"error": str(exc)}
        cur = self.snippet_combo.currentText()
        self.snippet_combo.blockSignals(True)
        self.snippet_combo.clear()
        self.snippet_combo.addItems(list(self._snips))
        if cur in self._snips:
            self.snippet_combo.setCurrentText(cur)
        self.snippet_combo.blockSignals(False)
        self._show_snippet()

    def _show_snippet(self):
        self.snippet_txt.setPlainText(self._snips.get(self.snippet_combo.currentText(), ""))

    def _copy_snippet(self):
        QtWidgets.QApplication.clipboard().setText(self.snippet_txt.toPlainText())
        self.status_lbl.setText("copied to clipboard")


# ==========================================================================
# History tab
# ==========================================================================

COLS = ("#", "time", "client", "tool", "summary", "ms", "result", "objects", "undo")


class HistoryTab(QtWidgets.QWidget):
    def __init__(self, status_lbl: QtWidgets.QLabel, parent=None):
        super().__init__(parent)
        self.status_lbl = status_lbl
        self.entries = []
        self._build()

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        self.limit = QtWidgets.QSpinBox(); self.limit.setRange(10, 5000); self.limit.setValue(200)
        self.auto = QtWidgets.QCheckBox("auto-refresh"); self.auto.setChecked(True)
        self.undo_lbl = QtWidgets.QLabel("")
        top.addWidget(self.btn_refresh); top.addWidget(QtWidgets.QLabel("rows:")); top.addWidget(self.limit)
        top.addWidget(self.auto); top.addStretch(1); top.addWidget(self.undo_lbl)
        lay.addLayout(top)
        self.btn_refresh.clicked.connect(self.refresh)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.table = QtWidgets.QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._show_detail)
        split.addWidget(self.table)
        self.detail = QtWidgets.QPlainTextEdit(); self.detail.setReadOnly(True)
        self.detail.setFont(QtGui.QFont("Consolas"))
        split.addWidget(self.detail)
        split.setSizes([360, 220])
        lay.addWidget(split, 1)

        row = QtWidgets.QHBoxLayout()
        self.btn_copy = QtWidgets.QPushButton("Copy code")
        self.btn_rerun = QtWidgets.QPushButton("Re-run")
        self.btn_revert = QtWidgets.QPushButton("Revert to here (undo)")
        self.btn_restore = QtWidgets.QPushButton("Restore checkpoint")
        self.btn_open = QtWidgets.QPushButton("Open history folder")
        for b in (self.btn_copy, self.btn_rerun, self.btn_revert, self.btn_restore, self.btn_open):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_rerun.clicked.connect(self._rerun)
        self.btn_revert.clicked.connect(self._revert)
        self.btn_restore.clicked.connect(self._restore)
        self.btn_open.clicked.connect(self._open_folder)

    # --- data -----------------------------------------------------------------
    def refresh(self):
        try:
            ls = _listener()
            h = ls.h_history({"limit": self.limit.value(), "full": True})
            info = ls.h_undo_info({})
        except Exception as exc:  # noqa: BLE001
            self.undo_lbl.setText("history unavailable: %s" % exc)
            return
        self.undo_lbl.setText("undo stack: %d / %d levels, top: %s" % (
            info["current_levels"], info["max_levels"],
            ", ".join(info["undo_names"][:3]) or "(empty)"))
        self.entries = list(reversed(h["entries"]))  # newest first in the table
        sel = self._selected_id()
        self.table.setRowCount(len(self.entries))
        for r, e in enumerate(self.entries):
            st = e.get("undo_state")
            undo_txt = {"on_stack": "on stack", "undone": "undone", "gone": "gone"}.get(st, "-")
            if e.get("tool") in ("undo", "revert"):
                undo_txt = "undid %s" % ",".join("#%s" % i for i in e.get("undone_ids") or [])
            d = e.get("diff") or {}
            objs = ""
            if d:
                objs = "+%d -%d" % (len(d.get("added") or []), len(d.get("removed") or []))
                if d.get("scene_changed"):
                    objs += " *"
            vals = (str(e["id"]), e.get("time", "")[11:], e.get("client", ""), e.get("tool", ""),
                    e.get("summary", ""), str(e.get("duration_ms", "")),
                    "ok" if e.get("ok") else "ERROR", objs, undo_txt)
            grey = bool(e.get("undo_label")) and st != "on_stack"
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if grey:
                    it.setForeground(GREY)
                if not e.get("ok"):
                    it.setForeground(QtGui.QColor(208, 64, 64))
                self.table.setItem(r, c, it)
            if e.get("checkpoint"):
                self.table.item(r, 0).setToolTip("checkpoint: %s" % e["checkpoint"])
        self.table.resizeColumnsToContents()
        if sel is not None:
            self._select_id(sel)

    def _selected_id(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.entries[rows[0].row()]["id"] if rows and rows[0].row() < len(self.entries) else None

    def _select_id(self, entry_id):
        for r, e in enumerate(self.entries):
            if e["id"] == entry_id:
                self.table.selectRow(r)
                return

    def _entry(self):
        eid = self._selected_id()
        return next((e for e in self.entries if e["id"] == eid), None)

    def _show_detail(self):
        e = self._entry()
        if not e:
            self.detail.setPlainText("")
            return
        args = e.get("args") or {}
        code = args.get("code") if isinstance(args, dict) else None
        lines = ["#%s  %s  %s  %s  [%s, %s ms]" % (e["id"], e.get("time"), e.get("client"), e.get("tool"),
                                                   "ok" if e.get("ok") else "ERROR", e.get("duration_ms"))]
        if e.get("undo_label"):
            st = _listener().history_status(e["id"]) if e.get("undo_state") == "on_stack" else None
            lines.append("undo record: %s  (%s%s)" % (e["undo_label"], e.get("undo_state"),
                         ", %d step(s) back, user steps in between: %s" % (st["steps"], st["foreign_steps"] or "none") if st else ""))
        if e.get("checkpoint"):
            lines.append("checkpoint: %s" % e["checkpoint"])
        d = e.get("diff") or {}
        if d:
            lines.append("added: %s | removed: %s | selection %s -> %s | scene changed: %s" % (
                d.get("added"), d.get("removed"), d.get("selection_before"), d.get("selection_after"), d.get("scene_changed")))
        lines.append("")
        lines.append("--- code / args ---")
        lines.append(code if code else json.dumps(args, indent=1, ensure_ascii=False))
        lines.append("")
        lines.append("--- output ---" if e.get("ok") else "--- error ---")
        lines.append(str(e.get("output") if e.get("ok") else e.get("error")))
        self.detail.setPlainText("\n".join(lines))
        self.btn_revert.setEnabled(e.get("undo_state") == "on_stack")
        self.btn_restore.setEnabled(bool(e.get("checkpoint")))
        self.btn_rerun.setEnabled(e.get("tool") not in ("undo", "revert", "restore_checkpoint", "history", "undo_info"))

    # --- actions ------------------------------------------------------------
    def _guard(self, fn):
        try:
            res = fn()
            self.status_lbl.setText(str(res)[:200])
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "max-mcp", str(exc))
        self.refresh()

    def _copy(self):
        e = self._entry()
        if e:
            args = e.get("args") or {}
            QtWidgets.QApplication.clipboard().setText(args.get("code") or json.dumps(args, indent=1, ensure_ascii=False))
            self.status_lbl.setText("copied")

    def _rerun(self):
        e = self._entry()
        if e:
            self._guard(lambda: _listener().rerun(e["id"]))

    def _revert(self):
        e = self._entry()
        if not e:
            return
        st = _listener().history_status(e["id"])
        if not st["revertable"]:
            QtWidgets.QMessageBox.information(self, "max-mcp", "Cannot revert: %s" % st["reason"])
            return
        msg = "Undo %d step(s) back to before history #%d (%s)?" % (st["steps"], e["id"], e.get("summary", ""))
        if st["foreign_steps"]:
            msg += "\n\nWARNING: these steps were NOT made by the bridge and will be undone too:\n  - " + \
                   "\n  - ".join(st["foreign_steps"])
        if QtWidgets.QMessageBox.question(self, "max-mcp revert", msg) == QtWidgets.QMessageBox.StandardButton.Yes:
            self._guard(lambda: _listener().revert_to(e["id"]))

    def _restore(self):
        e = self._entry()
        if not e or not e.get("checkpoint"):
            return
        msg = ("Load the scene copy taken BEFORE history #%d?\n%s\n\nEverything done in the scene since then "
               "(by you or by the AI) will be lost and the undo stack is cleared. The scene keeps its current "
               "file name and path; nothing is written to your file until you save." % (e["id"], e["checkpoint"]))
        if QtWidgets.QMessageBox.question(self, "max-mcp restore checkpoint", msg) == QtWidgets.QMessageBox.StandardButton.Yes:
            self._guard(lambda: _listener().restore_checkpoint(e["checkpoint"]))

    def _open_folder(self):
        from max_mcp import config as mcfg
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(mcfg.history_dir())))


# ==========================================================================
# window
# ==========================================================================

class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, model: max_mcp_settings.SettingsModel, parent=None):
        super().__init__(parent)
        self.model = model
        self.setWindowTitle("max-mcp")
        self.setMinimumSize(760, 640)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.Tool)
        lay = QtWidgets.QVBoxLayout(self)
        self.status_lbl = QtWidgets.QLabel("..."); self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("font-weight: bold")
        lay.addWidget(self.status_lbl)
        self.tabs = QtWidgets.QTabWidget()
        self.settings_tab = SettingsTab(model, self.status_lbl)
        self.history_tab = HistoryTab(self.status_lbl)
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.addTab(self.history_tab, "History")
        lay.addWidget(self.tabs, 1)
        self._timer = QtCore.QTimer(self); self._timer.setInterval(1500)
        self._timer.timeout.connect(self._tick); self._timer.start()
        self._tick()

    def _tick(self):
        txt = self.model.status_text()
        running = txt.startswith("LISTENING")
        self.status_lbl.setText(txt)
        self.status_lbl.setStyleSheet("font-weight: bold; color: %s" % ("#3cb043" if running else "#d04040"))
        self.settings_tab.btn_start.setEnabled(not running)
        self.settings_tab.btn_stop.setEnabled(running)
        if self.tabs.currentWidget() is self.history_tab and self.history_tab.auto.isChecked():
            self.history_tab.refresh()


def show():
    """Open (or raise) the window inside Max."""
    global _dialog
    if _dialog is None:
        _dialog = SettingsDialog(max_mcp_settings.SettingsModel(), parent=_max_main_window())
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()
    return _dialog
