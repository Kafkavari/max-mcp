"""
Shared configuration for both halves of max-mcp (standard library only, so the
in-Max listener can import it too).

File: %LOCALAPPDATA%/max-mcp/config.json  (override with MAX_MCP_CONFIG)

Precedence, highest first:  environment variable > explicit argument > config
file > built-in default.  (``resolve()`` implements exactly this order.)

Keys and their environment variables:
    port              MAX_MCP_PORT         listener TCP port (default 30025)
    host              MAX_MCP_HOST         listener bind address (default 127.0.0.1)
    key               MAX_MCP_KEY          shared key; setting the env var also enables it
    key_enabled       MAX_MCP_KEY_ENABLED  require the key (default false)
    autostart         MAX_MCP_AUTOSTART    start the listener when Max starts (default true)
    pump_interval_ms  MAX_MCP_PUMP_MS      main-thread pump interval (default 50)
    timeout           MAX_MCP_TIMEOUT      seconds a call may wait for Max (default 60)
    http_host         MAX_MCP_HTTP_HOST    MCP streamable-HTTP bind address (default 127.0.0.1)
    http_port         MAX_MCP_HTTP_PORT    MCP streamable-HTTP port (default 30026)
    checkpoint_mode   MAX_MCP_CHECKPOINT_MODE   off | changing | all (default off)
    checkpoint_max_count / checkpoint_max_mb     how many / how big the checkpoints may get
    history_max_mb    MAX_MCP_HISTORY_MAX_MB    history.jsonl size before rotation (default 5)
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "port": 30025,
    "host": "127.0.0.1",
    "key": "",
    "key_enabled": False,
    "autostart": True,
    "pump_interval_ms": 50,
    "timeout": 60.0,
    "http_host": "127.0.0.1",
    "http_port": 30026,
    # history / checkpoints (see max_side/max_mcp_history.py)
    "checkpoint_mode": "off",      # off | changing | all
    "checkpoint_max_count": 10,
    "checkpoint_max_mb": 2048,
    "history_max_mb": 5,
    "ai_undo_enabled": True,       # may the max_undo tool undo the bridge's own steps
}

CHECKPOINT_MODES = ("off", "changing", "all")

ENV_NAMES: dict[str, str] = {
    "checkpoint_mode": "MAX_MCP_CHECKPOINT_MODE",
    "checkpoint_max_count": "MAX_MCP_CHECKPOINT_MAX_COUNT",
    "checkpoint_max_mb": "MAX_MCP_CHECKPOINT_MAX_MB",
    "history_max_mb": "MAX_MCP_HISTORY_MAX_MB",
    "ai_undo_enabled": "MAX_MCP_AI_UNDO",
    "port": "MAX_MCP_PORT",
    "host": "MAX_MCP_HOST",
    "key": "MAX_MCP_KEY",
    "key_enabled": "MAX_MCP_KEY_ENABLED",
    "autostart": "MAX_MCP_AUTOSTART",
    "pump_interval_ms": "MAX_MCP_PUMP_MS",
    "timeout": "MAX_MCP_TIMEOUT",
    "http_host": "MAX_MCP_HTTP_HOST",
    "http_port": "MAX_MCP_HTTP_PORT",
}

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    p = env.get("MAX_MCP_CONFIG")
    if p:
        return Path(p)
    base = env.get("LOCALAPPDATA") or env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "max-mcp" / "config.json"


def _coerce(key: str, value: Any) -> Any:
    """Cast a raw (string/json) value to the type of the default."""
    default = DEFAULTS[key]
    if value is None:
        return default
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return str(value)


def load_file(path: Path | None = None) -> dict[str, Any]:
    """Read the config file; unknown keys are ignored, a missing or broken
    file yields {} (never raises)."""
    path = config_path() if path is None else Path(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in DEFAULTS}


def save_file(cfg: dict[str, Any], path: Path | None = None) -> Path:
    path = config_path() if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: _coerce(k, cfg.get(k)) for k in DEFAULTS if k in cfg}
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2)
    os.replace(tmp, path)
    return path


def resolve(args: dict[str, Any] | None = None, env: dict[str, str] | None = None,
            file_cfg: dict[str, Any] | None = None, path: Path | None = None) -> dict[str, Any]:
    """Merge: env > args > file > defaults. ``args`` values that are None are
    treated as "not given". Returns a complete, typed dict."""
    env = os.environ if env is None else env
    file_cfg = load_file(path) if file_cfg is None else file_cfg
    args = args or {}
    out: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        value: Any = default
        if key in file_cfg:
            value = file_cfg[key]
        if args.get(key) is not None:
            value = args[key]
        env_val = env.get(ENV_NAMES[key])
        if env_val not in (None, ""):
            value = env_val
        out[key] = _coerce(key, value)
    # a key given through the environment is meant to be used
    if env.get("MAX_MCP_KEY"):
        out["key_enabled"] = True
    if out["checkpoint_mode"] not in CHECKPOINT_MODES:
        out["checkpoint_mode"] = DEFAULTS["checkpoint_mode"]
    return out


def history_dir(cfg: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> Path:
    """Folder for history.jsonl and checkpoints/ (next to config.json)."""
    return config_path(env).parent


def effective_key(cfg: dict[str, Any]) -> str:
    """The key the listener enforces / the client sends; '' when disabled."""
    return cfg["key"] if cfg.get("key_enabled") and cfg.get("key") else ""


def generate_key() -> str:
    return secrets.token_urlsafe(24)


def is_loopback(host: str) -> bool:
    return host.strip().lower() in LOOPBACK


# --------------------------------------------------------------------------
# client configuration snippets (shown in the settings window and README)
# --------------------------------------------------------------------------

def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2)


def client_snippets(cfg: dict[str, Any], python_exe: str = "python",
                    http_url: str | None = None) -> dict[str, str]:
    """Ready-to-paste configuration for common MCP clients. The stdio form runs
    ``python -m max_mcp.server``; the HTTP form points at the streamable-HTTP
    endpoint. Non-default port/key are passed through environment variables so
    the MCP side matches the listener."""
    env: dict[str, str] = {}
    if int(cfg["port"]) != DEFAULTS["port"]:
        env["MAX_MCP_PORT"] = str(cfg["port"])
    if cfg["host"] != DEFAULTS["host"]:
        env["MAX_MCP_HOST"] = str(cfg["host"])
    key = effective_key(cfg)
    if key:
        env["MAX_MCP_KEY"] = key
    stdio_entry: dict[str, Any] = {"command": python_exe, "args": ["-m", "max_mcp.server"]}
    if env:
        stdio_entry["env"] = env
    url = http_url or "http://%s:%s/mcp" % (cfg["http_host"], cfg["http_port"])
    http_entry: dict[str, Any] = {"url": url}
    if key:
        http_entry["headers"] = {"Authorization": "Bearer " + key}

    env_flags = "".join(" -e %s=%s" % (k, v) for k, v in env.items())
    out = {
        "Claude Code (stdio)":
            "claude mcp add -s user%s max -- %s -m max_mcp.server" % (env_flags, python_exe),
        "Claude Code (HTTP)":
            "claude mcp add -s user --transport http max %s%s" % (
                url, (" --header \"Authorization: Bearer %s\"" % key) if key else ""),
        "Claude Desktop (claude_desktop_config.json)": _json({"mcpServers": {"max": stdio_entry}}),
        "Cursor (.cursor/mcp.json)": _json({"mcpServers": {"max": stdio_entry}}),
        "Cursor (HTTP)": _json({"mcpServers": {"max": http_entry}}),
        "Gemini CLI (~/.gemini/settings.json)": _json({"mcpServers": {"max": stdio_entry}}),
        "Gemini CLI (HTTP)": _json({"mcpServers": {"max": {"httpUrl": url, **({"headers": http_entry["headers"]} if key else {})}}}),
        "Generic HTTP client": "URL: %s\n%s" % (
            url, ("Header: Authorization: Bearer %s" % key) if key else "No auth header (key disabled)"),
        "HTTP server command":
            "%s -m max_mcp.server --transport http --http-host %s --http-port %s" % (
                python_exe, cfg["http_host"], cfg["http_port"]),
    }
    return out
