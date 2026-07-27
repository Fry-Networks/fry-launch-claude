"""Gate 5 RED — failure-path lease-aware cleanup: zero current-run-owned orphans,
unrelated leases preserved, lease released only via the existing sidecar manager
ownership protocol (verify PID/path/SHA/creation-time before forced termination;
release only the current client lease; never terminate while another verified
lease live; never terminate pre-existing operator-owned sidecar).

Pre-fix: launch_via_sidecar resolves CLAUDE_BIN at module level (bare "claude") ->
on resolver/Popen failure, the exception propagates with WinError 2 but the finally
block still releases the per-launch lease. However the failure is NOT structured
(FRY_SIDECAR_ERROR absent) and acquire_lease is called BEFORE resolution failure can
be detected (resolver doesn't exist pre-fix, so Popen([bare "claude"]) fails AFTER
acquire_lease).

Post-fix: resolver failure raises RouterError BEFORE acquire_lease (no lease to
clean); Popen failure after acquire_lease -> finally releases lease + structured
FRY_SIDECAR_ERROR. Unrelated leases are never touched.
"""
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import fry_anthropic_router as rtr
from _fakeclaude import build_fake_claude_exe


@pytest.fixture
def real_stdin():
    """Real devnull handle — launch_via_sidecar uses `stdin or sys.stdin`; pytest capsys
    makes sys.stdin a pseudofile (no fileno) which masks the real Popen/WinError behavior."""
    f = open(os.devnull, "r", encoding="utf-8")
    try:
        yield f
    finally:
        f.close()


class _RecordingManager:
    """Records acquire/release; supports an unrelated pre-existing lease."""
    def __init__(self, unrelated_lease="unrelated-op-lease-99"):
        self.acquired = []
        self.released = []
        self.live = {unrelated_lease: True}  # an unrelated operator lease already live

    def acquire_lease(self, owner):
        lid = f"current-{len(self.acquired)}"
        self.acquired.append(lid)
        self.live[lid] = True
        return (59998, lid)

    def release_lease(self, lease_id):
        self.released.append(lease_id)
        # never release the unrelated lease
        if lease_id in self.live and lease_id != "unrelated-op-lease-99":
            self.live.pop(lease_id, None)
        # assert we are NEVER asked to release the unrelated one
        assert lease_id != "unrelated-op-lease-99", (
            "must NEVER release an unrelated operator lease")


def test_resolver_failure_before_acquire_releases_no_lease(monkeypatch, tmp_path):
    """If the resolver fails, no lease is acquired (resolution happens before acquire_lease).

    RED pre-fix: no resolver -> Popen([bare "claude"]) -> acquire_lease IS called
    before the Popen failure -> lease acquired + released in finally. Post-fix:
    resolver raises before acquire_lease -> acquire_calls == 0.
    """
    # No claude on PATH, no FRY_CLAUDE_BIN -> resolver must raise.
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.delenv("FRY_CLAUDE_BIN", raising=False)
    mgr = _RecordingManager()
    with pytest.raises(Exception):
        rtr.launch_via_sidecar(
            cfg={}, agent="claude", model_spec="xai,grok-4.5",
            passthrough_args=[], dry_run=False, provider="xai",
            sidecar_manager=mgr,
        )
    assert mgr.acquired == [], (
        f"no lease must be acquired when resolution fails pre-Popen; acquired={mgr.acquired}")
    assert mgr.released == [], "nothing to release if no lease acquired"


