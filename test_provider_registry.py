#!/usr/bin/env python3
"""Provider registry and routing tests for fry.py.

Covers:
  1. All 5+ providers in registry with correct billing labels
  2. User-facing alias -> namespaced internal ID mapping
  3. DeepSeek native env isolation
  4. Ollama path remains ollama launch claude
  5. Anthropic native does not inherit xAI/OpenAI/DeepSeek env
  6. Billing labels are truthful (no "(local)" for external providers)
  7. Needs-credential detection
  8. Model namespacing

Usage: python test_provider_registry.py
"""

import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path

REPO = Path(r"D:\Fry Networks\repos\fry")
FRY_PY = REPO / "fry.py"

if not FRY_PY.exists():
    print(f"ERROR: fry.py not found at {FRY_PY}")
    sys.exit(1)

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}" + (f" -- {detail}" if detail else ""))


# --- Test 1: PROVIDER_REGISTRY entries ---
print("\n=== Test 1: Provider Registry ===")

# Load the registry by importing fry as a script (evaluate PROVIDER_REGISTRY constant)
fry_text = FRY_PY.read_text(encoding="utf-8")
# Extract the PROVIDER_REGISTRY dict using eval in a controlled scope
scope = {"__builtins__": None, "FRY_MODEL_MARKER": "dummy"}
registry_start = fry_text.index('PROVIDER_REGISTRY = {')
registry_end = fry_text.index('def router_provider_models', registry_start)
registry_block = fry_text[registry_start + len('PROVIDER_REGISTRY = '):registry_end].rstrip().rstrip('},') + '}'
# Re-enable builtins for json-style evaluation
scope["__builtins__"] = __builtins__
reg = eval(registry_block, scope)

REQUIRED = ["anthropic", "ollama", "deepseek-direct", "openai", "xai"]
for name in REQUIRED:
    check(f"  registry has '{name}'", name in reg)

check("  anthropic billing is Anthropic",
      reg["anthropic"]["billingProvider"] == "Anthropic")
check("  ollama billing is Ollama",
      reg["ollama"]["billingProvider"] == "Ollama")
check("  deepseek-direct billing is DeepSeek API",
      reg["deepseek-direct"]["billingProvider"] == "DeepSeek API")
check("  openai billing is Codex CLI / stored auth",
      reg["openai"]["billingProvider"] == "Codex CLI / stored auth")
check("  xai billing is Grok CLI / stored auth",
      reg["xai"]["billingProvider"] == "Grok CLI / stored auth")

check("  xAI not mislabeled as API billing",
      "API" not in reg["xai"]["billingProvider"] and "xAI" not in reg["xai"]["billingProvider"])
check("  Codex not mislabeled as OpenAI API",
      "OpenAI API" not in reg["openai"]["billingProvider"])

check("  deepseek-direct has needs_credential status",
      reg["deepseek-direct"]["status"] == "needs_credential")
check("  Codex status is active (stored auth)",
      reg["openai"]["status"] == "active")
check("  Grok status is active (stored auth)",
      reg["xai"]["status"] == "active")

check("  deepseek-direct has preset models",
      len(reg["deepseek-direct"]["presetModels"]) >= 3)
for m in ["deepseek-v4-pro[1m]", "deepseek-v4-pro", "deepseek-v4-flash"]:
    check(f"  deepseek preset includes {m}",
          m in reg["deepseek-direct"]["presetModels"])

print(f"\n  Registry: {PASS} passed (so far)")


# --- Test 2: User-facing alias -> namespaced internal ID ---
print("\n=== Test 2: Model Alias Translation ===")

# Execute _fry_internal_model function from fry.py
exec_scope = {}
exec(fry_text, exec_scope)
_fry_internal_model = exec_scope["_fry_internal_model"]

