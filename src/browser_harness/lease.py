"""Exclusive per-daemon lease (opt-out; on by default via run.py).

The tab guard (see helpers.py) stops a run from acting on a tab it did not
open. It does nothing about two runs acting on the SAME daemon at the same
time: the daemon holds one attached target per BU_NAME, so an interactive
session and an unattended launchd loop (or a session and its subagent)
interleaving invocations can each move the attached tab out from under the
other. Incidents 2026-09-24/25: a subagent's password text landed in a CRM
cell, a DNS-delete click landed on an X composer, stray keystrokes duplicated
Meta ad campaigns.

The fix is a lease file per BU_NAME, next to the daemon's socket/pid files
(same _RUNTIME dir as _ipc.py — short path, AF_UNIX-safe). Every invocation
acquires or renews the lease BEFORE running the caller's script:

  - same holder                          -> renew (extend expiry)
  - different holder, lease unexpired     -> wait (poll) up to BH_LEASE_WAIT,
                                             then raise LeaseBusy
  - lease expired (no heartbeat within
    its TTL), or its OWNER pid is dead
    on this host                          -> take over, log it

Owner anchor (2026-09-28, see owner_pid()): the recorded pid is the
long-lived process that owns the task — BH_HOLDER_PID if set, else the Claude
Code process (CLAUDE_PID), else the POSIX session leader — NOT the
browser-harness invocation itself. The first cut recorded the invocation's
own pid, which is dead the moment each call returns, so every lease read as
"holder dead" between calls and any other agent took it over: the lease only
covered one running call, never a multi-call task such as a Substack publish
(incident 2026-09-28). Now the lease lives as long as its owner does, until a
heartbeat (any invocation by the same holder) is missed for a full TTL:
BH_LEASE_TTL (20 min) for an explicit BH_HOLDER, BH_LEASE_TTL_IMPLICIT
(5 min) for an implicit session identity, so a session that used the browser
once does not starve a scheduled tick for long. Takeovers and busy timeouts
are appended to <stem>.lease.log next to the lease file.

Holder identity (see holder_identity()): BH_HOLDER env if the caller set it;
else the Claude Code session id (CLAUDE_CODE_SESSION_ID) if present; else this
process's POSIX session leader. Subagents of one Claude Code session inherit
their parent's CLAUDE_CODE_SESSION_ID and therefore share one lease holder —
intentional, since they are cooperating parts of one task, not two unrelated
users. A subagent that legitimately needs its own browser turn sets BH_HOLDER
itself.

Read-modify-write is guarded by an flock'd sidecar lock file (msvcrt on
Windows) so two invocations racing to acquire can never both see "no lease"
and both write — the classic TOCTOU that a bare tempfile-rename doesn't close
on its own (rename is atomic for the WRITE, not for the preceding read+decide).
"""
import json
import os
import socket
import time
from pathlib import Path

from . import _ipc as ipc

if ipc.IS_WINDOWS:
    import msvcrt
else:
    import fcntl

DEFAULT_TTL = float(os.environ.get("BH_LEASE_TTL", 1200))                   # explicit BH_HOLDER: 20 min
IMPLICIT_TTL = float(os.environ.get("BH_LEASE_TTL_IMPLICIT", 300))         # implicit identity: 5 min
DEFAULT_WAIT = float(os.environ.get("BH_LEASE_WAIT", 120))  # 2 min
POLL_INTERVAL = 2.0
EXIT_BUSY = 75


class LeaseBusy(RuntimeError):
    """A live lease held by a different holder did not free up within BH_LEASE_WAIT."""


def _lease_path(name):
    return ipc._RUNTIME / f"{ipc._runtime_stem(name)}.lease.json"


def _lock_path(name):
    return ipc._RUNTIME / f"{ipc._runtime_stem(name)}.lease.lock"


def _log_path(name):
    return ipc._RUNTIME / f"{ipc._runtime_stem(name)}.lease.log"


