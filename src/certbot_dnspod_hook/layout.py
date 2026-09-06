"""Locate the single installation directory without creating anything."""

import sys
from pathlib import Path


def installation_root() -> Path:
    environment = Path(sys.prefix).resolve()
    if environment.parent.name == "versions":
        candidate = environment.parent.parent
        if (candidate / "install.json").is_file():
            return candidate
    return Path.home() / ".certbot-dnspod-hook"
