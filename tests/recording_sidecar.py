#!/usr/bin/env python3
"""Recording mock raine sidecar for protocol-fidelity tests.

Binds 127.0.0.1:<port>, /healthz -> 200, /v1/messages POST -> records the RAW
request body (verbatim) to the path in FRY_REC_OUT and returns a minimal Anthropic
message response (SSE if "stream":true, else JSON). This proves the new sidecar
path forwards the FULL Anthropic request unchanged (no flattening).

Usage: python recording_sidecar.py --port <p>
"""
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _record(self, body: bytes):
        out = os.environ.get("FRY_REC_OUT")
        if out:
            try:
                with open(out, "wb") as f:
                    f.write(body)
            except Exception:
                pass

    def do_GET(self):
        if self.path.endswith("/healthz"):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length", "0") or "0")
        body = self.rfile.read(n) if n else b""
        self._record(body)
        try:
            obj = json.loads(body) if body else {}
        except Exception:
            obj = {}
        stream = bool(obj.get("stream"))
        if stream:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            # minimal SSE event sequence: message_start, content_block_delta, message_stop
            ev1 = b'event: message_start\ndata: {"type":"message_start","message":{"id":"rec","type":"message","role":"assistant","content":[],"model":"mock","stop_reason":null,"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
            ev2 = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
            ev3 = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            for ev in (ev1, ev2, ev3):
                try: self.wfile.write(ev); self.wfile.flush()
                except Exception: break
        else:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "rec", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "model": "mock", "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode())

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
a = ap.parse_args()
srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
srv.serve_forever()