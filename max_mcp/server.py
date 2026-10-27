"""
MCP server exposing a running 3ds Max session through the listener.

    python -m max_mcp.server                      # stdio (default)
    python -m max_mcp.server --transport http     # streamable HTTP on http://127.0.0.1:30026/mcp
    python -m max_mcp.server --transport http --http-host 0.0.0.0 --key <secret>

Depends only on the official ``mcp`` SDK (which brings starlette/uvicorn for
HTTP) and the standard library. Every tool forwards to max_mcp.client.call();
nothing here touches Max directly. Settings: env > CLI argument > config file
> default (see max_mcp.config).

Security of the HTTP transport: it binds to 127.0.0.1 unless told otherwise.
Binding to a non-loopback address REQUIRES a key; clients then send
``Authorization: Bearer <key>``. The same key protects the Max listener.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import client
from . import config as mcfg

mcp = FastMCP(
    "max",
    instructions=(
        "Bridge to a live Autodesk 3ds Max session. Commands run on Max's main "
        "thread; long operations block the Max UI. The bridge never saves the "
        "scene. Scene-changing calls are wrapped in an undo record."
    ),
)


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False)


def _client_name() -> str:
    """MCP clientInfo of the current session ('claude-code 1.2.3', 'cursor' ...)."""
    try:
        ci = mcp.get_context().session.client_params.clientInfo
        return ("%s %s" % (ci.name, ci.version or "")).strip()
    except Exception:  # noqa: BLE001  (no request context, e.g. direct calls in tests)
        return ""


def _call(cmd: str, args: dict[str, Any] | None = None, timeout: float | None = None) -> str:
    return _fmt(client.call(cmd, args, timeout=timeout, client_name=_client_name()))


@mcp.tool()
def max_info() -> str:
    """Check the connection and return Max version, open scene path, dirty flag,
    units, object/selection counts, renderer and listener status."""
    return _call("info")


@mcp.tool()
def max_selection(limit: int = 200, detail: bool = False) -> str:
    """List the currently selected objects (name, class, layer, position; with
    detail=True also bbox, transform, parent, material, user properties)."""
    return _call("selection", {"limit": limit, "detail": detail})


@mcp.tool()
def max_query_objects(
    name_pattern: str = "",
    class_name: str = "",
    layer: str = "",
    selected_only: bool = False,
    detail: bool = False,
    limit: int = 100,
) -> str:
    """Find scene objects. name_pattern is a MAXScript wildcard ('Wall_*', '*door*',
    case-insensitive); class_name matches classOf (e.g. 'Editable_Poly', 'Box');
    layer matches the layer name. detail=True adds bbox, transform, material,
    user properties."""
    return _call("query_objects", {
        "name_pattern": name_pattern, "class_name": class_name, "layer": layer,
        "selected_only": selected_only, "detail": detail, "limit": limit,
    })


@mcp.tool()
def max_layers() -> str:
    """List all layers with hidden/frozen/current flags and object counts."""
    return _call("layers")


@mcp.tool()
def max_set_layer_hidden(name: str, hidden: bool = True) -> str:
    """Hide (hidden=True) or show (hidden=False) a layer by name. Undoable."""
    return _call("set_layer_hidden", {"name": name, "hidden": hidden})


@mcp.tool()
def max_viewport_capture(path: str = "", max_size: int = 1024) -> str:
    """Grab the active viewport to a PNG file and return its path and size.
    path defaults to %TEMP%/max-mcp/capture_<timestamp>.png. max_size limits
    the longer edge (0 = keep the viewport resolution)."""
    return _call("viewport_capture", {"path": path, "max_size": max_size})


@mcp.tool()
def max_run_maxscript(code: str, undo: bool = True, timeout: float = 60) -> str:
    """Execute MAXScript inside Max and return the value of the last expression.
    (format/print output goes to Max's Listener window; return values instead.)
    undo=True wraps the code in an undo record."""
    return _call("run_maxscript", {"code": code, "undo": undo}, timeout=timeout)


@mcp.tool()
def max_run_python(code: str, undo: bool = True, timeout: float = 60) -> str:
    """Execute Python (Max's own 3.11 with pymxs) inside Max. `rt` (pymxs.runtime)
    and `pymxs` are predefined. A single expression returns its value; for
    multi-line code assign to `result`. print() output is returned too."""
    return _call("run_python", {"code": code, "undo": undo}, timeout=timeout)


@mcp.tool()
def max_history(limit: int = 30, tool: str = "", full: bool = False) -> str:
    """Read the bridge's command history (time, client, tool, summary, duration,
    ok/error, objects added/removed, selection, undo_state). undo_state is
    'on_stack' (max_undo can target it), 'undone' or 'gone'. full=True
    includes complete args and output."""
    return _call("history", {"limit": limit, "tool": tool, "full": full})


@mcp.tool()
def max_undo(steps: int = 1, to_history_id: int = 0) -> str:
    """Undo the bridge's OWN scene changes: either the last `steps` undo records
    or everything down to and including history entry `to_history_id`. Only
    records labelled 'MCP: <tool> #<id>' are undone; if a user's manual step
    sits in between the call is refused and names it (the user can revert past
    it from the settings window). Fails when the target has left the undo
    stack or when AI undo is disabled in the settings window."""
    args: dict[str, Any] = {"steps": steps}
    if to_history_id:
        args["to_history_id"] = to_history_id
    return _call("undo", args)


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

def build_http_app(key: str):
    """Starlette app for streamable HTTP; with a key, every request must carry
    ``Authorization: Bearer <key>`` (401 otherwise)."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    app = mcp.streamable_http_app()
    if not key:
        return app

    class BearerKey(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            auth = request.headers.get("authorization", "")
            if auth != "Bearer " + key:
                return JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            return await call_next(request)

    app.add_middleware(BearerKey)
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="max-mcp", description="MCP server for a live 3ds Max session")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--port", type=int, help="Max listener port (default from config, 30025)")
    ap.add_argument("--host", help="Max listener host (default 127.0.0.1)")
    ap.add_argument("--key", help="shared key (enables key checking)")
    ap.add_argument("--timeout", type=float, help="seconds to wait for Max per call")
    ap.add_argument("--http-host", help="HTTP bind address (default 127.0.0.1)")
    ap.add_argument("--http-port", type=int, help="HTTP port (default 30026)")
    ap.add_argument("--config", help="config file path (default %%LOCALAPPDATA%%/max-mcp/config.json)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    a = parse_args(argv)
    if a.config:
        import os
        os.environ["MAX_MCP_CONFIG"] = a.config
    overrides: dict[str, Any] = {"port": a.port, "host": a.host, "timeout": a.timeout,
                                 "http_host": a.http_host, "http_port": a.http_port}
    if a.key:
        overrides["key"] = a.key
        overrides["key_enabled"] = True
    cfg = client.configure(overrides)

    if a.transport == "stdio":
        mcp.run()
        return

    key = mcfg.effective_key(cfg)
    if not mcfg.is_loopback(cfg["http_host"]) and not key:
        sys.stderr.write(
            "refusing to bind HTTP to %s without a key: set MAX_MCP_KEY, --key or enable the "
            "key in the settings window.\n" % cfg["http_host"])
        sys.exit(2)
    import uvicorn

    app = build_http_app(key)
    sys.stderr.write("max-mcp streamable HTTP on http://%s:%s/mcp (key %s)\n" % (
        cfg["http_host"], cfg["http_port"], "required" if key else "disabled"))
    uvicorn.run(app, host=cfg["http_host"], port=int(cfg["http_port"]), log_level="warning")


if __name__ == "__main__":
    main()
