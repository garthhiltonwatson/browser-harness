import json

import pytest

from browser_harness import telemetry


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point telemetry config at a tmp dir and clear every opt-out/opt-in env."""
    monkeypatch.setattr(telemetry, "_config_dir", lambda: tmp_path)
    for name in telemetry.DISABLE_ENVS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write(cfg, data):
    (cfg / "telemetry.json").write_text(json.dumps(data), encoding="utf-8")


def test_off_by_default_on_fresh_install(cfg):
    # No config file at all -> telemetry must be OFF.
    assert telemetry.is_enabled() is False
    assert telemetry.status()["enabled"] is False


def test_empty_config_is_off(cfg):
    _write(cfg, {})
    assert telemetry.is_enabled() is False


def test_install_id_not_minted_while_disabled(cfg):
    # A disabled install must not create/persist an identity.
    telemetry.status()
    assert not (cfg / "telemetry.json").exists() or \
        "install_id" not in json.loads((cfg / "telemetry.json").read_text())
    assert telemetry.status()["install_id"] is None


def test_explicit_opt_in_enables(cfg):
    _write(cfg, {"enabled": True})
    assert telemetry.is_enabled() is True


def test_legacy_disabled_false_still_opts_in(cfg):
    _write(cfg, {"disabled": False})
    assert telemetry.is_enabled() is True


def test_env_opt_out_beats_config_opt_in(cfg, monkeypatch):
    _write(cfg, {"enabled": True})
    monkeypatch.setenv("BH_TELEMETRY", "0")
    assert telemetry.is_enabled() is False


def test_env_opt_in(cfg, monkeypatch):
    monkeypatch.setenv("BH_TELEMETRY", "1")
    assert telemetry.is_enabled() is True


def test_set_enabled_round_trip_writes_positive_flag(cfg):
    telemetry.set_enabled(True)
    assert telemetry.is_enabled() is True
    data = json.loads((cfg / "telemetry.json").read_text())
    assert data.get("enabled") is True and "disabled" not in data

    telemetry.set_enabled(False)
    assert telemetry.is_enabled() is False
    data = json.loads((cfg / "telemetry.json").read_text())
    assert data.get("enabled") is False


def test_capture_sends_nothing_when_disabled(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(telemetry, "_send_detached", lambda payload: sent.append(payload))
    telemetry.capture_cli_event(action="completed", command="script", task="secret pw here")
    assert sent == []
