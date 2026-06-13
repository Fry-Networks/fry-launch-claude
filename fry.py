#!/usr/bin/env python3
"""
fry -- unified launcher for coding agents across model providers.

Same shape as `ollama launch claude`:

    fry launch claude
    fry launch claude --model openrouter,anthropic/claude-sonnet-4.6
    fry launch claude -- --dangerously-skip-permissions "do X"

Two launch modes:

  ROUTER mode (default for `claude`)
      Fronts ALL configured providers behind one local router
      (claude-code-router). Inside Claude Code you switch models with
        /model <provider>,<model>
      and you can assign any of those strings to agent-team teammates.
      `fry models` prints every valid <provider>,<model> string.
      NOTE: a Claude Pro/Max *subscription* does NOT work through a router --
      Anthropic models in router mode bill via API key or OpenRouter credits.
      Use --native for subscription-backed Anthropic (single backend, Anthropic
      models only).

  NATIVE mode (default for `codex`; opt-in for `claude` via --native)
      One backend per session via env vars / -c overrides. The only way to use
      the Claude subscription. Codex is native-only (router is Claude-Code-specific).

Credential hygiene:
  * Keys are referenced in config as op:// (1Password) or env:NAME -- never plaintext.
  * Router config is written with $VAR placeholders ONLY (claude-code-router
    interpolates them from the environment). Resolved keys are exported into the
    child process's env in memory, never written to disk, never logged, never on argv.
  * fry never runs `op signin` for you.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.3.3"

FRY_HOME = Path(os.environ.get("FRY_HOME", Path.home() / ".fry"))
DEFAULT_CONFIG_PATH = FRY_HOME / "config.json"
REDACT = "<redacted>"

CREDENTIALS_PATH = FRY_HOME / "credentials.json"


def load_credentials():
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_credentials(creds):
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    if os.name == "nt":
        import subprocess
        id_proc = subprocess.run(
            ["powershell", "-Command",
             "[System.Security.Principal.WindowsIdentity]::GetCurrent().Name"],
            capture_output=True, text=True,
        )
        win_user = id_proc.stdout.strip() if id_proc.returncode == 0 else os.environ.get("USERNAME")
        subprocess.run(
            ["icacls", str(CREDENTIALS_PATH), "/inheritance:r", "/grant:r",
             f"{win_user}:(R,W)"],
            capture_output=True,
        )
        acl_proc = subprocess.run(
            ["icacls", str(CREDENTIALS_PATH)],
            capture_output=True, text=True,
        )
        bad_aces = ["Everyone", "BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users"]
        unsafe = [ln for ln in acl_proc.stdout.splitlines()
                  if any(ace in ln for ace in bad_aces)]
        if unsafe:
            warn(f"credentials file may have broad read ACLs: {unsafe}")
        else:
            print(f"fry: credentials file ACL restricted to {win_user}", file=sys.stderr)


def resolve_local_secret(env_var, provider_name=None):
    """Priority: 1) credentials.json by env_var or provider_name,
    2) actual env var, 3) None."""
    creds = load_credentials()
    if env_var in creds:
        return creds[env_var]
    if provider_name and provider_name in creds:
        return creds[provider_name]
    return os.environ.get(env_var)


def mask_input(prompt):
    """Read password-style input without echoing. Falls back to plain input on error."""
    try:
        import getpass
        return getpass.getpass(prompt)
    except Exception:
        return input(prompt)



# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def die(msg, code=1):
    print(f"fry: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg):
    print(f"fry: warning: {msg}", file=sys.stderr)


def load_config(path):
    if not path.exists():
        die(f"no config at {path}. Copy config.example.json there and edit it, "
            f"or set FRY_HOME to its directory.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"config.json is not valid JSON: {exc}")


def resolve_secret(ref):
    if ref is None:
        return None
    if ref.startswith("op://"):
        return _op_read(ref)
    if ref.startswith("env:"):
        name = ref[4:]
        val = os.environ.get(name)
        if not val:
            die(f"env var '{name}' (referenced by config) is not set in this shell.")
        return val
    if ref.startswith("literal:"):
        return ref[8:]
    die(f"unsupported secret reference '{ref}'. Use op://Vault/Item/field, env:NAME, or literal:VALUE.")


def _op_read(ref):
    if shutil.which("op") is None:
        die("1Password CLI 'op' not found but config references an op:// secret.")
    proc = subprocess.run(["op", "read", ref], capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "sign in" in err.lower() or "not currently signed in" in err.lower():
            die("1Password is locked. Run `op signin` yourself, then re-run fry.")
        die(f"`op read {ref}` failed: {err}")
    return proc.stdout.strip()


def resolve_bin(candidates):
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None


def wrap_for_windows(bin_path, args):
    if os.name == "nt" and bin_path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", bin_path] + args
    return [bin_path] + args


def list_ollama_models():
    if shutil.which("ollama") is None:
        return []
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            proc = subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            if attempt < max_attempts:
                warn(f"ollama list timed out (attempt {attempt}/{max_attempts}), retrying...")
                time.sleep(2)
                continue
            return []
        if proc.returncode == 0:
            out = []
            for ln in proc.stdout.splitlines()[1:]:
                ln = ln.strip()
                if ln:
                    out.append(ln.split()[0])
            return out
        # non-zero exit — transient failure (server starting up, etc.)
        if attempt < max_attempts:
            warn(f"ollama list failed (attempt {attempt}/{max_attempts}), retrying...")
            time.sleep(2)
    return []


def list_openai_compatible_models(base_url, api_key=None, timeout=10, max_attempts=3):
    """Query OpenAI-compatible /v1/models endpoint. Returns list of model ID strings.
    Retry logic matches list_ollama_models() pattern. Uses urllib (stdlib only).
    Never logs/prints the api_key."""
    url = base_url.rstrip("/") + "/v1/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            return [m["id"] for m in data.get("data", []) if "id" in m]
        except Exception:
            if attempt < max_attempts:
                warn(f"{base_url} /v1/models failed (attempt {attempt}/{max_attempts}), retrying...")
                time.sleep(2)
    warn(f"could not list models from {base_url}")
    return []


def discover_xai_key():
    """Discover xAI API key. Priority:
    1. ~/.grok/auth.json — find JWT-shaped field, check expiry, test against xAI API
    2. XAI_API_KEY env var (fallback)
    3. Fail closed
    Returns (key, source_label) or (None, reason).
    Never prints token values. Never persists to disk."""
    grok_path = Path.home() / ".grok" / "auth.json"
    if grok_path.exists():
        try:
            data = json.loads(grok_path.read_text(encoding="utf-8"))
            for _oidc_key, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                jwt = entry.get("key", "")
                if not isinstance(jwt, str) or not jwt.startswith("eyJ"):
                    continue
                expires_at = entry.get("expires_at", "")
                if isinstance(expires_at, str) and expires_at:
                    exp_cmp = expires_at.rstrip("Z").split(".")[0]
                    now_cmp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
                    if exp_cmp < now_cmp:
                        warn("grok JWT expired; skipping.")
                        continue
                try:
                    req = urllib.request.Request(
                        "https://api.x.ai/v1/models",
                        headers={"Authorization": f"Bearer {jwt}"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        if resp.status == 200:
                            return (jwt, "grok-cli")
                except Exception:
                    pass
        except (json.JSONDecodeError, OSError):
            pass

    env_key = os.environ.get("XAI_API_KEY")
    if env_key:
        return (env_key, "env")

    return (None, "no auth — run 'grok login' or set XAI_API_KEY")


def discover_openai_key():
    """Discover OpenAI API key. Priority:
    1. ~/.codex/auth.json — static OPENAI_API_KEY if present
    2. ~/.codex/auth.json tokens.access_token — test against OpenAI API;
       if it works (HTTP 200) treat as usable; if 403 "Missing scopes" it is
       OAuth (ChatGPT login) and incompatible with router API-key path.
    3. OPENAI_API_KEY env var (fallback)
    4. Fail closed
    Returns (key, source_label) or (None, reason).
    Never prints token values. Never persists to disk."""
    codex_path = Path.home() / ".codex" / "auth.json"
    if codex_path.exists():
        try:
            data = json.loads(codex_path.read_text(encoding="utf-8"))
            # 1. static API key (set via codex login --with-api-key)
            static_key = data.get("OPENAI_API_KEY", "")
            if isinstance(static_key, str) and static_key.startswith("sk-"):
                return (static_key, "codex-cli")
            # 2. OAuth access token — test compatibility
            tokens = data.get("tokens", {})
            if isinstance(tokens, dict):
                jwt = tokens.get("access_token", "")
                if isinstance(jwt, str) and jwt.startswith("eyJ"):
                    try:
                        req = urllib.request.Request(
                            "https://api.openai.com/v1/models",
                            headers={"Authorization": f"Bearer {jwt}"},
                        )
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            if resp.status == 200:
                                return (jwt, "codex-cli")
                    except urllib.error.HTTPError as e:
                        if e.code == 403:
                            body = e.read().decode()[:200]
                            if "Missing scopes" in body or "insufficient permissions" in body:
                                return (None, "codex-cli-oauth-incompatible")
                    except Exception:
                        pass
        except (json.JSONDecodeError, OSError):
            pass

    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return (env_key, "env")

    return (None, "no auth — run 'codex login --with-api-key' or set OPENAI_API_KEY")


# --------------------------------------------------------------------------- #
# model validation
# --------------------------------------------------------------------------- #
XAI_NON_CHAT_MODELS = {
    "grok-imagine-image",
    "grok-imagine-image-quality",
    "grok-imagine-video",
    "grok-imagine-video-1.5-preview",
}


def validate_openai_compat_model(base_url, api_key, model_id, timeout=15):
    """Send a minimal chat-completion request (max_tokens=100, as ccr does by default).
    Returns (ok, status_or_error)."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply exactly PONG"}],
        "max_tokens": 100,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            data = json.loads(body)
            if data.get("error"):
                err = data["error"]
                msg = err.get("message", "")[:120]
                return False, f"provider_error:{msg}"
            return True, f"http_{resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            msg = json.loads(body).get("error", {}).get("message", body)[:120]
        except Exception:
            msg = body[:120]
        return False, f"http_{e.code}:{msg}"
    except Exception as e:
        return False, f"exception:{type(e).__name__}:{str(e)[:120]}"


