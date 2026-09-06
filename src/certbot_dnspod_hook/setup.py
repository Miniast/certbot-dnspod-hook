"""Persist credentials and let Certbot test and save its own renewal configuration."""

import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from .config import Config, HookError, domain_name
from .state import StateStore

CONFIG_DIR = Path("/etc/certbot-dnspod-hook")
STATE_DIR = Path("/var/lib/certbot-dnspod-hook")
RENEWAL_DIR = Path("/etc/letsencrypt/renewal")
HOOK_COMMAND = Path("/usr/local/bin/certbot-dnspod-hook")
KEY_NAMES = ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "TENCENTCLOUD_TOKEN")
NGINX_DEPLOY = "#!/bin/sh\nset -eu\n/usr/sbin/nginx -t\n/usr/bin/systemctl reload nginx\n"


def add_arguments(parser):
    parser.add_argument("--cert-name", required=True, help="Existing Certbot certificate name")
    parser.add_argument(
        "--zone", action="append", required=True, help="DNSPod zone; repeat as needed"
    )
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--credentials-file", type=Path, help="Private KEY=value file")
    credentials.add_argument("--secret-id", help="Tencent Cloud SecretId")
    parser.add_argument("--secret-key", help="Tencent Cloud SecretKey (with --secret-id)")
    parser.add_argument("--token", help="Optional temporary credential token")
    parser.add_argument(
        "--renew-now", action="store_true", help="Renew once after the staging test"
    )
    parser.add_argument(
        "--deploy-nginx", action="store_true", help="Save a checked nginx reload hook"
    )
    parser.add_argument("--propagation-seconds", type=int, default=120)
    parser.add_argument("--propagation-timeout", type=int, default=600)


def read_credentials(args) -> tuple[str, str, str]:
    if args.credentials_file:
        if args.secret_key is not None or args.token is not None:
            raise HookError("Use either a credentials file or inline credentials, not both")
        fd = os.open(args.credentials_file, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            allowed_owners = {os.geteuid(), int(os.environ.get("SUDO_UID", os.geteuid()))}
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid not in allowed_owners
            ):
                raise HookError("Credentials file must be owned by you or root with mode 600")
            values = {}
            for line in stream:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if separator != "=" or key not in KEY_NAMES or key in values:
                    raise HookError(
                        "Credentials file requires unique TENCENTCLOUD_* KEY=value lines"
                    )
                if value.startswith(('"', "'")):
                    if len(value) < 2 or value[-1] != value[0]:
                        raise HookError("Unmatched quote in credentials file")
                    value = value[1:-1]
                values[key] = value
        credentials = tuple(values.get(key, "") for key in KEY_NAMES)
    else:
        credentials = (args.secret_id or "", args.secret_key or "", args.token or "")
    if not all(credentials[:2]) or any(
        not re.fullmatch(r"[A-Za-z0-9_+/=.-]*", value) for value in credentials
    ):
        raise HookError("Provide both SecretId and SecretKey; shell expressions are not supported")
    return credentials


def run(command, *, capture=False):
    # Credentials live in the config file, never in Certbot arguments or its environment.
    env = {key: value for key, value in os.environ.items() if key not in KEY_NAMES}
    result = subprocess.run(command, text=True, capture_output=capture, env=env, check=False)
    if result.returncode:
        raise HookError(
            f"{Path(command[0]).name} failed (exit {result.returncode}); inspect its log"
        )
    return result.stdout or ""


def executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise HookError(f"Required executable not found: {name}")
    # Preserve /snap/bin/certbot: resolving that symlink would execute plain snap instead.
    return os.path.abspath(path)


