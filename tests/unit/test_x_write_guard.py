"""X write guard: no write on x.com / twitter.com by browser automation (Garth 2026-11-15)."""
import pytest

from browser_harness import helpers


def _send_at(url, calls=None, point="", session_url=None):
    def send(req, response_timeout=None):
        if calls is not None:
            calls.append(req)
        if req.get("meta") == "current_tab":
            return {"targetId": "T", "url": url, "title": ""} if url is not None else {"error": "not_attached"}
        expr = (req.get("params") or {}).get("expression", "")
        if expr == "location.href":
            return {"result": {"result": {"value": session_url}}}
        if "elementFromPoint" in expr:
            return {"result": {"result": {"value": point}}}
        return {"result": {}}
    return send


def _writes(calls):
    return [c for c in calls if c.get("method", "").startswith(("Input.", "DOM.set"))
            or (c.get("method") == "Runtime.evaluate" and "click" in c["params"].get("expression", ""))]


@pytest.fixture(autouse=True)
def no_tab_guard(monkeypatch):
    monkeypatch.delenv("BH_TAB_GUARD", raising=False)
    monkeypatch.delenv("BH_X_READ_ONLY_HARVEST", raising=False)


X_URLS = ["https://x.com/home", "https://twitter.com/compose/post", "https://mobile.x.com/i/bookmarks", "https://x.com"]


@pytest.mark.parametrize("url", X_URLS)
@pytest.mark.parametrize("act", [
    lambda: helpers.click_at_xy(10, 10),
    lambda: helpers.type_text("hello"),
    lambda: helpers.press_key("Enter"),
    lambda: helpers.press_key("a"),
    lambda: helpers.js("document.querySelector('[data-testid=\"tweetButton\"]').click()"),
    lambda: helpers.js("fetch('/i/api/graphql/x/CreateTweet', {method: 'POST'})"),
    lambda: helpers.cdp("DOM.setFileInputFiles", files=["/tmp/a.png"], nodeId=1),
    lambda: helpers.cdp("Input.dispatchTouchEvent", type="touchStart", touchPoints=[]),
])
def test_every_write_on_x_is_refused(monkeypatch, url, act):
    calls = []
    monkeypatch.setattr(helpers, "_send", _send_at(url, calls))
    with pytest.raises(helpers.XWriteRefused):
        act()
    assert not _writes(calls)


@pytest.mark.parametrize("act", [
    lambda: helpers.scroll(10, 10),
    lambda: helpers.cdp("Input.dispatchMouseEvent", type="mouseMoved", x=1, y=1),
    lambda: helpers.js("document.title"),
    lambda: helpers.js("[...document.querySelectorAll('[data-testid=\"tweetText\"]')].map(e => e.innerText)"),
    lambda: helpers.goto_url("https://x.com/i/bookmarks"),
])
def test_reads_on_x_pass(monkeypatch, act):
    monkeypatch.setattr(helpers, "_send", _send_at("https://x.com/i/bookmarks"))
    act()


def test_harvest_may_click_and_scroll_but_never_type_or_touch_an_action(monkeypatch):
    monkeypatch.setenv("BH_X_READ_ONLY_HARVEST", "1")
    monkeypatch.setattr(helpers, "_send", _send_at("https://x.com/i/bookmarks"))
    helpers.click_at_xy(10, 10)
    helpers.js("document.querySelector('[data-testid=\"SideNav_AccountSwitcher_Button\"]').click()")
    helpers.press_key("Escape")
    helpers.press_key("PageDown")
    for act in (
        lambda: helpers.type_text("hi"),
        lambda: helpers.press_key("a"),
        lambda: helpers.press_key("Enter"),
        lambda: helpers.js("document.querySelector('[data-testid=\"like\"]').click()"),
        lambda: helpers.js("document.querySelector('[data-testid=\"retweet\"]').click()"),
        lambda: helpers.js("document.querySelector('[data-testid=\"reply\"]').click()"),
        lambda: helpers.js("document.querySelector('[data-testid=\"removeBookmark\"]').click()"),
        lambda: helpers.cdp("DOM.setFileInputFiles", files=["/tmp/a.png"], nodeId=1),
    ):
        with pytest.raises(helpers.XWriteRefused):
            act()