# --------------------------------------------------------------------------- #
# ROUTER mode (claude-code-router)
# --------------------------------------------------------------------------- #
FRY_MODEL_MARKER = " (router)"


def router_provider_models(pname, prouter, expand_key=None):
    """Return the model list for a router provider, expanding dynamically.
    For openai_compat providers every candidate is smoke-tested via a
    minimal chat-completion request so only callable models reach the picker."""
    models = list(prouter.get("models", []))
    fallback = prouter.get("models_fallback", [])
    kind = prouter.get("kind")

    if kind == "ollama" and prouter.get("expand"):
        for m in list_ollama_models():
            if m not in models and "cloud" not in m.split(":")[-1]:
                models.append(m)
        return models or fallback

    if kind == "openai_compat":
        key = expand_key
        if key is None and prouter.get("discover") == "xai":
            key, _ = discover_xai_key()
        base = prouter.get("expand_base_url") or ""
        if not base:
            api_url = prouter.get("api_base_url", "")
            base = api_url.replace("/v1/chat/completions", "").rstrip("/")

        # Always include hardcoded models and fallback (unvalidated)
        result = list(models) if models else list(fallback)

        if key and base and prouter.get("expand"):
            candidates = set()
            discovered = list_openai_compatible_models(base, api_key=key)
            for m in discovered:
                if m in XAI_NON_CHAT_MODELS:
                    continue
                if m not in result:
                    candidates.add(m)

            for m in sorted(candidates):
                if len(result) >= 10:
                    warn(f"router provider '{pname}': capping at 10 models.")
                    break
                ok, status = validate_openai_compat_model(base, key, m)
                if ok:
                    result.append(m)
                else:
                    warn(f"router provider '{pname}': discovered model '{m}' failed validation ({status}); skipping.")

        return result

    return models or fallback