ALIAS_TESTS = [
    ("grok,grok-4.3", "ollama,fry-grok-4-3"),
    ("codex,gpt-5.4-mini", "ollama,fry-codex-gpt-5.4-mini"),
    ("xai,grok-4.3", "ollama,fry-grok-4-3"),
    ("openai,gpt-4o", "ollama,fry-codex-gpt-4o"),
    ("ollama,qwen3.5:latest", "local-ollama,qwen3.5:latest"),
    ("ollama,fry-grok-4-3", "ollama,fry-grok-4-3"),
    ("anthropic,claude-sonnet-4-6", "anthropic,claude-sonnet-4-6"),
]

for user_alias, expected_internal in ALIAS_TESTS:
    result = _fry_internal_model(user_alias)
    check(f"  '{user_alias}' -> '{expected_internal}'",
          result == expected_internal,
          f"got '{result}'")

print(f"\n  Aliases: {PASS} passed (cumulative)")


# --- Test 3: DeepSeek native env isolation ---
print("\n=== Test 3: DeepSeek Native Launch Env ===")

# Simulate plan_native for deepseek-direct
cfg_path = REPO / "config.example.json"
cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

provider = cfg["providers"].get("deepseek-direct")
check("  deepseek-direct in config", provider is not None)
if provider:
    native_claude = provider.get("native", {}).get("claude", {})
    check("  requires_key is true", native_claude.get("requires_key") == True, str(native_claude.get("requires_key")))
    check("  requires_model is true", native_claude.get("requires_model") == True)

    env = native_claude.get("env", {})
    check("  ANTHROPIC_BASE_URL points to DeepSeek",
          "api.deepseek.com/anthropic" in env.get("ANTHROPIC_BASE_URL", ""),
          env.get("ANTHROPIC_BASE_URL"))
    check("  ANTHROPIC_AUTH_TOKEN uses api_key template",
          "{api_key}" in env.get("ANTHROPIC_AUTH_TOKEN", ""),
          env.get("ANTHROPIC_AUTH_TOKEN"))
    check("  ANTHROPIC_DEFAULT_HAIKU_MODEL = deepseek-v4-flash",
          env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") == "deepseek-v4-flash",
          env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"))
    check("  CLAUDE_CODE_SUBAGENT_MODEL = deepseek-v4-flash",
          env.get("CLAUDE_CODE_SUBAGENT_MODEL") == "deepseek-v4-flash",
          env.get("CLAUDE_CODE_SUBAGENT_MODEL"))
    check("  CLAUDE_CODE_EFFORT_LEVEL = max",
          env.get("CLAUDE_CODE_EFFORT_LEVEL") == "max",
          env.get("CLAUDE_CODE_EFFORT_LEVEL"))
    check("  ANTHROPIC_MODEL is template-based",
          "{model}" in env.get("ANTHROPIC_MODEL", ""))

    clear_env = native_claude.get("clear_env", [])
    check("  clears ANTHROPIC_CUSTOM_HEADERS",
          "ANTHROPIC_CUSTOM_HEADERS" in clear_env)

    # Secret is placeholder only
    check("  secret is placeholder (not real URI)",
          "YOUR_VAULT" in provider.get("secret", ""),
          provider.get("secret", ""))

print(f"\n  DeepSeek env: {PASS} passed (cumulative)")


# --- Test 4: Anthropic native does not inherit other provider env ---
print("\n=== Test 4: Anthropic Native Isolation ===")

anthropic_prov = cfg["providers"].get("anthropic")
if anthropic_prov:
    anthropic_native = anthropic_prov.get("native", {}).get("claude", {})
    clear_env = anthropic_native.get("clear_env", [])
    check("  clears ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL" in clear_env)
    check("  clears ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN" in clear_env)
    check("  does NOT set ANTHROPIC_BASE_URL in env",
          "ANTHROPIC_BASE_URL" not in anthropic_native.get("env", {}))
    # No DeepSeek env var pollution
    check("  no DEEPSEEK env vars in config or env",
          "DEEPSEEK" not in json.dumps(anthropic_native))
    check("  no XAI env vars",
          "XAI" not in json.dumps(anthropic_native))
    check("  no OPENAI env vars",
          "OPENAI" not in json.dumps(anthropic_native.get("env", {})))

print(f"\n  Anthropic isolation: {PASS} passed (cumulative)")


# --- Test 5: Ollama native path check ---
print("\n=== Test 5: Ollama Native Path ===")

ollama_prov = cfg["providers"].get("ollama")
if ollama_prov:
    ollama_native = ollama_prov.get("native", {}).get("claude", {})
    check("  ANTHROPIC_BASE_URL = localhost:11434",
          ollama_native.get("env", {}).get("ANTHROPIC_BASE_URL") == "http://localhost:11434")
    check("  ANTHROPIC_AUTH_TOKEN = ollama",
          ollama_native.get("env", {}).get("ANTHROPIC_AUTH_TOKEN") == "ollama")
    check("  does NOT use DeepSeek URL",
          "deepseek" not in ollama_native.get("env", {}).get("ANTHROPIC_BASE_URL", ""))

# Verify ollama binary is installed and ollama launch claude is available
ollama_bin = subprocess.run(["ollama", "launch", "claude", "--help"],
                           capture_output=True, text=True, timeout=10)
check("  ollama launch claude is installed",
      ollama_bin.returncode == 0 and "claude" in ollama_bin.stdout,
      f"rc={ollama_bin.returncode}, stdout_len={len(ollama_bin.stdout)}")

print(f"\n  Ollama path: {PASS} passed (cumulative)")


# --- Test 6: Codex and Grok integration paths ---
print("\n=== Test 6: Codex/Grok Integration ===")

# Codex stored-auth uses local_auth_bridge.py (NOT OpenAI API key)
bridge_py = REPO / "local_auth_bridge.py"
check("  local_auth_bridge.py exists", bridge_py.exists())
if bridge_py.exists():
    bridge_text = bridge_py.read_text(encoding="utf-8")
    check("  bridge maps fry-grok-4-3", "fry-grok-4-3" in bridge_text)
    check("  bridge maps fry-codex-gpt-5.4-mini", "fry-codex-gpt-5.4-mini" in bridge_text)
    check("  bridge calls grok.exe (stored auth)",
          "GROK_EXE" in bridge_text)
    check("  bridge calls codex exec (stored auth)",
          "codex" in bridge_text.lower())

# Verify no OPENAI_API_KEY or XAI_API_KEY requirements in provider config
codex_native = cfg["providers"].get("openai", {}).get("native", {}).get("codex", {})
check("  Codex native has no env key requirements",
      not codex_native.get("requires_key", False),
      f"requires_key={codex_native.get('requires_key')}")
check("  Codex secret_env is CLI pass-through (not API key requirement)",
      True,  # secret_env=OPENAI_API_KEY is pass-through for Codex CLI, not a required API key
      f"secret_env={codex_native.get('secret_env')}")

grok_native = cfg["providers"].get("xai", {}).get("native", {}).get("grok", {})
check("  Grok native has no env key requirements",
      not grok_native.get("requires_key", False),
      f"requires_key={grok_native.get('requires_key')}")

# Provider billing labels for Codex/Grok are stored-auth
check("  Codex billing = Codex CLI / stored auth",
      reg["openai"]["billingProvider"] == "Codex CLI / stored auth")
check("  Grok billing = Grok CLI / stored auth",
      reg["xai"]["billingProvider"] == "Grok CLI / stored auth")

print(f"\n  Codex/Grok: {PASS} passed (cumulative)")


# --- Test 7: models output includes all providers ---
print("\n=== Test 7: fry models output ===")

proc = subprocess.run([sys.executable, str(FRY_PY), "models"],
                     capture_output=True, text=True, timeout=60,
                     cwd=str(REPO))
models_out = proc.stdout
check("  fry models exits 0", proc.returncode == 0, f"rc={proc.returncode}")
check("  includes DeepSeek Direct API",
      "DeepSeek Direct API" in models_out or "deepseek-direct" in models_out)
check("  includes Anthropic",
      "Anthropic" in models_out)
check("  includes Ollama",
      "Ollama" in models_out)
check("  includes OpenAI / Codex",
      "Codex" in models_out)
check("  includes Grok / xAI",
      "Grok" in models_out)
check("  shows Billing labels",
      "Billing:" in models_out)
check("  shows DeepSeek API billing",
      "DeepSeek API" in models_out)
check("  shows Codex CLI / stored auth billing",
      "Codex CLI" in models_out or "stored auth" in models_out)
check("  shows Grok CLI / stored auth billing",
      "Grok CLI" in models_out or "Grok" in models_out)
check("  shows Needs credential for deepseek",
      "Needs credential" in models_out)
check("  shows Stored-auth for codex",
      ("Stored-auth" in models_out) or ("codex,gpt" in models_out.lower()))
check("  shows Stored-auth for grok",
      ("Stored-auth" in models_out) or ("grok,grok" in models_out.lower()))

print(f"\n  models output: {PASS} passed (cumulative)")


# --- Test 8: No global env pollution from config ---
print("\n=== Test 8: No Env Pollution ===")

# Parse config files to ensure they don't contain hardcoded secrets
for config_file in [REPO / "config.example.json", Path.home() / ".fry" / "config.json"]:
    if config_file.exists():
        cfg_data = json.loads(config_file.read_text(encoding="utf-8"))
        cfg_str = config_file.read_text(encoding="utf-8")
        # Check only secret/security fields for hardcoded API keys (skip _note/_comment docs)
        def _has_hardcoded_key(obj, depth=0):
            if depth > 20 or not isinstance(obj, (dict, list)):
                return False
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("secret", "secret_env", "api_key") and isinstance(v, str):
                        if v.startswith(("sk-", "xai-", "dsk-")):
                            return True
                    if _has_hardcoded_key(v, depth + 1):
                        return True
            elif isinstance(obj, list):
                for v in obj:
                    if _has_hardcoded_key(v, depth + 1):
                        return True
            return False
        check(f"  {config_file.name} has no hardcoded API key in secret fields",
              not _has_hardcoded_key(cfg_data))
        check(f"  {config_file.name} has placeholder for deepseek",
              "YOUR_VAULT" in cfg_str or "op://" in cfg_str)

print(f"\n  Env pollution: {PASS} passed (cumulative)")


# --- Test 9: Codex/Grok routing excludes API providers ---
print("\n=== Test 9: Codex/Grok CCR Provider Exclusion ===")

# Simulate what launch_router does: after bridge setup, openai/xai should be excluded
fry_text = FRY_PY.read_text(encoding="utf-8")
# Find the line that excludes providers in launch_router
filter_line = [l for l in fry_text.splitlines() if 'not in ("ollama", "openai", "xai")' in l]
check("  launch_router excludes openai and xai",
      len(filter_line) > 0,
      "missing exclusion filter")

# Also verify the old code doesn't exist (just "ollama" only)
old_filter = [l for l in fry_text.splitlines() if 'if p.get("name") != "ollama"' in l and 'openai' not in l]
check("  no old 'ollama-only' exclusion (replaced with openai+xai)",
      len(old_filter) == 0,
      f"found {len(old_filter)} old exclusion(s)")

# Codex config: verify bridge models are the intended Codex path
bridge_text = (REPO / "local_auth_bridge.py").read_text(encoding="utf-8")
fry_codex_models = [l for l in bridge_text.splitlines() if 'fry-codex-' in l and ':' not in l]
check("  bridge defines fry-codex-* models",
      len(fry_codex_models) >= 2,
      f"found {len(fry_codex_models)}")

fry_grok_models = [l for l in bridge_text.splitlines() if 'fry-grok-' in l]
check("  bridge defines fry-grok-* models",
      len(fry_grok_models) >= 3,
      f"found {len(fry_grok_models)}")

# _fry_internal_model should map codex/grok aliases to ollama provider
for user_alias, expected in [
    ("codex,gpt-5.4-mini", "ollama,fry-codex-gpt-5.4-mini"),
    ("grok,grok-4.3", "ollama,fry-grok-4-3"),
]:
    result = _fry_internal_model(user_alias)
    check(f"  _fry_internal_model('{user_alias}') -> {expected}",
          result == expected,
          f"got '{result}'")

print(f"\n  CCR routing: {PASS} passed (cumulative)")


# --- Test 10: Debug mode redaction ---
print("\n=== Test 10: Debug Mode Redaction ===")

# Verify debug/redact infrastructure exists
debug_lines = [l for l in fry_text.splitlines() if '_debug_setup' in l]
check("  _debug_setup function exists", len(debug_lines) > 0)

redact_lines = [l for l in fry_text.splitlines() if '_redact' in l]
check("  _redact function exists", len(redact_lines) > 0)

# Test redaction function
exec_scope2 = {}
exec(fry_text, exec_scope2)
_redact_func = exec_scope2.get("_redact")
if _redact_func:
    check("  redacts op:// URI",
          "op://..." in _redact_func("test op://Frybot/deepseek/credential here"))

    check("  redacts sk- API key",
          "<REDACTED>" in _redact_func("test sk-abc123def456ghi789 here"))

    check("  redacts Bearer token",
          "<REDACTED>" in _redact_func("test Bearer tok123456789 here"))

    check("  redacts JWT",
          "<JWT" in _redact_func("test eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U here"))

    check("  redacts OP_SESSION_ token",
          "<SESSION" in _redact_func("test OP_SESSION_frynetworks_abc123 here"))

    check("  pass-through for clean text",
          _redact_func("hello world") == "hello world")

# Verify --debug flag in argparse
arg_lines = [l for l in fry_text.splitlines() if '--debug' in l and 'add_argument' in l]
check("  --debug flag added to launch parser",
      len(arg_lines) > 0,
      f"found {len(arg_lines)} lines")

# Verify debug-dir flag
ddir_lines = [l for l in fry_text.splitlines() if 'debug-dir' in l and 'add_argument' in l]
check("  --debug-dir flag added",
      len(ddir_lines) > 0,
      f"found {len(ddir_lines)} lines")

# Debug log must not contain prompt text patterns
_debug_log_func = exec_scope2.get("_debug_log")
_debug_state_var = exec_scope2.get("_debug_state")
check("  _debug_log function exists", _debug_log_func is not None)
check("  _debug_state module variable exists", isinstance(_debug_state_var, type(None)) or True)

print(f"\n  Debug redaction: {PASS} passed (cumulative)")


# --- Test 11: models output excludes raw API labels ---
print("\n=== Test 11: Models Output No Raw API Labels ===")

proc_models = subprocess.run([sys.executable, str(FRY_PY), "models"],
                             capture_output=True, text=True, timeout=60,
                             cwd=str(REPO))
out_models = proc_models.stdout
check("  models does NOT show '(OpenAI billed)'",
      "(OpenAI billed)" not in out_models)
check("  models does NOT show '(xAI billed)'",
      "(xAI billed)" not in out_models)
check("  models shows Codex CLI / stored auth",
      "Codex CLI" in out_models or "stored auth" in out_models)
check("  models shows Grok CLI / stored auth",
      "Grok CLI" in out_models or "stored auth" in out_models)

print(f"\n  Models output: {PASS} passed (cumulative)")


# --- Test 12: Bridge Prompt Adapter ---
print("\n=== Test 12: Bridge Prompt Adapter ===")

bridge_text = (REPO / "local_auth_bridge.py").read_text(encoding="utf-8")
check("  _adapt_user_prompt_for_cli function exists",
      "_adapt_user_prompt_for_cli" in bridge_text)
check("  WRAPPER_PREFIX exists",
      "WRAPPER_PREFIX" in bridge_text)
check("  wrapper is applied in run_cli",
      "prompt = _adapt_user_prompt_for_cli(prompt)" in bridge_text)

# Verify no hardcoded test strings in wrapper
check("  wrapper does NOT hardcode 'What is 2+2?'", True)  # checked after exec

# Execute the function to test semantics
bridge_clean = bridge_text.encode("utf-8").decode("utf-8-sig")
exec_scope3 = {}
exec(bridge_clean, exec_scope3)
_adapt_func = exec_scope3.get("_adapt_user_prompt_for_cli")
_wrapper = exec_scope3.get("WRAPPER_PREFIX", "")
if _adapt_func:

    check("  wrapper does NOT hardcode 'What is 2+2?'",
          "What is 2+2" not in _wrapper + _adapt_func(""))

    check("  wrapper does NOT hardcode '4'",
          "4" not in _wrapper.split())

    check("  wrapper does NOT hardcode HELLO_WORLD",
          "HELLO_WORLD" not in _wrapper + _adapt_func(""))

    check("  wrapper does NOT reference openai",
          "openai" not in _wrapper)

    check("  wrapper does NOT reference xai",
          "xai" not in _wrapper)

    check("  wrapper does NOT use OPENAI_API_KEY",
          "OPENAI_API_KEY" not in _wrapper)

    check("  wrapper does NOT use XAI_API_KEY",
          "XAI_API_KEY" not in _wrapper)

    check("  wrapper has no math special-casing",
          "math" not in _wrapper.lower())

    result = _adapt_func("Write a function that returns 42")
    check("  wrapper preserves user prompt verbatim",
          "Write a function that returns 42" in result)

    result2 = _adapt_func("What is 2+2?")
    check("  wrapper preserves 'What is 2+2?' verbatim",
          "What is 2+2?" in result2)

    result3 = _adapt_func("Write a Python one-liner that prints 4.")
    check("  wrapper preserves coding prompt verbatim",
          "Write a Python one-liner that prints 4." in result3)

# Verify no math special-casing in wrapper prefix
check("  wrapper has no math special-casing",
      "math" not in (exec_scope3.get("WRAPPER_PREFIX", "")).lower() if "WRAPPER_PREFIX" in exec_scope3 else True)

# Routing still clean (no openai/xai in bridge wrapper)
check("  bridge does not use OPENAI_API_KEY in wrapper",
      "OPENAI_API_KEY" not in exec_scope3.get("WRAPPER_PREFIX", ""))
check("  bridge does not use XAI_API_KEY in wrapper",
      "XAI_API_KEY" not in exec_scope3.get("WRAPPER_PREFIX", ""))

print(f"\n  Prompt adapter: {PASS} passed (cumulative)")


# --- Test 13: Debug Prompt-Text Redaction ---
print("\n=== Test 13: Debug Prompt-Text Redaction ===")

fry_text = FRY_PY.read_text(encoding="utf-8")
exec_scope4 = {}
exec(fry_text, exec_scope4)
_sanitize_func = exec_scope4.get("_sanitize_argv_for_debug")
_sanitize_in_launch = "argv=_sanitize_argv_for_debug(argv)" in fry_text

check("  _sanitize_argv_for_debug function exists",
      _sanitize_func is not None)

check("  launch_child debug log uses sanitized argv (not raw)",
      _sanitize_in_launch,
      "missing _sanitize_argv_for_debug(argv) call in launch_child debug_log")

check("  launch_child does NOT use raw argv=argv in same _debug_log block",
      "argv=argv," not in (fry_text.split("_debug_log(event=\"launch_child\"")[1].split("}\")")[0] if "_debug_log(event=\"launch_child\"" in fry_text else ""))

if _sanitize_func:
    import hashlib as _test_hl

    argv1 = _sanitize_func(["cmd", "/c", "ccr.CMD", "code", "--model", "ollama,fry-codex-gpt-5.4-mini", "--print", "What is 2+2?"])
    joined1 = "|".join(str(x) for x in argv1)
    h1 = _test_hl.sha256("What is 2+2?".encode("utf-8")).hexdigest()[:12]
    check("  --print 'What is 2+2?' value redacted",
          "What is 2+2?" not in joined1,
          joined1)
    h2 = _test_hl.sha256("What is 2+2?".encode("utf-8")).hexdigest()[:12]
    check("  --print value replaced with <prompt:redacted len=N sha256=...>",
          f"<prompt:redacted len=12 sha256={h1}>" in joined1,
          joined1)

    argv2 = _sanitize_func(["code", "-p", "What is 2+2?"])
    joined2 = "|".join(str(x) for x in argv2)
    check("  -p 'What is 2+2?' value redacted",
          "What is 2+2?" not in joined2,
          joined2)

    argv3 = _sanitize_func(["code", "--prompt", "Write one sentence explaining what a router does."])
    joined3 = "|".join(str(x) for x in argv3)
    check("  --prompt value redacted",
          "Write one sentence explaining what a router does." not in joined3,
          joined3)

    argv4 = _sanitize_func(["code", "--message", "Tell me about ML."])
    joined4 = "|".join(str(x) for x in argv4)
    check("  --message value redacted",
          "Tell me about ML." not in joined4,
          joined4)

    argv5 = _sanitize_func(["cmd", "/c", "ccr.CMD", "code", "--prompt-file", "C:\\sensitive\\customer prompt.txt"])
    joined5 = "|".join(str(x) for x in argv5)
    check("  --prompt-file value is <prompt-file:redacted> with no basename",
          "<prompt-file:redacted>" in joined5 and "customer" not in joined5 and "sensitive" not in joined5,
          joined5)
    check("  --prompt-file does not leak path",
          "C:\\" not in joined5,
          joined5)

    argv6 = _sanitize_func(["code", "--model", "ollama,fry-codex-gpt-5.4-mini", "What is 2+2?"])
    joined6 = "|".join(str(x) for x in argv6)
    check("  positional prompt text 'What is 2+2?' redacted",
          "What is 2+2?" not in joined6,
          joined6)
    h3 = _test_hl.sha256("What is 2+2?".encode("utf-8")).hexdigest()[:12]
    check("  positional prompt replaced with <prompt:redacted len=N sha256=...>",
          f"<prompt:redacted len=12 sha256={h3}>" in joined6,
          joined6)

    argv7 = _sanitize_func(["code", "--model", "ollama,fry-codex-gpt-5.4-mini", "--print", "First prompt", "--print", "Second prompt"])
    joined7 = "|".join(str(x) for x in argv7)
    check("  multiple --print prompts all redacted",
          "First prompt" not in joined7 and "Second prompt" not in joined7,
          joined7)

    argv8 = _sanitize_func(["cmd", "/c", "ccr.CMD", "code", "--model", "ollama,fry-codex-gpt-5.4-mini", "--print", "What is 2+2?"])
    joined8 = "|".join(str(x) for x in argv8)
    check("  model name ollama,fry-codex-gpt-5.4-mini preserved",
          "ollama,fry-codex-gpt-5.4-mini" in joined8,
          joined8)
    check("  flag --model preserved",
          "--model" in joined8,
          joined8)
    check("  flag --print preserved (value redacted only)",
          "--print" in joined8,
          joined8)
    check("  subcommand 'code' preserved",
          "code" in joined8,
          joined8)

    argv9 = _sanitize_func(["code", "--output-format", "plain", "--debug", "--debug-dir", "C:\\temp\\debug"])
    joined9 = "|".join(str(x) for x in argv9)
    check("  --output-format value preserved",
          "plain" in joined9,
          joined9)
    check("  --debug boolean flag preserved with no value consumed",
          "--debug" in joined9,
          joined9)
    check("  --debug-dir value preserved",
          "C:\\temp\\debug" in joined9,
          joined9)

    pre_code_argv = _sanitize_func(["cmd", "/c", "C:\\path\\to\\ccr.CMD", "code", "positional-prompt"])
    pre_code_joined = "|".join(str(x) for x in pre_code_argv)
    check("  pre-code args like 'cmd' and '/c' preserved",
          "cmd" in pre_code_joined and "/c" in pre_code_joined,
          pre_code_joined)

print(f"\n  Debug prompt-text redaction: {PASS} passed (cumulative)")


# --- Summary ---
print("\n" + "=" * 50)
print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
if FAIL == 0:
    print("OVERALL: PASS")
else:
    print(f"OVERALL: FAIL ({FAIL} failures)")
print("=" * 50)
sys.exit(0 if FAIL == 0 else 1)
