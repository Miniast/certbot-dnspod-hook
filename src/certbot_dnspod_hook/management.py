"""Track owned configuration and remove it without deleting certificates or user files."""

import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .config import HookError

MANIFEST = "managed.json"
HOOK_KEYS = {"authenticator", "pref_challs", "manual_auth_hook", "manual_cleanup_hook"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path, content):
    fd, temporary = tempfile.mkstemp(prefix=".managed-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def fields(text):
    result, section = {}, ""
    for line in text.splitlines():
        if line.strip().startswith("["):
            section = line.strip()
        elif section == "[renewalparams]":
            match = re.match(r"^([a-z_]+)\s*=\s*(.*)$", line)
            if match:
                result[match[1]] = match[2]
    return result


def restore_fields(text, baseline, keys):
    lines, section, inserted = [], "", False

    def insert_missing():
        for key in sorted(keys):
            if key in baseline:
                lines.append(f"{key} = {baseline[key]}\n")

    for line in text.splitlines(keepends=True):
        if line.strip().startswith("["):
            if section == "[renewalparams]":
                insert_missing()
                inserted = True
            section = line.strip()
        key = line.partition("=")[0].strip()
        if section != "[renewalparams]" or key not in keys:
            lines.append(line)
    if section == "[renewalparams]":
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        insert_missing()
        inserted = True
    if not inserted:
        raise HookError("Renewal configuration is missing [renewalparams]")
    return "".join(lines)


def managed_config(text, directory, hook_command):
    command = fields(text).get("manual_auth_hook", "")
    tokens = shlex.split(command)
    if (
        len(tokens) != 4
        or tokens[0] != str(hook_command)
        or tokens[1] != "--config"
        or tokens[3] != "auth"
    ):
        return None
    path = Path(tokens[2])
    if path.parent != directory or not re.fullmatch(
        r"[A-Za-z0-9_.-]+-[0-9a-f]{12}\.toml", path.name
    ):
        raise HookError("Unrecognized hook configuration; detach it manually before uninstalling")
    return path


def register(
    directory, cert_name, config_path, backup, hook_command, *, final=None, deploy_owned=False
):
    manifest_path = directory / MANIFEST
    data = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"version": 1, "certificates": {}}
    )
    if data.get("version") != 1:
        raise HookError("Unsupported ownership metadata version")
    entries = data["certificates"]
    if cert_name not in entries:
        # Adopt configuration from 0.2.x, following only the tool's paired private backups.
        original, owned, seen = backup.read_text(), {}, set()
        while (previous := managed_config(original, directory, hook_command)) is not None:
            if previous.name in seen:
                raise HookError("Configuration backup cycle; inspect it before proceeding")
            seen.add(previous.name)
            previous_backup = previous.with_suffix(".renewal-backup")
            for path in (previous, previous_backup):
                if path.is_symlink() or not path.is_file():
                    raise HookError("Cannot verify older managed configuration")
                owned[path.name] = digest(path)
            original = previous_backup.read_text()
        entries[cert_name] = {"baseline": fields(original), "files": owned}
    entry = entries[cert_name]
    entry["files"].update({config_path.name: digest(config_path), backup.name: digest(backup)})
    current = fields(final if final is not None else backup.read_text())
    keys = set(entry.get("keys", HOOK_KEYS))
    deploy = directory / "nginx-deploy.sh"
    if deploy_owned or str(deploy) in (current.get("renew_hook"), current.get("deploy_hook")):
        keys.update(("renew_hook", "deploy_hook"))
        entry["files"][deploy.name] = digest(deploy)
    entry["keys"] = sorted(keys)
    entry["expected"] = {key: current.get(key) for key in keys}
    atomic_write(manifest_path, json.dumps(data, indent=2) + "\n")


def add_arguments(parser):
    parser.add_argument(
        "--detach", action="store_true", help="Restore only owned renewal options before removal"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate ownership and print the removal plan"
    )


def uninstall(args):
    from . import setup as setup_module

    setup_module.require_root()
    setup_module.check_hook_installation()
    directory, state_root = setup_module.CONFIG_DIR, setup_module.STATE_DIR
    for path in (directory, state_root):
        if path.exists():
            setup_module.StateStore(path, create=False)
    prefix = Path("/opt/certbot-dnspod-hook")
    return remove_installation(
        args, directory, state_root, setup_module.RENEWAL_DIR, prefix, setup_module.HOOK_COMMAND
    )