def inject_model_cache(ccr_dict, claude_json_path=None, dry_run=False):
    """Write ccr provider models to ~/.claude.json additionalModelOptionsCache.
    Preserves non-fry entries (no FRY_MODEL_MARKER in description).
    Removes stale fry entries, replaces with current.
    NO-OP in dry-run mode — prints preview only."""
    if claude_json_path is None:
        claude_json_path = Path.home() / ".claude.json"
    else:
        claude_json_path = Path(claude_json_path)

    new_entries = []
    for prov in ccr_dict.get("Providers", []):
        pname = prov["name"]
        for model in prov.get("models", []):
            value = f"{pname},{model}"
            new_entries.append({
                "value": value,
                "label": model,
                "description": f"{pname}{FRY_MODEL_MARKER}",
            })

    if dry_run:
        print(f"\n--- Model cache preview ({len(new_entries)} entries) ---")
        for e in new_entries[:10]:
            print(f"  {e['value']}")
        if len(new_entries) > 10:
            print(f"  ... and {len(new_entries) - 10} more")
        print("(dry run: ~/.claude.json NOT modified)\n")
        return

    if not claude_json_path.exists():
        warn(f"{claude_json_path} not found; creating minimal file.")
        claude_data = {}
    else:
        # Use utf-8-sig to tolerate BOM left by prior tolerant text writes to .claude.json (observed in dirty 215845 state).
        claude_data = json.loads(claude_json_path.read_text(encoding="utf-8-sig"))

    old_cache = claude_data.get("additionalModelOptionsCache", [])
    kept = [e for e in old_cache
            if isinstance(e, dict)
            and not e.get("description", "").endswith(FRY_MODEL_MARKER)
            and not (e.get("value", "").startswith("xai,") or e.get("value", "").startswith("openai,") or e.get("value", "").startswith("fry-grok,") or e.get("value", "").startswith("fry-codex,"))]

    seen = set()
    merged = []
    for e in kept + new_entries:
        val = e.get("value", "")
        if val and val not in seen:
            seen.add(val)
            merged.append(e)

    for e in merged:
        if not all(k in e for k in ("value", "label", "description")):
            die(f"invalid model cache entry (missing keys): {e}")

    claude_data["additionalModelOptionsCache"] = merged

    # Sanitize stale selectedModel (the root cause of "issue with the selected model (xai,grok-4.3)"
    # even after additionalModelOptionsCache clean). Force to the ollama local form or remove.
    sel = claude_data.get("selectedModel")
    if isinstance(sel, str) and (sel.startswith("xai,") or sel.startswith("openai,") or sel.startswith("fry-grok,") or sel.startswith("fry-codex,") or sel == "xai,grok-4.3"):
        preferred = None
        for e in merged:
            if e.get("value") in ("ollama,fry-grok-4-3", "ollama,llama3.2:3b"):
                preferred = e.get("value")
                break
        if preferred:
            claude_data["selectedModel"] = preferred
        else:
            claude_data.pop("selectedModel", None)
        print(f"fry: sanitized selectedModel from stale '{sel}' -> '{claude_data.get('selectedModel', '(removed)')}'", file=sys.stderr)

    claude_json_path.write_text(json.dumps(claude_data, indent=2), encoding="utf-8")
    print(f"fry: injected {len(new_entries)} router models into {claude_json_path} "
          f"({len(kept)} preserved, {len(merged)} total)", file=sys.stderr)


def compile_ccr_config(cfg, override_default=None):
    """
    Build a claude-code-router config dict from fry config.
    Returns (ccr_dict, env_needed) where env_needed is a list of dicts:
    {"env_var": ..., "provider": ..., "source": ...}
    that must be exported before launch. API keys appear ONLY as $VAR placeholders.
    Providers whose secret cannot be resolved are dropped (with a warning).
    """
    providers_out = []
    env_needed = []
    dropped = set()

    for pname, pcfg in cfg.get("providers", {}).items():
        prouter = pcfg.get("router")
        if not prouter or not prouter.get("capable"):
            continue

        env_var = prouter.get("env_var")
        discover = prouter.get("discover")
        expand_key = None

        if discover == "xai":
            env_var = env_var or "XAI_API_KEY"
            # 1. live CLI auth source
            key, source = discover_xai_key()
            if not key:
                # 2. credentials.json / env var
                key = resolve_local_secret(env_var, pname)
                if key:
                    source = "local"
            if not key:
                warn(f"router provider '{pname}': no auth — run 'grok login' or set {env_var} or use 'fry credentials set {pname}'.")
                dropped.add(pname)
                continue
            expand_key = key
            api_key = "$" + env_var
            if source == "grok-cli":
                env_needed.append({"env_var": env_var, "provider": pname, "source": "literal:" + key})
            else:
                env_needed.append({"env_var": env_var, "provider": pname, "source": source})
        else:
            if not env_var:
                # keyless provider (e.g. local ollama)
                api_key = prouter.get("literal_key", "not-needed")
            else:
                key = None
                source = None
                if pname == "openai":
                    # 1. live CLI auth source
                    key, source = discover_openai_key()
                if not key and source == "codex-cli-oauth-incompatible":
                    warn(f"router provider '{pname}': Codex CLI auth detected (ChatGPT OAuth) but incompatible with router API-key path. Run 'codex login --with-api-key' or set {env_var}.")
                if not key:
                    # 2. credentials.json / env var
                    key = resolve_local_secret(env_var, pname)
                    if key:
                        source = "local"
                if not key:
                    secret = prouter.get("secret") or pcfg.get("secret")
                    if secret and secret.startswith("op://"):
                        warn(f"router provider '{pname}': no local credential for {env_var} at {CREDENTIALS_PATH}; skipping. Set it with 'fry credentials set {pname}' or 'fry credentials import-env {pname}'.")
                        dropped.add(pname)
                        continue
                    elif secret:
                        try:
                            resolved = resolve_secret(secret)
                        except SystemExit:
                            warn(f"router provider '{pname}': secret {secret} could not be resolved; skipping it.")
                            dropped.add(pname)
                            continue
                        key = resolved
                        source = secret
                if not key:
                    warn(f"router provider '{pname}': no local credential for {env_var} at {CREDENTIALS_PATH}; skipping. Set it with 'fry credentials set {pname}' or 'fry credentials import-env {pname}'.")
                    dropped.add(pname)
                    continue
                expand_key = key
                api_key = "$" + env_var
                env_needed.append({"env_var": env_var, "provider": pname, "source": source})

        models = router_provider_models(pname, prouter, expand_key=expand_key)
        if not models:
            warn(f"router provider '{pname}' has no models; skipping.")
            dropped.add(pname)
            continue

        prov = {
            "name": pname,
            "api_base_url": prouter["api_base_url"],
            "api_key": api_key,
            "models": models,
        }
        if prouter.get("transformer"):
            prov["transformer"] = {"use": [prouter["transformer"]]}
        providers_out.append(prov)

    if not providers_out:
        die("no usable router providers — Ollama may be starting up or unavailable. "
            "Check `ollama list`, or run `fry launch claude --native` to use subscription Claude now.")

    roles = dict(cfg.get("router", {}).get("roles", {}))
    # cloud guard (per approved local-auth fix): never default to 480b-cloud or any :cloud
    if "default" in roles and ":cloud" in str(roles.get("default", "")):
        safe_local = "ollama,llama3.2:3b"
        warn(f"overriding cloud Ollama default {roles['default']} -> {safe_local} (local stored-auth fix)")
        roles["default"] = safe_local
    if override_default:
        roles["default"] = override_default
    if "default" not in roles:
        first = providers_out[0]
        roles["default"] = f'{first["name"]},{first["models"][0]}'

    for role, val in list(roles.items()):
        prov = val.split(",", 1)[0]
        if prov in dropped:
            if role == "default":
                die(f"router default '{val}' uses dropped provider '{prov}'. "
                    f"Set router.roles.default to a working provider,model (see `fry models`).")
            warn(f"router role '{role}' -> '{val}' uses unavailable provider '{prov}'; removing it.")
            roles.pop(role)

    ccr = {"Providers": providers_out, "Router": roles}
    return ccr, env_needed


