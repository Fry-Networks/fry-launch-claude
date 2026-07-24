#!/usr/bin/env python3
"""Fake `claude` CLI for protocol-fidelity tests.

Reads ANTHROPIC_BASE_URL from env, POSTs a CANNED FULL Anthropic /v1/messages
request body (system array + tool defs + tool_result + thinking + image +
stream:true) to <base>/v1/messages, drains the SSE response, then exits 0.
Writes nothing to stdout except a marker. The recording sidecar records the
body verbatim; the test compares it to the canned body to PROVE no flattening.

This stands in for the real `claude` binary so we never depend on a live
Claude Code install inside the unit test.
"""
import json, os, sys, urllib.request

BASE = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
OUT = os.environ.get("FRY_FAKE_SENT_OUT", "")

# A rich, FULL Anthropic request that the OLD bridge would have flattened to
# just the last user text. The new sidecar path must forward ALL of it.
BODY = {
    "model": os.environ.get("ANTHROPIC_MODEL", "grok-4.5"),
    "max_tokens": 1024,
    "stream": True,
    "system": [
        {"type": "text", "text": "You are a coding agent.",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "<system-reminder>plan mode active</system-reminder>"},
    ],
    "tools": [
        {"name": "Read", "description": "read a file",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        {"name": "Bash", "description": "run a command",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
    ],
    "tool_choice": {"type": "auto"},
    "thinking": {"type": "enabled", "budget_tokens": 2048},
    "metadata": {"user_id": "test-user"},
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "first turn, please plan"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok, planning"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents here"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0=="}},
            {"type": "text", "text": "here is a screenshot and the tool output; now act"},
        ]},
    ],
}

def main():
    if not BASE:
        print("fake_claude: ANTHROPIC_BASE_URL not set", file=sys.stderr); sys.exit(2)
    data = json.dumps(BODY).encode()
    if OUT:
        try: open(OUT, "wb").write(data)
        except Exception: pass
    argv_out = os.environ.get("FRY_FAKE_ARGV_OUT")
    if argv_out:
        try:
            with open(argv_out, "w", encoding="utf-8") as f:
                json.dump(sys.argv[1:], f, ensure_ascii=False)
        except Exception:
            pass
    req = urllib.request.Request(BASE + "/v1/messages", data=data, method="POST",
                                 headers={"content-type": "application/json",
                                          "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            # drain (SSE or JSON)
            while True:
                chunk = r.read(4096)
                if not chunk: break
    except Exception as e:
        print(f"fake_claude: request failed: {e}", file=sys.stderr); sys.exit(3)
    sys.exit(0)

if __name__ == "__main__":
    main()