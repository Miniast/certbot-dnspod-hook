import json
from types import SimpleNamespace

import pytest

from certbot_dnspod_hook.config import HookError
from certbot_dnspod_hook.management import register, remove_installation


def snapshot(root):
    return {
        str(path.relative_to(root)): ("link", str(path.readlink()))
        if path.is_symlink()
        else ("file", path.read_bytes())
        if path.is_file()
        else ("dir",)
        for path in root.rglob("*")
    }


@pytest.fixture
def installation(tmp_path):
    directory, state_root, prefix = [tmp_path / name for name in ("config", "state", "program")]
    renewal_root = tmp_path / "letsencrypt/renewal"
    bin_dir = tmp_path / "bin"
    for path in (directory, state_root, prefix, renewal_root, bin_dir):
        path.mkdir(parents=True)
    launcher = bin_dir / "certbot-dnspod-hook"
    slot = prefix / "versions" / ("0.3.0-" + "a" * 12)
    slot.mkdir(parents=True)
    (slot / "owned-runtime").write_text("application and dependencies")
    (prefix / "current").symlink_to(slot)
    launcher.symlink_to(prefix / "current/bin/certbot-dnspod-hook")
    (prefix / "install.json").write_text(
        json.dumps({"version": 1, "launcher": str(launcher), "versions": [slot.name]})
    )
    (prefix / ".install.lock").touch()
    (directory / ".setup.lock").touch()
    state = state_root / "example.com"
    state.mkdir()
    (state / ".lock").touch()
    cert = renewal_root.parent / "live/example.com/fullchain.pem"
    cert.parent.mkdir(parents=True)
    cert.write_text("KEEP CERTIFICATE")
    baseline = (
        f"cert = {cert}\nversion = 5.8.0\n[renewalparams]\n"
        "authenticator = manual\npref_challs = dns-01,\npost_hook = user-deploy\n"
    )
    cfg = directory / ("example.com-" + "b" * 12 + ".toml")
    cfg.write_text('secret_id = "example-id"\nsecret_key = "example-key"\n')
    backup = cfg.with_suffix(".renewal-backup")
    backup.write_text(baseline)
    script = directory / "nginx-deploy.sh"
    script.write_text("owned deploy script")
    current = (
        baseline
        + f"manual_auth_hook = {launcher} --config {cfg} auth\n"
        + f"manual_cleanup_hook = {launcher} --config {cfg} cleanup\n"
        + f"renew_hook = {script}\n[acme_renewal_info]\n"
        + "ari_retry_after = KEEP-NEW-TIMESTAMP\n"
    )
    renewal = renewal_root / "example.com.conf"
    renewal.write_text(current)
    register(directory, "example.com", cfg, backup, launcher, final=current, deploy_owned=True)
    return SimpleNamespace(
        root=tmp_path,
        directory=directory,
        state=state_root,
        prefix=prefix,
        renewal_root=renewal_root,
        launcher=launcher,
        renewal=renewal,
        cfg=cfg,
        cert=cert,
        current=current,
    )


def remove(item, *, detach=True, dry_run=False):
    return remove_installation(
        SimpleNamespace(detach=detach, dry_run=dry_run),
        item.directory,
        item.state,
        item.renewal_root,
        item.prefix,
        item.launcher,
    )


def test_preview_has_no_writes(installation):
    before = snapshot(installation.root)
    assert remove(installation, dry_run=True) == 0
    assert snapshot(installation.root) == before


def test_clean_uninstall_restores_only_owned_fields_and_preserves_certificate(installation):
    item = installation
    assert remove(item) == 0
    assert not item.directory.exists() and not item.state.exists() and not item.prefix.exists()
    assert not item.launcher.is_symlink()
    assert item.cert.read_text() == "KEEP CERTIFICATE"
    text = item.renewal.read_text()
    assert "manual_auth_hook" not in text and "renew_hook" not in text
    assert "post_hook = user-deploy" in text
    assert "ari_retry_after = KEEP-NEW-TIMESTAMP" in text
    assert not (item.renewal_root.parent / ".certbot.lock").exists()


@pytest.mark.parametrize("change", ["credentials", "renewal", "pending", "symlink"])
def test_modified_or_pending_data_prevents_any_removal(installation, change):
    item = installation
    if change == "credentials":
        item.cfg.write_text("changed credentials")
    elif change == "renewal":
        item.renewal.write_text(
            item.current.replace("authenticator = manual", "authenticator = webroot")
        )
    elif change == "pending":
        (item.state / "example.com/challenge.json").write_text("{}")
    else:
        target = item.root / "elsewhere"
        target.mkdir()
        (item.state / "example.com/.lock").unlink()
        (item.state / "example.com").rmdir()
        (item.state / "example.com").symlink_to(target)
    before = snapshot(item.root)
    with pytest.raises(HookError):
        remove(item)
    assert snapshot(item.root) == before


def test_untracked_config_file_is_preserved(installation):
    extra = installation.directory / "user-notes.txt"
    extra.write_text("KEEP USER FILE")
    remove(installation)
    assert extra.read_text() == "KEEP USER FILE"
    assert sorted(path.name for path in installation.directory.iterdir()) == ["user-notes.txt"]


def test_explicit_detach_is_required_for_live_references(installation):
    before = snapshot(installation.root)
    with pytest.raises(HookError, match="--detach"):
        remove(installation, detach=False)
    assert snapshot(installation.root) == before


def test_untracked_state_refuses_uninstall(installation):
    (installation.state / "untracked.json").write_text("{}")
    before = snapshot(installation.root)
    with pytest.raises(HookError, match="Untracked state"):
        remove(installation)
    assert snapshot(installation.root) == before


def test_already_removed_owned_file_does_not_prevent_cleanup(installation):
    installation.cfg.unlink()
    remove(installation)
    assert not installation.prefix.exists()
    assert not installation.directory.exists()
    assert installation.cert.read_text() == "KEEP CERTIFICATE"
