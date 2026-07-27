"""Shared fixtures for Fry wrapper modernization RED tests.

Builds a fake Claude Code install (npm-style .cmd shim + node.exe + cli.js) in a
temp dir so the resolver can be exercised end-to-end without a real Claude
Code install. The fake node.exe is a copy of the running Python interpreter so
Popen list-form works on Windows (a .py / .cmd cannot be direct-executed).
"""
import os
import shutil
import sys
from pathlib import Path

ARGV_RECORDER = str(Path(__file__).parent / "argv_recorder.py")


def _venv_home() -> str:
    """Resolve the base CPython `home` the running venv stub points at.

    sys.executable under `uv run` is a venv stub (Scripts/python.exe); copied
    alone it exits 106 ("failed to locate pyvenv.cfg"). We write a pyvenv.cfg
    next to the copy whose `home =` is the base install so the stub can locate
    its stdlib + DLL. Falls back to sys.executable's dir if not a venv.
    """
    exe = Path(sys.executable)
    # venv layout: <root>/Scripts/python.exe + <root>/pyvenv.cfg
    for cfg in (exe.parent / "pyvenv.cfg", exe.parent.parent / "pyvenv.cfg"):
        if cfg.exists():
            try:
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("home"):
                        return line.split("=", 1)[1].strip()
            except OSError:
                pass
            break
    return str(exe.parent)


def _write_pyvenv_cfg(dest_dir: Path) -> None:
    """Write a pyvenv.cfg next to a copied venv-stub exe so it can resolve its base."""
    home = _venv_home()
    cfg = dest_dir / "pyvenv.cfg"
    cfg.write_text(f"home = {home}\ninclude-system-site-packages = false\n",
                   encoding="utf-8")


def build_fake_node_cli(install_dir: Path, cli_body: str = None) -> dict:
    """Create a fake npm-style claude.cmd shim + node.exe + cli.js.

    Returns dict with shim/node/cli paths. node.exe is a copy of sys.executable
    so it is a real Win32 console exe (Popen list-form works). cli.js holds the
    given python body (defaults to argv_recorder's body) so invoking
    `node.exe cli.js <args>` runs that python with the args.
    """
    install_dir = Path(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)

    # node.exe = copy of the running interpreter (a real .exe). If it's a venv
    # stub, write a pyvenv.cfg alongside so it can locate its base CPython.
    node_exe = install_dir / "node.exe"
    shutil.copy2(sys.executable, node_exe)
    _write_pyvenv_cfg(install_dir)

    # cli.js — holds the python body to run. Default = argv_recorder.
    if cli_body is None:
        cli_body = Path(ARGV_RECORDER).read_text(encoding="utf-8")
    cli_js = install_dir / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    cli_js.parent.mkdir(parents=True, exist_ok=True)
    cli_js.write_text(cli_body, encoding="utf-8")

    # npm-style claude.cmd shim (subset of the real format; enough for the
    # resolver to identify node.exe + cli.js).
    cli_js_rel = r"node_modules\@anthropic-ai\claude-code\cli.js"
    shim = install_dir / "claude.cmd"
    shim.write_text(
        "@ECHO off\r\n"
        "setlocal\r\n"
        'set "_prog=%~dp0node.exe"\r\n'
        'if not exist "%_prog%" set "_prog=node"\r\n'
        f'"%_prog%" "%~dp0{cli_js_rel}" %*\r\n',
        encoding="utf-8",
    )

    return {
        "install_dir": str(install_dir),
        "shim": str(shim),
        "node_exe": str(node_exe),
        "cli_js": str(cli_js),
    }


def build_fake_claude_exe(install_dir: Path, body: str = None) -> str:
    """Create a fake claude.exe (copy of interpreter) that runs `body` python.

    The exe, when invoked as `claude.exe <script> <args>`, runs the script. For
    tests that just need an exit-0 native exe, body=None yields a copy that
    behaves like python.exe (takes a script arg).
    """
    install_dir = Path(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    exe = install_dir / "claude.exe"
    shutil.copy2(sys.executable, exe)
    _write_pyvenv_cfg(install_dir)
    return str(exe)