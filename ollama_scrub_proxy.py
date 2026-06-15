#!/usr/bin/env python3
"""Ollama request-scrubbing proxy for CCR / local-ollama.

CCR sends an OpenAI-shaped body to this proxy. For models that Ollama reports
as NOT supporting the `thinking` capability (via POST /api/show), strip the
reasoning fields Ollama rejects (reasoning_effort and reasoning.effort), then
forward to the real Ollama endpoint and stream the response back unchanged.
Thinking-capable models pass through untouched.

Launched as an owned child by fry.py ONLY for launches routing to local-ollama.

Usage: python ollama_scrub_proxy.py <port>
"""
import http.server
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

OLLAMA_BASE = "http://localhost:11434"
CAP_TIMEOUT = 10
UPSTREAM_TIMEOUT = 120
LOG_PATH = os.path.join(tempfile.gettempdir(), "fry_scrub_proxy.log")

_CAP_CACHE = {}


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    print(line, file=sys.stderr, flush=True)


def _model_supports_thinking(model):
    """POST /api/show; cache result. Defaults to False (strip) on any error."""
    if model in _CAP_CACHE:
        return _CAP_CACHE[model]
    supports = False
    try:
        payload = json.dumps({"model": model}).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/show", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=CAP_TIMEOUT) as resp:
            info = json.loads(resp.read().decode("utf-8", errors="replace"))
        caps = info.get("capabilities") or info.get("model_info", {}).get("capabilities") or []
        supports = "thinking" in caps
    except Exception as e:
        _log(f"capability-probe-failed model={model!r} err={e} -> assume non-thinking")
        supports = False
    _CAP_CACHE[model] = supports
    return supports


def _scrub_body(body):
    """Strip reasoning fields for non-thinking models. Returns (body, is_stream)."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return body, False

    model = obj.get("model", "")
    is_stream = bool(obj.get("stream", False))
    if not model:
        return body, is_stream

    supports = _model_supports_thinking(model)
    stripped = False
    if not supports:
        if "reasoning_effort" in obj:
            del obj["reasoning_effort"]
            stripped = True
        reasoning = obj.get("reasoning")
        if isinstance(reasoning, dict):
            if "effort" in reasoning:
                del reasoning["effort"]
                stripped = True
            if not reasoning:
                del obj["reasoning"]
                stripped = True

    _log(f"scrub model={model!r} supports_thinking={supports} stripped={stripped} stream={is_stream}")
    if stripped:
        return json.dumps(obj).encode("utf-8"), is_stream
    return body, is_stream


def _send_json_error(handler, code, message):
    body = json.dumps({"error": {"message": message, "type": "proxy_error"}}).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            content_len = int(self.headers.get("Content-Length", 0))
        except Exception:
            _send_json_error(self, 400, "invalid Content-Length")
            return

        original = self.rfile.read(content_len)
        scrubbed, is_stream = _scrub_body(original)

        url = f"{OLLAMA_BASE}/v1/chat/completions"
        forward_headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "text/event-stream"),
        }
        try:
            req = urllib.request.Request(url, data=scrubbed, headers=forward_headers, method="POST")
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as upstream:
                self.send_response(upstream.status)
                self.send_header("Connection", "close")
                for h, v in upstream.headers.items():
                    if h.lower() in {"content-length", "transfer-encoding", "connection"}:
                        continue
                    self.send_header(h, v)
                self.end_headers()
                if is_stream:
                    for line in upstream:
                        self.wfile.write(line)
                        self.wfile.flush()
                        if b"[DONE]" in line:
                            break
                else:
                    while True:
                        chunk = upstream.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(err_body)
        except urllib.error.URLError as e:
            _send_json_error(self, 502, f"upstream unavailable: {e.reason}")
        except socket.timeout:
            _send_json_error(self, 504, "upstream timeout")
        except Exception as e:
            _send_json_error(self, 500, f"proxy error: {e}")

    def log_message(self, fmt, *args):
        pass


def main():
    if len(sys.argv) < 2:
        print("usage: ollama_scrub_proxy.py <port>", file=sys.stderr)
        sys.exit(2)
    try:
        port = int(sys.argv[1])
    except Exception:
        print("usage: ollama_scrub_proxy.py <port>", file=sys.stderr)
        sys.exit(2)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"ollama_scrub_proxy on 127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
