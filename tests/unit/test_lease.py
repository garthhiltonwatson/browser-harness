import os
import time

import pytest

from browser_harness import lease


@pytest.fixture(autouse=True)
def isolated_runtime_dir(tmp_path, monkeypatch):
    """Point the lease module's runtime dir at a fresh tmp dir per test, so
    tests never collide with a real daemon's lease file or each other."""
    monkeypatch.setattr(lease.ipc, "_RUNTIME", tmp_path)
    monkeypatch.delenv("BH_HOLDER", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


# --- holder_identity() ------------------------------------------------------

def test_holder_identity_prefers_bh_holder(monkeypatch):
    monkeypatch.setenv("BH_HOLDER", "loop:social-posting")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    assert lease.holder_identity() == "loop:social-posting"


def test_holder_identity_falls_back_to_claude_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    assert lease.holder_identity() == "claude-session:abc-123"


def test_holder_identity_falls_back_to_session_leader(monkeypatch):
    assert lease.holder_identity().startswith(("pgrp:", "pid:"))


# --- acquire(): fresh + renewal ---------------------------------------------

def test_acquire_fresh_lease_when_none_exists():
    record = lease.acquire("t1", holder="alice", ttl=60, wait=0)
    assert record["holder"] == "alice"
    assert lease.status("t1")["holder"] == "alice"


def test_lone_user_never_waits():
    """Same holder re-acquiring (renewing) must return instantly, no polling."""
    lease.acquire("t1", holder="alice", ttl=60, wait=0)
    start = time.time()
    record = lease.acquire("t1", holder="alice", ttl=60, wait=120)
    assert time.time() - start < 1.0
    assert record["holder"] == "alice"


def test_renew_extends_expiry():
    first = lease.acquire("t1", holder="alice", ttl=60, wait=0)
    time.sleep(0.05)
    second = lease.acquire("t1", holder="alice", ttl=60, wait=0)
    assert second["expires_at"] > first["expires_at"]


# --- contention: wait then busy ---------------------------------------------

def test_contention_waits_then_raises_lease_busy():
    lease.acquire("t1", holder="alice", ttl=60, wait=0)
    waited = []
    start = time.time()
    with pytest.raises(lease.LeaseBusy) as ei:
        lease.acquire("t1", holder="bob", ttl=60, wait=0.3, on_wait=waited.append)
    elapsed = time.time() - start
    assert elapsed >= 0.2
    assert "alice" in str(ei.value)
    assert "held by alice" in str(ei.value)
    assert len(waited) >= 1


def test_busy_exit_code_constant_is_75():
    assert lease.EXIT_BUSY == 75


# --- expiry takeover ---------------------------------------------------------

def test_expired_lease_is_taken_over_without_waiting():
    rec = lease.acquire("t1", holder="alice", ttl=0.05, wait=0)
    lease._write("t1", {**rec, "invocation_pid": _find_dead_pid()})  # alice's call has ended
    time.sleep(0.1)
    took_over = []
    start = time.time()
    record = lease.acquire("t1", holder="bob", ttl=60, wait=120, on_takeover=took_over.append)
    assert time.time() - start < 1.0
    assert record["holder"] == "bob"
    assert took_over and took_over[0]["holder"] == "alice"


# --- dead-pid takeover --------------------------------------------------------

def test_dead_pid_lease_is_taken_over_without_waiting():
    dead_pid = _find_dead_pid()
    lease._write("t1", {
        "holder": "alice", "pid": dead_pid, "host": lease._hostname(),
        "acquired_at": time.time(), "expires_at": time.time() + 600,
    })
    took_over = []
    start = time.time()
    record = lease.acquire("t1", holder="bob", ttl=60, wait=120, on_takeover=took_over.append)
    assert time.time() - start < 1.0
    assert record["holder"] == "bob"
    assert took_over and took_over[0]["holder"] == "alice"


def test_live_pid_on_other_host_is_not_taken_over_just_because_pid_looks_dead():
    """A lease recorded on a different host can never be proven dead from here
    — only its expiry may free it."""
    lease._write("t1", {
        "holder": "alice", "pid": 999999999, "host": "some-other-host",
        "acquired_at": time.time(), "expires_at": time.time() + 600,
    })
    with pytest.raises(lease.LeaseBusy):
        lease.acquire("t1", holder="bob", ttl=60, wait=0.1)


def _find_dead_pid():
    """A pid that is (almost certainly) not running anything, for takeover tests."""
    candidate = 999999
    while True:
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            candidate -= 1
            continue
        candidate -= 1


# --- release() ----------------------------------------------------------------

def test_release_own_lease():
    lease.acquire("t1", holder="alice", ttl=60, wait=0)
    assert lease.release("t1", holder="alice") is True
    assert lease.status("t1") is None


def test_release_is_noop_for_someone_elses_lease():
    lease.acquire("t1", holder="alice", ttl=60, wait=0)
    assert lease.release("t1", holder="bob") is False
    assert lease.status("t1")["holder"] == "alice"


def test_release_with_no_lease_returns_false():
    assert lease.release("t1", holder="alice") is False


# --- status() -----------------------------------------------------------------

def test_status_none_when_unheld():
    assert lease.status("t1") is None


def test_status_reflects_current_holder():
    lease.acquire("t1", holder="alice", ttl=60, wait=0)
    assert lease.status("t1")["holder"] == "alice"


# --- independent daemons never contend ---------------------------------------

def test_different_bu_names_do_not_contend():
    lease.acquire("nameA", holder="alice", ttl=60, wait=0)
    record = lease.acquire("nameB", holder="bob", ttl=60, wait=0)
    assert record["holder"] == "bob"


# =============================================================================
# Owner-anchored lease (2026-09-28). Two incidents: an agent's calls ran while
# another agent was mid-publish to Substack, and an agent deleted another's
# hand-rolled mkdir lock. Root cause in this module: the lease recorded the pid
# of the short-lived browser-harness INVOCATION, so the moment each call
# exited the lease read as "holder dead" and anyone took it over — the lease
# only covered a single running call, never a multi-call task.
# =============================================================================

import json
import subprocess
import sys
from pathlib import Path

from browser_harness import run as _run_mod

SRC = str(Path(__file__).resolve().parents[2] / "src")


def _child_env(extra=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("BH_HOLDER", "BH_HOLDER_PID", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID")}
    env.update({
        "PYTHONPATH": SRC,
        "BH_RUNTIME_DIR": str(lease.ipc._RUNTIME),
        "BH_RUNTIME_DIR_SHARED": "1",  # same bu-<name> stem as this process
    })
    env.update(extra or {})
    return env


def _child_acquire(holder, extra_env=None, wait=0):
    """Acquire in a SEPARATE short-lived process, like one CLI invocation."""
    code = (
        "import sys\n"
        "from browser_harness import lease\n"
        f"try:\n    lease.acquire('t1', wait={wait})\n    print('GOT')\n"
        "except lease.LeaseBusy:\n    print('BUSY')\n"
    )
    env = _child_env({"BH_HOLDER": holder, **(extra_env or {})})
    return subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True)


def test_concurrent_acquire_exactly_one_winner():
    procs = [_child_acquire(f"agent-{i}", {"BH_HOLDER_PID": str(os.getpid())}) for i in range(8)]
    outs = [p.communicate(timeout=30)[0].strip() for p in procs]
    assert outs.count("GOT") == 1, outs
    assert outs.count("BUSY") == 7, outs


def test_lease_outlives_the_invocation_while_the_owner_lives():
    """The regression: invocation exits, owner (session/tick) still alive ->
    the lease must still block everyone else."""
    p = _child_acquire("publisher", {"BH_HOLDER_PID": str(os.getpid())})
    assert p.communicate(timeout=30)[0].strip() == "GOT"
    with pytest.raises(lease.LeaseBusy):
        lease.acquire("t1", holder="intruder", wait=0.2)
    assert lease.status("t1")["holder"] == "publisher"


def test_claude_pid_is_the_default_owner_anchor(monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    assert lease.owner_pid() == os.getpid()


def test_dead_owner_anchor_falls_through_to_session_leader(monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", str(_find_dead_pid()))
    assert lease.owner_pid() != int(os.environ["CLAUDE_PID"])


def test_reentrant_owner_across_invocations_keeps_acquired_at():
    first = _child_acquire("publisher", {"BH_HOLDER_PID": str(os.getpid())})
    assert first.communicate(timeout=30)[0].strip() == "GOT"
    acquired = lease.status("t1")["acquired_at"]
    time.sleep(0.05)
    second = _child_acquire("publisher", {"BH_HOLDER_PID": str(os.getpid())})
    assert second.communicate(timeout=30)[0].strip() == "GOT"
    rec = lease.status("t1")
    assert rec["acquired_at"] == acquired
    assert rec["heartbeat_at"] > acquired


def test_explicit_holder_gets_the_long_ttl_implicit_the_short(monkeypatch):
    monkeypatch.setenv("BH_HOLDER", "publisher")
    rec = lease.acquire("t1", wait=0)
    assert rec["expires_at"] - rec["heartbeat_at"] == pytest.approx(lease.DEFAULT_TTL)
    monkeypatch.delenv("BH_HOLDER")
    rec = lease.acquire("t2", wait=0)
    assert rec["expires_at"] - rec["heartbeat_at"] == pytest.approx(lease.IMPLICIT_TTL)
    assert lease.DEFAULT_TTL == 1200 and lease.IMPLICIT_TTL < lease.DEFAULT_TTL


def test_explicit_holder_blocks_its_own_sessions_implicit_calls(monkeypatch):
    """A subagent that set BH_HOLDER is protected even from sibling agents of
    the same Claude session (which share the implicit session identity)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "same-session")
    lease.acquire("t1", holder="substack-publish", wait=0)
    with pytest.raises(lease.LeaseBusy):
        lease.acquire("t1", wait=0.1)


def test_stale_takeover_only_when_owner_dead_and_is_logged():
    live = {"holder": "alice", "pid": os.getpid(), "host": lease._hostname(),
            "acquired_at": time.time(), "heartbeat_at": time.time(), "expires_at": time.time() + 600}
    lease._write("t1", live)
    with pytest.raises(lease.LeaseBusy):
        lease.acquire("t1", holder="bob", wait=0.1)
    lease._write("t1", {**live, "pid": _find_dead_pid()})
    assert lease.acquire("t1", holder="bob", wait=0)["holder"] == "bob"
    events = [json.loads(line) for line in lease._log_path("t1").read_text().splitlines()]
    assert any(e["event"] == "takeover" and e["from"] == "alice" and e["to"] == "bob" for e in events)


def test_busy_timeout_is_logged():
    lease.acquire("t1", holder="alice", wait=0)
    with pytest.raises(lease.LeaseBusy):
        lease.acquire("t1", holder="bob", wait=0)
    events = [json.loads(line) for line in lease._log_path("t1").read_text().splitlines()]
    assert any(e["event"] == "busy" and e["holder"] == "alice" and e["waiter"] == "bob" for e in events)


def test_old_format_record_with_dead_invocation_pid_is_still_free():
    """Rollout: a lease written by the pre-anchor code (no heartbeat_at, pid =
    a finished invocation) must not wedge the first new-code caller."""
    lease._write("t1", {"holder": "old", "pid": _find_dead_pid(), "host": lease._hostname(),
                        "acquired_at": time.time(), "expires_at": time.time() + 600})
    assert lease.acquire("t1", holder="new", wait=0)["holder"] == "new"


# --- CLI: the gate is enforced, not advisory ------------------------------------

def _cli(args, stdin="", extra_env=None, timeout=60):
    env = _child_env(extra_env)
    env["BU_NAME"] = "t1"
    env["BH_LEASE_WAIT"] = "0.3"
    return subprocess.run([sys.executable, "-m", "browser_harness.run", *args], input=stdin,
                          env=env, capture_output=True, text=True, timeout=timeout)


def test_cli_busy_exits_75_and_never_runs_the_script():
    lease.acquire("t1", holder="publisher", owner=os.getpid(), wait=0)
    r = _cli([], stdin="print('RAN-WITHOUT-LOCK')\n", extra_env={"BH_HOLDER": "intruder"})
    assert r.returncode == 75
    assert "RAN-WITHOUT-LOCK" not in r.stdout
    assert "browser busy: held by publisher" in r.stderr


def test_cli_acquire_prints_a_holder_token_and_holds_the_lease():
    r = _cli(["--acquire", "substack"], extra_env={"BH_HOLDER_PID": str(os.getpid())})
    assert r.returncode == 0, r.stderr
    token = r.stdout.strip().splitlines()[0]
    assert token.startswith("BH_HOLDER=substack-")
    assert lease.status("t1")["holder"] == token.split("=", 1)[1]


def test_cli_acquire_busy_exits_75():
    lease.acquire("t1", holder="publisher", owner=os.getpid(), wait=0)
    r = _cli(["--acquire"], extra_env={"BH_HOLDER": "intruder"})
    assert r.returncode == 75
    assert lease.status("t1")["holder"] == "publisher"


def test_cli_release_never_deletes_a_foreign_live_lease():
    lease.acquire("t1", holder="publisher", owner=os.getpid(), wait=0)
    r = _cli(["--release"], extra_env={"BH_HOLDER": "intruder"})
    assert r.returncode == 0
    assert "held by publisher" in r.stderr and "left in place" in r.stderr
    assert lease.status("t1")["holder"] == "publisher"


# --- review round (PR #3) -----------------------------------------------------

def test_explicit_holder_with_fallback_owner_is_not_freed_by_owner_death():
    """A non-Claude caller (no CLAUDE_PID) whose owner guess is a per-command
    shell: that shell dying between calls must not free a declared task."""
    lease._write("t1", {"holder": "oc-task", "pid": _find_dead_pid(), "owner_src": "fallback",
                        "explicit": True, "invocation_pid": _find_dead_pid(), "host": lease._hostname(),
                        "acquired_at": time.time(), "heartbeat_at": time.time(), "expires_at": time.time() + 600})
    with pytest.raises(lease.LeaseBusy):
        lease.acquire("t1", holder="intruder", wait=0.1)


def test_implicit_fallback_owner_frees_once_owner_and_call_are_gone():
    lease._write("t1", {"holder": "pgrp:1", "pid": _find_dead_pid(), "owner_src": "fallback",
                        "explicit": False, "invocation_pid": _find_dead_pid(), "host": lease._hostname(),
                        "acquired_at": time.time(), "heartbeat_at": time.time(), "expires_at": time.time() + 600})
    assert lease.acquire("t1", holder="next", wait=0)["holder"] == "next"


def test_expired_lease_is_not_taken_while_its_call_is_still_running():
    """A single long call never renews its own heartbeat."""
    lease._write("t1", {"holder": "alice", "pid": os.getpid(), "owner_src": "env", "explicit": False,
                        "invocation_pid": os.getpid(), "host": lease._hostname(),
                        "acquired_at": time.time() - 900, "heartbeat_at": time.time() - 900,
                        "expires_at": time.time() - 600})
    with pytest.raises(lease.LeaseBusy):
        lease.acquire("t1", holder="bob", wait=0.1)


def test_cli_reload_respects_a_foreign_lease():
    lease.acquire("t1", holder="publisher", owner=os.getpid(), wait=0)
    r = _cli(["--reload"], extra_env={"BH_HOLDER": "intruder"})
    assert r.returncode == 75
    assert "held by publisher" in r.stderr


# --- launchd jobs (sid 1) -----------------------------------------------------

def test_launchd_jobs_do_not_share_one_identity_or_anchor_on_launchd(monkeypatch):
    """Found live 2026-09-28: a launchd job's lease read `pgrp:1` — every
    scheduled job shared one identity, and pid 1 as owner never dies."""
    monkeypatch.setattr(lease.os, "getsid", lambda _pid: 1)
    assert lease.holder_identity() == f"ppid:{os.getppid()}"
    assert lease.owner_pid() == os.getppid()


def test_orphan_under_launchd_never_anchors_on_pid_1(monkeypatch):
    monkeypatch.setattr(lease.os, "getsid", lambda _pid: 1)
    monkeypatch.setattr(lease.os, "getppid", lambda: 1)
    assert lease.holder_identity() == f"pid:{os.getpid()}"
    assert lease.owner_pid() == os.getpid()
