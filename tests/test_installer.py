"""Opt-in release integration test; no root, credentials or external network required."""

import functools
import http.server
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
    served = tmp_path / "served"
    shutil.copytree(release, served)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        prefix, bin_dir = tmp_path / "installed", tmp_path / "bin"
        command = [
            "sh",
            str(release / "install.sh"),
            "--base-url",
            f"http://127.0.0.1:{server.server_port}",
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(bin_dir),
        ]
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("TENCENTCLOUD_")
        }
        env["PIP_INDEX_URL"] = "http://127.0.0.1:1/no-network-index"
        installed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
        assert installed.returncode == 0, installed.stdout + installed.stderr
        launcher = bin_dir / "certbot-dnspod-hook"
        assert subprocess.check_output([str(launcher), "--version"], text=True).strip() == "0.2.0"
        assert "setup" in subprocess.check_output([str(launcher), "--help"], text=True)
        before = (prefix / "current").readlink()
        bundle = next(served.glob("*-bundle.tar.gz"))
        bundle.write_bytes(bundle.read_bytes() + b"corrupted")
        failed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
        assert failed.returncode != 0
        assert "checksum mismatch" in failed.stderr
        assert (prefix / "current").readlink() == before
        assert subprocess.check_output([str(launcher), "--version"], text=True).strip() == "0.2.0"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
