import argparse
import shlex
import subprocess
from pathlib import Path

import pytest

from certbot_dnspod_hook import setup as module
from certbot_dnspod_hook.config import HookError, load_config


def arguments(*extra):
    parser = argparse.ArgumentParser()
    module.add_arguments(parser)
    return parser.parse_args(["--cert-name", "example.com", "--zone", "example.com", *extra])


def test_credentials_file_imports_quotes_without_execution(tmp_path):
    path = tmp_path / "keys.env"
    path.write_text(
        "# private\nTENCENTCLOUD_SECRET_ID=\"test-id\"\nTENCENTCLOUD_SECRET_KEY='test-key'\n"
    )
    path.chmod(0o600)
    assert module.read_credentials(arguments("--credentials-file", str(path))) == (
        "test-id",
        "test-key",
        "",
    )
    path.write_text(
        "TENCENTCLOUD_SECRET_ID=test-id\nTENCENTCLOUD_SECRET_KEY=$(touch /tmp/should-not-exist)\n"
    )
    with pytest.raises(HookError, match="shell expressions"):
        module.read_credentials(arguments("--credentials-file", str(path)))


@pytest.mark.parametrize(
    "contents",
    [
        "TENCENTCLOUD_SECRET_ID=id\n",
        "TENCENTCLOUD_SECRET_ID=id\nTENCENTCLOUD_SECRET_ID=duplicate\n",
        "UNKNOWN=value\n",
        'TENCENTCLOUD_SECRET_ID="unclosed\n',
    ],
)
def test_invalid_credential_file_is_rejected(tmp_path, contents):
    path = tmp_path / "keys"
    path.write_text(contents)
    path.chmod(0o600)
    with pytest.raises(HookError):
        module.read_credentials(arguments("--credentials-file", str(path)))


def test_credential_permissions_and_mixed_sources(tmp_path):
    path = tmp_path / "keys"
    path.write_text("TENCENTCLOUD_SECRET_ID=id\nTENCENTCLOUD_SECRET_KEY=key\n")
    path.chmod(0o644)
    with pytest.raises(HookError, match="mode 600"):
        module.read_credentials(arguments("--credentials-file", str(path)))
    with pytest.raises(HookError, match="not both"):
        module.read_credentials(arguments("--credentials-file", str(path), "--secret-key", "key"))


def test_snap_symlink_selects_snap_timer_without_resolving_to_snap_binary(tmp_path):
    link = tmp_path / "certbot"
    link.symlink_to("/snap/bin/certbot")
    assert module.certbot_timer(str(link)) == "snap.certbot.renew.timer"
    assert module.certbot_timer("/usr/bin/non-snap-certbot") == "certbot.timer"


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "require_root", lambda: None)
    monkeypatch.setattr(module, "check_hook_installation", lambda: None)
    monkeypatch.setattr(module, "executable", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(module, "certbot_timer", lambda _: "snap.certbot.renew.timer")
    monkeypatch.setattr(module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(module, "RENEWAL_DIR", tmp_path / "renewal")
    module.RENEWAL_DIR.mkdir()
    renewal = module.RENEWAL_DIR / "example.com.conf"
    original = (
        "cert = /etc/letsencrypt/live/example.com/cert.pem\n"
        "[renewalparams]\nauthenticator = manual\npost_hook = keep-me\n"
    )
    renewal.write_text(original)
    calls = []

    def run(command, *, capture=False):
        calls.append(command)
        if "--version" in command:
            return "certbot 5.8.0"
        if "x509" in command:
            return "DNS:example.com, DNS:*.example.com"
        if "show" in command:
            return "loaded\n"
        if "reconfigure" in command:
            auth = command[command.index("--manual-auth-hook") + 1]
            cleanup = command[command.index("--manual-cleanup-hook") + 1]
            renewal.write_text(
                original + f"manual_auth_hook = {auth}\nmanual_cleanup_hook = {cleanup}\n"
            )
        return ""

    monkeypatch.setattr(module, "run", run)
    return calls, run, renewal, original


def test_setup_stages_before_renewal_and_never_passes_keys(system, monkeypatch, capsys):
    calls, _, renewal, original = system
    assert (
        module.setup(
            arguments("--secret-id", "private-id", "--secret-key", "private-key", "--renew-now")
        )
        == 0
    )
    reconfigure = next(command for command in calls if "reconfigure" in command)
    renew = next(command for command in calls if "renew" in command)
    assert calls.index(reconfigure) < calls.index(renew)
    assert "--force-renewal" in renew
    assert any("enable" in command and "snap.certbot.renew.timer" in command for command in calls)
    output = capsys.readouterr().out
    assert "private-id" not in output and "private-key" not in output
    assert "private-key" not in repr(calls)
    assert "post_hook = keep-me" in renewal.read_text()
    assert next(module.CONFIG_DIR.glob("*.renewal-backup")).read_text() == original
    hook = shlex.split(reconfigure[reconfigure.index("--manual-auth-hook") + 1])
    config = load_config(Path(hook[hook.index("--config") + 1]))
    assert config.secret_id == "private-id" and config.secret_key == "private-key"


def test_failed_staging_preserves_old_configuration_and_prevents_production(system, monkeypatch):
    calls, normal_run, renewal, original = system

    def failed(command, **kwargs):
        if "reconfigure" in command:
            calls.append(command)
            raise HookError("staging failed")
        return normal_run(command, **kwargs)

    monkeypatch.setattr(module, "run", failed)
    with pytest.raises(HookError, match="staging failed"):
        module.setup(arguments("--secret-id", "id", "--secret-key", "key", "--renew-now"))
    assert renewal.read_text() == original
    assert not any("renew" in command or "enable" in command for command in calls)
    assert len(list(module.CONFIG_DIR.glob("*.toml"))) == 1


def test_setup_without_renew_now_only_connects(system, monkeypatch):
    calls, _, _, _ = system
    module.setup(arguments("--secret-id", "id", "--secret-key", "key"))
    assert not any("renew" in command for command in calls)


def test_subprocess_environment_does_not_forward_credentials(monkeypatch):
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "private-key")
    received = []

    def fake(command, **kwargs):
        received.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    monkeypatch.setattr(module.subprocess, "run", fake)
    assert module.run(["/usr/bin/certbot", "--version"], capture=True) == "ok"
    assert "TENCENTCLOUD_SECRET_KEY" not in received[0]["env"]


def test_uncovered_certificate_domain_fails_before_writing_configuration(system, monkeypatch):
    _, normal_run, renewal, original = system

    def other_domain(command, **kwargs):
        if "x509" in command:
            return "DNS:example.com, DNS:outside.net"
        return normal_run(command, **kwargs)

    monkeypatch.setattr(module, "run", other_domain)
    with pytest.raises(HookError, match="outside"):
        module.setup(arguments("--secret-id", "id", "--secret-key", "key"))
    assert not module.CONFIG_DIR.exists()
    assert renewal.read_text() == original


def test_failed_production_renewal_keeps_successfully_saved_hooks(system, monkeypatch):
    _, normal_run, renewal, _ = system

    def failed(command, **kwargs):
        if "renew" in command:
            raise HookError("production failed")
        return normal_run(command, **kwargs)

    monkeypatch.setattr(module, "run", failed)
    with pytest.raises(HookError, match="production failed"):
        module.setup(arguments("--secret-id", "id", "--secret-key", "key", "--renew-now"))
    assert "manual_auth_hook" in renewal.read_text()
    assert len(list(module.CONFIG_DIR.glob("*.toml"))) == 1