@pytest.mark.parametrize("url", ["https://example.com/", "https://www.netflix.com/", "https://box.com/x.com", "https://notx.com/"])
def test_other_sites_are_untouched(monkeypatch, url):
    monkeypatch.setattr(helpers, "_send", _send_at(url))
    helpers.click_at_xy(10, 10)
    helpers.type_text("hello")
    helpers.press_key("Enter")
    helpers.js("document.querySelector('button').click()")


def test_unreadable_url_fails_closed_for_writes_only(monkeypatch):
    calls = []
    monkeypatch.setattr(helpers, "_send", _send_at(None, calls))
    with pytest.raises(helpers.XGuardUnavailable, match="not_attached"):
        helpers.click_at_xy(10, 10)
    assert sum(c.get("meta") == "current_tab" for c in calls) == 2  # one retry before refusing
    helpers.scroll(10, 10)
    helpers.js("document.title")


@pytest.mark.parametrize("act", [
    lambda: helpers.cdp("Runtime.callFunctionOn", functionDeclaration="function(){this.click()}", objectId="1"),
    lambda: helpers.cdp("Page.addScriptToEvaluateOnNewDocument", source="document.body.click()"),
    lambda: helpers.cdp("Page.navigate", url="javascript:document.forms[0].submit()"),
    lambda: helpers.cdp("DOM.setOuterHTML", nodeId=1, outerHTML="<p>"),
    lambda: helpers.cdp("Input.insertText", text="hi"),
    lambda: helpers.js("navigator.sendBeacon('/i/api/1.1/x', 'a')"),
    lambda: helpers.js("const r=new XMLHttpRequest();r.open('POST','/i/api/x');r.send()"),
    lambda: helpers.js("fetch('/i/api/graphql/q/CreateNoteTweet', {})"),
    lambda: helpers.cdp("Input.dispatchKeyEvent", type="keyDown", key="ArrowDown", commands=["selectAll"]),
])
def test_other_write_routes_on_x_are_refused_even_for_the_harvest(monkeypatch, act):
    monkeypatch.setenv("BH_X_READ_ONLY_HARVEST", "1")
    monkeypatch.setattr(helpers, "_send", _send_at("https://x.com/home"))
    with pytest.raises(helpers.XWriteRefused):
        act()


def test_explicit_session_is_checked_by_its_own_url(monkeypatch):
    # The attached tab is elsewhere, but the addressed session is on X.
    monkeypatch.setattr(helpers, "_send", _send_at("https://example.com/", session_url="https://x.com/home"))
    with pytest.raises(helpers.XWriteRefused):
        helpers.cdp("Input.insertText", session_id="S1", text="hi")
    monkeypatch.setattr(helpers, "_send", _send_at("https://x.com/home", session_url="https://example.com/"))
    helpers.cdp("Input.insertText", session_id="S1", text="hi")


@pytest.mark.parametrize("point", ["like|Like", "retweet|Repost", "reply|Reply", "|Follow @someone", "tweetButtonInline|Post"])
def test_harvest_click_on_an_action_button_is_refused(monkeypatch, point):
    monkeypatch.setenv("BH_X_READ_ONLY_HARVEST", "1")
    calls = []
    monkeypatch.setattr(helpers, "_send", _send_at("https://x.com/i/bookmarks", calls, point=point))
    with pytest.raises(helpers.XWriteRefused):
        helpers.click_at_xy(10, 10)
    assert not _writes(calls)


def test_harvest_flag_in_env_file_is_ignored(monkeypatch, tmp_path):
    p = tmp_path / ".env"
    p.write_text("BH_X_READ_ONLY_HARVEST=1\nBH_SOME_OTHER=1\n")
    monkeypatch.delenv("BH_SOME_OTHER", raising=False)
    helpers._load_env_file(p)
    import os
    assert os.environ.get("BH_X_READ_ONLY_HARVEST") is None
    assert os.environ.get("BH_SOME_OTHER") == "1"
    monkeypatch.delenv("BH_SOME_OTHER", raising=False)