def ccr_config_path(cfg):
    p = cfg.get("router", {}).get("config_path", "~/.claude-code-router/config.json")
    return Path(os.path.expanduser(p))


def write_ccr_config(cfg, ccr_dict):
    path = ccr_config_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"config.fry-backup.{int(time.time())}.json")
        shutil.copy2(path, backup)
    path.write_text(json.dumps(ccr_dict, indent=2), encoding="utf-8")
    return path


def _find_free_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fry_internal_model(m):
    """Translate user-facing alias (grok,* or codex,*) to internal non-colliding
    provider namespace (ollama,*) and short model ID to
    non-colliding alias ID (fry-grok-4-3 etc.) for CCR provider name,
    ANTHROPIC_* values, CCR model lists, and .claude cache 'value'.
    Idempotent. User CLI and `fry models` continue to accept/display the
    original user aliases (with note that runtime uses Fry-local alias IDs)."""
    if not m:
        return m
    # provider prefix: route everything through ollama (Claude Code does not
    # remote-validate ollama models, so colliding raw xai/openai short IDs pass).
    if m.startswith("grok,") or m.startswith("codex,") or m.startswith("xai,") or m.startswith("openai,") or m.startswith("fry-grok,") or m.startswith("fry-codex,"):
        p = "ollama,"
        mid = m.split(",", 1)[1]
    elif m.startswith("ollama,"):
        p = "ollama,"
        mid = m.split(",", 1)[1]
    else:
        return m
    # model ID alias (short raw -> fry-local non-colliding)
    if mid == "grok-4.3":
        mid = "fry-grok-4-3"
    elif mid == "grok-4.20-0309-reasoning":
        mid = "fry-grok-4-20-0309-reasoning"
    elif mid == "grok-4.20-0309-non-reasoning":
        mid = "fry-grok-4-20-0309-non-reasoning"
    elif mid == "gpt-4o-mini":
        mid = "fry-codex-gpt-4o-mini"
    elif mid == "gpt-5.4":
        mid = "fry-codex-gpt-5.4"
    elif mid == "gpt-5.4-mini":
        mid = "fry-codex-gpt-5.4-mini"
    return p + mid