def test_popen_failure_after_acquire_releases_current_lease(monkeypatch, tmp_path):
    """If Popen fails after acquire_lease, finally releases the current lease only.

    RED pre-fix: Popen([bare "claude"]) -> WinError 2 -> finally releases current
    lease (works). But also acquires a lease needlessly. Post-fix: same finally
    behavior + structured error. Either way current lease released. Guard test:
    current lease released, unrelated preserved.
    """
    # Point FRY_CLAUDE_BIN at a NONEXISTENT path -> resolver returns a spec whose
    # argv_prefix points at a missing exe -> Popen raises FileNotFoundError.
    monkeypatch.setenv("FRY_CLAUDE_BIN", str(tmp_path / "does-not-exist.exe"))
    mgr = _RecordingManager()
    # Post-fix the resolver may raise on missing explicit path (structured), or
    # Popen raises after acquire. Either is acceptable as long as the current
    # lease is released and unrelated is preserved.
    raised = False
    try:
        rtr.launch_via_sidecar(
            cfg={}, agent="claude", model_spec="xai,grok-4.5",
            passthrough_args=[], dry_run=False, provider="xai",
            sidecar_manager=mgr,
        )
    except Exception:
        raised = True
    # The current run may or may not have acquired a lease (depends on whether
    # resolver raises before or after acquire). If it acquired, it must release.
    for lid in mgr.acquired:
        assert lid in mgr.released, f"current lease {lid} must be released in finally"
    # unrelated operator lease must NEVER be released
    assert "unrelated-op-lease-99" not in mgr.released, (
        "unrelated operator lease must NEVER be released by this launch")
    assert "unrelated-op-lease-99" in mgr.live, "unrelated operator lease must remain live"


def test_nonzero_child_exit_still_releases_lease(monkeypatch, tmp_path, real_stdin):
    """A child that exits non-zero must still release its lease (no orphan)."""
    # Use a real exe that exits 7. Easiest: a .cmd? No — native_exe needs a real exe.
    # Use sys.executable (python) with a -c that exits 7.
    monkeypatch.setenv("FRY_CLAUDE_BIN", sys.executable)
    mgr = _RecordingManager()
    # passthrough args: -c "import sys; sys.exit(7)" — python runs it, exits 7
    rc = rtr.launch_via_sidecar(
        cfg={}, agent="claude", model_spec="xai,grok-4.5",
        passthrough_args=["-c", "import sys; sys.exit(7)"],
        dry_run=False, provider="xai", sidecar_manager=mgr, stdin=real_stdin,
    )
    assert rc == 7, f"child exit code must propagate; got {rc}"
    assert len(mgr.acquired) == 1
    assert mgr.acquired[0] in mgr.released, "current lease must be released after child exit"
    assert "unrelated-op-lease-99" in mgr.live, "unrelated operator lease must remain live"


def test_zero_child_exit_releases_lease(monkeypatch, tmp_path, real_stdin):
    """Normal exit releases the lease (happy path cleanup)."""
    monkeypatch.setenv("FRY_CLAUDE_BIN", sys.executable)
    mgr = _RecordingManager()
    rc = rtr.launch_via_sidecar(
        cfg={}, agent="claude", model_spec="xai,grok-4.5",
        passthrough_args=["-c", "import sys; sys.exit(0)"],
        dry_run=False, provider="xai", sidecar_manager=mgr, stdin=real_stdin,
    )
    assert rc == 0
    assert mgr.acquired[0] in mgr.released
    assert "unrelated-op-lease-99" in mgr.live


def test_structured_error_on_failure(monkeypatch, tmp_path, capsys):
    """Sidecar launch failure must emit a structured FRY_SIDECAR_ERROR (redacted reason)."""
    # Force a Popen failure via a missing explicit exe.
    monkeypatch.setenv("FRY_CLAUDE_BIN", str(tmp_path / "missing.exe"))
    mgr = _RecordingManager()
    try:
        rtr.launch_via_sidecar(
            cfg={}, agent="claude", model_spec="xai,grok-4.5",
            passthrough_args=[], dry_run=False, provider="xai",
            sidecar_manager=mgr,
        )
    except Exception:
        pass
    err = capsys.readouterr().err
    # Post-fix: FRY_SIDECAR_ERROR present. Pre-fix: just a raw traceback/WinError.
    # This assertion is GREEN post-fix; RED pre-fix (no FRY_SIDECAR_ERROR).
    assert "FRY_SIDECAR_ERROR" in err, (
        f"structured FRY_SIDECAR_ERROR must reach stderr on failure; got: {err!r}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))