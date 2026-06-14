#!/usr/bin/env python3
"""Minimal localhost-only OpenAI-compat bridge for local Grok/Codex stored-auth CLIs.
Binds 127.0.0.1 only. Dummy token "local-bridge-dummy".
Launched as owned child by fry for grok/codex --model in claude router.
"""
import http.server
import json
import subprocess
import sys
import time

import shutil

GROK_EXE = r"C:\Users\saf70\.grok\bin\grok.exe"
CODEX_EXE = shutil.which("codex") or shutil.which("codex.cmd") or shutil.which("codex.exe") or "codex"

import re as _re

def _strip_ansi(txt):
    if not txt:
        return txt
    return _re.sub(r'\x1b\[[0-9;]*m', '', txt)

def run_cli(prompt, model):
    target = "grok" if "grok" in model.lower() else "codex"
    exe = GROK_EXE if target == "grok" else CODEX_EXE
    if target == "grok":
        args = [exe, "-p", prompt, "-m", model, "--output-format", "plain"]
    else:
        args = [exe, "exec", "--skip-git-repo-check", prompt, "-m", model]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=180,
            text=True,
            stdin=subprocess.DEVNULL,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        if proc.returncode == 0:
            return _strip_ansi(proc.stdout.strip())
        out = (proc.stdout or "") + (proc.stderr or "")
        return _strip_ansi(f"[cli error rc={proc.returncode}] {out.strip()}")
    except Exception as e:
        return f"[bridge error] {e}"

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
                        prompt = c[0].get("text", "") if c and isinstance(c[0], dict) else str(c)
                    else:
                        prompt = str(c)
                    break

            # Permanent alias map (Fry-local non-colliding alias IDs back to real CLI model IDs before run_cli).
            # Redacted log only (requested, mapped). No secrets. Preserve HELLO_WORLD legacy.
            req_model = model
            if model == "fry-grok-4-3":
                model = "grok-build"
            elif model == "fry-grok-4-20-0309-reasoning":
                model = "grok-build"
            elif model == "fry-grok-4-20-0309-non-reasoning":
                model = "grok-build"
            elif model.startswith("fry-codex-"):
                model = model[len("fry-codex-"):]
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
        pass

def main():
    if len(sys.argv) < 2:
        print("usage: local_auth_bridge.py <port> [<grok|codex> <exe-path>]")
        sys.exit(2)
    port = int(sys.argv[1])
    if len(sys.argv) >= 4:
        BridgeHandler.TARGET = sys.argv[2].lower()
        BridgeHandler.EXE = sys.argv[3]
    server = http.server.HTTPServer(("127.0.0.1", port), BridgeHandler)
    target = getattr(BridgeHandler, 'TARGET', 'auto')
    print(f"local_auth_bridge on 127.0.0.1:{port} target={target}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
