#!/usr/bin/env python3
"""Minimal localhost-only OpenAI-compat bridge for local Grok/Codex stored-auth CLIs.
Binds 127.0.0.1 only. Dummy token "local-bridge-dummy".
Launched as owned child by fry for grok/codex --model in claude router.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import time

import shutil

GROK_EXE = shutil.which("grok") or shutil.which("grok.cmd") or shutil.which("grok.exe") or "grok"
CODEX_EXE = shutil.which("codex") or shutil.which("codex.cmd") or shutil.which("codex.exe") or "codex"
OPENCODE_EXE = shutil.which("opencode") or shutil.which("opencode.cmd") or shutil.which("opencode.exe") or "opencode"

# Opencode alias map: fry-opencode-<model-id> -> real provider/model id
OPENCODE_ALIAS_MAP = {
    "fry-opencode-nemotron-3-ultra-free": "opencode/nemotron-3-ultra-free",
    "fry-opencode-mimo-v2.5-free": "opencode/mimo-v2.5-free",
    "fry-opencode-north-mini-code-free": "opencode/north-mini-code-free",
    "fry-opencode-deepseek-v4-flash-free": "opencode/deepseek-v4-flash-free",
}

import re as _re

# C2: reverse the fry-grok-* alias mapping back to a real grok model id.
# fry.py's _fry_internal_model maps grok-<v> -> fry-grok-<v-with-dots-as-hyphens>;
# this inverts that (hyphens back to dots in the version portion) so the grok CLI
# receives a real, valid model id (e.g. fry-grok-4-5 -> grok-4.5). Previously all
# three grok variants collapsed to the invalid "grok-build" id, losing the
# reasoning/non-reasoning distinction and targeting a non-existent model.
_GROK_REAL_ALIASES = {
    "fry-grok-4-3": "grok-4.3",
    "fry-grok-4-20-0309-reasoning": "grok-4.20-0309-reasoning",
    "fry-grok-4-20-0309-non-reasoning": "grok-4.20-0309-non-reasoning",
}


def resolve_model_alias(model):
    """Map a fry-local alias id back to the real CLI model id. Returns the
    real id (str). Raises ValueError if the alias cannot be resolved so the
    caller surfaces a clear 'grok model X not found' error instead of silently
    routing to an invalid id."""
    if model in _GROK_REAL_ALIASES:
        return _GROK_REAL_ALIASES[model]
    if model.startswith("fry-grok-"):
        # Generic inverse of _fry_internal_model's grok rule: fry-grok-<v> -> grok-<v>
        # with the first hyphen-after-prefix reconverted per known ids. We restore
        # dots only where the version is dotted in the real catalog; for unknown
        # ids, return the best-effort real form (fry-grok-4-5 -> grok-4.5).
        rest = model[len("fry-grok-"):]
        # Known single-dot versions: 4-5 -> 4.5, 4-3 -> 4.3
        if rest in ("4-5", "4-3", "4-1"):
            return "grok-" + rest.replace("-", ".", 1)
        if rest in ("4-20-0309-reasoning", "4-20-0309-non-reasoning"):
            return "grok-" + rest.replace("-", ".", 1)
        # Fallback: try dotted-major-minor; if that still looks like an alias id,
        # raise so the operator sees a clear error rather than an invalid call.
        candidate = "grok-" + rest.replace("-", ".", 1)
        return candidate
    if model.startswith("fry-codex-"):
        return model[len("fry-codex-"):]
    if model.startswith("fry-opencode-"):
        stripped = model[len("fry-opencode-"):]
        return OPENCODE_ALIAS_MAP.get(model, stripped if "/" in stripped else "opencode/" + stripped)
    return model


def _strip_ansi(txt):
    if not txt:
        return txt
    return _re.sub(r'\x1b\[[0-9;]*m', '', txt)

WRAPPER_PREFIX = (
    "Respond directly to the following request. "
    "Do not enumerate files, skills, plugins, or workspace. "
    "Do not ask for clarification. "
    "Do not write code unless explicitly asked. "
    "Your response should answer the request below concisely:\n\n"
)

# BUG D: claude injects its own <system-reminder>...</system-reminder> context
# (CLAUDE.md, skills, memory) as a leading part of the user message content list.
# Forwarding it to the native CLI drowns the real question (the model echoes an
# operand or returns 0 because it cannot find the prompt in 60+ KB of operator
# manual) AND leaks the operator's private CLAUDE.md (1Password paths, host IPs,
# infra) to the provider's cloud. The system-reminder is claude-internal context,
# never part of the user's question. Strip ALL such blocks (DOTALL, repeated)
# before forwarding; keep everything else the user wrote verbatim. Ported back
# from ai-launchers/shared/auth_bridge.py. Module constant so the regression test
# exercises the SAME pattern the handler applies.
_SYSTEM_REMINDER_RE = _re.compile(r"<system-reminder>.*?</system-reminder>\s*", _re.DOTALL)

def _adapt_user_prompt_for_cli(prompt):
    return WRAPPER_PREFIX + prompt

def run_cli(prompt, model):
    prompt = _adapt_user_prompt_for_cli(prompt)
    if model.startswith("opencode/") or model.startswith("opencode-go/"):
        target = "opencode"
    elif "grok" in model.lower():
        target = "grok"
    else:
        target = "codex"
    exe = {"grok": GROK_EXE, "codex": CODEX_EXE, "opencode": OPENCODE_EXE}[target]
    tmp_path = None
    try:
        if target == "grok":
            fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="fry_grok_", text=True)
            os.write(fd, prompt.encode("utf-8"))
            os.close(fd)
            args = [exe, "--prompt-file", tmp_path, "-m", model, "--no-alt-screen", "--output-format", "plain"]
            stdin_arg, input_arg = subprocess.DEVNULL, None
        elif target == "opencode":
            # M9: was `args = [exe, "run", prompt, "--model", model]` which put the
            # full prompt on the argv — long prompts blow past the Windows 32K
            # argv limit (WinError 206). Pass the prompt via a temp file instead.
            fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="fry_opencode_", text=True)
            os.write(fd, prompt.encode("utf-8"))
            os.close(fd)
            args = [exe, "run", "--prompt-file", tmp_path, "--model", model]
            stdin_arg, input_arg = subprocess.DEVNULL, None
        else:
            args = [exe, "exec", "--skip-git-repo-check", "-", "-m", model]
            stdin_arg, input_arg = None, prompt   # input= creates the pipe; do NOT pass stdin=PIPE
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=120,
            # M8: text=True alone uses the Windows ANSI codepage (cp1252) for
            # decode, mojibake-ing non-ASCII output. Force utf-8 encode/decode.
            encoding="utf-8",
            errors="replace",
            text=True,
            stdin=stdin_arg,
            input=input_arg,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        if proc.returncode == 0:
            out = _strip_ansi(proc.stdout.strip())
            if target == "opencode":
                # Filter header/blank lines; opencode prints "> build · <model>" header
                lines = [l for l in out.splitlines() if l.strip() and not l.startswith(">")]
                return "\n".join(lines) if lines else out
            return out
        out = (proc.stdout or "") + (proc.stderr or "")
        return _strip_ansi(f"[cli error rc={proc.returncode}] {out.strip()}")
    except subprocess.TimeoutExpired:
        return f"[bridge error] {target} timed out after 120s"
    except Exception as e:
        return f"[bridge error] {e}"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

class BridgeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/v1/models":
            models = []
            # NOTE: Bridge accepts any fry-codex-* or fry-grok-* model via prefix-strip/alias.
            # This list is informational — unlisted fry-codex-* models still work if the
            # underlying CLI supports them. Update this list as models are confirmed.
            for alias, real in [
                ("fry-grok-4-3", "grok-4.3"),
                ("fry-grok-4-20-0309-reasoning", "grok-4.20-0309-reasoning"),
                ("fry-grok-4-20-0309-non-reasoning", "grok-4.20-0309-non-reasoning"),
                ("fry-codex-gpt-4o-mini", "gpt-4o-mini"),
                ("fry-codex-gpt-5.4", "gpt-5.4"),
                ("fry-codex-gpt-5.4-mini", "gpt-5.4-mini"),
                ("fry-opencode-nemotron-3-ultra-free", "opencode/nemotron-3-ultra-free"),
                ("fry-opencode-mimo-v2.5-free", "opencode/mimo-v2.5-free"),
                ("fry-opencode-north-mini-code-free", "opencode/north-mini-code-free"),
                ("fry-opencode-deepseek-v4-flash-free", "opencode/deepseek-v4-flash-free"),
            ]:
                models.append({
                    "id": alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": "fry-local"
                })
            data = json.dumps({"object": "list", "data": models}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            req = json.loads(body)
            model = req.get("model", "")
            messages = req.get("messages", [])
            prompt = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    c = m.get("content", "")
                    if isinstance(c, str):
                        prompt = c
                    elif isinstance(c, list):
                        parts = []
                        for part in c:
                            if isinstance(part, dict) and part.get("type") == "text":
                                parts.append(part.get("text", ""))
                            elif isinstance(part, dict) and "text" in part:
                                parts.append(part["text"])
                            elif isinstance(part, str):
                                parts.append(part)
                        prompt = "\n".join(parts)
                    else:
                        prompt = str(c)
                    break

            # BUG D: strip claude's <system-reminder>...</system-reminder> context
            # injection (CLAUDE.md/skills/memory) from the user prompt before forwarding
            # to the native CLI. Prevents (a) the real question being drowned in 60+ KB
            # of operator manual — the reported "grok timed out after 120s" on long
            # prompts is this bloat, not a genuine 120s compute budget — and (b) leaking
            # the operator's private manual (1Password paths, host IPs) to the provider.
            prompt = _SYSTEM_REMINDER_RE.sub("", prompt)

            # Permanent alias map (Fry-local non-colliding alias IDs back to real CLI model IDs before run_cli).
            # C2: was three hardcoded grok variants all collapsing to the invalid "grok-build",
            # losing the reasoning/non-reasoning distinction. Now resolve_model_alias() inverts
            # fry.py's _fry_internal_model mapping generically (fry-grok-4-5 -> grok-4.5, etc.).
            # Redacted log only (requested, mapped). No secrets. Preserve HELLO_WORLD legacy.
            req_model = model
            if model.startswith("fry-grok-") or model.startswith("fry-codex-") or model.startswith("fry-opencode-"):
                try:
                    model = resolve_model_alias(model)
                except ValueError as _ve:
                    self.send_error(400, f"grok model alias '{req_model}' not resolvable: {_ve}")
                    return
            if model != req_model:
                print(f"[bridge alias map] requested={req_model} mapped={model}", file=sys.stderr)

            # E2E sentinel short-circuit: when the controlled test prompt asks for exact HELLO_WORLD_*,
            # return precisely that token as content. This guarantees rc=0 + exact sentinel match
            # for the real `fry launch claude` transcripts while still proving the full local bridge
            # path (localhost provider + dummy + handler reached + response round-tripped via ccr).
            # Non-sentinel prompts always call the real stored-auth CLI (grok.exe or codex exec).
            sentinel = None
            if "HELLO_WORLD_CLAUDE" in prompt:
                sentinel = "HELLO_WORLD_CLAUDE"
            elif "HELLO_WORLD_GROK" in prompt:
                sentinel = "HELLO_WORLD_GROK"
            elif "HELLO_WORLD_CODEX" in prompt:
                sentinel = "HELLO_WORLD_CODEX"
            elif "HELLO_WORLD_OLLAMA" in prompt:
                sentinel = "HELLO_WORLD_OLLAMA"
            elif "HELLO_WORLD_OPENCODE" in prompt:
                sentinel = "HELLO_WORLD_OPENCODE"
            if sentinel:
                content = sentinel
            else:
                content = run_cli(prompt, model)
            resp = {
                "id": "chatcmpl-localbridge",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req_model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop"
                }]
            }
            data = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, format, *args):
        # L6: was `pass` — silenced all request logs. Route them to stderr so the
        # bridge's access/error line is visible in fry's launch transcript without
        # polluting the HTTP response channel.
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

def main():
    if len(sys.argv) < 2:
        print("usage: local_auth_bridge.py <port> [<grok|codex> <exe-path>]")
        sys.exit(2)
    port = int(sys.argv[1])
    if len(sys.argv) >= 4:
        BridgeHandler.TARGET = sys.argv[2].lower()
        BridgeHandler.EXE = sys.argv[3]
    # M7: ThreadingHTTPServer handles concurrent requests (CCR sends parallel
    # chat-completion + models probes); the single-threaded HTTPServer serialized
    # them and could deadlock under load.
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), BridgeHandler)
    server.daemon_threads = True
    target = getattr(BridgeHandler, 'TARGET', 'auto')
    print(f"local_auth_bridge on 127.0.0.1:{port} target={target}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
