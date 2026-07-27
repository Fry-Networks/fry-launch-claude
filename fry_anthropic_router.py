#!/usr/bin/env python3
"""Fry Anthropic router — launches Claude Code against a raine/routatic sidecar.

Builds a COPIED child environment (never mutates the parent/global env), points
Claude Code at the sidecar via ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL/
SMALL_FAST_MODEL, and runs `claude` with full wrapper transparency:
  * all user-supplied args passed through verbatim (no prepend, no flatten)
  * cwd inherited
  * stdin/stdout/stderr inherited (interactive terminal preserved)
  * exit code propagated
  * Ctrl-C / SIGINT propagated to the child
  * Unicode, spaces, and quoting preserved by passing argv as a list

A sidecar lease is acquired before launch and released in `finally`. The sidecar
is shared across concurrent launches (see fry_proxy_sidecar.RaineSidecarManager);
release only shuts the sidecar down at zero live leases.

This module mutates NO .claude.json / settings.json / CCR / model-cache / MCP /
plugin state -> NO restore logic is required on this path.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

import fry_proxy_sidecar as sc

# Subscription providers routed through raine. Keys accept BOTH the fry
# provider id (openai/xai/kimi) AND the raine provider name (codex/grok/kimi)
# so the fry dispatcher and the ai-launchers facade (which uses raine names)
# both resolve correctly. opencode would route through routatic; that path is
# wired but gated on an OpenCode Go API key (AUTH_ACTION_REQUIRED for live E2E
# if absent).
RAINE_PROVIDERS = {"openai": "codex", "xai": "grok", "kimi": "kimi",
                   "codex": "codex", "grok": "grok"}
ROUTATIC_PROVIDERS = {"opencode"}

# Deprecated module-level cache of the Claude binary. Kept only for backward
# compatibility with any external importer; the fixed launch path NEVER uses
# this — it calls _resolve_claude_invocation() at launch time so changing
# FRY_CLAUDE_BIN / PATH between launches takes effect (no frozen global cache).
CLAUDE_BIN = os.environ.get("FRY_CLAUDE_BIN", "claude")


class RouterError(RuntimeError):
    pass


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _inspect_cmd_shim(shim_path: str):
    """Read-only inspection of an npm-style `claude.cmd` shim.

    Returns (node_path, script_path) if the shim matches the trusted pattern
    (`set "_prog=%~dp0...node.exe"` + `"%_prog%" "%~dp0...cli.js" %*`), else None.
    Never executes the shim; only parses its text. Both resolved paths must
    exist on disk.
    """
    try:
        text = Path(shim_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    shim_dir = Path(shim_path).parent
    # node.exe reference: `set "_prog=%~dp0node.exe"` OR `"%~dp0...node.exe"`
    node_match = re.search(r'%~dp0([^"\s]*node\.exe)', text)
    if not node_match:
        node_match = re.search(r'set\s+"_prog=%~dp0([^"\s]+)"', text)
    script_match = re.search(r'%~dp0([^"\s]*cli\.js)', text)
    if not node_match or not script_match:
        return None
    try:
        node_path = (shim_dir / node_match.group(1)).resolve(strict=False)
        script_path = (shim_dir / script_match.group(1)).resolve(strict=False)
    except (OSError, ValueError):
        return None
    if not node_path.exists() or not script_path.exists():
        return None
    return str(node_path), str(script_path)


def _fresh_spec(argv_prefix, resolved_kind, resolved_command_path,
                node_path=None, script_path=None):
    """Build a fresh immutable-ish spec dict (caller gets a new object each call)."""
    spec = {
        "argv_prefix": list(argv_prefix),
        "resolved_kind": resolved_kind,
        "resolved_command_path": resolved_command_path,
        "resolved_command_sha256": _sha256_file(resolved_command_path),
        "node_path": None,
        "node_sha256": None,
        "script_path": None,
        "script_sha256": None,
    }
    if node_path:
        spec["node_path"] = node_path
        spec["node_sha256"] = _sha256_file(node_path)
    if script_path:
        spec["script_path"] = script_path
        spec["script_sha256"] = _sha256_file(script_path)
    return spec


def _resolve_claude_invocation(user_env=None):
    """Resolve the Claude Code launcher to an immutable launch spec, at launch time.

    Resolution order (first wins, no global cache):
      1. explicit FRY_CLAUDE_BIN (absolute path; must exist)
      2. shutil.which on PATH (PATHEXT-aware: claude.exe / claude.cmd)

    A `.cmd` shim is INSPECTED read-only — if it matches the trusted npm pattern
    we invoke `node.exe + cli.js` directly with list-form argv (no shell). If
    inspection fails we fall back to `%COMSPEC% /c <shim>` (comspec_cmd). Never
    shell=True unproven against adversarial args; the node_script path is the
    safe list-form path.

    Returns a fresh dict per call (no shared mutable state). Raises RouterError
    if no Claude is found or the explicit path is missing.
    """
    env = user_env if user_env is not None else os.environ
    candidates = []
    explicit = env.get("FRY_CLAUDE_BIN")
    if explicit:
        candidates.append(explicit)
    path_env = env.get("PATH", "")
    # shutil.which accepts a path arg; respects PATHEXT on Windows.
    found = (shutil.which("claude", path=path_env)
             or shutil.which("claude.cmd", path=path_env)
             or shutil.which("claude.exe", path=path_env))
    if found and os.path.abspath(found) not in [os.path.abspath(c) for c in candidates]:
        candidates.append(found)
    if not candidates:
        raise RouterError("FRY_CLAUDE_ERROR stage=resolve reason=no_claude_found")
    resolved = os.path.abspath(candidates[0])
    if not os.path.exists(resolved):
        raise RouterError("FRY_CLAUDE_ERROR stage=resolve reason=explicit_path_missing")
    lower = resolved.lower()
    if lower.endswith(".cmd") or lower.endswith(".bat"):
        inspected = _inspect_cmd_shim(resolved)
        if inspected:
            node_path, script_path = inspected
            return _fresh_spec(
                argv_prefix=[node_path, script_path],
                resolved_kind="node_script",
                resolved_command_path=resolved,
                node_path=node_path, script_path=script_path)
        comspec = env.get("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
        return _fresh_spec(
            argv_prefix=[comspec, "/c", resolved],
            resolved_kind="comspec_cmd",
            resolved_command_path=resolved)
    # native executable (claude.exe or any real exe)
    return _fresh_spec(
        argv_prefix=[resolved],
        resolved_kind="native_exe",
        resolved_command_path=resolved)


def _copy_env_for_sidecar(port: int, main_model: str, small_model: str,
                          strip_anthropic: bool = True) -> dict:
    """Return a NEW env dict pointing Claude Code at the sidecar.

    Never mutates os.environ. Strips inherited ANTHROPIC_* that would otherwise
    leak the legacy CCR/ollama routing (parent FryStation env sets
    ANTHROPIC_BASE_URL=http://0.0.0.0:11434, ANTHROPIC_AUTH_TOKEN=ollama,
    ANTHROPIC_API_KEY=...). We replace ONLY the confirmed-compat variables.
    """
    env = dict(os.environ)
    if strip_anthropic:
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                  "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"):
            env.pop(k, None)
    env["ANTHROPIC_BASE_URL"] = f"http://{sc.LISTEN_HOST}:{port}"
    env["ANTHROPIC_AUTH_TOKEN"] = "unused"  # raine needs no upstream token; local proxy accepts
    env["ANTHROPIC_MODEL"] = main_model
    env["ANTHROPIC_SMALL_FAST_MODEL"] = small_model
    return env


def _resolve_models(provider_key: str, requested: Optional[str]) -> tuple:
    """Resolve (main_model, small_model) for a raine provider.

    Both must resolve to the SAME provider so background/title/token-count
    traffic does not cross-route. small/fast defaults to a small model within
    the provider catalog.
    """
    cat = sc.RAINE_MODEL_CATALOG.get(provider_key)
    if not cat:
        raise RouterError(f"no raine catalog for provider '{provider_key}'")
    main = sc.resolve_raine_model(provider_key, requested)
    # pick a small model from the same provider
    small_pref = {
        "codex": "claude-haiku-4-5",
        "grok": "grok-4.5",
        "kimi": "kimi-k2.6",
    }
    small = small_pref.get(provider_key, cat[0])
    if small not in cat:
        small = cat[0]
    return main, small


def launch_via_sidecar(cfg, agent, model_spec, passthrough_args,
                       dry_run=False, provider=None, stdout=None, stderr=None,
                       stdin=None, sidecar_manager=None):
    """Launch Claude Code routed through a raine sidecar.

    model_spec is the fry "<provider>,<model>" form (e.g. "grok,grok-4.5") OR a
    bare provider. passthrough_args is the list of extra Claude Code args to
    forward verbatim.
    Returns the child exit code (int). Raises RouterError on misconfiguration.
    """
    provider_key, requested = _parse_model_spec(model_spec, provider)
    raine_provider = RAINE_PROVIDERS.get(provider_key)
    if raine_provider is None:
        raise RouterError(f"provider '{provider_key}' is not a raine subscription "
                          f"provider; launch_via_sidecar should not have been called")
    main_model, small_model = _resolve_models(raine_provider, requested)

    if dry_run:
        sys.stderr.write(
            f"[fry:dry-run] sidecar raine provider={raine_provider} "
            f"main={main_model} small={small_model} args={passthrough_args}\n")
        return 0

    mgr = sidecar_manager or sc.RaineSidecarManager(
        sc.DEFAULT_RAINE_EXE, sc.PINNED_RAINE_SHA256, sc.DEFAULT_LEASE_DIR)
    owner = f"fry:{os.getpid()}"
    lease_id = None
    port = None
    proc = None
    try:
        # Resolve the Claude launcher BEFORE acquiring the sidecar lease, so a
        # resolution failure leaves no lease to clean up and no orphan process.
        # Resolution reads FRY_CLAUDE_BIN / PATH at launch time (no module cache).
        spec = _resolve_claude_invocation()
        port, lease_id = mgr.acquire_lease(owner)
        env = _copy_env_for_sidecar(port, main_model, small_model)
        argv = list(spec["argv_prefix"]) + list(passthrough_args or [])
        # Foreground, inherited stdio -> interactive terminal + Ctrl-C propagation.
        # We do NOT capture output; streaming + cancellation belong to Claude Code.
        proc = subprocess.Popen(argv, env=env, cwd=os.getcwd(),
                                stdin=stdin or sys.stdin,
                                stdout=stdout or sys.stdout,
                                stderr=stderr or sys.stderr,
                                close_fds=True)
        # Propagate Ctrl-C to the child only (we are the foreground parent).
        rc = _wait_propagating(proc)
        return rc
    except Exception:
        # Structured, redacted error — never leak the raw exception (may carry
        # tokens/paths). stage=resolve if no lease yet, else spawn.
        stage = "resolve" if lease_id is None else "spawn"
        print(f"FRY_SIDECAR_ERROR provider={raine_provider} stage={stage} "
              f"reason=<redacted>", file=sys.stderr)
        raise
    finally:
        if lease_id is not None:
            try:
                mgr.release_lease(lease_id)
            except Exception:
                pass


def _wait_propagating(proc: subprocess.Popen) -> int:
    """Wait for the child, forwarding SIGINT/SIGTERM to it."""
    if sys.platform == "win32":
        # On Windows, Ctrl-C is delivered to the whole console process group
        # automatically; the child shares the console. Just wait.
        return proc.wait()
    else:
        # Forward SIGINT/SIGTERM/SIGHUP to the child.
        def _fwd(signum, _frame):
            try:
                proc.send_signal(signum)
            except Exception:
                pass
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(s, _fwd)
            except Exception:
                pass
        return proc.wait()


def _parse_model_spec(model_spec: Optional[str], provider: Optional[str]) -> tuple:
    """Parse 'provider,model' or bare provider/model. Returns (provider_key, requested_model)."""
    if model_spec and "," in model_spec:
        p, _, m = model_spec.partition(",")
        return p.strip(), (m.strip() or None)
    if model_spec:
        # could be just a model id under an explicit provider
        if provider:
            return provider.strip(), model_spec.strip()
        # bare token: treat as provider if known, else error
        if model_spec in RAINE_PROVIDERS or model_spec in ROUTATIC_PROVIDERS:
            return model_spec, None
        raise RouterError(f"ambiguous model spec '{model_spec}'; use 'provider,model'")
    if provider:
        return provider.strip(), None
    raise RouterError("no provider or model specified for sidecar launch")


def is_sidecar_provider(provider_key: Optional[str]) -> bool:
    """True if this provider should route through a sidecar (raine or routatic)."""
    if not provider_key:
        return False
    return provider_key in RAINE_PROVIDERS or provider_key in ROUTATIC_PROVIDERS


if __name__ == "__main__":
    # manual smoke: fry_anthropic_router.py grok,grok-4.5 -- <claude args...>
    args = sys.argv[1:]
    if not args:
        print("usage: fry_anthropic_router.py <provider>,<model> [-- claude-args...]")
        sys.exit(2)
    spec = args[0]
    rest = args[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    rc = launch_via_sidecar(None, "claude", spec, rest)
    sys.exit(rc)