# fry — unified coding-agent launcher

`fry launch <agent>` — same shape as `ollama launch claude`, but one command launches
**any agent** (Claude Code, Codex) against **any provider** (Anthropic, local Ollama,
OpenRouter, OpenAI), and gives Claude Code a **cross-provider `/model` list** you can
switch between mid-session and assign to **agent-team teammates**.

```
fry launch claude                                            # router mode (default)
fry launch claude --model openrouter,anthropic/claude-opus-4.6
fry launch claude --native --provider ollama --model qwen3-coder:latest
fry launch codex  --provider ollama --model qwen2.5-coder:14b
fry launch claude -- --dangerously-skip-permissions "do X"   # -- = passthrough to agent
```

---

## The one thing to understand first

Claude Code talks to **one backend per session**. There is no native way for `/model`
to list Anthropic + Ollama + OpenAI models at once. To get that, `fry` runs a local
**router** (claude-code-router) in front of all your providers. Then inside Claude Code:

```
/model openrouter,anthropic/claude-opus-4.6
/model ollama,qwen3-coder:latest
/model openai,gpt-5.1
```

…and the same `provider,model` strings can be handed to teammates ("spawn a teammate on
`ollama,qwen3-coder:latest` to do the grep work, keep the lead on
`openrouter,anthropic/claude-opus-4.6`"). Run **`fry models`** to print every valid string.

Two consequences, stated plainly:

- **Selection is by typing `/model <provider>,<model>`.** Whether Claude Code's arrow-key
  picker auto-lists all of them depends on the CC/router version; the typed form always
  works, and `fry models` gives you the exact strings to type or paste.
- **A Claude Pro/Max *subscription* does not work through a router.** In router mode,
  Anthropic models bill via an API key or (default here) via your OpenRouter credits.
  For subscription-backed Claude, use **`--native`** (single backend, Anthropic models only).

---

## Modes

| Mode | When | What you get |
|------|------|--------------|
| **router** (default for `claude`) | you want the cross-provider `/model` list + teammate model-mixing | all providers behind `ccr`; `/model provider,model`; teammates can use any model |
| **native** (default for `codex`; `--native` for `claude`) | you want one backend / the Claude subscription | env-var (Claude Code) or `-c` (Codex) wiring to a single provider |

Codex is native-only (the router is a Claude-Code construct). Your OpenAI/Codex models
still show up **inside Claude Code's router** as `openai,gpt-…`; and `fry launch codex`
launches the real Codex agent.

---

## Install (FryStation / Windows)

1. `fry.py` + `config.example.json` → `%USERPROFILE%\.fry\`
2. `copy %USERPROFILE%\.fry\config.example.json %USERPROFILE%\.fry\config.json`, then edit the `secret` fields (see Secrets).
3. `fry.cmd` → any folder on PATH (it runs `%USERPROFILE%\.fry\fry.py`; override with `set FRY_PY=...`).
4. Install the router (only needed for router mode):
   ```
   npm install -g @musistudio/claude-code-router
   ```
5. Verify:
   ```
   fry doctor
   ```

`fry.cmd` calls `python` (falls back to `py -3`). Python 3.8+, stdlib only — no pip.
PowerShell alternative to the shim: `function fry { python "$env:USERPROFILE\.fry\fry.py" @args }`

Linux/macOS hosts later: drop `fry.py` + `config.json` in `~/.fry/`, put the `fry` shim
on PATH (`chmod +x`), `npm i -g @musistudio/claude-code-router`. Same config.

---

## Secrets (1Password) — keys never touch disk

In `config.json`, keys are **references**, never plaintext:

- `op://Vault/Item/field` — pulled at launch via `op read`, in memory only
- `env:NAME` — read from an env var
- `literal:VALUE` — escape hatch, discouraged

**Router mode:** fry writes the `ccr` config with `$VAR` placeholders ONLY
(claude-code-router interpolates them from the environment). fry resolves your `op://`
secret at launch, exports it into the child process's env, and `ccr` reads it from there.
The plaintext key never lands in `~/.claude-code-router/config.json`, never on argv, never
in logs. Providers whose key can't be resolved are dropped with a warning (a missing
OpenAI key won't block your launch).

**Native mode:** same — keys go into the agent's env (Claude Code) or via an env-var *name*
(Codex `env_key`), never onto the command line.

fry will **not** run `op signin` for you; if 1Password is locked it tells you and stops.

Set your real vault paths in `config.json`, e.g.:
```json
"openrouter": { "router": { "secret": "op://FryFarm/OpenRouter/credential", ... } }
```

---

## Commands

```
fry launch claude [--model P,M] [--native|--router] [-- passthrough]
fry models                      # every routable provider,model + native launch forms
fry doctor                      # tools, providers, live ollama models, agent-teams flag
fry list                        # agents + router providers
fry router config [--write]     # print (or write) the compiled ccr config
fry router start|stop|restart|status
fry launch claude --dry-run ... # show exactly what will happen; write nothing, launch nothing
```

`--dry-run` in router mode prints the compiled router config (keys shown as `$VAR`, never
resolved) and the `ccr code` command. Use it to confirm before you launch.

---

## Your Ollama models appear automatically

The `ollama` router provider has `"expand": true` — fry runs `ollama list` at launch and
adds **every** installed model to the `/model` list. Pull a new model, it shows up next
launch. No hand-maintained list.

---

## Adding a provider / model (no code)

Edit `config.json`:

- **Add a model to the `/model` list** → add the string to that provider's
  `router.models` array (e.g. add `"x-ai/grok-4"` under `openrouter.router.models`).
- **Add a whole new OpenAI-compatible provider** → copy the `openai` block, change
  `name`, `api_base_url`, `env_var`, `secret`, `models`. Set `transformer` if the
  provider needs request/response shaping (claude-code-router ships transformers like
  `openrouter`, `deepseek`, `gemini`).
- **Change which model serves which task class** → edit `router.roles`
  (`default` / `background` / `think` / `longContext`).
- **Add native wiring for an agent** → add a `providers.<p>.native.<agent>` block
  (`env` style for env-var agents like Claude Code, `args` style for `-c`-flag agents
  like Codex). Placeholders: `{model}`, `{api_key}` (env values only — fry refuses it in
  args), `{base_url}`.

---

## Notes / gotchas

- **Ollama context length:** coding agents want ≥64k context. Raise it in Ollama's
  settings or models truncate hard.
- **Enable Anthropic-direct in the router (optional):** the `anthropic` provider has
  `router.capable: false` by default, because Anthropic-through-a-proxy needs an API key
  and the right transformer for your `ccr` build. Default routes Claude via OpenRouter
  (one key). To use a direct Anthropic key, flip `capable: true`, set `router.secret`,
  and set the transformer your `ccr` version expects.
- **Codex `wire_api`:** the native OpenRouter→Codex block defaults to `wire_api="chat"`;
  some Codex builds want `responses`. Flip that one value if Codex errors.
- **Codex + remote Ollama:** Codex honors only `localhost` for Ollama (upstream bug).
  Fine on FryStation; won't reach ARES00's Ollama.
- **OpenRouter + Claude Code** is officially best-effort for non-Anthropic models
  (extended thinking / prompt caching may not pass through). Anthropic models via the
  Anthropic Skin behave normally.
- **Agent teams:** fry exports `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` for router
  launches so teammates work out of the box.
- fry never backgrounds anything and writes nothing at launch except the router config
  (with `$VAR` placeholders) and a timestamped backup of any prior router config.
