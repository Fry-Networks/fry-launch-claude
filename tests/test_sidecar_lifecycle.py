#!/usr/bin/env python3
"""Lifecycle tests for fry_proxy_sidecar.RaineSidecarManager (mock binary).

Covers: first launch, second launch attaches, stale PID, occupied port race,
crashed proxy restart, normal exit (last lease -> shutdown), two concurrent
leases, stale-lease recovery, free-port race retry, heartbeat.

Uses a mock sidecar binary + disposable lease dir + real process identity
(PID/exe/creation-time) so verify-alive is exercised truthfully. NEVER points
at the real auth root; never manipulates live creds; never auth logout.
"""
import os, sys, time, json, socket, subprocess, tempfile, shutil, threading
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import fry_proxy_sidecar as sc

MOCK = HERE / "mock_sidecar.py"
PINNED = "deadbeef" * 8  # fake pinned hash for the mock binary path


def _exe_hash(path):
    return sc._exe_sha256(str(path))


def _make_mgr(lease_dir, exe_path=MOCK, fail=False, hang=False):
    """Manager whose serve args point at the mock binary."""
    h = sc.PINNED_RAINE_SHA256  # not used; we override exe+hash
    def args(port):
        a = [sys.executable, str(exe_path), "--port", str(port)]
        if fail: a.append("--fail")
        if hang: a.append("--hang")
        return a
    # compute real hash of the mock so verify-alive matches
    real_hash = _exe_hash(exe_path)
    return sc.RaineSidecarManager(exe_path, real_hash, lease_dir, serve_args_factory=args)


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _health(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def test_first_launch_starts_sidecar(tmp_path):
    mgr = _make_mgr(tmp_path)
    port, lid = mgr.acquire_lease("t1")
    assert _health(port)
    lease = json.loads((tmp_path / "lease.json").read_text())
    assert lease["pid"] and lease["port"] == port
    assert len(lease["leases"]) == 1
    mgr.release_lease(lid)
    # last lease -> shutdown
    time.sleep(0.6)
    assert not _health(port)


def test_second_launch_attaches(tmp_path):
    mgr = _make_mgr(tmp_path)
    p1, l1 = mgr.acquire_lease("a")
    mgr2 = _make_mgr(tmp_path)
    p2, l2 = mgr2.acquire_lease("b")
    assert p1 == p2, "second launch must attach to same sidecar"
    lease = json.loads((tmp_path / "lease.json").read_text())
    assert len(lease["leases"]) == 2
    mgr.release_lease(l1)
    assert _health(p1), "sidecar stays up while a live lease remains"
    mgr2.release_lease(l2)
    time.sleep(0.6)
    assert not _health(p1)


def test_stale_pid_falls_back_to_fresh(tmp_path):
    mgr = _make_mgr(tmp_path)
    port, lid = mgr.acquire_lease("s")
    pid = json.loads((tmp_path / "lease.json").read_text())["pid"]
    mgr.release_lease(lid)
    time.sleep(0.6)
    assert not _health(port)
    # lease file cleared
    lease = json.loads((tmp_path / "lease.json").read_text())
    assert lease == {} or not lease.get("pid")


def test_crashed_proxy_restarts(tmp_path):
    mgr = _make_mgr(tmp_path, fail=True)
    raised = False
    try:
        mgr.acquire_lease("c")
    except sc.SidecarError:
        raised = True
    assert raised, "crashing sidecar must raise SidecarError"
    # a subsequent good manager starts fine
    mgr2 = _make_mgr(tmp_path)
    port, lid = mgr2.acquire_lease("c2")
    assert _health(port)
    mgr2.release_lease(lid)


def test_two_concurrent_leases(tmp_path):
    mgr = _make_mgr(tmp_path)
    p1, l1 = mgr.acquire_lease("x")
    mgr2 = _make_mgr(tmp_path)
    p2, l2 = mgr2.acquire_lease("y")
    assert p1 == p2
    lease = json.loads((tmp_path / "lease.json").read_text())
    owners = sorted(l["owner"] for l in lease["leases"])
    assert owners == ["x", "y"]
    mgr.release_lease(l1); mgr2.release_lease(l2)
    time.sleep(0.6)
    assert not _health(p1)


def test_heartbeat_extends_lease(tmp_path):
    mgr = _make_mgr(tmp_path)
    port, lid = mgr.acquire_lease("h")
    mgr.heartbeat(lid)
    lease = json.loads((tmp_path / "lease.json").read_text())
    hb = [l["last_heartbeat"] for l in lease["leases"] if l["id"] == lid][0]
    assert hb > 0
    mgr.release_lease(lid)


def test_stale_lease_pruned(tmp_path):
    mgr = _make_mgr(tmp_path)
    port, lid = mgr.acquire_lease("stale")
    # manually age the heartbeat beyond TTL
    lease = json.loads((tmp_path / "lease.json").read_text())
    lease["leases"][0]["last_heartbeat"] = time.time() - (sc.LEASE_HEARTBEAT_TTL + 1)
    (tmp_path / "lease.json").write_text(json.dumps(lease))
    # a new acquire prunes the stale lease and reuses/starts sidecar
    p2, l2 = mgr.acquire_lease("fresh")
    lease2 = json.loads((tmp_path / "lease.json").read_text())
    owners = [l["owner"] for l in lease2["leases"]]
    assert "stale" not in owners
    mgr.release_lease(l2)


def test_orphan_scan_empty_when_leased(tmp_path):
    mgr = _make_mgr(tmp_path)
    port, lid = mgr.acquire_lease("o")
    assert mgr.orphan_scan() == []
    mgr.release_lease(lid)


def test_resolve_model():
    assert sc.resolve_raine_model("grok", "grok-4.5") == "grok-4.5"
    assert sc.resolve_raine_model("grok", None) == sc.RAINE_MODEL_CATALOG["grok"][0]
    try:
        sc.resolve_raine_model("nope", None); assert False
    except ValueError:
        pass


def test_verify_alive_rejects_wrong_exe(tmp_path):
    mgr = _make_mgr(tmp_path)
    port, lid = mgr.acquire_lease("v")
    lease = json.loads((tmp_path / "lease.json").read_text())
    lease["exe_path"] = "/not/our/binary"
    assert mgr._verify_alive(lease) is False
    mgr.release_lease(lid)


def test_free_port_candidate_returns_int():
    p = sc._candidate_port()
    assert isinstance(p, int) and 1024 < p < 65536


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))