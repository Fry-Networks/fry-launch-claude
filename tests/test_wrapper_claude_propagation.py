"""Gate 4/5 RED — wrappers must not depend on module-level bare claude; resolved
invocation threaded explicitly + copied child env (never os.environ global mutation).

Pre-fix: fry_anthropic_router.launch_via_sidecar builds `argv=[CLAUDE_BIN]+args`
from the module-level bare `CLAUDE_BIN = os.environ.get("FRY_CLAUDE_BIN","claude")`
and `subprocess.Popen(argv, env=env)`. A wrapper (grok-wrap/codex-wrap via
shared.sidecar -> this module) thus depends on a module-level bare claude ->
WinError 2.

Post-fix: launch_via_sidecar calls `_resolve_claude_invocation(user_env=env)` at
launch time and Popen's `spec["argv_prefix"] + passthrough_args` (list-form).

This test uses a MOCK sidecar manager (no real Raine, no port bind) + a fake
claude install (node.exe=python copy + cli.js=argv_recorder) so the resolved
launch runs the recorder, which captures argv + the inherited env.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import fry_anthropic_router as rtr
from _fakeclaude import build_fake_node_cli


@pytest.fixture
def real_stdin():
    """launch_via_sidecar does `stdin or sys.stdin`; under pytest capsys sys.stdin is a
    pseudofile with no fileno() -> Popen breaks before the real bug surfaces. Provide a
    real devnull handle so the actual WinError 2 / resolver behavior is exercised."""
    f = open(os.devnull, "r", encoding="utf-8")
    try:
        yield f
    finally:
        f.close()


class _MockSidecarManager:
    """Mock RaineSidecarManager — acquire returns a fake port+lease; release is a no-op spy."""
    def __init__(self):
        self.released = []
        self.acquire_calls = 0

    def acquire_lease(self, owner):
        self.acquire_calls += 1
        return (59999, "mock-lease-1")

    def release_lease(self, lease_id):
        self.released.append(lease_id)


def _setup_fake_claude(tmp_path, monkeypatch):
    """Put a fake claude.cmd shim (node.exe+cli.js=recorder) on PATH; clear FRY_CLAUDE_BIN."""
    fake = build_fake_node_cli(tmp_path / "wrap")
    monkeypatch.setenv("PATH", str(tmp_path / "wrap"))
    monkeypatch.delenv("FRY_CLAUDE_BIN", raising=False)
    return fake


def test_launch_via_sidecar_uses_resolver_not_module_level_claude(monkeypatch, tmp_path, real_stdin):
    """RED pre-fix: launch_via_sidecar uses bare module-level CLAUDE_BIN -> WinError 2.
    GREEN: calls resolver + recorder runs (rc 0) + passthrough preserved + lease released."""
    _setup_fake_claude(tmp_path, monkeypatch)
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FRY_ARGV_OUT", str(out))

    resolver_calls = {"n": 0}
    orig_resolver = getattr(rtr, "_resolve_claude_invocation", None)
    if orig_resolver is not None:
        def _spy_resolver(user_env=None):
            resolver_calls["n"] += 1
            return orig_resolver(user_env=user_env)
        monkeypatch.setattr(rtr, "_resolve_claude_invocation", _spy_resolver)

    mgr = _MockSidecarManager()
    passthrough = ["--print", "-p", "hello world", "--weird", "a&b|c"]
    rc = rtr.launch_via_sidecar(
        cfg={}, agent="claude", model_spec="xai,grok-4.5",
        passthrough_args=passthrough, dry_run=False, provider="xai",
        sidecar_manager=mgr, stdin=real_stdin,
    )

    if orig_resolver is not None:
        assert resolver_calls["n"] >= 1, "launch_via_sidecar must call the launch-time resolver"
    assert rc == 0, f"recorder launch should exit 0; got {rc}"
    assert mgr.acquire_calls == 1
    assert "mock-lease-1" in mgr.released, "per-launch lease must be released in finally"
    assert out.exists(), "recorder must have run (resolved exe launched, not bare 'claude')"
    rec = json.loads(out.read_text(encoding="utf-8"))
    received = rec["argv"]
    # invoked as [node.exe, cli.js, *passthrough]; recorder argv = [cli.js, *passthrough]
    if received and os.path.basename(received[0]) == "cli.js":
        received = received[1:]
    assert received == passthrough, (
        f"passthrough args not preserved:\n sent={passthrough}\n got={received}")


def test_launch_via_sidecar_uses_copied_env_not_global(monkeypatch, tmp_path, real_stdin):
    """Child env must strip inherited ANTHROPIC_* + set sidecar URL; os.environ restored after."""
    _setup_fake_claude(tmp_path, monkeypatch)
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FRY_ARGV_OUT", str(out))
    # poison the parent env; _copy_env_for_sidecar must strip + replace
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://PARENT-LEAK-0.0.0.0:11434")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "PARENT-LEAK-ollama")

    mgr = _MockSidecarManager()
    rtr.launch_via_sidecar(
        cfg={}, agent="claude", model_spec="xai,grok-4.5",
        passthrough_args=[], dry_run=False, provider="xai",
        sidecar_manager=mgr, stdin=real_stdin,
    )
    rec = json.loads(out.read_text(encoding="utf-8"))
    base = rec.get("anthropic_base_url", "")
    assert "PARENT-LEAK" not in base, (
        f"inherited ANTHROPIC_BASE_URL must be stripped + replaced with sidecar URL; got {base!r}")
    assert "127.0.0.1" in base or "localhost" in base, (
        f"child env ANTHROPIC_BASE_URL must point at the sidecar; got {base!r}")


def test_no_bare_claude_string_in_resolved_argv(monkeypatch, tmp_path, real_stdin):
    """The resolved argv_prefix must never be the bare string 'claude'."""
    _setup_fake_claude(tmp_path, monkeypatch)
    out = tmp_path / "argv.json"
    monkeypatch.setenv("FRY_ARGV_OUT", str(out))
    mgr = _MockSidecarManager()
    # Pre-fix: CLAUDE_BIN bare "claude" (no FRY_CLAUDE_BIN) -> Popen(["claude"]) ->
    # WinError 2 -> launch_via_sidecar raises -> rc is the exception, recorder never runs.
    # Post-fix: resolver finds claude.cmd shim -> node.exe+cli.js -> recorder runs.
    try:
        rc = rtr.launch_via_sidecar(
            cfg={}, agent="claude", model_spec="xai,grok-4.5",
            passthrough_args=[], dry_run=False, provider="xai",
            sidecar_manager=mgr, stdin=real_stdin,
        )
    except Exception as e:
        pytest.fail(f"launch_via_sidecar raised on bare-claude path (pre-fix WinError 2): {e}")
    assert out.exists(), "recorder must have run — bare 'claude' must NOT be Popen'd"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))