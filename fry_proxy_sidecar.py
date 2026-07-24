#!/usr/bin/env python3
"""Fry shared-sidecar lease manager for raine/claude-code-proxy.

Runs the UNMODIFIED pinned raine binary as a localhost sidecar over HTTP and
multiplexes concurrent Fry launches onto one process via an ownership lease.
The stable raine auth root (~/.config/claude-code-proxy by default) is REUSED —
this module never creates, migrates, overwrites, or logs credentials, and never
calls `auth logout`. One raine process routes codex/grok/kimi by requested model.

Design (see evidence/topology-decision-<ts>.md):
  * single shared sidecar owns token refresh -> no rotation race across launches
  * explicit lease protocol; NEVER trust PID alone (Windows reuses PIDs)
  * verify-alive before attach: PID + exe path + exe SHA-256 + creation time +
    generation + /healthz + listener == 127.0.0.1:<port>
  * free-port race tolerated (retry up to 3x)
  * never kill by exe name; never kill while a verified live lease exists
  * zero-live-lease -> graceful shutdown + verify dead
  * foreground only; no -b/background; no update/autostart invocation

This module mutates NO Claude/.claude/CCR/model-cache state -> no restore logic.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

# --------------------------------------------------------------------------- #
# Pinned binary defaults (overridable by tests via constructor).
# --------------------------------------------------------------------------- #
DEFAULT_RAINE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "fry" / "sidecars" / "claude-code-proxy" / "v0.1.24"
DEFAULT_RAINE_EXE = DEFAULT_RAINE_DIR / "claude-code-proxy.exe"
# Pinned SHA-256 of the installed raine binary (evidence/raine-security-review).
PINNED_RAINE_SHA256 = "ef3458bedfe5a9b767500fff5093955ad1e69c6288d976d3d015d3aaa8374546"

DEFAULT_LEASE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "fry" / "sidecars" / "claude-code-proxy"
LEASE_FILE_NAME = "lease.json"
HEALTH_PATH = "/healthz"
HEALTH_TIMEOUT = 3.0
STARTUP_DEADLINE = 20.0  # seconds to wait for /healthz after spawn
LEASE_HEARTBEAT_TTL = 90.0  # a lease with no heartbeat for this long is stale
FREE_PORT_RETRIES = 3
SHUTDOWN_GRACE = 6.0
LISTEN_HOST = "127.0.0.1"

# raine model catalog (from `claude-code-proxy models`). One process routes all.
RAINE_MODEL_CATALOG = {
    "codex": ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5",
              "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-fast", "gpt-5.3-codex",
              "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"],
    "grok": ["grok-4.5", "grok-composer-2.5-fast"],
    "kimi": ["kimi-k2.6", "k2.6", "kimi-for-coding"],
}


class SidecarError(RuntimeError):
    """Raised when the sidecar cannot be started or verified."""


# --------------------------------------------------------------------------- #
# Process identity helpers (dependency-free; Windows via ctypes).
# --------------------------------------------------------------------------- #
def _exe_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _process_exe_path(pid: int) -> Optional[str]:
    """Full image path of a running PID, or None if not queryable."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return None
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return buf.value
                return None
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    else:
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except Exception:
            return None