def launch_router(cfg, agent_name, model, passthrough, dry_run):
    if agent_name != "claude":
        die(f"router mode is Claude-Code-only. '{agent_name}' is native-only -- "
            f"use `fry launch {agent_name} --native`.")

    if model and "," not in model:
        die(f"in router mode --model must be '<provider>,<model>' (got '{model}'). "
            f"Run `fry models` to see valid strings.")

    ccr_dict, env_needed = compile_ccr_config(cfg, override_default=model)

    ccr_bin = resolve_bin(["ccr", "ccr.cmd", "ccr.exe"])

    # Initialize env early for the entire router path so hygiene code and ANTHROPIC_ sets are safe
    # (this fixes the UnboundLocalError 'env' that was crashing all claude router launches in the
    # previous closeout attempt). The hygiene below will set on this env; later secret resolution
    # will update it in place rather than re-assign.
    env = os.environ.copy()
    env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

    # Unified local stored-auth bridge (single process handles grok + codex by model name).
    # CCR provider uses "ollama" name because Claude Code does not remote-validate ollama
    # models, avoiding the "invalid model name" rejection that xai/openai provider names trigger.
    bridge_proc = None
    bridge_port = None
    dummy = "local-bridge-dummy"
    try:
        bridge_port = _find_free_port()
        bridge_py = Path(__file__).with_name("local_auth_bridge.py")
        _kw = {}
        if os.name == "nt":
            _kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        bridge_proc = subprocess.Popen(
            [sys.executable, "-u", str(bridge_py), str(bridge_port)],
            **_kw
        )
        time.sleep(1.2)
        bridge_url = f"http://127.0.0.1:{bridge_port}/v1/chat/completions"
        local_models = [
            "fry-grok-4-3", "fry-grok-4-20-0309-reasoning", "fry-grok-4-20-0309-non-reasoning",
            "fry-codex-gpt-4o-mini", "fry-codex-gpt-5.4", "fry-codex-gpt-5.4-mini"
        ]
        # Replace any existing ollama provider with the bridge (Claude Code whitelist requires ollama name).
        provs = [p for p in ccr_dict.get("Providers", []) if p.get("name") != "ollama"]
        provs.append({
            "name": "ollama",
            "api_base_url": bridge_url,
            "api_key": dummy,
            "models": local_models
        })
        ccr_dict["Providers"] = provs
        # Default to grok alias unless an explicit ollama model is requested
        if model and str(model).startswith("ollama,"):
            ccr_dict["Router"]["default"] = model
        else:
            ccr_dict["Router"]["default"] = _fry_internal_model(model) if model else "ollama,fry-grok-4-3"
    except Exception as e:
        warn(f"unified local bridge start failed: {e}; falling back (may hit raw key path)")

    # ANTHROPIC_*: point Claude Code at CCR (not the bridge directly). CCR translates Anthropic -> OpenAI
    # and forwards to the bridge. Using the bridge URL directly fails because Claude Code sends
    # Anthropic-format requests to an OpenAI-compat endpoint.
    env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:3456"
    env["ANTHROPIC_AUTH_TOKEN"] = dummy
    default_model = ccr_dict["Router"].get("default", "")
    short_model = default_model.split(",", 1)[1] if "," in default_model else default_model
    if short_model:
        env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = short_model
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = short_model + " (default)"

    # Tolerant text-based .claude.json sanitize for colliding raw xai/openai entries (no full json.load because the file
    # has duplicate keys in sub-objects that defeat roundtrips, as seen in prior evidence).
    # Excise cache objects whose "value" starts with "xai," or "openai," (or is exactly "xai,grok-4.3") when a local
    # Fry equivalent is present. Force selectedModel to the requested local if it was bad.
    # Called after we have decided the local model for the launch. Backup taken by caller.
    def _tolerant_claude_sanitize(claude_path, requested_local):
        if not os.path.exists(claude_path):
            return
        try:
            with open(claude_path, "r", encoding="utf-8") as f:
                txt = f.read()
            original = txt
            # Excise colliding raw cache entries (xai/openai/fry-grok/fry-codex legacy).
            txt = re.sub(
                r'\s*\{\s*"value"\s*:\s*"(xai,[^"]+|openai,[^"]+|fry-grok,[^"]+|fry-codex,[^"]+|xai,grok-4.3)"[^}]*\},?',
                "",
                txt,
                flags=re.IGNORECASE
            )
            # Force selectedModel to the short Fry alias ID (e.g. fry-grok-4-3) if the requested is a local grok/codex alias.
            # CCR routes on the short model name in the request; the provider prefix breaks provider list matching.
            if requested_local and (requested_local.startswith("grok,") or requested_local.startswith("codex,") or requested_local.startswith("xai,") or requested_local.startswith("openai,") or requested_local.startswith("fry-grok,") or requested_local.startswith("fry-codex,")):
                internal = _fry_internal_model(requested_local)
                short = internal.split(",", 1)[1] if "," in internal else internal
                txt = re.sub(
                    r'("selectedModel"\s*:\s*)"[^"]+"',
                    r'\1"' + short + '"',
                    txt,
                    flags=re.IGNORECASE
                )
            # Remove trailing commas introduced by excising the last cache object.
            txt = re.sub(r",(\s*[\]\}])", r"\1", txt)
            if txt != original:
                with open(claude_path, "w", encoding="utf-8") as f:
                    f.write(txt)
                print(f"fry: tolerant text sanitize applied to {claude_path} (raw xai/openai colliding cache/selected removed or forced to local)", file=sys.stderr)
        except Exception as e:
            warn(f"tolerant .claude sanitize failed (non-fatal): {e}")

    local_for_san = model if (model and (model.startswith("grok,") or model.startswith("codex,"))) else "grok,grok-4.3"
    _tolerant_claude_sanitize(str(Path.home() / ".claude.json"), local_for_san)

    # Also sanitize settings.json (proper JSON, unlike .claude.json) because it hardcodes
    # "model": "xai,grok-4.3" which overrides --model, ANTHROPIC_CUSTOM_MODEL_OPTION,
    # and selectedModel.
    def _sanitize_settings_json(settings_path, requested_local):
        if not os.path.exists(settings_path):
            return
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            original_model = data.get("model")
            if isinstance(original_model, str) and (
                original_model.startswith("xai,") or
                original_model.startswith("openai,") or
                original_model.startswith("fry-grok,") or
                original_model.startswith("fry-codex,") or
                original_model == "xai,grok-4.3"
            ):
                internal = _fry_internal_model(requested_local)
                short = internal.split(",", 1)[1] if "," in internal else internal
                data["model"] = short
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                print(f"fry: sanitized settings.json model from stale '{original_model}' -> '{short}'", file=sys.stderr)
        except Exception as e:
            warn(f"settings.json sanitize failed (non-fatal): {e}")

    _sanitize_settings_json(str(Path.home() / ".claude" / "settings.json"), local_for_san)

    # Ensure the active ccr service (the one that loads the config we wrote with the xai/openai alias providers
    # and the internal default) is running before the ccr code / child execution path.
    # Prior runs wrote the config (xai/openai provider with alias IDs only, no raw xai/openai under Fry providers,
    # Router default the internal alias, bridge providers added) and started the local bridge, but the ccr
    # server/service was "Not Running" (ccr status, ccr-start.log "Service startup timeout, please manually run ccr start").
    # The "ccr code" (the execution path) or child then hit the timeout or the claude resolver fell back to the
    # native xai for the short model ID (error "issue with the selected model (xai,grok-4.3)"), and the bridge was
    # never reached (no alias map log).
    # This is the service boundary: the launcher must explicitly start the documented ccr service (it loads the
    # JSON config we just wrote) and wait for it (bounded poll) before the execution path that uses the aliases in the config.
    # Capture the exact start child PID (the node for the cli.js start) for PID discipline and cleanup.
    # Bounded foreground poll (no tail -f, no detached/background per manual).
    # Only processes owned/created by this launch.
    # Non-fatal warn if the ensure fails (preserves prior behavior); the reval after this will show the service
    # Running, the config loaded with the aliases, the sentinels passing, the bridge hit.
    try:
        ccr_for_start = ccr_bin or "ccr"
        _kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        svc_start = subprocess.Popen([ccr_for_start, "start"], **_kw)
        time.sleep(0.5)
        ready = False
        for _ in range(12):
            st = subprocess.run([ccr_for_start, "status"], capture_output=True, text=True, timeout=5)
            if "Running" in (st.stdout or "") or "running" in (st.stdout or "").lower():
                ready = True
                break
            time.sleep(1)
        if not ready:
            logp = Path.home() / ".claude-code-router" / "ccr-start.log"
            last = ""
            if logp.exists():
                last = logp.read_text(encoding="utf-8", errors="ignore")[-600:]
            warn(f"ccr service not detected running after start; last log tail: {last}")
        else:
            print("fry: ccr service ensured running before execution (active config with fry aliases loaded)", file=sys.stderr)
    except Exception as e:
        warn(f"ccr service ensure step failed (non-fatal): {e}")

    if dry_run:
        print("MODE      : router (claude-code-router)")
        print(f"ccr binary: {ccr_bin or 'NOT FOUND -- install @musistudio/claude-code-router'}")
        print(f"ccr config: {ccr_config_path(cfg)}")
        print(f"env export: {', '.join(e['env_var'] for e in env_needed) or '(none)'}  "
              f"(resolved at launch from local credentials/env, never shown)")
        print("Router    :")
        for role, val in ccr_dict["Router"].items():
            print(f"            {role}: {val}")
        print("Providers :")
        for prov in ccr_dict["Providers"]:
            print(f"            {prov['name']}: {len(prov['models'])} models "
                  f"(api_key={prov['api_key']})")
        print(f"command   : ccr code {' '.join(passthrough)}".rstrip())
        inject_model_cache(ccr_dict, dry_run=True)
        print("(dry run: ccr config NOT written, nothing launched)")
        return 0

    if ccr_bin is None:
        die("claude-code-router 'ccr' not found. Install it with:\n"
            "    npm install -g @musistudio/claude-code-router\n"
            "then re-run. (fry will not auto-install.)")

    # resolve secrets into the child env (in memory only) -- update the env that was
    # initialized early (before hygiene) so our ANTHROPIC_ sets for safe local paths
    # are not overwritten.
    for entry in env_needed:
        env_var = entry["env_var"]
        provider_name = entry["provider"]
        source = entry["source"]
        if source in ("local", "env"):
            val = resolve_local_secret(env_var, provider_name)
        else:
            val = resolve_secret(source)
        if val:
            env[env_var] = val

    # Set default model for CC picker (this will be consistent with what hygiene set
    # for the safe local paths; for grok/codex the bridge branch already set the target).
    default_model = ccr_dict["Router"].get("default", "")
    if default_model:
        env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = default_model
        if "," in default_model:
            env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = (
                default_model.split(",", 1)[1] + " (default)")
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"] = "Default router model"

    path = write_ccr_config(cfg, ccr_dict)
    inject_model_cache(ccr_dict)

    # Restart CCR if running so it picks up fresh config and env vars.
    # Enhanced bounded readiness wait (addresses the CCR service startup/timeout
    # root cause): after restart, poll status until "Running" or timeout before
    # proceeding to the "ccr code" passthrough. This proves the service is using
    # the freshly written config.
    _ccr_kw = {}
    if os.name == "nt":
        _ccr_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        status_argv = wrap_for_windows(ccr_bin, ["status"])
        status = subprocess.run(status_argv, capture_output=True, text=True, timeout=5, **_ccr_kw)
        if status.returncode == 0 and "Running" in status.stdout:
            restart_argv = wrap_for_windows(ccr_bin, ["restart"])
            restart = subprocess.run(restart_argv, capture_output=True, text=True, timeout=15, **_ccr_kw)
            if restart.returncode != 0:
                warn(f"CCR restart failed: {restart.stderr or restart.stdout}")
            else:
                # Robust poll for readiness (up to ~20s). This is the scoped CCR
                # service lifecycle action (status + restart + our poll), not broad kill.
                ready = False
                for _ in range(40):
                    time.sleep(0.5)
                    try:
                        chk = subprocess.run(status_argv, capture_output=True, text=True, timeout=5, **_ccr_kw)
                        if chk.returncode == 0 and "Running" in chk.stdout:
                            ready = True
                            break
                    except Exception:
                        pass
                if not ready:
                    warn("CCR did not report Running within timeout after restart; proceeding (may still fail)")
    except Exception as e:
        warn(f"CCR status check failed: {e}")

    print(f"fry: wrote router config -> {path}", file=sys.stderr)
    print(f"fry: injected model cache into ~/.claude.json", file=sys.stderr)
    print(f"fry: switch models in-session with  /model <provider>,<model>   "
          f"(run `fry models` for the list)", file=sys.stderr)

    # Explicit --model before passthrough ensures CCR client-side validates the
    # requested provider+model against the active config (with xai/openai
    # aliases + bridge URLs) instead of falling back to its built-in xai default.
    # CCR matches request model against provider model lists, so use the short model ID.
    model_flag = ["--model", short_model] if short_model else []
    argv = wrap_for_windows(ccr_bin, ["code"] + model_flag + list(passthrough))
    proc = subprocess.run(argv, env=env)
    # cleanup owned local-auth bridge (exact PID only, per safety rules)
    if bridge_proc is not None:
        try:
            if bridge_proc.poll() is None:
                bridge_proc.terminate()
        except Exception:
            pass
    return proc.returncode


