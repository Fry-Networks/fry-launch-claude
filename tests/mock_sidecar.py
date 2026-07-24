#!/usr/bin/env python3
"""Mock raine sidecar for lifecycle tests. Binds 127.0.0.1:<port>, serves /healthz.
Usage: python mock_sidecar.py --port <p> [--fail] [--hang]
  --fail : exit immediately (simulate crash)
  --hang : never bind (simulate stall)
"""
import argparse, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.endswith("/healthz"):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        # minimal /v1/messages echo so protocol tests can hit it
        self.send_response(200); self.send_header("content-type","application/json"); self.end_headers()
        self.wfile.write(b'{"id":"mock","type":"message","role":"assistant","content":[{"type":"text","text":"ok"}],"model":"mock","stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":1}}')

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--fail", action="store_true")
ap.add_argument("--hang", action="store_true")
a = ap.parse_args()
if a.fail:
    sys.exit(1)
if a.hang:
    while True:
        time.sleep(1)
srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
srv.serve_forever()