def private_write(path: Path, content: str, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def config_text(zones, credentials, state_dir, args) -> str:
    values = dict(
        zones=zones,
        state_dir=str(state_dir),
        secret_id=credentials[0],
        secret_key=credentials[1],
        token=credentials[2],
        propagation_seconds=args.propagation_seconds,
        propagation_timeout=args.propagation_timeout,
    )
    return "".join(
        f"{key} = {json.dumps(value, ensure_ascii=True)}\n" for key, value in values.items()
    )


def check_hook_installation():
    if not HOOK_COMMAND.is_file() or not os.access(HOOK_COMMAND, os.X_OK):
        raise HookError("Install with install.sh before running setup")
    for path in (HOOK_COMMAND.resolve(), *HOOK_COMMAND.resolve().parents, HOOK_COMMAND.parent):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise HookError("Installed hook and its parent directories must be root-owned")


def certbot_timer(certbot: str) -> str:
    path = Path(certbot)
    for _ in range(10):
        if path == Path("/snap/bin/certbot"):
            return "snap.certbot.renew.timer"
        if not path.is_symlink():
            break
        target = path.readlink()
        path = target if target.is_absolute() else path.parent / target
    return "certbot.timer"


def require_root():
    if os.geteuid() != 0:
        raise HookError("setup must run as root: sudo certbot-dnspod-hook setup ...")


def setup(args) -> int:
    require_root()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", args.cert_name):
        raise HookError("Invalid certificate name; use the name from certbot certificates")
    if not 0 <= args.propagation_seconds < args.propagation_timeout <= 7200:
        raise HookError("Require 0 <= propagation-seconds < propagation-timeout <= 7200")
    if args.propagation_seconds > 3600:
        raise HookError("propagation-seconds must be at most 3600")
    credentials = read_credentials(args)
    zones = list(dict.fromkeys(domain_name(zone) for zone in args.zone))
    certbot, openssl, systemctl = (executable(name) for name in ("certbot", "openssl", "systemctl"))
    version = re.search(r"(\d+)\.(\d+)", run([certbot, "--version"], capture=True))
    if not version or tuple(map(int, version.groups())) < (2, 3):
        raise HookError("setup requires Certbot 2.3 or newer (reconfigure support)")
    check_hook_installation()
    renewal = RENEWAL_DIR / f"{args.cert_name}.conf"
    original = renewal.read_text()
    cert_match = re.search(r"^cert\s*=\s*(.+)$", original, re.MULTILINE)
    if not cert_match:
        raise HookError("Cannot locate certificate in existing renewal configuration")
    san = run(
        [openssl, "x509", "-in", cert_match[1].strip(), "-noout", "-ext", "subjectAltName"],
        capture=True,
    )
    domains = re.findall(r"DNS:([^,\s]+)", san)
    if not domains or "IP Address:" in san:
        raise HookError("setup requires an existing certificate containing only DNS identifiers")
    state_dir = STATE_DIR / args.cert_name
    config = Config(zones=tuple(zones), state_dir=state_dir)
    for domain in domains:
        config.zone_for(domain_name(domain.removeprefix("*.")))
    timer = certbot_timer(certbot)
    if (
        run([systemctl, "show", timer, "--property=LoadState", "--value"], capture=True).strip()
        != "loaded"
    ):
        raise HookError(
            f"Expected Certbot timer {timer} is missing; install its systemd timer first"
        )
    deploy_path = CONFIG_DIR / "nginx-deploy.sh"
    if args.deploy_nginx:
        previous = re.search(r"^deploy_hook\s*=\s*(.+)$", original, re.MULTILINE)
        if previous and previous[1].strip() != str(deploy_path):
            raise HookError(
                "Certificate already has a deploy hook; omit --deploy-nginx to preserve it"
            )
        run(["/usr/sbin/nginx", "-t"])
        run([systemctl, "is-active", "--quiet", "nginx"])
    StateStore(CONFIG_DIR)
    StateStore(STATE_DIR)
    with (CONFIG_DIR / ".setup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HookError("Another setup is running") from None
        with StateStore(state_dir):
            if list(state_dir.glob("*.json")):
                raise HookError("Pending challenges exist; clean them up before running setup")
        revision = uuid.uuid4().hex[:12]
        path = CONFIG_DIR / f"{args.cert_name}-{revision}.toml"
        backup = CONFIG_DIR / f"{args.cert_name}-{revision}.renewal-backup"
        private_write(backup, original)
        private_write(path, config_text(zones, credentials, state_dir, args))
        print(f"Credentials saved privately: {path}", flush=True)
        print(f"Renewal configuration backup: {backup}", flush=True)
        # Each setup gets a durable file. Failed reconfigure cannot replace credentials
        # used by an existing schedule, and retained challenges remain recoverable.
        hook = [str(HOOK_COMMAND), "--config", str(path)]
        command = [
            certbot,
            "reconfigure",
            "--non-interactive",
            "--cert-name",
            args.cert_name,
            "--authenticator",
            "manual",
            "--preferred-challenges",
            "dns",
            "--manual-auth-hook",
            shlex.join([*hook, "auth"]),
            "--manual-cleanup-hook",
            shlex.join([*hook, "cleanup"]),
        ]
        if args.deploy_nginx:
            if deploy_path.exists():
                if deploy_path.read_text() != NGINX_DEPLOY:
                    raise HookError("Existing nginx deploy script differs; inspect it first")
            else:
                private_write(deploy_path, NGINX_DEPLOY, 0o700)
            command += ["--deploy-hook", str(deploy_path), "--run-deploy-hooks"]
        print("Testing and saving renewal options with Certbot staging...", flush=True)
        run(command)
        saved = renewal.read_text()
        if (
            str(path) not in saved
            or "manual_auth_hook" not in saved
            or "manual_cleanup_hook" not in saved
        ):
            raise HookError(
                "Certbot did not persist the expected hooks; inspect renewal configuration"
            )
        run([systemctl, "enable", "--now", timer])
        if args.renew_now:
            print("Staging passed. Requesting one production renewal...", flush=True)
            run(
                [
                    certbot,
                    "renew",
                    "--non-interactive",
                    "--cert-name",
                    args.cert_name,
                    "--force-renewal",
                ]
            )
        print(f"Setup complete. Automatic renewal: {timer}", flush=True)
        print("Inspect pending challenges: " + shlex.join([*hook, "status"]), flush=True)
    return 0
