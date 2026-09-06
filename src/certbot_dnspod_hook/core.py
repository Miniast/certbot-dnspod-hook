"""Challenge lifecycle; never overwrite or adopt somebody else's TXT record."""

import hashlib
import logging
import re
import time
import uuid

from .config import Config, HookError, domain_name
from .provider import APIError, DNSPod
from .state import StateStore

LOG = logging.getLogger(__name__)


def challenge(domain: str, value: str) -> tuple[str, str, str]:
    domain = domain_name(domain.removeprefix("*."))
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise HookError("CERTBOT_VALIDATION must be a DNS-01 SHA-256 validation value")
    key = hashlib.sha256(f"{domain}\n{value}".encode()).hexdigest()
    return domain, value, key


def matches(record: dict, state: dict) -> bool:
    return (
        record.get("Type") == "TXT"
        and record.get("Name") == state["name"]
        and record.get("Value") == state["value"]
        and record.get("Remark") == state["marker"]
    )


class Hook:
    def __init__(self, config: Config, api: DNSPod, store: StateStore, wait):
        self.config, self.api, self.store, self.wait = config, api, store, wait

    def load(self, key: str) -> dict | None:
        state = self.store.read(key)
        if state is None:
            return None
        try:
            domain, _, expected = challenge(state["domain"], state["value"])
            zone = self.config.zone_for(domain)
            fqdn = "_acme-challenge." + domain
            valid = (
                state["version"] == 1
                and key == expected
                and state["zone"] == zone
                and state["name"] == fqdn[: -(len(zone) + 1)]
                and re.fullmatch(r"certbot-dnspod-hook:[0-9a-f]{32}", state["marker"])
                and (
                    state["record_id"] is None
                    or type(state["record_id"]) is int
                    and state["record_id"] > 0
                )
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise HookError(f"Invalid or incompatible state {key}; inspect it before proceeding")
        return state

    def locate_pending(self, key: str, state: dict) -> int:
        # Includes DNSPod's documented list-index delay. Never repeat CreateRecord here.
        for attempt in range(7):
            found = [r for r in self.api.records(state["zone"], state["name"]) if matches(r, state)]
            if len(found) > 1:
                raise HookError(f"Multiple owned records for {key}; inspect DNSPod manually")
            if found:
                state["record_id"] = found[0]["RecordId"]
                self.store.write(key, state)
                return state["record_id"]
            if attempt < 6:
                time.sleep(10)
        raise HookError(
            f"Create outcome is still unknown for {key}; state retained. "
            "Use status and cleanup --state-id after checking DNSPod"
        )

    def auth(self, domain: str, value: str) -> str:
        domain, value, key = challenge(domain, value)
        zone = self.config.zone_for(domain)
        state = self.load(key)
        if state is None:
            name = ("_acme-challenge." + domain)[: -(len(zone) + 1)]
            existing = self.api.records(zone, name)
            if any(r["Type"] == "CNAME" for r in existing):
                raise HookError("CNAME challenge delegation is not supported in this release")
            if any(r["Type"] == "TXT" and r["Value"] == value for r in existing):
                raise HookError(
                    "An unowned TXT record already has this value; refusing to adopt it"
                )
            state = dict(
                version=1,
                domain=domain,
                zone=zone,
                name=name,
                value=value,
                marker="certbot-dnspod-hook:" + uuid.uuid4().hex,
                record_id=None,
            )
            self.store.write(key, state)  # Persist ownership before the mutating request.
            try:
                state["record_id"] = self.api.create(state, self.config.ttl)
                self.store.write(key, state)
            except APIError as exc:
                if exc.code.startswith(
                    (
                        "AuthFailure",
                        "UnauthorizedOperation",
                        "InvalidParameter",
                        "OperationDenied",
                        "LimitExceeded",
                        "RequestLimitExceeded",
                    )
                ):
                    self.store.remove(key)  # Explicit rejection: no record was created.
                    raise
                LOG.warning("%s; looking up the saved operation marker", exc)
                self.locate_pending(key, state)
        elif state["record_id"] is None:
            self.locate_pending(key, state)
        else:
            record = self.api.get(zone, state["record_id"])
            if record is None or not matches(record, state):
                raise HookError(
                    f"Saved TXT record is missing or changed; run cleanup --state-id {key}"
                )
        try:
            self.wait(self.config, "_acme-challenge." + domain, zone, value)
        except (HookError, KeyboardInterrupt):
            try:
                self.cleanup(key)
            except HookError as exc:
                LOG.error("Cleanup failed; state retained: %s", exc)
            raise
        LOG.info("TXT ready for %s; state ID %s", domain, key)
        return key

    def cleanup(self, key: str) -> None:
        state = self.load(key)
        if state is None:
            return
        record_id = state["record_id"] or self.locate_pending(key, state)
        record = self.api.get(state["zone"], record_id)
        if record is not None:
            if not matches(record, state):
                raise HookError(
                    f"Record {record_id} changed; refusing deletion and retaining state"
                )
            self.api.delete(state["zone"], record_id)
        self.store.remove(key)
        LOG.info("Cleanup complete for %s", state["domain"])
