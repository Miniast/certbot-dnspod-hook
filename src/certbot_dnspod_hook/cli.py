"""Certbot hook entry points. Stdout carries only the per-challenge state ID."""

import argparse
import json
import logging
import os
from pathlib import Path

from .config import HookError, load_config
from .core import Hook, challenge
from .propagation import wait_for_txt
from .provider import DNSPod
from .state import StateStore


def environment_challenge() -> tuple[str, str, str]:
    domain = os.environ.get("CERTBOT_IDENTIFIER") or os.environ.get("CERTBOT_DOMAIN", "")
    return challenge(domain, os.environ.get("CERTBOT_VALIDATION", ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Certbot DNS-01 hooks for Tencent Cloud DNSPod")
    parser.add_argument("--config", type=Path, default=Path("/etc/certbot-dnspod-hook/config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="Create TXT, wait for DNS and print a state ID")
    cleanup = sub.add_parser("cleanup", help="Remove only the TXT owned by this challenge")
    cleanup.add_argument("--state-id", help="Recover a saved challenge after Certbot has stopped")
    sub.add_parser("status", help="List local pending states without contacting DNSPod")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        config = load_config(args.config)
        with StateStore(config.state_dir) as store:
            if args.command == "status":
                states = [
                    {
                        "state_id": path.stem,
                        "domain": store.read(path.stem).get("domain"),
                        "record_id": store.read(path.stem).get("record_id"),
                    }
                    for path in sorted(config.state_dir.glob("*.json"))
                ]
                print(json.dumps(states, indent=2))
                return 0
            if args.command == "auth":
                domain, value, _ = environment_challenge()
                hook = Hook(config, DNSPod(config), store, wait_for_txt)
                print(hook.auth(domain, value), flush=True)
            else:
                key = args.state_id
                if key is None:
                    _, _, key = environment_challenge()
                    output = os.environ.get("CERTBOT_AUTH_OUTPUT", "").strip()
                    if output and output != key:
                        raise HookError("CERTBOT_AUTH_OUTPUT does not match this challenge")
                # A duplicate cleanup also works after credentials have been removed.
                if store.read(key) is not None:
                    Hook(config, DNSPod(config), store, wait_for_txt).cleanup(key)
        return 0
    except HookError as exc:
        logging.error("%s", exc)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logging.error(
            "Cannot read config/state or API response (%s); check paths, permissions "
            "and file formats",
            type(exc).__name__,
        )
    except KeyboardInterrupt:
        logging.error("Interrupted; use status to inspect any retained state")
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
