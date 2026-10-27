"""Config file, precedence (env > argument > file > default), snippets and the
UI-free settings model. No 3ds Max needed."""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "max_side"))

from max_mcp import config as mcfg  # noqa: E402
from max_mcp import paths as mpaths  # noqa: E402


def test_defaults_when_nothing_set(tmp_path):
    cfg = mcfg.resolve(env={}, path=tmp_path / "none.json")
    assert cfg == mcfg.DEFAULTS


def test_file_overrides_default(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"port": 40000, "autostart": False, "junk": 1}), encoding="utf-8")
    cfg = mcfg.resolve(env={}, path=p)
    assert cfg["port"] == 40000 and cfg["autostart"] is False
    assert "junk" not in cfg


def test_arg_overrides_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"port": 40000}), encoding="utf-8")
    cfg = mcfg.resolve(args={"port": 41000, "host": None}, env={}, path=p)
    assert cfg["port"] == 41000 and cfg["host"] == "127.0.0.1"  # None = not given


def test_env_overrides_arg_and_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"port": 40000, "pump_interval_ms": 10}), encoding="utf-8")
    env = {"MAX_MCP_PORT": "42000", "MAX_MCP_PUMP_MS": "99", "MAX_MCP_AUTOSTART": "false"}
    cfg = mcfg.resolve(args={"port": 41000}, env=env, path=p)
    assert cfg["port"] == 42000 and cfg["pump_interval_ms"] == 99 and cfg["autostart"] is False
    assert isinstance(cfg["port"], int)


def test_env_key_enables_key(tmp_path):
    cfg = mcfg.resolve(env={"MAX_MCP_KEY": "abc"}, path=tmp_path / "x.json")
    assert cfg["key_enabled"] is True and mcfg.effective_key(cfg) == "abc"
    cfg = mcfg.resolve(file_cfg={"key": "abc", "key_enabled": False}, env={})
    assert mcfg.effective_key(cfg) == ""


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "sub" / "config.json"
    mcfg.save_file({"port": "30999", "key_enabled": "true", "key": "k", "bogus": 1}, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data == {"port": 30999, "key_enabled": True, "key": "k"}
    assert mcfg.load_file(p) == data
    p.write_text("{broken", encoding="utf-8")
    assert mcfg.load_file(p) == {}


def test_config_path_env_override():
    assert str(mcfg.config_path({"MAX_MCP_CONFIG": "/x/y.json"})).endswith("y.json")
    p = mcfg.config_path({"LOCALAPPDATA": "C:/Users/u/AppData/Local"})
    assert p.as_posix().endswith("max-mcp/config.json")


def test_generate_key_and_loopback():
    k1, k2 = mcfg.generate_key(), mcfg.generate_key()
    assert k1 != k2 and len(k1) >= 24
    assert mcfg.is_loopback("127.0.0.1") and mcfg.is_loopback("LocalHost")
    assert not mcfg.is_loopback("0.0.0.0")


def test_client_snippets_follow_port_and_key():
    cfg = dict(mcfg.DEFAULTS, port=31000, key="sec", key_enabled=True, http_port=31001)
    s = mcfg.client_snippets(cfg, python_exe="py.exe")
    assert "MAX_MCP_PORT=31000" in s["Claude Code (stdio)"] and "py.exe" in s["Claude Code (stdio)"]
    desk = json.loads(s["Claude Desktop (claude_desktop_config.json)"])
    assert desk["mcpServers"]["max"]["env"] == {"MAX_MCP_PORT": "31000", "MAX_MCP_KEY": "sec"}
    assert "http://127.0.0.1:31001/mcp" in s["Generic HTTP client"]
    assert "Bearer sec" in s["Claude Code (HTTP)"]
    # defaults: no env block at all
    s2 = mcfg.client_snippets(dict(mcfg.DEFAULTS))
    assert "env" not in json.loads(s2["Cursor (.cursor/mcp.json)"])["mcpServers"]["max"]


def test_loader_text_and_install_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_MCP_MAX_STARTUP", str(tmp_path / "startup"))
    assert not mpaths.loader_installed()
    target = mpaths.install_loader()
    assert target == tmp_path / "startup" / "max_mcp_loader.ms" and target.is_file()
    txt = target.read_text(encoding="utf-8")
    assert "MaxMcp_StartupLoad = true" in txt and "max_mcp_start.ms" in txt
    assert mpaths.loader_installed()
    assert mpaths.remove_loader() is True and not mpaths.loader_installed()
    assert mpaths.remove_loader() is False


# --------------------------------------------------------------------------
# settings model (UI-free)
# --------------------------------------------------------------------------

class _FakeListener:
    def __init__(self):
        self.calls = []
        self.running = False

    def status(self):
        return {"running": self.running, "host": "127.0.0.1", "port": 30025,
                "stats": {"requests": 3, "handled": 2, "errors": 1, "timeouts": 0},
                "pump_mode": "qt", "uptime_s": 5.0, "last_error": None}

    def start(self, cfg):
        self.calls.append(("start", cfg["port"])); self.running = True

    def stop(self):
        self.calls.append(("stop",)); self.running = False

    def restart(self, cfg):
        self.calls.append(("restart", cfg["port"])); self.running = True


def test_settings_model(tmp_path, monkeypatch):
    import max_mcp_settings
    monkeypatch.delenv("MAX_MCP_PORT", raising=False)
    fake = _FakeListener()
    m = max_mcp_settings.SettingsModel(listener=fake, path=tmp_path / "config.json")
    assert m.values["port"] == 30025
    assert m.status_text() == "STOPPED"
    with pytest.raises(ValueError, match="port"):
        m.save(dict(m.values, port=0))
    with pytest.raises(ValueError, match="key is enabled but empty"):
        m.save(dict(m.values, key_enabled=True, key=""))
    with pytest.raises(ValueError, match="non-loopback"):
        m.save(dict(m.values, http_host="0.0.0.0"))
    m.save(dict(m.values, port=30500, key_enabled=True, key=m.generate_key(), http_host="0.0.0.0"))
    assert json.loads((tmp_path / "config.json").read_text())["port"] == 30500
    m.start(); m.restart(); m.stop()
    assert fake.calls == [("start", 30500), ("restart", 30500), ("stop",)]
    fake.running = True
    assert m.status_text().startswith("LISTENING on 127.0.0.1:30025 | requests 3")
    snips = m.snippets()
    assert "MAX_MCP_PORT=30500" in snips["Claude Code (stdio)"]
    # env override is reported, and wins when starting
    monkeypatch.setenv("MAX_MCP_PORT", "30777")
    assert m.env_overrides() == {"MAX_MCP_PORT": "30777"}
    m.start()
    assert fake.calls[-1] == ("start", 30777)
