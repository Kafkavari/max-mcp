"""Streamable-HTTP transport, end to end on localhost without 3ds Max:
a fake listener (the real Listener class with plain handlers) + the real
server in a subprocess + the official MCP streamable-HTTP client."""

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "max_side"))


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait(port, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@pytest.fixture()
def fake_max(tmp_path):
    import max_mcp_listener
    port = _free_port()
    handlers = {"info": lambda a: {"scene_file": "fake.max", "object_count": 7},
                "layers": lambda a: {"count": 0, "layers": []}}
    ls = max_mcp_listener.Listener(handlers, port=port, key="topsecret")
    ls.start()
    th = threading.Thread(target=ls.serve_blocking, kwargs={"seconds": 60}, daemon=True)
    th.start()
    yield port
    ls.stop_requested = True
    ls.stop()
    th.join(5)


def _spawn_server(fake_port, http_port, extra, env_extra=None):
    env = dict(os.environ, MAX_MCP_PORT=str(fake_port), MAX_MCP_CONFIG=os.devnull)
    env.pop("MAX_MCP_KEY", None)
    env.update(env_extra or {})
    cmd = [sys.executable, "-m", "max_mcp.server", "--transport", "http",
           "--http-port", str(http_port)] + extra
    return subprocess.Popen(cmd, cwd=ROOT, env=env, stderr=subprocess.PIPE, stdout=subprocess.PIPE)


async def _session(url, headers=None):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            res = await s.call_tool("max_info", {})
            return [t.name for t in tools.tools], res.isError, res.content[0].text


def test_http_transport_with_key(fake_max):
    http_port = _free_port()
    proc = _spawn_server(fake_max, http_port, ["--key", "topsecret"])
    try:
        assert _wait(http_port), proc.stderr.read().decode()
        url = "http://127.0.0.1:%d/mcp" % http_port
        names, is_err, text = asyncio.run(_session(url, {"Authorization": "Bearer topsecret"}))
        assert "max_info" in names and len(names) == 10
        assert is_err is False and '"scene_file": "fake.max"' in text
        # wrong / missing key -> 401
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 401
    finally:
        proc.terminate(); proc.wait(10)


def test_http_transport_without_key_localhost(fake_max, monkeypatch):
    """No key on loopback is allowed; the fake listener still wants its key,
    so the tool call must surface the listener's 'key mismatch' error."""
    http_port = _free_port()
    proc = _spawn_server(fake_max, http_port, [])
    try:
        assert _wait(http_port), proc.stderr.read().decode()
        names, is_err, text = asyncio.run(_session("http://127.0.0.1:%d/mcp" % http_port))
        assert len(names) == 10 and is_err is True and "anahtar" in text
    finally:
        proc.terminate(); proc.wait(10)


def test_http_refuses_public_bind_without_key(fake_max):
    proc = _spawn_server(fake_max, _free_port(), ["--http-host", "0.0.0.0"])
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 2 and b"refusing to bind" in err


def test_cli_precedence_env_over_arg(fake_max, monkeypatch):
    """--port argument is beaten by MAX_MCP_PORT (env > arg)."""
    from max_mcp import server, client
    monkeypatch.setenv("MAX_MCP_PORT", str(fake_max))
    monkeypatch.setenv("MAX_MCP_CONFIG", os.devnull)
    a = server.parse_args(["--port", "1", "--key", "topsecret"])
    cfg = client.configure({"port": a.port, "key": a.key, "key_enabled": True})
    assert cfg["port"] == fake_max
    assert '"object_count": 7' in server.max_info()
