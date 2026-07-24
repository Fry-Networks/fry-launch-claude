#!/usr/bin/env python3
"""Fry capture proxy — EVIDENCE ONLY, sanitized.

A localhost HTTP proxy that fronts the real Claude Code upstream and records
ONLY the protocol fields needed to compare an ordinary turn vs a /plan turn:
  * system block count + source/labels (NOT text content)
  * tool list (names only, NOT definitions/input)
  * tool_choice type
  * thinking/output_token config presence
  * metadata fields (keys only)
  * cache_control markers (presence per block type)
  * model field
  * per-message role counts (NOT content)
  * context-management / betas fields (keys only)

It NEVER persists: Authorization headers, x-api-key, cookies, OAuth tokens,
complete prompt text, complete tool_result output. Request bodies are reduced
to a structural fingerprint; response bodies are NOT stored at all (only
stream/non-stream + event-type counts). This is for the /plan-mode delta trace
(evidence/plan-mode-delta.md) and is NOT part of the runtime path.

Usage (foreground, bounded):
  python fry_capture_proxy.py --port <port> --upstream <url> --out <dir>
Claude Code is pointed at it via ANTHROPIC_BASE_URL=http://127.0.0.1:<port>.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlreq

SECRET_HDRS = {"authorization", "x-api-key", "cookie", "set-cookie",
               "anthropic-auth-token", "proxy-authorization"}
SECRET_BODY_KEYS = {"api_key", "apikey", "token", "access_token", "refresh_token",
                    "password", "secret", "authorization"}


def _redact_headers(hdrs) -> dict:
    out = {}
    for k, v in hdrs.items():
        if k.lower() in SECRET_HDRS:
            out[k] = f"<redacted:len={len(str(v))}>"
        else:
            out[k] = v
    return out


def _structural_fingerprint(body: bytes) -> dict:
    """Reduce a request body to non-secret protocol fields only."""
    if not body:
        return {"empty": True}
    try:
        obj = json.loads(body)
    except Exception:
        return {"non_json_len": len(body)}
    fp = {}
    fp["model"] = obj.get("model")
    fp["stream"] = obj.get("stream")
    fp["tool_choice"] = (obj.get("tool_choice") or {}).get("type") \
        if isinstance(obj.get("tool_choice"), dict) else obj.get("tool_choice")
    # system
    sysv = obj.get("system")
    if isinstance(sysv, list):
        fp["system_blocks"] = [
            {"type": b.get("type"), "cache_control": bool(b.get("cache_control")),
             "name": b.get("name"), "source_type": (b.get("source") or {}).get("type")
             if isinstance(b.get("source"), dict) else None}
            for b in sysv if isinstance(b, dict)
        ]
    elif isinstance(sysv, str):
        fp["system_blocks"] = [{"type": "string", "len": len(sysv),
                                 "cache_control": False}]
    else:
        fp["system_blocks"] = []
    # tools — names only
    tools = obj.get("tools") or []
    fp["tool_names"] = [t.get("name") for t in tools if isinstance(t, dict)]
    fp["tool_count"] = len(tools)
    # messages — roles + cache markers, NOT content
    msgs = obj.get("messages") or []
    roles = {}
    cache_roles = set()
    for m in msgs:
        if not isinstance(m, dict):
            continue
        r = m.get("role", "?")
        roles[r] = roles.get(r, 0) + 1
        c = m.get("content")
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("cache_control"):
                    cache_roles.add(r)
    fp["message_roles"] = roles
    fp["cache_control_roles"] = sorted(cache_roles)
    # thinking
    th = obj.get("thinking")
    fp["thinking"] = bool(th) if th is not None else None
    if isinstance(th, dict):
        fp["thinking_type"] = th.get("type")
        fp["thinking_budget"] = th.get("budget_tokens")
    # metadata keys only
    md = obj.get("metadata")
    fp["metadata_keys"] = list(md.keys()) if isinstance(md, dict) else None
    # betas / context management
    fp["betas"] = obj.get("betas")
    fp["top_level_keys"] = sorted(k for k in obj.keys()
                                   if k.lower() not in SECRET_BODY_KEYS)
    # hash of full body for dedup identity (NOT the body itself)
    fp["body_sha256"] = hashlib.sha256(body).hexdigest()[:16]
    return fp


class CaptureHandler(BaseHTTPRequestHandler):
    upstream = None
    out_dir = None
    capture_id = None

    def log_message(self, *a):  # silence default logging
        pass

    def _capture_path(self, name):
        return Path(self.out_dir) / f"{self.capture_id}_{name}.json"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        fp = _structural_fingerprint(body)
        rec = {"ts": time.time(), "path": self.path, "method": "POST",
               "request_fingerprint": fp,
               "request_headers_redacted": _redact_headers(dict(self.headers))}
        # forward to upstream
        up = self.upstream.rstrip("/") + self.path
        req = urlreq.Request(up, data=body, method="POST")
        for k, v in self.headers.items():
            if k.lower() in SECRET_HDRS:
                continue  # never forward through our capture log; upstream gets real hdr from client
            req.add_header(k, v)
        # actually forward secret headers to upstream (they are on the wire to the
        # real provider) but DO NOT record them.
        for k, v in self.headers.items():
            if k.lower() in SECRET_HDRS:
                req.add_header(k, v)
        try:
            with urlreq.urlopen(req, timeout=120) as resp:
                resp_body = resp.read()
                rec["response_status"] = resp.status
                rec["response_headers_redacted"] = _redact_headers(dict(resp.headers))
                rec["response_event_types"] = self._sse_event_types(resp_body)
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in SECRET_HDRS:
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp_body)
        except Exception as e:
            rec["upstream_error"] = str(e)
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b'{"error":"capture upstream failed"}')
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)
        self._capture_path("post").write_text(json.dumps(rec, indent=2), encoding="utf-8")

    def _sse_event_types(self, body: bytes) -> list:
        if not body:
            return []
        types = []
        for line in body.split(b"\n"):
            if line.startswith(b"event:"):
                types.append(line[6:].strip().decode("utf-8", "replace"))
        return types

    def do_GET(self):
        # health passthrough
        if self.path.endswith("/healthz"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        up = self.upstream.rstrip("/") + self.path
        req = urlreq.Request(up, method="GET")
        try:
            with urlreq.urlopen(req, timeout=30) as resp:
                b = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in SECRET_HDRS:
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b)
        except Exception:
            self.send_response(502)
            self.end_headers()


def run(port, upstream, out_dir):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    CaptureHandler.upstream = upstream
    CaptureHandler.out_dir = out_dir
    CaptureHandler.capture_id = f"cap-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), CaptureHandler)
    sys.stderr.write(f"[capture] listening 127.0.0.1:{port} -> {upstream} "
                     f"out={out_dir} id={CaptureHandler.capture_id}\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    run(a.port, a.upstream, a.out)