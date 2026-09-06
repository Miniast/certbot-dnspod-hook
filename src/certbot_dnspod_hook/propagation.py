"""Sample every authoritative nameserver after a configurable propagation delay."""

import time

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdatatype
import dns.resolver

from .config import Config, HookError


def has_txt(response, fqdn: str, value: str) -> bool:
    name = dns.name.from_text(fqdn)
    if any(rr.name == name and rr.rdtype == dns.rdatatype.CNAME for rr in response.answer):
        raise HookError("CNAME challenge delegation is not supported in this release")
    if response.rcode() != dns.rcode.NOERROR or not response.flags & dns.flags.AA:
        return False
    return any(
        b"".join(item.strings) == value.encode("ascii")
        for rr in response.answer
        if rr.name == name and rr.rdtype == dns.rdatatype.TXT
        for item in rr
    )


def wait_for_txt(config: Config, fqdn: str, zone: str, value: str) -> None:
    deadline = time.monotonic() + config.propagation_timeout
    resolver = dns.resolver.Resolver()

    def budget() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HookError(f"DNS propagation timed out for {fqdn}; check NS, delegation and delay")
        return min(5.0, remaining)

    time.sleep(config.propagation_seconds)
    query = dns.message.make_query(fqdn, "TXT")
    query.flags &= ~dns.flags.RD
    while True:
        try:
            actual = (
                dns.resolver.zone_for_name(
                    dns.name.from_text(fqdn), resolver=resolver, lifetime=budget()
                )
                .to_text()
                .rstrip(".")
            )
            if actual != zone:
                raise HookError(
                    "Challenge is delegated to another zone; configure the actual "
                    "DNSPod zone. CNAME delegation is unsupported"
                )
            servers = resolver.resolve(zone + ".", "NS", lifetime=budget())
            all_ready = bool(servers)
            for server in servers:
                ready = False
                for family in ("A", "AAAA"):
                    try:
                        addresses = resolver.resolve(server.target, family, lifetime=budget())
                    except (dns.exception.DNSException, OSError):
                        continue
                    for address in addresses:
                        try:
                            response, _ = dns.query.udp_with_fallback(
                                query, address.address, timeout=budget()
                            )
                            ready = has_txt(response, fqdn, value)
                            break
                        except (dns.exception.DNSException, OSError):
                            continue
                    if ready:
                        break
                all_ready = all_ready and ready
            if all_ready:
                return
        except (dns.exception.DNSException, OSError):
            pass
        time.sleep(min(5.0, budget()))
