"""Gate 5 RED — subscription sidecar failure must NOT fall back to legacy flattening.

Pre-fix: fry.py cmd_launch sidecar interception (`:1712-1714` Raine, `:1724-1726`
Routatic, `:1727-1731` opencode-no-key) prints "falling back to legacy router path" and
FALLS THROUGH to `:1733 launch_router(...)` — the legacy flattening path that mutated
.claude.json/settings/CCR in the historical bug.

Post-fix: on sidecar failure, emit `FRY_SIDECAR_ERROR provider=<p> stage=<s>
reason=<redacted>` to stderr, release the per-launch sidecar lease (lease-aware), and
return non-zero that propagates to sys.exit. launch_router/launch_native MUST NOT be
reached for subscription providers. Retained providers (ollama/deepseek/gemini/nvidia)
still route legacy.

This test imports the WORKTREE fry.py (pre-fix) and monkeypatches the sidecar launch
to raise, spying on legacy launch_router/launch_native. It does NOT touch active
install, real Raine, real creds, or immutable config.
"""
import os
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import fry  # worktree fry.py (pre-fix)


def _args(**kw):
    defaults = dict(
        debug=False, debug_dir=None, agent="claude", model="xai,grok-4.5",
        provider=None, dry_run=False, native=False, router=True,
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def test_sidecar_failure_does_not_call_legacy_router(monkeypatch, capsys):
    """RED pre-fix: launch_router IS called (fallback). GREEN: NOT called + non-zero + FRY_SIDECAR_ERROR."""
    fry.passthrough_global = []
    calls = {"router": 0, "native": 0}

    def _raising_sidecar(*a, **kw):
        raise RuntimeError("injected sidecar start failure: raine binary missing")

    def _router_spy(*a, **kw):
        calls["router"] += 1
        return 0

    def _native_spy(*a, **kw):
        calls["native"] += 1
        return 0

    monkeypatch.setattr(fry._sidecar_router, "launch_via_sidecar", _raising_sidecar)
    monkeypatch.setattr(fry, "launch_router", _router_spy)
    monkeypatch.setattr(fry, "launch_native", _native_spy)

    rc = fry.cmd_launch({}, _args(model="xai,grok-4.5"))
    err = capsys.readouterr().err

    assert calls["router"] == 0, (
        f"legacy launch_router MUST NOT be called on sidecar failure; called {calls['router']}x. "
        f"stderr={err!r}")
    assert calls["native"] == 0, "launch_native MUST NOT be called on sidecar failure either"
    assert rc != 0, f"non-zero exit required on sidecar failure; got rc={rc}"
    assert "FRY_SIDECAR_ERROR" in err, (
        f"structured FRY_SIDECAR_ERROR must reach stderr; got: {err!r}")
    assert "falling back to legacy" not in err, (
        f"must NOT fall back to legacy; stderr says fallback: {err!r}")


def test_sidecar_failure_reason_is_redacted(monkeypatch, capsys):
    """The structured error must not leak the raw exception (which may contain secrets)."""
    fry.passthrough_global = []

    def _raising(*a, **kw):
        raise RuntimeError("SECRET_TOKEN_xyz_and_api_key_999 leaked in detail")

    monkeypatch.setattr(fry._sidecar_router, "launch_via_sidecar", _raising)
    monkeypatch.setattr(fry, "launch_router", lambda *a, **kw: 0)
    monkeypatch.setattr(fry, "launch_native", lambda *a, **kw: 0)
    fry.cmd_launch({}, _args(model="xai,grok-4.5"))
    err = capsys.readouterr().err
    assert "SECRET_TOKEN_xyz_and_api_key_999" not in err, (
        f"sidecar error reason must be redacted; raw secret leaked to stderr: {err!r}")
    assert "FRY_SIDECAR_ERROR" in err


def test_codex_sidecar_failure_no_fallback(monkeypatch, capsys):
    """Codex (openai) provider: same no-fallback contract as Grok (xai)."""
    fry.passthrough_global = []
    calls = {"router": 0}

    def _raising(*a, **kw):
        raise RuntimeError("injected codex sidecar failure")

    monkeypatch.setattr(fry._sidecar_router, "launch_via_sidecar", _raising)
    monkeypatch.setattr(fry, "launch_router", lambda *a, **kw: (calls.__setitem__("router", calls["router"] + 1) or 0))
    monkeypatch.setattr(fry, "launch_native", lambda *a, **kw: 0)
    rc = fry.cmd_launch({}, _args(model="codex,gpt-5.4"))
    err = capsys.readouterr().err
    assert calls["router"] == 0
    assert rc != 0
    assert "FRY_SIDECAR_ERROR" in err


def test_kimi_sidecar_failure_no_fallback(monkeypatch, capsys):
    """Kimi provider: same no-fallback contract."""
    fry.passthrough_global = []
    calls = {"router": 0}

    def _raising(*a, **kw):
        raise RuntimeError("injected kimi sidecar failure")
    monkeypatch.setattr(fry._sidecar_router, "launch_via_sidecar", _raising)
    monkeypatch.setattr(fry, "launch_router", lambda *a, **kw: (calls.__setitem__("router", calls["router"] + 1) or 0))
    monkeypatch.setattr(fry, "launch_native", lambda *a, **kw: 0)
    rc = fry.cmd_launch({}, _args(model="kimi,kimi-k2.6"))
    err = capsys.readouterr().err
    assert calls["router"] == 0
    assert rc != 0
    assert "FRY_SIDECAR_ERROR" in err


def test_retained_ollama_still_routes_legacy(monkeypatch):
    """Retained provider (ollama) MUST still route to legacy launch_router (unchanged)."""
    fry.passthrough_global = []
    calls = {"router": 0}
    monkeypatch.setattr(fry, "launch_router", lambda *a, **kw: (calls.__setitem__("router", calls["router"] + 1) or 0))
    monkeypatch.setattr(fry, "launch_native", lambda *a, **kw: 0)
    # ensure sidecar path not entered for ollama
    sidecar_called = {"v": 0}
    orig = getattr(fry._sidecar_router, "launch_via_sidecar", None)
    if orig is not None:
        def _spy(*a, **kw):
            sidecar_called["v"] += 1
            return 0
        monkeypatch.setattr(fry._sidecar_router, "launch_via_sidecar", _spy)
    fry.cmd_launch({}, _args(model="ollama,llama3", router=True))
    assert calls["router"] >= 1, "retained ollama must route to legacy launch_router"
    assert sidecar_called["v"] == 0, "ollama must NOT enter sidecar path"


def test_no_flattened_answer_on_stdout(monkeypatch, capsys):
    """On sidecar failure, stdout must not contain a flattened/legacy provider answer."""
    fry.passthrough_global = []

    def _raising(*a, **kw):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(fry._sidecar_router, "launch_via_sidecar", _raising)
    monkeypatch.setattr(fry, "launch_router", lambda *a, **kw: 0)
    monkeypatch.setattr(fry, "launch_native", lambda *a, **kw: 0)
    fry.cmd_launch({}, _args(model="xai,grok-4.5"))
    out = capsys.readouterr().out
    # a flattened answer would look like a single-line model response; assert the
    # stdout does not contain a fabricated completion marker.
    assert "flattened" not in out.lower()
    assert "[launch]" not in out, "must not emit a legacy launch banner on sidecar failure"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))