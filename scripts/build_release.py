"""Build a portable wheel bundle and an installer pinned to that bundle's SHA-256."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(repository):
    if repository and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must be OWNER/NAME")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {package["name"]: package for package in lock["package"]}
    selected = {"pip"}
    pending = [dependency["name"] for dependency in packages[project["name"]]["dependencies"]]
    while pending:
        name = pending.pop()
        if name not in selected:
            selected.add(name)
            pending.extend(
                dependency["name"] for dependency in packages[name].get("dependencies", [])
            )
    output = ROOT / "dist/release" / version
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        wheels = work / "wheels"
        wheels.mkdir()
        requirements = []
        for name in sorted(selected):
            package = packages[name]
            # All current runtime dependencies publish universal pure-Python wheels.
            candidates = [
                wheel for wheel in package["wheels"] if wheel["url"].endswith("-none-any.whl")
            ]
            if len(candidates) != 1:
                raise ValueError(
                    f"Expected one universal wheel for {name}; review release portability"
                )
            wheel = candidates[0]
            filename = urllib.parse.urlsplit(wheel["url"]).path.rsplit("/", 1)[-1]
            with urllib.request.urlopen(wheel["url"], timeout=60) as response:
                data = response.read()
            digest = hashlib.sha256(data).hexdigest()
            if "sha256:" + digest != wheel["hash"]:
                raise ValueError("Dependency hash mismatch: " + name)
            (wheels / filename).write_bytes(data)
            requirements.append(f"{name}=={package['version']} --hash=sha256:{digest}")
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=ROOT, check=True)
        application = next(wheels.glob("certbot_dnspod_hook-*.whl"))
        app_hash = hashlib.sha256(application.read_bytes()).hexdigest()
        requirements.append(f"{project['name']}=={version} --hash=sha256:{app_hash}")
        (work / "requirements.txt").write_text("\n".join(requirements) + "\n")
        shutil.copy2(ROOT / "LICENSE", work / "LICENSE")
        bundle = output / f"certbot-dnspod-hook-{version}-bundle.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            for path in sorted(work.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(work), recursive=False)
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        base_url = (
            f"https://github.com/{repository}/releases/download/v{version}"
            if repository
            else "UNPUBLISHED"
        )
        script = (
            (ROOT / "scripts/install.sh.in")
            .read_text()
            .replace("@VERSION@", version)
            .replace("@BUNDLE_SHA256@", digest)
            .replace("@RELEASE_BASE_URL@", base_url)
        )
        installer = output / "install.sh"
        installer.write_text(script)
        installer.chmod(0o755)
        (output / "SHA256SUMS").write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                for path in (bundle, installer)
            )
        )
        shutil.copy2(work / "requirements.txt", output / "requirements.txt")
        (output / "release.json").write_text(
            json.dumps(
                {"version": version, "repository": repository, "bundle_sha256": digest}, indent=2
            )
            + "\n"
        )
    print("Release artifacts: " + str(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", help="GitHub OWNER/NAME; omitted for local-only validation")
    build(parser.parse_args().repository)
