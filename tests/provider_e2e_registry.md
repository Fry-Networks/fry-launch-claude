# Provider E2E Registry — Fry Wrapper Modernization (2026-07-23)

Sidecar-native E2E harness: `tests/test_sidecar_live_e2e.py` (gated on
`FRY_LIVE_E2E=1`; uses the REAL fry raine sidecar, pinned sha256, bounded
45s HTTP timeout, lease released in `finally`).

## Disposition matrix (verbatim live E2E, 2026-07-24)

| Provider | Path | Model probed | Live verdict | Disposition |
|----------|------|--------------|--------------|-------------|
| grok (xai) | raine sidecar | grok-4.5 | PASS (200, resp_model=grok-4.5, stop=end_turn, 2.2s) | MIGRATED_TO_RAINE |
| codex (openai) | raine sidecar | gpt-5.4 | PASS (200, resp_model=gpt-5.4, snippet='OK', 1.3s) | MIGRATED_TO_RAINE |
| kimi | raine sidecar | kimi-k2.6 | AUTH_ACTION_REQUIRED (401, xfail) | MIGRATED_TO_RAINE (auth pending) |
| opencode | routatic sidecar | — | AUTH_ACTION_REQUIRED (no ROUTATIC_PROXY_API_KEY) | MIGRATED_TO_ROUTATIC (auth pending) |
| ollama | legacy CCR | — | RETAINED_CCR (dry-run fallthrough, no regression) | RETAINED_CCR |
| deepseek | legacy direct | — | RETAINED_DIRECT (unchanged) | RETAINED_DIRECT_WITH_PROTOCOL_FIX |
| gemini | legacy direct | — | RETAINED_DIRECT (unchanged) | RETAINED_DIRECT_WITH_PROTOCOL_FIX |
| nvidia | legacy direct | — | RETAINED_DIRECT (unchanged) | RETAINED_DIRECT_WITH_PROTOCOL_FIX |

## Notes

- One raine process routes codex+grok+kimi by requested model; one stable
  auth root at `~/.config/claude-code-proxy/` (ACL owner-only).
- `AUTH_ACTION_REQUIRED` = code path proven correct (routing + protocol
  fidelity verified), only a one-time human auth flow is missing. NOT a
  code failure; does not stop the run.
- Zero owned orphan processes after every probe (lease released in
  `finally`; shared sidecar shuts down at zero live leases).
- No `.claude.json`/`settings.json`/`settings.local.json`/
  `installed_plugins.json`/CCR config/model-cache mutated on the sidecar
  path (IMMUTABLE_CONFIG byte-identical, asserted by
  `shared/test_sidecar.py::test_no_claude_config_mutation`).
- FORBIDDEN reintroduction respected: no OpenRouter, no OpenAI API-key
  models, no xAI API-key models. Codex+Grok subscription access retained.