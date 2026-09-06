"""Explicit zones and credentials; no account-wide zone discovery."""

import ipaddress
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class HookError(Exception):
    """An actionable failure whose message is safe to log."""


def domain_name(value: str) -> str:
    name = value.rstrip(".").lower().encode("idna").decode("ascii")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise HookError("DNS-01 requires a domain name, not an IP address")
    if (
        len(name) > 253
        or "." not in name
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in name.split(".")
        )
    ):
        raise HookError("Expected a DNS domain name, without wildcards or a URL")
    return name


@dataclass(frozen=True)
class Config:
    zones: tuple[str, ...]
    state_dir: Path
    secret_id: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    propagation_seconds: int = 120
    propagation_timeout: int = 600
    ttl: int = 600

    def zone_for(self, domain: str) -> str:
        candidates = [z for z in self.zones if domain == z or domain.endswith("." + z)]
        if not candidates:
            raise HookError(f"Domain {domain} is outside the configured zones")
        return max(candidates, key=len)


def load_config(path: Path) -> Config:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise HookError("Config must be owned by the current user with mode 600")
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    allowed = {
        "zones",
        "state_dir",
        "secret_id",
        "secret_key",
        "token",
        "propagation_seconds",
        "propagation_timeout",
        "ttl",
    }
    if data.keys() - allowed:
        raise HookError("Unknown config keys: " + ", ".join(sorted(data.keys() - allowed)))
    zones = data.get("zones")
    if not isinstance(zones, list) or not zones or any(not isinstance(z, str) for z in zones):
        raise HookError("Config requires a nonempty zones array")
    for key, low, high in (
        ("propagation_seconds", 0, 3600),
        ("propagation_timeout", 1, 7200),
        ("ttl", 1, 604800),
    ):
        if key in data and (type(data[key]) is not int or not low <= data[key] <= high):
            raise HookError(f"{key} must be an integer between {low} and {high}")
    state_dir = Path(data.get("state_dir", path.resolve().parent / "state"))
    if not state_dir.is_absolute():
        raise HookError("state_dir must be an absolute path")
    for key, env in (
        ("secret_id", "TENCENTCLOUD_SECRET_ID"),
        ("secret_key", "TENCENTCLOUD_SECRET_KEY"),
        ("token", "TENCENTCLOUD_TOKEN"),
    ):
        data[key] = os.environ.get(env, data.get(key, ""))
        if not isinstance(data[key], str):
            raise HookError(f"{key} must be a string")
    data.update(zones=tuple(domain_name(z) for z in zones), state_dir=state_dir)
    config = Config(**data)
    if config.propagation_timeout <= config.propagation_seconds:
        raise HookError("propagation_timeout must exceed propagation_seconds")
    return config
