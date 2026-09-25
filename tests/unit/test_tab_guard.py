"""Tab guard: an unattended run may act only on tabs it opened (BH_TAB_GUARD=1)."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from browser_harness import helpers


FOREIGN = {"targetId": "FOREIGN", "url": "https://mail.example.com/", "title": "Inbox"}


def _fake_send(current=FOREIGN, created="MINE", session="SESSION-MINE", target_type="page"):
    def send(req, response_timeout=None):
        if req.get("meta") == "current_tab":
            return dict(current)
        method = req.get("method")
        if method == "Target.createTarget":
            return {"result": {"targetId": created}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": session}}
        if method == "Target.getTargetInfo":
            return {"result": {"targetInfo": {"type": target_type}}}
        return {"result": {}}
    return send


@pytest.fixture
def guard(tmp_path, monkeypatch):
    """Guard on, ownership isolated to tmp_path, daemon attached to a tab this
    run did not open."""
    monkeypatch.setenv("BH_TAB_GUARD", "1")
    monkeypatch.setenv("BH_TAB_GUARD_RUN", "test-run")
    monkeypatch.delenv("BH_TAB_GUARD_LOG", raising=False)
    monkeypatch.setattr(helpers.ipc, "_TMP", tmp_path)
    helpers.tab_guard_reset()
    monkeypatch.setattr(helpers, "_send", _fake_send())


@pytest.fixture
def owning(guard, monkeypatch):
    """As `guard`, but the run has opened a tab and the daemon is attached to it."""
    assert helpers.cdp("Target.createTarget", url="about:blank")["targetId"] == "MINE"
    monkeypatch.setattr(helpers, "_send", _fake_send(current={"targetId": "MINE", "url": "https://example.com/", "title": "t"}))


# --- refusals -------------------------------------------------------------

def test_refuses_navigating_a_tab_the_run_did_not_open(guard, capsys):
    with pytest.raises(helpers.TabGuardRefused):
        helpers.goto_url("https://example.com/")
    assert "[tab-guard] REFUSED Page.navigate FOREIGN https://example.com/" in capsys.readouterr().err


def test_refuses_closing_and_activating_someone_elses_tab(guard):
    for method in ("Target.closeTarget", "Target.activateTarget", "Target.attachToTarget"):
        with pytest.raises(helpers.TabGuardRefused):
            helpers.cdp(method, targetId="FOREIGN")


@pytest.mark.parametrize("call,label", [
    (lambda: helpers.js("location.href='https://example.com'"), "js navigation"),
    (lambda: helpers.js("document.querySelector('button').click()"), "js click"),
    (lambda: helpers.page_info(), "page_info"),
    (lambda: helpers.capture_screenshot(), "screenshot"),
    (lambda: helpers.type_text("hello"), "type_text"),
    (lambda: helpers.press_key("Enter"), "press_key"),
    (lambda: helpers.click_at_xy(10, 10), "click_at_xy"),
    (lambda: helpers.scroll(0, 0), "scroll"),
    (lambda: helpers.cdp("DOM.setFileInputFiles", files=["/etc/passwd"]), "DOM.setFileInputFiles"),
    (lambda: helpers.cdp("Page.setDocumentContent", html="<h1>x</h1>"), "Page.setDocumentContent"),
])
def test_refuses_every_session_scoped_call_on_a_foreign_tab(guard, call, label):
    """The reason this is an allowlist. A blocklist of "mutating" methods misses
    Runtime.evaluate, and js() is the ordinary fallback for a blocked click —
    so the guard would look present while being trivially bypassable."""
    with pytest.raises(helpers.TabGuardRefused):
        call()


def test_fails_closed_when_the_attached_tab_cannot_be_read(tmp_path, monkeypatch):
    """"Unknown" must not mean "permitted": treating an unreadable current tab
    as nothing-to-refuse turns any daemon hiccup into a bypass."""
    monkeypatch.setenv("BH_TAB_GUARD", "1")
    monkeypatch.setenv("BH_TAB_GUARD_RUN", "test-run")
    monkeypatch.setattr(helpers.ipc, "_TMP", tmp_path)
    helpers.tab_guard_reset()

    def flaky(req, response_timeout=None):
        if req.get("meta") == "current_tab":
            raise RuntimeError("daemon unreachable")
        return {"result": {}}

    monkeypatch.setattr(helpers, "_send", flaky)
    with pytest.raises(helpers.TabGuardRefused) as exc:
        helpers.goto_url("https://example.com/")
    assert "failing closed" in str(exc.value)


# --- what must still work -------------------------------------------------

def test_allows_everything_on_a_tab_the_run_opened(owning):
    helpers.goto_url("https://example.com/page")
    helpers.js("document.title")
    helpers.type_text("hi")
    helpers.cdp("Target.closeTarget", targetId="MINE")


def test_allows_enumerating_tabs_without_attaching(guard):
    """Refusing reads on a foreign tab costs a run nothing, because
    Target.getTargets still answers "what else is open?"."""
    helpers.cdp("Target.getTargets")
    helpers.cdp("Target.getTargetInfo", targetId="FOREIGN")


def test_allows_attaching_to_a_subframe_while_attached_to_a_tab_the_run_holds(owning, monkeypatch):
    """js(target_id=...) reaches an out-of-process iframe, whose target id is
    only discoverable through a page the run already has. The unit of ownership
    is the TAB, so a subframe is not a separate thing to protect — but only
    while the daemon is actually attached to an owned tab (see the refusal
    test below for why)."""
    monkeypatch.setattr(helpers, "_send", _fake_send(
        current={"targetId": "MINE", "url": "https://example.com/", "title": "t"},
        target_type="iframe",
    ))
    helpers.cdp("Target.attachToTarget", targetId="SOME-IFRAME", flatten=True)


def test_refuses_a_foreign_iframe_reached_while_attached_to_a_foreign_tab(guard, monkeypatch):
    """CDP's Target domain exposes no parent-tab link for an iframe target, so
    the guard cannot verify a given iframe id belongs to an OWNED tab — it can
    only refuse to treat "not a page target" as blanket permission while the
    run is attached to a tab it does not own. Without this, discovering a
    foreign tab's iframe (e.g. via iframe_target(url_substr), which scans ALL
    targets) and attaching to it would bypass the guard entirely."""
    monkeypatch.setattr(helpers, "_send", _fake_send(target_type="iframe"))
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.attachToTarget", targetId="SOME-FOREIGN-IFRAME", flatten=True)


def test_allows_a_session_this_run_attached_and_refuses_one_it_did_not(owning):
    sid = helpers.cdp("Target.attachToTarget", targetId="MINE", flatten=True)["sessionId"]
    helpers.cdp("Runtime.evaluate", session_id=sid, expression="1")  # ours: fine
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Runtime.evaluate", session_id="SOMEONE-ELSES", expression="1")


def test_new_tab_does_not_reuse_a_blank_tab_the_run_does_not_own(guard):
    """Upstream new_tab() navigates the attached tab when it is blank. A blank
    tab is still someone's tab, so under the guard a fresh one is always made."""
    assert helpers._may_reuse_attached_tab() is False