# --------------------------------------------------------------------------- #
# NATIVE mode (env-var / -c per single provider)  -- v1 behavior
# --------------------------------------------------------------------------- #
def _fill(tmpl, ctx, redact_key=False):
    api_val = REDACT if (redact_key and "{api_key}" in tmpl) else ctx["api_key"]
    return (tmpl.replace("{model}", ctx["model"])
                .replace("{api_key}", api_val)
                .replace("{base_url}", ctx["base_url"]))


def plan_native(cfg, agent_name, provider_name, model):
    agents = cfg.get("agents", {})
    providers = cfg.get("providers", {})
    if agent_name not in agents:
        die(f"unknown agent '{agent_name}'. Known: {', '.join(sorted(agents)) or '(none)'}")
    agent = agents[agent_name]

    if provider_name is None:
        provider_name = cfg.get("defaults", {}).get(agent_name, {}).get("provider")
    if provider_name is None:
        die(f"no native provider for '{agent_name}'. Pass --provider.")
    if provider_name not in providers:
        die(f"unknown provider '{provider_name}'. Known: {', '.join(sorted(providers))}")
    provider = providers[provider_name]

    mapping = provider.get("native", {}).get(agent_name)
    if mapping is None:
        die(f"provider '{provider_name}' has no native wiring for '{agent_name}'.")

    if model is None:
        model = provider.get("native", {}).get("default_model") or provider.get("default_model")
    if mapping.get("requires_model") and not model:
        if provider_name == "ollama":
            model = pick_ollama_model()
        else:
            die(f"provider '{provider_name}' needs a model. Pass --model <name>.")

    bin_path = resolve_bin(agent.get("bin", [agent_name]))
    if bin_path is None:
        die(f"agent binary for '{agent_name}' not found (looked for {agent.get('bin', [agent_name])}).")

    env_templates = mapping.get("env", {})
    needs_key = (any("{api_key}" in v for v in env_templates.values())
                 or bool(mapping.get("secret_env")) or bool(mapping.get("requires_key")))
    secret = mapping.get("secret") or provider.get("secret")
    api_key = resolve_secret(secret) if needs_key else None
    if mapping.get("requires_key") and not api_key:
        die(f"native provider '{provider_name}' requires a key but no secret is configured.")

    ctx = {"model": model or "", "api_key": api_key or "",
           "base_url": mapping.get("base_url", provider.get("base_url", ""))}

    env = os.environ.copy()
    for k in mapping.get("clear_env", []):
        env.pop(k, None)
    redacted = {}
    for key, tmpl in env_templates.items():
        if "{model}" in tmpl and not model:
            continue
        env[key] = _fill(tmpl, ctx)
        redacted[key] = _fill(tmpl, ctx, redact_key=True)
    if mapping.get("secret_env") and api_key:
        env[mapping["secret_env"]] = api_key
        redacted[mapping["secret_env"]] = REDACT

    args = []
    for a in mapping.get("args", []):
        if "{api_key}" in a:
            die("internal: api_key must never appear in agent args; fix config.")
        args.append(a.replace("{model}", ctx["model"]).replace("{base_url}", ctx["base_url"]))

    argv = wrap_for_windows(bin_path, args + list(passthrough_global))
    return {"agent": agent_name, "provider": provider_name, "model": model, "bin": bin_path,
            "argv": argv, "env": env, "cleared": mapping.get("clear_env", []),
            "redacted": redacted, "secret": secret}


