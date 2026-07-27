"""Gate 4 RED — Windows Claude .cmd resolution via immutable launch spec.

Pre-fix: fry_anthropic_router.py:44 reads `CLAUDE_BIN = os.environ.get("FRY_CLAUDE_BIN","claude")`
(bare, module-level, no shutil.which). `subprocess.Popen([CLAUDE_BIN, ...])` cannot resolve bare
"claude" to claude.cmd -> WinError 2.

Post-fix: a launch-time `_resolve_claude_invocation(user_env=...)` returns an immutable structured
spec; resolution order = FRY_CLAUDE_BIN -> claude.exe -> native launcher -> .cmd shim (inspect
read-only -> node.exe + cli.js, list-form argv). Never shell=True unproven.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import fry_anthropic_router as rtr
from _fakeclaude import build_fake_node_cli, build_fake_claude_exe, ARGV_RECORDER


def _env_with_path(path_dir, *, drop_fry_claude_bin=True, **extra):
    env = dict(os.environ)
    if drop_fry_claude_bin:
        env.pop("FRY_CLAUDE_BIN", None)
    env["PATH"] = str(path_dir)
    env.update(extra)
    return env


# --- resolver existence + immutable spec shape ---

def test_resolver_function_exists():
    """RED pre-fix: no _resolve_claude_invocation exists; GREEN post-fix."""
    assert hasattr(rtr, "_resolve_claude_invocation"), (
        "fry_anthropic_router must expose a launch-time resolver "
        "(_resolve_claude_invocation), not a module-level bare CLAUDE_BIN")


def test_spec_has_immutable_fields(tmp_path):
    fake = build_fake_claude_exe(tmp_path / "native")
    env = _env_with_path(tmp_path / "native", drop_fry_claude_bin=False, **{"FRY_CLAUDE_BIN": fake})
    spec = rtr._resolve_claude_invocation(user_env=env)
    for field in ("argv_prefix", "resolved_kind", "resolved_command_path",
                  "resolved_command_sha256"):
        assert field in spec, f"spec missing immutable field: {field}"
    assert isinstance(spec["argv_prefix"], list)
    assert spec["resolved_kind"] in ("native_exe", "node_script", "comspec_cmd")


def test_spec_immutable_across_fetches(tmp_path):
    fake = build_fake_claude_exe(tmp_path / "native")
    env = _env_with_path(tmp_path / "native", drop_fry_claude_bin=False, **{"FRY_CLAUDE_BIN": fake})
    s1 = rtr._resolve_claude_invocation(user_env=env)
    s2 = rtr._resolve_claude_invocation(user_env=env)
    assert s1 == s2, "same env -> equal specs"
    assert s1 is not s2, "resolver must return a fresh object each call (no shared mutable cache)"
    # mutating one must not affect the other
    s1["argv_prefix"].append("MUT")
    assert "MUT" not in s2.get("argv_prefix", []), "spec objects must not share mutable state"


# --- resolution order ---

def test_explicit_fry_claude_bin_preferred(tmp_path):
    """FRY_CLAUDE_BIN (explicit absolute path) wins over PATH lookups."""
    explicit = build_fake_claude_exe(tmp_path / "explicit")
    # also put a DIFFERENT claude.exe on PATH to prove explicit wins
    build_fake_claude_exe(tmp_path / "onpath")
    env = _env_with_path(tmp_path / "onpath", drop_fry_claude_bin=False, **{"FRY_CLAUDE_BIN": explicit})
    spec = rtr._resolve_claude_invocation(user_env=env)
    assert os.path.samefile(spec["resolved_command_path"], explicit), (
        "FRY_CLAUDE_BIN must take precedence over PATH-resolved claude.exe")


def test_claude_exe_on_path(tmp_path):
    build_fake_claude_exe(tmp_path / "p")
    env = _env_with_path(tmp_path / "p")
    spec = rtr._resolve_claude_invocation(user_env=env)
    assert spec["resolved_kind"] == "native_exe"
    assert os.path.exists(spec["resolved_command_path"])


def test_cmd_shim_resolves_to_node_script(tmp_path):
    """claude.cmd shim -> resolver inspects read-only -> [node.exe, cli.js] (node_script)."""
    fake = build_fake_node_cli(tmp_path / "shim")
    env = _env_with_path(tmp_path / "shim")
    spec = rtr._resolve_claude_invocation(user_env=env)
    assert spec["resolved_kind"] in ("node_script", "comspec_cmd"), (
        f"expected node_script or comspec_cmd for .cmd shim, got {spec.get('resolved_kind')}")
    if spec["resolved_kind"] == "node_script":
        assert spec.get("node_path") and os.path.exists(spec["node_path"])
        assert spec.get("script_path") and os.path.exists(spec["script_path"])
        # argv_prefix must be [node.exe, cli.js] — list-form, no shell
        assert len(spec["argv_prefix"]) >= 2
        assert os.path.samefile(spec["argv_prefix"][0], fake["node_exe"])


# --- failure handling ---

def test_missing_executable_raises_structured(tmp_path):
    """No claude anywhere + no FRY_CLAUDE_BIN -> RouterError (not silent bare 'claude')."""
    env = _env_with_path(tmp_path / "nowhere_empty")
    os.makedirs(tmp_path / "nowhere_empty", exist_ok=True)
    with pytest.raises(Exception):
        rtr._resolve_claude_invocation(user_env=env)


def test_resolution_at_launch_time_no_global_cache(tmp_path):
    """Two resolutions with different FRY_CLAUDE_BIN yield different specs (no global cache)."""
    a = build_fake_claude_exe(tmp_path / "a")
    b = build_fake_claude_exe(tmp_path / "b")
    env_a = _env_with_path(tmp_path / "a", drop_fry_claude_bin=False, **{"FRY_CLAUDE_BIN": a})
    env_b = _env_with_path(tmp_path / "b", drop_fry_claude_bin=False, **{"FRY_CLAUDE_BIN": b})
    s1 = rtr._resolve_claude_invocation(user_env=env_a)
    s2 = rtr._resolve_claude_invocation(user_env=env_b)
    assert not os.path.samefile(s1["resolved_command_path"], s2["resolved_command_path"]), (
        "resolver must re-resolve at launch time, not cache a module-level result")


# --- adversarial argv round-trip (proves list-form, no shell interpretation) ---

ADVERSARIAL_ARGS = [
    "with space", "uni→codeé", "a&b|c<d>e^f(g)h%i!j\"k", "trailing\\", "",
    "%PATHEXT%", "!delayed!", "tab\there", "semi;colon", "pipe|two",
    "quote'in", 'dquote"out', "newline\nhere",
]


def test_adversarial_argv_round_trip_via_node_script(tmp_path):
    """Resolved node_script argv_prefix + adversarial args arrive byte-for-byte."""
    fake = build_fake_node_cli(tmp_path / "rt")
    env = _env_with_path(tmp_path / "rt")
    spec = rtr._resolve_claude_invocation(user_env=env)
    if spec["resolved_kind"] != "node_script":
        pytest.skip("resolver did not pick node_script for .cmd shim; cannot test argv round-trip here")
    out = tmp_path / "argv.json"
    env_rt = dict(env)
    env_rt["FRY_ARGV_OUT"] = str(out)
    argv = list(spec["argv_prefix"]) + list(ADVERSARIAL_ARGS)
    proc = subprocess.run(argv, env=env_rt, capture_output=True)
    assert proc.returncode == 0, (
        f"recorder exited {proc.returncode}; stderr={proc.stderr.decode('utf-8','replace')}")
    rec = json.loads(out.read_text(encoding="utf-8"))
    # argv_recorder records sys.argv[1:] = [cli.js, *ADVERSARIAL] when invoked as
    # [node.exe, cli.js, *args]; drop the cli.js script element.
    received = rec["argv"]
    # The script path is argv_recorder.py-equivalent; strip the first element (script).
    if received and os.path.basename(received[0]) == "cli.js":
        received = received[1:]
    assert received == ADVERSARIAL_ARGS, (
        f"argv round-trip mismatch:\n sent={ADVERSARIAL_ARGS}\n got={received}")


def test_adversarial_argv_round_trip_via_native_exe(tmp_path):
    """Resolved native_exe argv_prefix + adversarial args arrive byte-for-byte.

    claude.exe = python copy; argv_prefix=[claude.exe]; we pass argv_recorder.py
    as the first user arg (the script), then adversarial args. recorder records
    sys.argv[1:] = adversarial (script consumed by python)."""
    exe = build_fake_claude_exe(tmp_path / "nrt")
    env = _env_with_path(tmp_path / "nrt", drop_fry_claude_bin=False, **{"FRY_CLAUDE_BIN": exe})
    spec = rtr._resolve_claude_invocation(user_env=env)
    assert spec["resolved_kind"] == "native_exe"
    out = tmp_path / "argv.json"
    env_rt = dict(env)
    env_rt["FRY_ARGV_OUT"] = str(out)
    argv = list(spec["argv_prefix"]) + [ARGV_RECORDER] + list(ADVERSARIAL_ARGS)
    proc = subprocess.run(argv, env=env_rt, capture_output=True)
    assert proc.returncode == 0, (
        f"recorder exited {proc.returncode}; stderr={proc.stderr.decode('utf-8','replace')}")
    rec = json.loads(out.read_text(encoding="utf-8"))
    # invoked as [claude.exe, argv_recorder.py, *adv]; python sets argv[0]=recorder,
    # so sys.argv[1:] = adversarial.
    assert rec["argv"] == ADVERSARIAL_ARGS, (
        f"native argv round-trip mismatch:\n sent={ADVERSARIAL_ARGS}\n got={rec['argv']}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))