def test_new_tab_may_still_reuse_a_blank_tab_the_run_opened(owning):
    assert helpers._may_reuse_attached_tab() is True


def test_guard_off_by_default_leaves_every_tab_reachable(tmp_path, monkeypatch):
    monkeypatch.delenv("BH_TAB_GUARD", raising=False)
    monkeypatch.setattr(helpers.ipc, "_TMP", tmp_path)
    monkeypatch.setattr(helpers, "_send", _fake_send())
    helpers.goto_url("https://example.com/")
    helpers.js("document.title")
    assert helpers._may_reuse_attached_tab() is True


# --- ownership bookkeeping ------------------------------------------------

def test_ownership_is_scoped_to_the_run_id(guard, monkeypatch):
    monkeypatch.setenv("BH_TAB_GUARD_RUN", "run-1")
    helpers.cdp("Target.createTarget", url="about:blank")
    assert helpers._owned_ids() == {"MINE"}
    monkeypatch.setenv("BH_TAB_GUARD_RUN", "run-2")
    assert helpers._owned_ids() == set()
    with pytest.raises(helpers.TabGuardRefused):
        helpers.cdp("Target.closeTarget", targetId="MINE")


def test_a_concurrent_run_cannot_consume_this_run_s_ownership(guard, monkeypatch):
    """Two harness users commonly share a daemon name (the default is
    "default"). A single shared state file let one overwrite the other's tabs,
    which refuses a run on its own tab mid-task."""
    helpers.cdp("Target.createTarget", url="about:blank")
    assert helpers._owned_ids() == {"MINE"}
    monkeypatch.setenv("BH_TAB_GUARD_RUN", "")   # a different (unguarded) user
    helpers._own_tab("THEIRS")
    monkeypatch.setenv("BH_TAB_GUARD_RUN", "test-run")
    assert helpers._owned_ids() == {"MINE"}