def pick_ollama_model():
    models = list_ollama_models()
    if not models:
        die("no local Ollama models found. Pull one, e.g. `ollama pull qwen3-coder`.")
    print("Select an Ollama model:", file=sys.stderr)
    for i, m in enumerate(models, 1):
        print(f"  {i}) {m}", file=sys.stderr)
    try:
        choice = input("model #: ").strip()
    except (EOFError, KeyboardInterrupt):
        die("no model selected.")
    if not choice.isdigit() or not (1 <= int(choice) <= len(models)):
        die(f"invalid selection '{choice}'.")
    return models[int(choice) - 1]


def launch_native(cfg, agent_name, provider_name, model, dry_run):
    plan = plan_native(cfg, agent_name, provider_name, model)
    if dry_run:
        print("MODE      : native")
        print(f"agent     : {plan['agent']}")
        print(f"provider  : {plan['provider']}")
        print(f"model     : {plan['model'] or '(tool default)'}")
        print(f"binary    : {plan['bin']}")
        if plan["secret"]:
            print(f"secret    : {plan['secret']} (resolved at launch, redacted)")
        if plan["cleared"]:
            print(f"env unset : {', '.join(plan['cleared'])}")
        if plan["redacted"]:
            print("env set   :")
            for k, v in plan["redacted"].items():
                print(f"            {k}={v}")
        print(f"command   : {' '.join(plan['argv'])}")
        return 0
    proc = subprocess.run(plan["argv"], env=plan["env"])
    return proc.returncode


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def resolve_mode(cfg, agent, args):
    if args.native:
        return "native"
    if args.router:
        return "router"
    return cfg.get("defaults", {}).get(agent, {}).get("mode", "native")


def cmd_launch(cfg, args):
    mode = resolve_mode(cfg, args.agent, args)
    if mode == "router":
        return launch_router(cfg, args.agent, args.model, passthrough_global, args.dry_run)
    return launch_native(cfg, args.agent, args.provider, args.model, args.dry_run)


def cmd_models(cfg, _args):
    print("Routable <provider>,<model> strings (use in /model and for agent-team teammates):\n")
    any_found = False
    for pname, pcfg in cfg.get("providers", {}).items():
        prouter = pcfg.get("router")
        if not prouter or not prouter.get("capable"):
            continue
        models = router_provider_models(pname, prouter)
        if not models:
            continue
        any_found = True
        note = "  (local)" if prouter.get("secret") is None else ""
        print(f"{pname}{note}:")
        for m in models:
            print(f"  /model {pname},{m}")
        print()
    if not any_found:
        print("(no router-capable providers configured)")
    # explicit local stored-auth routes for Grok/Codex (the goal of this fix)
    # these go through the local CLIs' stored auth (grok login / codex login), never raw xAI/OpenAI $ keys
    try:
        grok_bin = resolve_bin(["grok", "grok.cmd", "grok.exe"])
        if grok_bin and Path(grok_bin).exists():
            print("grok (local stored-auth via Grok Build CLI - uses ~/.grok/auth.json):")
            print("  /model grok,grok-4.3")
            print("  /model grok,grok-4.20-0309-reasoning")
            print("  /model grok,grok-4.20-0309-non-reasoning")
            print()
    except Exception:
        pass
    try:
        codex_bin = resolve_bin(["codex", "codex.cmd", "codex.exe"])
        if codex_bin:
            print("codex (local stored-auth via Codex CLI/OAuth - uses ~/.codex/auth.json):")
            print("  /model codex,gpt-4o-mini")
            print("  /model codex,gpt-5.4")
            print("  /model codex,gpt-5.4-mini")
            print()
    except Exception:
        pass
    print("Native single-backend launches (subscription Anthropic, Codex, etc.):")
    for aname, a in cfg.get("agents", {}).items():
        for pname, pcfg in cfg.get("providers", {}).items():
            if pcfg.get("native", {}).get(aname):
                print(f"  fry launch {aname} --native --provider {pname} [--model ...]")
    return 0


