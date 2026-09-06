import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from conftest import FakeDNSPod

from certbot_dnspod_hook import cli
from certbot_dnspod_hook.config import HookError, load_config
from certbot_dnspod_hook.core import Hook, challenge
from certbot_dnspod_hook.state import StateStore


def write_config(tmp_path, extra=""):
    path = tmp_path / "config.toml"
    path.write_text(
        'zones = ["example.com"]\nstate_dir = "' + str(tmp_path / "state") + '"\n' + extra
    )
    path.chmod(0o600)
    return path


def test_config_env_credentials_and_longest_zone(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "hidden")
    config = load_config(path)
    assert config.secret_key == "hidden"
    assert "hidden" not in repr(config)
    config = replace(config, zones=("example.com", "nested.example.com"))
    assert config.zone_for("www.nested.example.com") == "nested.example.com"


@pytest.mark.parametrize(
    "extra",
    [
        "ttl = false",
        "ttl = 0",
        "propagation_seconds = -1",
        "propagation_timeout = 120",
        'unknown = "typo"',
    ],
)
def test_invalid_settings_fail_before_network(tmp_path, extra):
    with pytest.raises(HookError):
        load_config(write_config(tmp_path, extra))


def test_world_readable_config_rejected(tmp_path):
    path = write_config(tmp_path)
    path.chmod(0o644)
    with pytest.raises(HookError, match="mode 600"):
        load_config(path)


def test_cli_roundtrip_and_legacy_environment(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    api = FakeDNSPod()
    monkeypatch.setattr(cli, "DNSPod", lambda _: api)
    monkeypatch.setattr(cli, "wait_for_txt", lambda *args: None)
    monkeypatch.delenv("CERTBOT_IDENTIFIER", raising=False)
    monkeypatch.setenv("CERTBOT_DOMAIN", "example.com")
    monkeypatch.setenv("CERTBOT_VALIDATION", "a" * 43)
    prefix = ["--config", str(path)]
    assert cli.main(prefix + ["auth"]) == 0
    key = capsys.readouterr().out.strip()
    assert key == challenge("example.com", "a" * 43)[2]
    monkeypatch.setenv("CERTBOT_AUTH_OUTPUT", "wrong-id")
    assert cli.main(prefix + ["cleanup"]) == 1
    assert not api.deleted
    monkeypatch.setenv("CERTBOT_AUTH_OUTPUT", key)
    assert cli.main(prefix + ["cleanup"]) == 0
    assert api.deleted == [1]
    monkeypatch.setattr(cli, "DNSPod", Mock(side_effect=AssertionError("No API call expected")))
    assert cli.main(prefix + ["cleanup"]) == 0
    assert cli.main(prefix + ["status"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_failed_auth_cleanup_falls_back_to_environment(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    config = load_config(path)
    api, store = FakeDNSPod(), StateStore(config.state_dir)
    hook = Hook(config, api, store, lambda *args: None)
    hook.auth("example.com", "a" * 43)
    monkeypatch.setattr(cli, "DNSPod", lambda _: api)
    monkeypatch.setenv("CERTBOT_IDENTIFIER", "example.com")
    monkeypatch.setenv("CERTBOT_VALIDATION", "a" * 43)
    monkeypatch.delenv("CERTBOT_AUTH_OUTPUT", raising=False)
    assert cli.main(["--config", str(path), "cleanup"]) == 0
    assert api.deleted == [1]


def test_state_permissions_lock_and_path_traversal(config):
    with StateStore(config.state_dir) as store:
        assert config.state_dir.stat().st_mode & 0o777 == 0o700
        key = "a" * 64
        store.write(key, {"record_id": 7})
        assert store.path(key).stat().st_mode & 0o777 == 0o600
        with pytest.raises(HookError, match="Another hook"):
            with StateStore(config.state_dir):
                pass
        with pytest.raises(HookError, match="Invalid state ID"):
            store.read("../../etc/shadow")
    with StateStore(config.state_dir):
        pass


def test_state_symlink_rejected(config, tmp_path):
    store = StateStore(config.state_dir)
    key = "a" * 64
    target = tmp_path / "target"
    target.write_text("{}")
    store.path(key).symlink_to(target)
    with pytest.raises(OSError):
        store.read(key)


def test_status_does_not_create_a_directory_or_lock(tmp_path, capsys):
    config = write_config(tmp_path)
    directory = tmp_path / "state"
    assert cli.main(["--config", str(config), "status"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not directory.exists()
    directory.mkdir(mode=0o700)
    assert cli.main(["--config", str(config), "status"]) == 0
    assert list(directory.iterdir()) == []
