"""X write guard: no write on x.com / twitter.com by browser automation (Garth 2026-11-15)."""
import pytest

from browser_harness import helpers


def _send_at(url, calls=None):
    def send(req, response_timeout=None):
        if calls is not None:
            calls.append(req)
        if req.get("meta") == "current_tab":
            return {"targetId": "T", "url": url, "title": ""} if url is not None else {"error": "not_attached"}
        return {"result": {}}
    return send


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
    assert not any(c.get("method", "").startswith(("Input.", "DOM.setFile", "Runtime.")) for c in calls)


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
    monkeypatch.setattr(helpers, "_send", _send_at(None))
    with pytest.raises(helpers.XWriteRefused):
        helpers.click_at_xy(10, 10)
    helpers.scroll(10, 10)
    helpers.js("document.title")
