"""Opt-in release integration test; no root, credentials or external network required."""

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not os.environ.get("DNSPOD_TEST_RELEASE"), reason="Build release and set DNSPOD_TEST_RELEASE"
)
def test_install_from_http_and_failed_update_preserves_working_install(tmp_path):
    release = Path(os.environ["DNSPOD_TEST_RELEASE"])
    version = json.loads((release / "release.json").read_text())["version"]
    served = tmp_path / "served"
    shutil.copytree(release, served)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        prefix = tmp_path / "installed"
        bin_dir = prefix / "bin"
        command = [
            "sh",
            str(release / "install.sh"),
            "--base-url",
            f"http://127.0.0.1:{server.server_port}",
            "--prefix",
            str(prefix),
        ]
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("TENCENTCLOUD_")
        }
        env["PIP_INDEX_URL"] = "http://127.0.0.1:1/no-network-index"
        installed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
        assert installed.returncode == 0, installed.stdout + installed.stderr
        assert set(tmp_path.iterdir()) == {served, prefix}
        launcher = bin_dir / "certbot-dnspod-hook"
        assert subprocess.check_output([str(launcher), "--version"], text=True).strip() == version
        assert "setup" in subprocess.check_output([str(launcher), "--help"], text=True)
        located = subprocess.check_output(
            [
                str(prefix / "current/bin/python"),
                "-B",
                "-c",
                "from certbot_dnspod_hook.layout import installation_root; "
                "print(installation_root())",
            ],
            text=True,
        ).strip()
        assert located == str(prefix)
        before = (prefix / "current").readlink()
        bundle = next(served.glob("*-bundle.tar.gz"))
        bundle.write_bytes(bundle.read_bytes() + b"corrupted")
        failed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
        assert failed.returncode != 0
        assert "checksum mismatch" in failed.stderr
        assert (prefix / "current").readlink() == before
        assert subprocess.check_output([str(launcher), "--version"], text=True).strip() == version
        # Execute removal from the installed interpreter, including deleting its own venv.
        renewal_root = tmp_path / "letsencrypt/renewal"
        renewal_root.mkdir(parents=True)
        program = """from pathlib import Path
from types import SimpleNamespace
from certbot_dnspod_hook.management import remove_installation
import sys
paths = [Path(value) for value in sys.argv[1:]]
raise SystemExit(remove_installation(SimpleNamespace(detach=False, dry_run=False), *paths))
"""
        removed = subprocess.run(
            [
                str(prefix / "current/bin/python"),
                "-B",
                "-c",
                program,
                str(prefix / "config"),
                str(prefix / "state"),
                str(renewal_root),
                str(prefix),
                str(launcher),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert removed.returncode == 0, removed.stdout + removed.stderr
        assert not prefix.exists() and not launcher.is_symlink()
        assert not (renewal_root.parent / ".certbot.lock").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