def _log(name, event, **fields):
    """Append one audit line (takeover / busy). Never raises — the log is
    evidence, not a gate."""
    try:
        with open(_log_path(name), "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "event": event, **fields}) + "\n")
    except Exception:
        pass


def _hostname():
    return socket.gethostname()


def holder_identity():
    """See module docstring. Order: BH_HOLDER, Claude Code session id, POSIX
    session leader, bare pid (last-resort, e.g. non-POSIX or getsid failure)."""
    h = os.environ.get("BH_HOLDER")
    if h:
        return h
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return f"claude-session:{sid}"
    try:
        leader = os.getsid(0)
        # launchd jobs run in launchd's own session (sid 1): every scheduled
        # job would share ONE identity. Use the parent (the job's wrapper).
        if leader > 1:
            return f"pgrp:{leader}"
        return f"ppid:{os.getppid()}"
    except Exception:
        return f"pid:{os.getpid()}"


def is_explicit():
    return bool(os.environ.get("BH_HOLDER"))


def owner_pid():
    return _owner()[0]


def _owner():
    """(pid, source). source "env" = BH_HOLDER_PID / CLAUDE_PID, a process the
    caller vouches for; "fallback" = session leader / parent, which may be a
    per-command shell that exits with the call (review of PR #3).

    The long-lived process whose life bounds this holder's turn. Order:
    BH_HOLDER_PID, CLAUDE_PID (the Claude Code process — shared by a session
    and its subagents, and by a headless `claude -p` tick), the POSIX session
    leader, the parent. A candidate that is not alive is skipped; a session
    leader that is this very process (we were setsid'd) falls through to the
    parent, since our own pid dies with this call."""
    for var in ("BH_HOLDER_PID", "CLAUDE_PID"):
        v = os.environ.get(var, "")
        if v.isdigit() and int(v) > 0 and _pid_alive(int(v)):
            return int(v), "env"
    try:
        sid = os.getsid(0)
        if sid > 1 and sid != os.getpid():  # sid 1 = launchd: never dies, anchors nothing
            return sid, "fallback"
    except Exception:
        pass
    return os.getppid(), "fallback"


def _fmt(epoch):
    if not isinstance(epoch, (int, float)):
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _read(name):
    try:
        return json.loads(_lease_path(name).read_text())
    except Exception:
        return None


def _write(name, record):
    p = _lease_path(name)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(record))
    os.replace(tmp, p)  # atomic on POSIX and Windows (Python >= 3.3)


def _delete(name):
    try:
        _lease_path(name).unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid):
    """Liveness probe only — never delivers a real signal. os.kill(pid, 0) is
    POSIX-only for this purpose: on Windows, signal 0 aliases CTRL_C_EVENT, so
    os.kill(pid, 0) there can dispatch a console-control event to a live
    process instead of merely probing it. Use OpenProcess there instead."""
    if ipc.IS_WINDOWS:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not signalable by us
    except Exception:
        return True  # unknown -> assume alive, never take over on a hunch


def _is_dead(record):
    """True iff the holder is verifiably gone on THIS host. A record from a
    different host can never be proven dead here, so it is treated as alive
    (only its expiry can take it over).

    A fallback-sourced owner (session leader / parent) may be a per-command
    shell that dies with every call, so for an EXPLICIT holder — one that
    declared a multi-call task — its death proves nothing and only the TTL
    frees the lease (set BH_HOLDER_PID to get prompt dead-owner recovery). For
    an implicit holder it counts only once the invocation is gone too.
    Records with no owner_src (pre-anchor format) keep the old pid check."""
    if record.get("host") != _hostname():
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    if record.get("owner_src") == "fallback":
        if record.get("explicit"):
            return False
        return not _pid_alive(pid) and not _invocation_alive(record)
    return not _pid_alive(pid)


def _invocation_alive(record):
    ipid = record.get("invocation_pid")
    return (record.get("host") == _hostname() and isinstance(ipid, int) and ipid > 0
            and _pid_alive(ipid))


