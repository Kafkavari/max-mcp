"""
Locate a 3ds Max installation and its per-user startup folder, and print the
startup loader that makes the listener start with Max.

    python -m max_mcp.paths                 # detect, print paths and loader text
    python -m max_mcp.paths --version 2026
    python -m max_mcp.paths --write         # actually write the startup loader

Nothing is written unless --write is given. Overrides:
    MAX_MCP_MAX_ROOT      e.g. "C:/Program Files/Autodesk/3ds Max 2025"
    MAX_MCP_MAX_STARTUP   e.g. "%LOCALAPPDATA%/Autodesk/3dsMax/2025 - 64bit/ENU/scripts/startup"
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START_MS = REPO_ROOT / "max_side" / "max_mcp_start.ms"
LOADER_NAME = "max_mcp_loader.ms"


def max_root(version: int = 2025) -> Path | None:
    env = os.environ.get("MAX_MCP_MAX_ROOT")
    if env:
        return Path(env)
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
        p = Path(base) / "Autodesk" / ("3ds Max %d" % version)
        if p.is_dir():
            return p
    return None


def batch_exe(version: int = 2025) -> Path | None:
    root = max_root(version)
    if root is None:
        return None
    p = root / "3dsmaxbatch.exe"
    return p if p.is_file() else None


def max_python(version: int = 2025) -> Path | None:
    root = max_root(version)
    if root is None:
        return None
    p = root / "Python" / "python.exe"
    return p if p.is_file() else None


def user_startup_dir(version: int = 2025, lang: str = "ENU") -> Path:
    env = os.environ.get("MAX_MCP_MAX_STARTUP")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA", "")
    return Path(local) / "Autodesk" / "3dsMax" / ("%d - 64bit" % version) / lang / "scripts" / "startup"


def loader_text(start_ms: Path = START_MS) -> str:
    return (
        "-- max-mcp loader: loads the MCP listener + settings macro when 3ds Max starts.\n"
        "-- Whether the listener actually starts is the 'autostart' setting (Settings window).\n"
        '-- Delete this file to disable. Source: %s\n'
        "global MaxMcp_StartupLoad = true\n"
        'try ( fileIn @"%s" ) catch ( format "[MaxMcp] loader failed: %%\\n" (getCurrentException()) )\n'
        % (start_ms, start_ms)
    )


def loader_path(version: int = 2025) -> Path:
    return user_startup_dir(version) / LOADER_NAME


def loader_installed(version: int = 2025) -> bool:
    return loader_path(version).is_file()


def install_loader(version: int = 2025, start_ms: Path = START_MS) -> Path:
    """Write the startup loader. Only call this on an explicit user action."""
    target = loader_path(version)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(loader_text(start_ms), encoding="utf-8")
    return target


def remove_loader(version: int = 2025) -> bool:
    target = loader_path(version)
    if target.is_file():
        target.unlink()
        return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", type=int, default=2025)
    ap.add_argument("--write", action="store_true", help="write the startup loader")
    a = ap.parse_args()
    root = max_root(a.version)
    startup = user_startup_dir(a.version)
    print("Max root      :", root or "(not found)")
    print("3dsmaxbatch   :", batch_exe(a.version) or "(not found)")
    print("Max python    :", max_python(a.version) or "(not found)")
    print("startup dir   :", startup, "(exists)" if startup.is_dir() else "(missing)")
    print("listener start:", START_MS)
    target = startup / LOADER_NAME
    print("loader file   :", target)
    print("--- loader content ---")
    print(loader_text(), end="")
    if a.write:
        print("--- written:", install_loader(a.version))
    else:
        print("--- not written (use --write)")


if __name__ == "__main__":
    main()
