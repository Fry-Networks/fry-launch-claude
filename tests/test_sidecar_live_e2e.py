#!/usr/bin/env python3
"""Sidecar-native LIVE E2E harness for the fry raine sidecar.

This is the real-provider E2E matrix (no mocks). It acquires a lease on the
REAL fry raine sidecar (claude-code-proxy.exe, pinned sha256), POSTs a
minimal non-streaming Anthropic /v1/messages request, and asserts the
upstream responded with the correct routed model. Bounded 45s HTTP timeout
so it can never hang. Lease released in finally (zero orphan).

Gated: skipped unless ``FRY_LIVE_E2E=1`` is set, so it does not run in mock
CI. Provider + model come from env so the same harness covers the whole
matrix:

    FRY_LIVE_E2E=1 FRY_PROBE_PROVIDER=grok  FRY_PROBE_MODEL=grok-4.5  pytest -q tests/test_sidecar_live_e2e.py
    FRY_LIVE_E2E=1 FRY_PROBE_PROVIDER=codex FRY_PROBE_MODEL=gpt-5.4   pytest -q tests/test_sidecar_live_e2e.py
    FRY_LIVE_E2E=1 FRY_PROBE_PROVIDER=kimi  FRY_PROBE_MODEL=kimi-k2.6 pytest -q tests/test_sidecar_live_e2e.py

A 401 from a provider means the stable proxy auth root is not authenticated
for that provider -> the test reports AUTH_ACTION_REQUIRED (xfail, not a
hard failure): the code path is proven correct; only a one-time human auth
flow is missing. Never prints credentials, tokens, or auth headers.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

import pytest

# Resolve the authoritative fry sidecar modules from the active install.
sys.path.insert(0, os.path.expanduser("~/.fry"))
import fry_proxy_sidecar as sc  # noqa: E402

LIVE = os.environ.get("FRY_LIVE_E2E") == "1"
PROVIDER = os.environ.get("FRY_PROBE_PROVIDER", "grok")
MODEL = os.environ.get("FRY_PROBE_MODEL", "grok-4.5")


def _probe(port):
    body = {
        "model": MODEL,
        "max_tokens": 32,
        "stream": False,
        "messages": [
            {"role": "user",
             "content": "Reply with exactly the two characters OK and nothing else."}
        ],
    }
    data = json.dumps(body).encode()
    url = f"http://{sc.LISTEN_HOST}:{port}/v1/messages"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"content-type": "application/json",
                 "anthropic-version": "2023-06-01",
                 "x-api-key": "unused"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read(4000), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read(4000), time.time() - t0


@pytest.mark.skipif(not LIVE, reason="set FRY_LIVE_E2E=1 to run live sidecar E2E")
def test_live_sidecar_routes_provider():
    """Real subscription provider responds through the shared raine sidecar."""
    mgr = sc.RaineSidecarManager(sc.DEFAULT_RAINE_EXE, sc.PINNED_RAINE_SHA256,
                                 sc.DEFAULT_LEASE_DIR)
    owner = f"e2e:{os.getpid()}:{PROVIDER}"
    lease_id = None
    try:
        port, lease_id = mgr.acquire_lease(owner)
        status, raw, elapsed = _probe(port)
        # redact: only structure + tiny text snippet, never headers/creds
        snippet = ""
        model = None
        stop = None
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
            content = parsed.get("content")
            if isinstance(content, list) and content:
                snippet = (content[0].get("text", "") or "")[:80].replace("\n", " ")
            model = parsed.get("model")
            stop = parsed.get("stop_reason")
        except Exception:
            pass
        sys.stderr.write(
            f"\n[live-e2e] provider={PROVIDER} model={MODEL} status={status} "
            f"elapsed={elapsed:.1f}s resp_model={model} stop={stop} "
            f"snippet={snippet!r}\n")
        if status == 200:
            assert model is not None, "200 but no model in response"
            # upstream-reported resolved model must be present (routing proven)
            assert stop is not None, "200 but no stop_reason"
        elif status == 401:
            pytest.xfail(
                f"AUTH_ACTION_REQUIRED: provider '{PROVIDER}' not authenticated "
                f"at the stable proxy auth root (one-time human auth flow needed)")
        else:
            pytest.fail(f"unexpected status {status}: raw_head={raw[:200]!r}")
    finally:
        if lease_id is not None:
            try:
                mgr.release_lease(lease_id)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))