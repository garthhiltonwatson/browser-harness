import base64
import io

import pytest
from PIL import Image


def make_png(width, height):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def fake_png():
    return make_png


@pytest.fixture(autouse=True)
def _isolated_lease_runtime(tmp_path_factory, monkeypatch):
    """Every test gets its own lease dir. Without this, a test that calls
    run.main() acquires the REAL per-daemon lease on the developer's machine —
    and since the lease now anchors on the long-lived parent (CLAUDE_PID, the
    session leader), it would hold the live browser for minutes after the
    test run ends."""
    from browser_harness import lease

    monkeypatch.setattr(lease.ipc, "_RUNTIME", tmp_path_factory.mktemp("lease-rt"))
    for var in ("BH_HOLDER", "BH_HOLDER_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
        monkeypatch.delenv(var, raising=False)