def remove_installation(args, directory, state_root, renewal_root, prefix, launcher):
    manifest_path = directory / MANIFEST
    data = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"certificates": {}}
    installation = json.loads((prefix / "install.json").read_text())
    if installation.get("version") != 1 or installation.get("launcher") != str(launcher):
        raise HookError("Installation ownership metadata does not match this command")
    versions = installation["versions"]
    if any(not re.fullmatch(r"\d+\.\d+\.\d+-[0-9a-f]{12}", name) for name in versions):
        raise HookError("Invalid version path in installation metadata")
    if not launcher.is_symlink() or os.readlink(launcher) != str(
        prefix / "current/bin/certbot-dnspod-hook"
    ):
        raise HookError("Launcher has changed; refusing to remove it")
    if (
        (prefix / "versions").is_symlink()
        or not (prefix / "current").is_symlink()
        or (prefix / "current").resolve().parent != prefix / "versions"
        or (prefix / "current").resolve().name not in versions
    ):
        raise HookError("Active version is not owned by this installation")
    for name in versions:
        if (prefix / "versions" / name).is_symlink():
            raise HookError("Version directory was replaced by a symlink")
    with operation_locks(args.dry_run, directory, prefix, renewal_root.parent) as locks:
        edits, files, states = [], set(), set()
        if state_root.exists() and any(
            path.name not in data["certificates"] for path in state_root.iterdir()
        ):
            raise HookError("Untracked state exists; inspect it before uninstalling")
        for cert_name, entry in data["certificates"].items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", cert_name):
                raise HookError("Invalid certificate in ownership metadata")
            state = state_root / cert_name
            if state.is_symlink():
                raise HookError("State directory was replaced by a symlink")
            if state.exists() and not args.dry_run:
                locks.enter_context(lock_file(state / ".lock"))
            if state.exists() and any(path.name != ".lock" for path in state.iterdir()):
                raise HookError(
                    "Pending or unknown state exists; clean up challenges before uninstalling"
                )
            states.add(state)
            for name, expected_hash in entry["files"].items():
                path = directory / name
                if (
                    Path(name).name != name
                    or path.is_symlink()
                    or path.exists()
                    and (not path.is_file() or digest(path) != expected_hash)
                ):
                    raise HookError(
                        "Managed configuration was changed; inspect it before uninstalling"
                    )
                files.add(path)
        for renewal in renewal_root.glob("*.conf"):
            text = renewal.read_text()
            if str(launcher) not in text and str(directory) not in text:
                continue
            entry = data["certificates"].get(renewal.stem)
            if not entry:
                raise HookError(
                    "An untracked certificate references this tool; detach it before uninstalling"
                )
            if not args.detach:
                raise HookError(
                    "Certbot still uses this tool; choose another hook or use uninstall --detach"
                )
            current = fields(text)
            if any(current.get(key) != value for key, value in entry["expected"].items()):
                raise HookError(
                    "Renewal options changed after setup; detach them manually to preserve changes"
                )
            restored = restore_fields(text, entry["baseline"], set(entry["keys"]))
            if str(launcher) in restored or str(directory) in restored:
                raise HookError("Unrecognized remaining hook reference; detach it manually")
            edits.append((renewal, restored))
        print("Remove program environments: " + str(prefix), flush=True)
        print(
            f"Remove {len(files)} owned config files; preserve certificates and Certbot data.",
            flush=True,
        )
        if edits:
            print(
                "Restore earlier renewal options for: " + ", ".join(path.stem for path, _ in edits),
                flush=True,
            )
            print(
                "Previously manual certificates need another hook for automatic renewal.",
                flush=True,
            )
        if args.dry_run:
            return 0
        for renewal, restored in edits:
            atomic_write(renewal, restored)
        for path in files:
            path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        (directory / ".setup.lock").unlink(missing_ok=True)
        for state in states:
            (state / ".lock").unlink(missing_ok=True)
            if state.exists():
                state.rmdir()
        for path in (state_root, directory):
            if path.exists() and not any(path.iterdir()):
                path.rmdir()
        launcher.unlink()
        (prefix / "current").unlink()
        for name in versions:
            path = prefix / "versions" / name
            if path.exists():
                shutil.rmtree(path)
        (prefix / "install.json").unlink()
        (prefix / ".install.lock").unlink(missing_ok=True)
        for path in (prefix / "versions", prefix):
            if path.exists() and not any(path.iterdir()):
                path.rmdir()
        print(
            "Uninstalled. Untracked files, certificates and Certbot timers were preserved.",
            flush=True,
        )
    return 0


@contextmanager
def lock_file(path, *, certbot=False):
    import fcntl

    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        method = fcntl.lockf if certbot else fcntl.flock
        try:
            method(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.path.samestat(os.fstat(fd), path.stat()):
                break
        except BlockingIOError:
            os.close(fd)
            raise HookError(
                "Install, setup or Certbot is running; retry after it finishes"
            ) from None
        except FileNotFoundError:
            pass
        os.close(fd)
    try:
        yield
    finally:
        # Certbot unlinks before releasing, and checks the inode when acquiring.
        if certbot and path.exists() and os.path.samestat(os.fstat(fd), path.stat()):
            path.unlink()
        os.close(fd)


@contextmanager
def operation_locks(dry_run, directory, prefix, certbot_dir):
    with ExitStack() as stack:
        if not dry_run:
            for path in (prefix / ".install.lock", directory / ".setup.lock"):
                if path.exists():
                    stack.enter_context(lock_file(path))
            stack.enter_context(lock_file(certbot_dir / ".certbot.lock", certbot=True))
        yield stack