def _expired(record, now):
    """Heartbeat missed for a full TTL — unless the call that wrote it is
    still running (a single long call never refreshes its own heartbeat, and
    must not be taken over mid-script: review of PR #3)."""
    return record.get("expires_at", 0) <= now and not _invocation_alive(record)


class _FileLock:
    """flock (POSIX) / msvcrt.locking (Windows) around the lease read-modify-write."""

    def __init__(self, name):
        self._path = _lock_path(name)
        self._fh = None

    def __enter__(self):
        self._path.touch(exist_ok=True)
        self._fh = open(self._path, "r+b")
        if ipc.IS_WINDOWS:
            if self._fh.read() == b"":
                self._fh.seek(0)
                self._fh.write(b"0")
                self._fh.flush()
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            if ipc.IS_WINDOWS:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()


def acquire(name, ttl=None, wait=None, holder=None, on_wait=None, on_takeover=None, owner=None):
    """Acquire or renew the lease for daemon `name`. Returns the lease record.

    on_wait(record) is called (if given) each time this call finds a live
    lease held by someone else, before sleeping and polling again.
    on_takeover(record) is called (if given) when an expired or dead-pid
    lease is taken over from a DIFFERENT holder (not on plain renewal).

    Raises LeaseBusy if a live lease held by someone else never frees up
    within `wait` seconds (default BH_LEASE_WAIT, 120s).

    ttl defaults to DEFAULT_TTL for an explicit holder (BH_HOLDER set, or
    `holder` passed in) and IMPLICIT_TTL otherwise. owner defaults to
    owner_pid().
    """
    explicit = bool(holder) or is_explicit()
    if ttl is None:
        ttl = DEFAULT_TTL if explicit else IMPLICIT_TTL
    wait = DEFAULT_WAIT if wait is None else wait
    holder = holder or holder_identity()
    owner, owner_src = (owner, "env") if owner else _owner()
    deadline = time.time() + wait
    blocking = None
    while True:
        with _FileLock(name):
            now = time.time()
            record = _read(name)
            free = (
                record is None
                or record.get("holder") == holder
                or _expired(record, now)
                or _is_dead(record)
            )
            if free:
                # A renewal keeps the original acquired_at; re-acquiring your
                # OWN expired lease starts a fresh turn.
                renewing = (bool(record) and record.get("holder") == holder
                            and record.get("expires_at", 0) > now)
                took_over = bool(record) and record.get("holder") != holder
                new_record = {
                    "holder": holder,
                    "pid": owner,                    # the OWNER anchor, not this call
                    "owner_src": owner_src,
                    "explicit": explicit,
                    "invocation_pid": os.getpid(),
                    "host": _hostname(),
                    "acquired_at": record.get("acquired_at", now) if renewing else now,
                    "heartbeat_at": now,
                    "expires_at": now + ttl,
                }
                _write(name, new_record)
                if took_over:
                    _log(name, "takeover", **{"from": record.get("holder"), "to": holder,
                         "reason": "expired" if record.get("expires_at", 0) <= now else "owner-dead",
                         "old_pid": record.get("pid")})
                    if on_takeover:
                        on_takeover(record)
                return new_record
            blocking = record
        if time.time() >= deadline:
            _log(name, "busy", holder=blocking.get("holder"), waiter=holder)
            raise LeaseBusy(
                f"browser busy: held by {blocking.get('holder')} since "
                f"{_fmt(blocking.get('acquired_at'))}, expires {_fmt(blocking.get('expires_at'))}"
            )
        if on_wait:
            on_wait(blocking)
        time.sleep(max(0.0, min(POLL_INTERVAL, deadline - time.time())))


def release(name, holder=None):
    """Release the lease iff it is still held by `holder` (default: our own
    identity). Returns True if it released a lease, False otherwise (no
    lease, or held by someone else — never releases another holder's turn)."""
    holder = holder or holder_identity()
    with _FileLock(name):
        record = _read(name)
        if record and record.get("holder") == holder:
            _delete(name)
            return True
    return False


def status(name):
    """The current lease record, or None if unheld/absent."""
    return _read(name)
