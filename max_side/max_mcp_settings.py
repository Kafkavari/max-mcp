"""
Settings logic for the in-Max settings window -- NO Qt here, so it can be
unit-tested without 3ds Max. The window (max_mcp_ui.py) only calls these.

Everything that touches disk outside the config file (the startup loader) is
an explicit method the UI calls from a button; nothing runs implicitly.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from max_mcp import config as mcfg  # noqa: E402
from max_mcp import paths as mpaths  # noqa: E402

FIELDS = ("port", "host", "key", "key_enabled", "autostart", "pump_interval_ms",
          "timeout", "http_host", "http_port")


def _listener():
    import max_mcp_listener
    return max_mcp_listener


class SettingsModel:
    """Holds the editable copy of the config and talks to the listener."""

    def __init__(self, listener=None, path=None, max_version=2025):
        self._ls = listener  # injectable for tests
        self.path = path or mcfg.config_path()
        self.max_version = max_version
        self.values = {}
        self.load()

    # --- config -----------------------------------------------------------
    def load(self):
        file_cfg = mcfg.load_file(self.path)
        self.values = {k: mcfg._coerce(k, file_cfg.get(k)) for k in mcfg.DEFAULTS}
        return self.values

    def validate(self, values):
        errors = []
        try:
            p = int(values.get("port"))
            if not 1 <= p <= 65535:
                errors.append("port must be 1-65535")
        except (TypeError, ValueError):
            errors.append("port must be a number")
        try:
            hp = int(values.get("http_port"))
            if not 1 <= hp <= 65535:
                errors.append("http_port must be 1-65535")
        except (TypeError, ValueError):
            errors.append("http_port must be a number")
        try:
            if int(values.get("pump_interval_ms")) < 5:
                errors.append("pump interval must be >= 5 ms")
        except (TypeError, ValueError):
            errors.append("pump interval must be a number")
        if not str(values.get("host", "")).strip():
            errors.append("host must not be empty")
        if values.get("checkpoint_mode", "off") not in mcfg.CHECKPOINT_MODES:
            errors.append("checkpoint_mode must be one of %s" % ", ".join(mcfg.CHECKPOINT_MODES))
        if values.get("key_enabled") and not str(values.get("key", "")).strip():
            errors.append("key is enabled but empty (use Generate)")
        if not mcfg.is_loopback(str(values.get("http_host", ""))) and not (
                values.get("key_enabled") and values.get("key")):
            errors.append("HTTP bound to a non-loopback address requires the key")
        return errors

    def save(self, values):
        errors = self.validate(values)
        if errors:
            raise ValueError("; ".join(errors))
        self.values = {k: mcfg._coerce(k, values.get(k)) for k in mcfg.DEFAULTS}
        return mcfg.save_file(self.values, self.path)

    def generate_key(self):
        return mcfg.generate_key()

    def env_overrides(self):
        """Keys currently forced by environment variables (shown as a warning)."""
        return {v: os.environ[v] for v in mcfg.ENV_NAMES.values() if os.environ.get(v)}

    # --- listener ---------------------------------------------------------
    def listener(self):
        if self._ls is None:
            self._ls = _listener()
        return self._ls

    def status(self):
        try:
            return self.listener().status()
        except Exception as exc:  # noqa: BLE001
            return {"running": False, "last_error": str(exc)}

    def start(self):
        cfg = mcfg.resolve(file_cfg=self.values)  # env still wins, like at startup
        return self.listener().start(cfg)

    def stop(self):
        return self.listener().stop()

    def restart(self):
        cfg = mcfg.resolve(file_cfg=self.values)
        return self.listener().restart(cfg)

    def status_text(self):
        s = self.status()
        if not s.get("running"):
            txt = "STOPPED"
            if s.get("last_error"):
                txt += " - " + str(s["last_error"])
            return txt
        st = s.get("stats") or {}
        return "LISTENING on %s:%s | requests %s, handled %s, errors %s, timeouts %s | pump %s | up %ss" % (
            s.get("host"), s.get("port"), st.get("requests", 0), st.get("handled", 0),
            st.get("errors", 0), st.get("timeouts", 0), s.get("pump_mode"), s.get("uptime_s"))

    # --- startup loader (explicit user action only) -------------------------
    def loader_path(self):
        return mpaths.loader_path(self.max_version)

    def loader_installed(self):
        return mpaths.loader_installed(self.max_version)

    def install_loader(self):
        return mpaths.install_loader(self.max_version)

    def remove_loader(self):
        return mpaths.remove_loader(self.max_version)

    # --- client snippets ----------------------------------------------------
    def snippets(self, values=None, python_exe="python"):
        cfg = mcfg.resolve(file_cfg=values or self.values, env={})
        return mcfg.client_snippets(cfg, python_exe=python_exe)
