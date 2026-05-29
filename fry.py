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
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "0.2.0"

FRY_HOME = Path(os.environ.get("FRY_HOME", Path.home() / ".fry"))
DEFAULT_CONFIG_PATH = FRY_HOME / "config.json"
REDACT = "<redacted>"


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
    proc = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    out = []
    for ln in proc.stdout.splitlines()[1:]:
        ln = ln.strip()
        if ln:
            out.append(ln.split()[0])
    return out


# --------------------------------------------------------------------------- #
# ROUTER mode (claude-code-router)
# --------------------------------------------------------------------------- #
def router_provider_models(pname, prouter):
    """Return the model list for a router provider, expanding ollama live."""
    models = list(prouter.get("models", []))
    if prouter.get("expand") and prouter.get("kind") == "ollama":
        for m in list_ollama_models():
            if m not in models:
                models.append(m)
    return models


def compile_ccr_config(cfg, override_default=None):
    """
    Build a claude-code-router config dict from fry config.
    Returns (ccr_dict, env_needed) where env_needed is a list of (env_var, secret_ref)
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

        models = router_provider_models(pname, prouter)
        if not models:
            warn(f"router provider '{pname}' has no models; skipping.")
            dropped.add(pname)
            continue

        secret = prouter.get("secret")
        env_var = prouter.get("env_var")
        if secret is None:
            # no real key needed (e.g. local ollama) -- use a literal placeholder value
            api_key = prouter.get("literal_key", "not-needed")
        else:
            if not env_var:
                warn(f"router provider '{pname}' has a secret but no env_var; skipping.")
                dropped.add(pname)
                continue
            # verify the secret resolves now so we can drop the provider if it doesn't
            try:
                _ = resolve_secret(secret)
            except SystemExit:
                warn(f"router provider '{pname}': secret {secret} could not be resolved; skipping it.")
                dropped.add(pname)
                continue
            api_key = "$" + env_var
            env_needed.append((env_var, secret))

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
        die("no usable router providers (check secrets / models in config).")

    roles = dict(cfg.get("router", {}).get("roles", {}))
    if override_default:
        roles["default"] = override_default
    if "default" not in roles:
        # fall back to first provider's first model
        first = providers_out[0]
        roles["default"] = f'{first["name"]},{first["models"][0]}'

    # if any role points at a dropped provider, fix or fail
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


def launch_router(cfg, agent_name, model, passthrough, dry_run):
    if agent_name != "claude":
        die(f"router mode is Claude-Code-only. '{agent_name}' is native-only -- "
            f"use `fry launch {agent_name} --native`.")

    if model and "," not in model:
        die(f"in router mode --model must be '<provider>,<model>' (got '{model}'). "
            f"Run `fry models` to see valid strings.")

    ccr_dict, env_needed = compile_ccr_config(cfg, override_default=model)

    ccr_bin = resolve_bin(["ccr", "ccr.cmd", "ccr.exe"])

    if dry_run:
        print("MODE      : router (claude-code-router)")
        print(f"ccr binary: {ccr_bin or 'NOT FOUND -- install @musistudio/claude-code-router'}")
        print(f"ccr config: {ccr_config_path(cfg)}")
        print(f"env export: {', '.join(v for v, _ in env_needed) or '(none)'}  "
              f"(resolved at launch from op://, never shown)")
        print("Router    :")
        for role, val in ccr_dict["Router"].items():
            print(f"            {role}: {val}")
        print("Providers :")
        for prov in ccr_dict["Providers"]:
            print(f"            {prov['name']}: {len(prov['models'])} models "
                  f"(api_key={prov['api_key']})")
        print(f"command   : ccr code {' '.join(passthrough)}".rstrip())
        print("\n(dry run: ccr config NOT written, nothing launched)")
        return 0

    if ccr_bin is None:
        die("claude-code-router 'ccr' not found. Install it with:\n"
            "    npm install -g @musistudio/claude-code-router\n"
            "then re-run. (fry will not auto-install.)")

    # resolve secrets into the child env (in memory only)
    env = os.environ.copy()
    env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
    for env_var, secret in env_needed:
        env[env_var] = resolve_secret(secret)

    path = write_ccr_config(cfg, ccr_dict)
    print(f"fry: wrote router config -> {path}", file=sys.stderr)
    print(f"fry: switch models in-session with  /model <provider>,<model>   "
          f"(run `fry models` for the list)", file=sys.stderr)

    argv = wrap_for_windows(ccr_bin, ["code"] + list(passthrough))
    proc = subprocess.run(argv, env=env)
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
            print(f"(remember to export at launch: {', '.join(v for v,_ in env_needed) or '(none)'})")
        else:
            print(json.dumps(ccr_dict, indent=2))
        return 0
    if ccr is None:
        die("ccr not found. Install: npm install -g @musistudio/claude-code-router")
    proc = subprocess.run(wrap_for_windows(ccr, [sub]))
    return proc.returncode


def cmd_doctor(cfg, _args):
    print(f"fry {VERSION}")
    print(f"config: {cfg['__path__']}\n")
    for tool in ("claude", "codex", "ccr", "ollama", "op"):
        path = resolve_bin([tool, tool + ".cmd", tool + ".exe"])
        print(f"  {tool:8s} : {path or 'NOT FOUND'}")
    print(f"\n  agent teams env: CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS="
          f"{os.environ.get('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS', '(unset -- fry sets it for router launches)')}")
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
    }[args.cmd]()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
