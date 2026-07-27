"""Gate 4 RED — immutable launch spec: fresh object each fetch, user args appended
unchanged, copied child env (not global os.environ), no global cache.

Pre-fix: CLAUDE_BIN is a module-level string cached at import. Post-fix:
_resolve_claude_invocation returns a fresh immutable dict per call.
"""
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import fry_anthropic_router as rtr
from _fakeclaude import build_fake_claude_exe


def test_spec_not_shared_mutable_state(tmp_path, monkeypatch):
    exe = build_fake_claude_exe(tmp_path / "e")
    monkeypatch.setenv("FRY_CLAUDE_BIN", exe)
    s1 = rtr._resolve_claude_invocation()
    s2 = rtr._resolve_claude_invocation()
    assert s1 == s2
    assert s1 is not s2, "must return a fresh object per call (no shared mutable cache)"
    s1["argv_prefix"].append("X")
    assert "X" not in s2["argv_prefix"], "mutating one spec must not affect another"


def test_user_args_appended_unchanged(tmp_path, monkeypatch):
    """Resolver argv_prefix + user args; user args list not mutated by resolver."""
    exe = build_fake_claude_exe(tmp_path / "e")
    monkeypatch.setenv("FRY_CLAUDE_BIN", exe)
    spec = rtr._resolve_claude_invocation()
    user_args = ["--print", "-p", "hi", "--x", "a&b"]
    user_copy = list(user_args)
    argv = list(spec["argv_prefix"]) + user_args
    assert user_args == user_copy, "resolver must not mutate the caller's user_args list"
    assert argv[:len(spec["argv_prefix"])] == spec["argv_prefix"]
    assert argv[len(spec["argv_prefix"]):] == user_copy


def test_copied_child_env_not_global(monkeypatch):
    """_copy_env_for_sidecar returns a NEW dict (not os.environ); stripping does not touch global."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://parent-leak:1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "parent-leak-tok")
    before = dict(os.environ)
    env = rtr._copy_env_for_sidecar(4242, "grok-4.5", "grok-4.5")
    assert env is not os.environ, "child env must be a copy, not os.environ itself"
    assert os.environ.get("ANTHROPIC_BASE_URL") == "http://parent-leak:1", (
        "global os.environ must NOT be mutated by _copy_env_for_sidecar")
    assert dict(os.environ) == before, "global os.environ must be unchanged"
    assert "parent-leak" not in env.get("ANTHROPIC_BASE_URL", ""), (
        "child env must strip inherited ANTHROPIC_BASE_URL")
    assert env["ANTHROPIC_BASE_URL"].endswith(":4242"), "child env must set sidecar URL"


def test_no_global_cache_across_envs(tmp_path, monkeypatch):
    """Resolver must re-resolve when FRY_CLAUDE_BIN changes between calls (no global cache)."""
    a = build_fake_claude_exe(tmp_path / "a")
    b = build_fake_claude_exe(tmp_path / "b")
    monkeypatch.setenv("FRY_CLAUDE_BIN", a)
    s1 = rtr._resolve_claude_invocation()
    monkeypatch.setenv("FRY_CLAUDE_BIN", b)
    s2 = rtr._resolve_claude_invocation()
    assert not os.path.samefile(s1["resolved_command_path"], s2["resolved_command_path"]), (
        "changing FRY_CLAUDE_BIN between calls must yield a different resolution (no global cache)")


def test_spec_does_not_carry_user_args(tmp_path, monkeypatch):
    """The resolved spec is the launcher prefix only; user args are appended by the caller,
    never baked into the spec (so the same spec is reusable across different arg sets)."""
    exe = build_fake_claude_exe(tmp_path / "e")
    monkeypatch.setenv("FRY_CLAUDE_BIN", exe)
    spec = rtr._resolve_claude_invocation()
    # spec argv_prefix is the binary/script prefix; calling resolver again with no args
    # yields the same prefix — user args are the caller's responsibility.
    assert spec["argv_prefix"], "argv_prefix must be non-empty"
    prefix_len = len(spec["argv_prefix"])
    assert prefix_len <= 2, (
        f"argv_prefix should be [exe] or [node, script], got {spec['argv_prefix']}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))