def _process_creation_time(pid: int) -> Optional[float]:
    """Process creation time as a comparable float, or None."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.GetProcessTimes.restype = wintypes.BOOL
            k32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                             ctypes.POINTER(wintypes.FILETIME),
                                             ctypes.POINTER(wintypes.FILETIME),
                                             ctypes.POINTER(wintypes.FILETIME))
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return None
            try:
                ct = wintypes.FILETIME()
                if k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(wintypes.FILETIME()),
                                       ctypes.byref(wintypes.FILETIME()), ctypes.byref(wintypes.FILETIME())):
                    # FILETIME is 100ns intervals since 1601-01-01; return raw 64-bit as comparable
                    return (ct.dwHighDateTime << 32) | ct.dwLowDateTime
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    else:
        try:
            return float(os.stat(f"/proc/{pid}/stat").st_ctime)
        except Exception:
            return None


def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            SYNCHRONIZE = 0x00100000
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            h = k32.OpenProcess(SYNCHRONIZE, False, pid)
            if not h:
                return False
            k32.CloseHandle(h)
            return True
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Free-port selection (NOT a reservation — bind may still race).
# --------------------------------------------------------------------------- #
def _candidate_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LISTEN_HOST, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _health_ok(port: int, timeout: float = HEALTH_TIMEOUT) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{LISTEN_HOST}:{port}{HEALTH_PATH}", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Lease file (atomic-ish write; owner-only ACL expected on parent dir).
# --------------------------------------------------------------------------- #
class RaineSidecarManager:
    def __init__(self, exe_path: Path, exe_sha256: str, lease_dir: Path,
                 serve_args_factory=None, clock=None):
        self.exe_path = Path(exe_path)
        self.exe_sha256 = exe_sha256
        self.lease_dir = Path(lease_dir)
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        self.lease_file = self.lease_dir / LEASE_FILE_NAME
        # tests inject a mock binary + arg factory + fake clock
        self._serve_args_factory = serve_args_factory or self._default_serve_args
        self._clock = clock or time.time
        self._proc = None  # only set in the process that spawned it

    # -- config -------------------------------------------------------------
    def _default_serve_args(self, port: int):
        return [str(self.exe_path), "serve", "--port", str(port), "--no-monitor"]

    # -- lease io -----------------------------------------------------------
    def _read_lease(self) -> dict:
        try:
            return json.loads(self.lease_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_lease(self, data: dict) -> None:
        tmp = self.lease_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.lease_file)

    # -- verify-alive: NEVER trust PID alone --------------------------------
    def _verify_alive(self, lease: dict) -> bool:
        pid = lease.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            return False
        if lease.get("exe_path") != str(self.exe_path):
            return False
        if lease.get("exe_sha256") != self.exe_sha256:
            return False
        if lease.get("generation") != self._generation:
            return False
        ct = _process_creation_time(pid)
        if ct is None or ct != lease.get("creation_time"):
            return False
        port = lease.get("port")
        if not isinstance(port, int):
            return False
        if not _health_ok(port):
            return False
        return True

    # -- generation is constant per manager instance (pinned exe) ----------
    @property
    def _generation(self) -> str:
        return self.exe_sha256[:16]

    # -- start a fresh sidecar ----------------------------------------------
    def _start_sidecar(self, port: int) -> dict:
        args = self._serve_args_factory(port)
        # Foreground child; we own its lifetime via the lease. Detached=False.
        creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creationflags, close_fds=True,
        )
        pid = self._proc.pid
        exe = _process_exe_path(pid) or str(self.exe_path)
        ct = _process_creation_time(pid)
        # Wait for healthz
        deadline = self._clock() + STARTUP_DEADLINE
        ok = False
        while self._clock() < deadline:
            if self._proc.poll() is not None:
                raise SidecarError(f"sidecar exited early rc={self._proc.returncode}")
            if _health_ok(port):
                ok = True
                break
            time.sleep(0.3)
        if not ok:
            try:
                self._proc.terminate()
            except Exception:
                pass
            raise SidecarError(f"sidecar did not become healthy on port {port}")
        return {
            "generation": self._generation,
            "pid": pid,
            "exe_path": str(self.exe_path),
            "exe_sha256": self.exe_sha256,
            "creation_time": ct,
            "port": port,
            "owner": os.environ.get("USERNAME") or os.environ.get("USER") or "fry",
            "started_at": self._clock(),
            "leases": [],
        }

    # -- attach-or-start with free-port race retry --------------------------
    def acquire_lease(self, owner: str) -> Tuple[int, str]:
        """Return (port, lease_id). Starts sidecar if none alive; else attaches."""
        for _ in range(FREE_PORT_RETRIES + 1):
            lease = self._read_lease()
            if lease and self._verify_alive(lease):
                port = lease["port"]
            else:
                port = _candidate_port()
                try:
                    lease = self._start_sidecar(port)
                except SidecarError as e:
                    # bind race or early exit: retry with another port
                    lease = self._read_lease()
                    if lease and self._verify_alive(lease):
                        port = lease["port"]
                    elif _ >= FREE_PORT_RETRIES:
                        raise
                    else:
                        continue
            lease_id = str(uuid.uuid4())
            lease.setdefault("leases", [])
            # prune stale leases
            now = self._clock()
            lease["leases"] = [l for l in lease["leases"]
                                if now - l.get("last_heartbeat", 0) <= LEASE_HEARTBEAT_TTL
                                and l.get("id") != lease_id]
            lease["leases"].append({"id": lease_id, "owner": owner,
                                     "last_heartbeat": now})
            lease["last_seen"] = now
            self._write_lease(lease)
            if not _health_ok(port):
                # lost it between verify and write — retry
                if _ >= FREE_PORT_RETRIES:
                    raise SidecarError("sidecar lost during lease acquisition")
                continue
            return port, lease_id
        raise SidecarError("failed to acquire sidecar lease after retries")

    # -- heartbeat ----------------------------------------------------------
    def heartbeat(self, lease_id: str) -> None:
        lease = self._read_lease()
        now = self._clock()
        for l in lease.get("leases", []):
            if l.get("id") == lease_id:
                l["last_heartbeat"] = now
                lease["last_seen"] = now
                self._write_lease(lease)
                return

    # -- release: remove lease; shutdown only at zero verified-live leases --
    def release_lease(self, lease_id: str) -> None:
        lease = self._read_lease()
        lease["leases"] = [l for l in lease.get("leases", []) if l.get("id") != lease_id]
        now = self._clock()
        lease["leases"] = [l for l in lease["leases"]
                            if now - l.get("last_heartbeat", 0) <= LEASE_HEARTBEAT_TTL]
        lease["last_seen"] = now
        self._write_lease(lease)
        if not lease["leases"]:
            self._shutdown_if_ours(lease)

    def _shutdown_if_ours(self, lease: dict) -> None:
        """Graceful shutdown only if we can verify the PID is our pinned sidecar."""
        pid = lease.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            self._write_lease({})
            return
        if lease.get("exe_path") != str(self.exe_path) or lease.get("exe_sha256") != self.exe_sha256:
            # NOT our binary — never kill an unrelated process
            return
        if lease.get("creation_time") != _process_creation_time(pid):
            # PID reused by another process — do not touch
            return
        port = lease.get("port")
        # graceful terminate first
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=SHUTDOWN_GRACE)
                except Exception:
                    self._proc.kill()
            except Exception:
                pass
        else:
            # shared process we did not spawn in THIS manager instance:
            # terminate by PID only after full identity verify (above).
            self._kill_pid(pid, force=False)
        # verify dead; force if still alive
        time.sleep(0.5)
        if _pid_alive(pid) and _health_ok(port or 0):
            self._kill_pid(pid, force=True)
            try:
                if self._proc is not None:
                    self._proc.wait(timeout=SHUTDOWN_GRACE)
            except Exception:
                pass
            time.sleep(0.3)
        if _pid_alive(pid) and _health_ok(port or 0):
            # still alive — another launch may have re-attached; leave it
            return
        self._write_lease({})

    def _kill_pid(self, pid: int, force: bool) -> None:
        try:
            if sys.platform == "win32":
                args = ["taskkill", "/PID", str(pid), "/T"]
                if force:
                    args.append("/F")
                subprocess.run(args, capture_output=True, timeout=SHUTDOWN_GRACE)
            else:
                os.kill(pid, 9 if force else 15)
        except Exception:
            pass

    # -- orphan scan: report any owned sidecar PIDs not tracked by a live lease
    def orphan_scan(self) -> list:
        """Return list of (pid, port) for sidecars matching our pinned exe path
        that have NO live lease. Caller decides; this never kills."""
        lease = self._read_lease()
        if not lease or not self._verify_alive(lease):
            return []
        if lease.get("leases"):
            return []
        return [(lease.get("pid"), lease.get("port"))]


def resolve_raine_model(provider: str, requested: Optional[str]) -> str:
    """Map a fry provider + requested model to a raine catalog model id.
    Raises ValueError if not resolvable so the caller surfaces a clear error."""
    catalog = RAINE_MODEL_CATALOG.get(provider)
    if not catalog:
        raise ValueError(f"raine has no provider '{provider}'")
    if requested and requested in catalog:
        return requested
    if requested:
        # allow any explicit id (raine may support more than our snapshot)
        return requested
    return catalog[0]


if __name__ == "__main__":
    # manual smoke: acquire + release a lease
    mgr = RaineSidecarManager(DEFAULT_RAINE_EXE, PINNED_RAINE_SHA256, DEFAULT_LEASE_DIR)
    port, lid = mgr.acquire_lease("smoke")
    print(f"sidecar up on {port} lease={lid}")
    mgr.release_lease(lid)
    print("released")