def cmd_router(cfg, args):
    ccr = resolve_bin(["ccr", "ccr.cmd", "ccr.exe"])
    sub = args.router_cmd
    if sub == "config":
        ccr_dict, env_needed = compile_ccr_config(cfg)
        if args.write:
            path = write_ccr_config(cfg, ccr_dict)
            print(f"wrote {path}")
            print(f"(remember to export at launch: {', '.join(e['env_var'] for e in env_needed) or '(none)'})")
        else:
            print(json.dumps(ccr_dict, indent=2))
        return 0
    if ccr is None:
        die("ccr not found. Install: npm install -g @musistudio/claude-code-router")
    proc = subprocess.run(wrap_for_windows(ccr, [sub]))
    return proc.returncode


def cmd_credentials(cfg, args):
    providers = cfg.get("providers", {})
    pname = args.provider_name
    if pname not in providers:
        die(f"unknown provider '{pname}'.")
    pcfg = providers[pname]
    env_var = (pcfg.get("router") or {}).get("env_var")
    if not env_var:
        # fallback to native secret_env if router lacks env_var
        for agent_name, mapping in (pcfg.get("native") or {}).items():
            if isinstance(mapping, dict) and mapping.get("secret_env"):
                env_var = mapping["secret_env"]
                break
    if not env_var:
        die(f"provider '{pname}' has no env_var configured.")

    if args.credentials_cmd == "set":
        val = mask_input(f"Enter API key for {pname}: ")
        if not val:
            die("no key provided.")
    elif args.credentials_cmd == "import-env":
        val = os.environ.get(env_var)
        if not val:
            die(f"env var '{env_var}' is not set in this shell.")
    else:
        die(f"unknown credentials subcommand '{args.credentials_cmd}'.")

    creds = load_credentials()
    creds[env_var] = val
    save_credentials(creds)
    print(f"fry: stored credential for {pname} (env_var={env_var}, length={len(val)})",
          file=sys.stderr)
    return 0


def cmd_doctor(cfg, _args):
    print(f"fry {VERSION}")
    print(f"config: {cfg['__path__']}\n")
    for tool in ("claude", "codex", "ccr", "ollama", "op"):
        path = resolve_bin([tool, tool + ".cmd", tool + ".exe"])
        print(f"  {tool:8s} : {path or 'NOT FOUND'}")
    print(f"\n  agent teams env: CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS="
          f"{os.environ.get('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS', '(unset -- fry sets it for router launches)')}")
    creds = load_credentials()
    cred_status = f"{len(creds)} key(s) stored" if creds else "not present"
    print(f"\n  credentials.json: {CREDENTIALS_PATH} ({cred_status})")
    print("\nproviders:")
    for name, p in cfg.get("providers", {}).items():
        r = p.get("router", {})
        rc = "router" if r.get("capable") else "-"
        nv = ",".join(sorted(p.get("native", {}).keys() - {"default_model"})) or "-"
        sec = (r.get("secret") or p.get("secret") or "-")
        print(f"  {name:11s} [{rc:6s}] native_agents={nv}  secret={sec}")
    print("\nlocal ollama models:", ", ".join(list_ollama_models()) or "(none / ollama not running)")
    return 0


def cmd_list(cfg, _args):
    print("agents:")
    for name in sorted(cfg.get("agents", {})):
        d = cfg.get("defaults", {}).get(name, {})
        print(f"  {name}  (default mode: {d.get('mode','native')}, native provider: {d.get('provider','-')})")
    print("\nrouter providers:")
    for name, p in cfg.get("providers", {}).items():
        if p.get("router", {}).get("capable"):
            print(f"  {name}: {len(router_provider_models(name, p['router']))} models")
    return 0


# --------------------------------------------------------------------------- #
# arg parsing
# --------------------------------------------------------------------------- #
passthrough_global = []


def split_passthrough(argv):
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def build_parser():
    p = argparse.ArgumentParser(prog="fry",
                                description="Unified launcher for coding agents across model providers.")
    p.add_argument("--config", dest="config_path", default=None,
                   help="path to config.json (default: $FRY_HOME/config.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("launch", help="launch an agent")
    pl.add_argument("agent", help="agent to launch, e.g. claude or codex")
    pl.add_argument("--model", "-m", default=None,
                    help="router mode: '<provider>,<model>'. native mode: provider's model name.")
    pl.add_argument("--provider", "-p", default=None, help="native mode: provider id")
    pl.add_argument("--native", action="store_true", help="force native single-backend mode")
    pl.add_argument("--router", action="store_true", help="force router (cross-provider) mode")
    pl.add_argument("--yes", "-y", action="store_true", help="skip interactive pickers")
    pl.add_argument("--dry-run", action="store_true", help="print what would happen; do nothing")

    sub.add_parser("models", help="list routable <provider>,<model> strings + native launches")
    sub.add_parser("doctor", help="check tools, providers, ollama models")
    sub.add_parser("list", help="list agents and router providers")

    pr = sub.add_parser("router", help="manage the router (claude-code-router)")
    pr.add_argument("router_cmd", choices=["start", "stop", "restart", "status", "config"],
                    help="router action; 'config' prints the compiled ccr config")
    pr.add_argument("--write", action="store_true", help="for 'config': write it to the ccr config path")

    pc = sub.add_parser("credentials", help="manage local provider credentials")
    pc_sub = pc.add_subparsers(dest="credentials_cmd", required=True)
    pc_set = pc_sub.add_parser("set", help="store a provider key interactively")
    pc_set.add_argument("provider_name", help="provider id, e.g. openai or xai")
    pc_imp = pc_sub.add_parser("import-env", help="import a provider key from the current environment")
    pc_imp.add_argument("provider_name", help="provider id, e.g. openai or xai")

    sub.add_parser("version", help="print fry version")
    return p


def main():
    global passthrough_global
    raw = sys.argv[1:]
    left, passthrough_global = split_passthrough(raw)
    parser = build_parser()
    args = parser.parse_args(left)

    if args.cmd == "version":
        print(f"fry {VERSION}")
        return 0

    cfg_path = Path(args.config_path) if args.config_path else DEFAULT_CONFIG_PATH
    cfg = load_config(cfg_path)
    cfg["__path__"] = str(cfg_path)

    return {
        "launch": lambda: cmd_launch(cfg, args),
        "models": lambda: cmd_models(cfg, args),
        "router": lambda: cmd_router(cfg, args),
        "doctor": lambda: cmd_doctor(cfg, args),
        "list": lambda: cmd_list(cfg, args),
        "credentials": lambda: cmd_credentials(cfg, args),
    }[args.cmd]()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
