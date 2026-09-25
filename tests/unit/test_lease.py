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
    lease.acquire("t1", holder="alice", ttl=0.05, wait=0)
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