def test_ownership_crosses_a_process_boundary(guard, tmp_path):
    """One run is usually several harness invocations, so ownership must survive
    a real process exit — which an in-process assertion cannot demonstrate."""
    helpers.cdp("Target.createTarget", url="about:blank")
    script = (
        "import pathlib, json\n"
        "from browser_harness import helpers\n"
        f"helpers.ipc._TMP = pathlib.Path({str(tmp_path)!r})\n"
        "print(json.dumps(sorted(helpers._owned_ids())))\n"
    )
    env = {**os.environ, "BH_TAB_GUARD": "1", "BH_TAB_GUARD_RUN": "test-run"}
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["MINE"]


def test_concurrent_own_tab_calls_do_not_drop_each_others_entries(guard):
    """Two invocations sharing one BH_TAB_GUARD_RUN (a session and its own
    subagent) can call _own_tab() around the same time. Without the lock
    around _remember()'s read-modify-write, a race here drops whichever
    write lost — stranding a tab the run genuinely opened."""
    import threading

    ids = [f"T{i}" for i in range(20)]
    threads = [threading.Thread(target=helpers._own_tab, args=(tid,)) for tid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert helpers._owned_ids() == set(ids)


def test_a_failed_ownership_write_is_reported_not_swallowed(guard, monkeypatch, capsys):
    """Silence here would present a disk problem as a guard bug: the run would
    be refused on tabs it did open, with nothing saying why."""
    monkeypatch.setattr(helpers, "_owned_path", lambda: pathlib.Path("/nonexistent-dir/owned.json"))
    helpers._own_tab("MINE")
    assert "[tab-guard] WARNING could not record ownership of MINE" in capsys.readouterr().err


def test_corrupt_ownership_state_reads_as_empty_rather_than_crashing(guard):
    helpers._owned_path().write_text("{not json")
    assert helpers._owned_ids() == set()
    helpers._owned_path().write_text('["legacy-list-form"]')
    assert helpers._owned_ids() == set()


# --- refusal reporting ----------------------------------------------------

def test_refusal_is_appended_to_BH_TAB_GUARD_LOG(guard, tmp_path, monkeypatch):
    """A supervisor counting refusals usually cannot see this process's stderr —
    it is a grandchild whose output is captured by whatever spawned it."""
    log = tmp_path / "run.log"
    monkeypatch.setenv("BH_TAB_GUARD_LOG", str(log))
    with pytest.raises(helpers.TabGuardRefused):
        helpers.goto_url("https://example.com/")
    assert "[tab-guard] REFUSED Page.navigate FOREIGN https://example.com/" in log.read_text()


def test_an_unwritable_log_does_not_swallow_the_refusal(guard, monkeypatch):
    monkeypatch.setenv("BH_TAB_GUARD_LOG", "/nonexistent-dir/run.log")
    with pytest.raises(helpers.TabGuardRefused):
        helpers.goto_url("https://example.com/")
