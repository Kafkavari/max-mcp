"""
TCP client for the in-Max listener (max_side/max_mcp_listener.py).

Protocol: one JSON object per line, one JSON reply per line. A fresh
connection is opened per call so a Max restart never leaves a stale socket.

Settings come from max_mcp.config (env > argument > config file > default):
host, port, key/key_enabled and timeout. ``configure()`` lets the server pass
command-line overrides.
"""

from __future__ import annotations

import itertools
import json
import socket
from typing import Any

from . import config as mcfg

_ids = itertools.count(1)
_cfg: dict[str, Any] | None = None


class MaxError(Exception):
    """Raised when Max is unreachable or the listener reports an error."""


def configure(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """(Re)load settings; overrides act as explicit arguments (env still wins)."""
    global _cfg
    _cfg = mcfg.resolve(args=overrides)
    return _cfg


def settings() -> dict[str, Any]:
    return _cfg if _cfg is not None else configure()


def call(cmd: str, args: dict[str, Any] | None = None, timeout: float | None = None,
         host: str | None = None, port: int | None = None, key: str | None = None,
         client_name: str = "") -> Any:
    """Send one command to the listener and return its ``result``.
    client_name (the MCP clientInfo name) is recorded in the Max-side history."""
    cfg = settings()
    timeout = float(cfg["timeout"]) if timeout is None else float(timeout)
    host = cfg["host"] if host is None else host
    port = int(cfg["port"]) if port is None else int(port)
    key = mcfg.effective_key(cfg) if key is None else key
    req = {"id": next(_ids), "cmd": cmd, "args": args or {}, "timeout": timeout,
           "client": client_name or ""}
    if key:
        req["key"] = key
    payload = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.create_connection((connect_host, port), timeout=5) as s:
            # the listener itself gives up after `timeout`; wait a little longer
            s.settimeout(timeout + 5)
            s.sendall(payload)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    raise MaxError("connection closed before a reply arrived")
                buf += chunk
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise MaxError(
            "cannot reach the 3ds Max listener at %s:%d (%s). Is 3ds Max running "
            "with the max-mcp listener started?" % (connect_host, port, exc)
        ) from exc
    line = buf.split(b"\n", 1)[0].decode("utf-8", "replace")
    try:
        reply = json.loads(line)
    except json.JSONDecodeError as exc:
        raise MaxError("invalid reply from listener: %s" % line[:200]) from exc
    if not reply.get("ok"):
        raise MaxError(reply.get("error") or "unknown listener error")
    return reply.get("result")
