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

import os
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

# Claude Code executable — resolve the authoritative one from PATH. We never
# prepend behavioral instructions and never consume slash commands.
CLAUDE_BIN = os.environ.get("FRY_CLAUDE_BIN", "claude")


class RouterError(RuntimeError):
    pass


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
        port, lease_id = mgr.acquire_lease(owner)
        env = _copy_env_for_sidecar(port, main_model, small_model)
        argv = [CLAUDE_BIN] + list(passthrough_args or [])
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